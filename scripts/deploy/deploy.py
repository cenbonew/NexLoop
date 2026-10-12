#!/usr/bin/env python3
"""NX-032 slice 3: the deployment dry-run (docs/implementation/NX-032-design.md §5, §7; AT-055 and AT-057 on the deploy side).

It decides whether a release may be deployed to one host and prints what would be done; it never executes a deployment step,
never connects to a host and never calls docker. Every step it lists is a B-class step the owner runs (NX-032-deploy-runbook.md).

    deploy.py dry-run --host-role app|data --stage-env <private.env> --out <new dir>
                      [--release-dir R --allowed-signers F --trusted-sha SHA | --repo DIR --trusted-ref REF]
                      [--image-var BACKEND_IMAGE=<manifest image name> ...] [--compose-env <private compose env>]
                      [--current-manifest <deployed manifest.json> --database-head NNNN] [--load-check]
    deploy.py load-check --release-dir R          (deploy host) load every archive; nexloop/<name>@<digest> must resolve to the manifest
    deploy.py promote --release-root ROOT --release-id ID --allowed-signers F
                                                  after the deploy gate: previous current → known_good, ID → current (atomic)
    deploy.py rollback-check --release-root ROOT --target ID [--database-head NNNN]
                                                  a rollback target must be known_good or older and run on the current schema

- APP_HOST: the signed release must verify (release.py verify, AT-055): the manifest's commit equals the trusted SHA (given, or
  resolved from the trusted ref of the local repository), every manifest image is deployed and every archive is unchanged. The
  Compose image variables are rendered from the manifest; a private Compose env naming any other digest is refused.
- DATA_HOST: the PostgreSQL and Valkey images are the digests pinned in versions.lock.json.
- With the currently deployed manifest: release.py rollback-check refuses a blind rollback (AT-057) and says how to move forward.
- The configuration templates of the host are rendered (whitelist renderer) into --out with the Compose image env.
- --load-check (on the deploy host only, it calls docker): release.py load-check (NX-034 part 2) loads every archive and refuses
  unless nexloop/<name>@<manifest digest> resolves to exactly that digest.
NX-034 part 2 wiring: promote and rollback-check work only inside the local release root (archive.py); no host is contacted.
Prints one JSON object. Exit 0 ready, 1 refused, 2 input error.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
sys.path.insert(0, str(ROOT / 'scripts/release'))
import release  # noqa: E402
from nexloop_eios import stage_render  # noqa: E402

STAGE = ROOT / 'deploy/stage'
TEMPLATES = {'data': ('postgresql.conf', 'pg_hba.conf', 'valkey.conf', 'data-host.nft'), 'app': ('Caddyfile', 'app-host.nft')}
DEFAULT_APP_IMAGES = {'BACKEND_IMAGE': 'backend', 'AGENT_HOST_IMAGE': 'agent-host', 'GATEWAY_IMAGE': 'gateway'}
DATA_IMAGES = {'POSTGRES_IMAGE': 'postgres', 'VALKEY_IMAGE': 'valkey'}
# The Compose file of each host role; every `${*_IMAGE:?...}` it requires must be rendered from the manifest (or the lock).
COMPOSE = {'app': STAGE / 'compose.app.example.yaml', 'data': STAGE / 'compose.data.example.yaml'}
_REQUIRED_IMAGE = re.compile(r'\$\{([A-Z][A-Z0-9_]*_IMAGE):\?')


def required_image_variables(host_role):
    """The image variables the host role's Compose file cannot start without."""
    return sorted(set(_REQUIRED_IMAGE.findall(COMPOSE[host_role].read_text())))
_IMAGE = re.compile(r'[a-z0-9][a-z0-9._/:-]{0,200}@(sha256:[0-9a-f]{64})')
STEPS = {
    'data': [
        ('inventory_before', 'python3 - --phase before --planned-ports "$PG_PORT,$VALKEY_PORT" --data-root "$NEXLOOP_ROOT" --out <private> < scripts/deploy/inventory.py', None),
        ('install_configuration', 'scp <out>/{postgresql.conf,pg_hba.conf,valkey.conf} "$SSH_ALIAS_DATA:$NEXLOOP_ROOT/config/"', 'restore the previous files and reload'),
        ('firewall', 'ssh "$SSH_ALIAS_DATA" sudo nft -c -f <data-host.nft> && sudo nft -f <data-host.nft>', 'sudo nft delete table inet nexloop_stage'),
        ('start_infra', 'ssh "$SSH_ALIAS_DATA" docker compose -p nexloop-data -f compose.data.yaml --env-file compose.data.env up -d',
         'docker compose -p nexloop-data down (no -v)'),
        ('inventory_after_and_gate', 'inventory.py --phase after …; nexloop-doctor --deploy-gate <data spec>', None)],
    'app': [
        ('inventory_before', 'python3 - --phase before --planned-ports "$GATEWAY_HTTPS_PORT,$GATEWAY_REDIRECT_PORT,$API_INTERNAL_PORT,$API_INTERNAL_PORT_2,$HOST_INTERNAL_PORT,$GUARD_PORT" '
                             '--data-root "$NEXLOOP_ROOT" --out <private> < scripts/deploy/inventory.py', None),
        ('load_images', 'deploy.py load-check --release-dir <release> (docker load each archive; nexloop/<name>@<digest> must resolve to the manifest digest)', 'docker image rm <digest>'),
        ('install_configuration', 'scp <out>/{Caddyfile,compose.images.env} "$SSH_ALIAS_APP:$NEXLOOP_ROOT/config/"', 'restore the previous files'),
        ('firewall', 'ssh "$SSH_ALIAS_APP" sudo nft -c -f <app-host.nft> && sudo nft -f <app-host.nft>', 'sudo nft delete table inet nexloop_stage'),
        ('backup_gate', 'verified backup before any migration (D5; nexloop-migrate refuses without one, AT-056)', None),
        ('stop_old_agent_host', 'docker compose -p nexloop-app stop agent-host (one SQLite owner at a time)', 'docker compose -p nexloop-app start agent-host'),
        ('start_app', 'docker compose -p nexloop-app -f compose.app.yaml --env-file compose.app.env --env-file compose.images.env up -d (REAL_DISPATCH_ENABLED=false)',
         'docker compose -p nexloop-app up -d with the previous compose.images.env'),
        ('inventory_after_and_gate', 'inventory.py --phase after …; nexloop-doctor --deploy-gate <app spec>; doctor --release (running digests = manifest)', None),
        ('promote', 'deploy.py promote --release-root <RELEASE_ROOT> --release-id <id> --allowed-signers <F> (previous current → known_good, this → current)',
         'deploy.py rollback-check … then promote the known_good release')],
}


class Refused(Exception):
    pass


def _env(path):
    return dict(line.split('=', 1) for line in Path(path).read_text().splitlines() if line and not line.startswith('#') and '=' in line)


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def dry_run(*, host_role, stage_env, out, release_dir=None, allowed_signers=None, trusted_sha=None, repo=None, trusted_ref=None, image_vars=None,
            compose_env=None, current_manifest=None, database_head=None, lock=None, load_check=False, docker=None):
    out = Path(out)
    if out.exists():
        raise ValueError('output directory exists')
    checks, details = {}, {}
    values = stage_render.load_env(stage_env)
    provided = _env(compose_env) if compose_env else {}
    images = {}
    if host_role == 'app':
        if not (release_dir and allowed_signers):
            raise ValueError('app deployment needs --release-dir and --allowed-signers')
        if trusted_sha is None:
            if not (repo and trusted_ref):
                raise ValueError('--trusted-sha or --repo with --trusted-ref required')
            trusted_sha = _git(repo, 'rev-parse', '--verify', trusted_ref + '^{commit}')
        manifest = json.loads((Path(release_dir) / 'manifest.json').read_text())
        mapping = image_vars or DEFAULT_APP_IMAGES
        by_name = {i['name']: i for i in manifest['images']}
        images = {var: f'nexloop/{name}@{by_name[name]["digest"]}' for var, name in mapping.items() if name in by_name}
        try:
            verified = release.verify(release_dir, allowed_signers=allowed_signers, deploy_sha=trusted_sha,
                                      deploy_images={name: by_name[name]['digest'] for name in mapping.values() if name in by_name})
            checks['release_verified'] = True
            details['release'] = {'release_id': verified['release_id'], 'commit': verified['commit']}
        except release.Refused as refused:
            checks['release_verified'] = False
            details['release_refused'] = str(refused).split(';')
        if load_check:
            try:
                details['loaded'] = release.load_check(release_dir, docker=docker)['loaded']
                checks['images_loaded_by_digest'] = True
            except release.Refused as refused:
                checks['images_loaded_by_digest'] = False
                details['load_refused'] = str(refused).split(';')
        unmapped = sorted(set(mapping.values()) - set(by_name))
        checks['image_variables_mapped'] = not unmapped
        details['unmapped_images'] = unmapped
        if current_manifest:
            try:
                details['rollback'] = release.rollback_check(current_manifest, Path(release_dir) / 'manifest.json', database_head=database_head)
                checks['rollback_allowed'] = True
            except release.Refused as refused:
                checks['rollback_allowed'] = False
                details['rollback_refused'] = json.loads(str(refused))
    else:
        lock = lock or json.loads((ROOT / 'versions.lock.json').read_text())['images']
        for var, name in DATA_IMAGES.items():
            images[var] = f'{lock[name]["reference"].split(":")[0]}@{lock[name]["digest"]}'
    # Every image the Compose file requires is rendered: an explicit --image-var mapping may not leave one out (it would only
    # fail at `docker compose up`).
    missing = sorted(set(required_image_variables(host_role)) - set(images))
    checks['compose_image_variables_complete'] = not missing
    details['missing_image_variables'] = missing
    # A private Compose env may carry image references only if they are exactly these (a registry mirror prefix is allowed).
    differing = sorted(var for var, value in provided.items() if var.endswith('_IMAGE') and
                       (var not in images or not _IMAGE.fullmatch(value) or _IMAGE.fullmatch(value).group(1) != images[var].rsplit('@', 1)[1]))
    checks['compose_images_from_manifest'] = not differing
    details['compose_image_mismatch'] = differing
    out.mkdir(mode=0o700, parents=True)
    rendered = []
    for name in TEMPLATES[host_role]:
        template = next(p for p in (STAGE / 'config' / f'{name}.template', STAGE / 'firewall' / f'{name}.template') if p.exists())
        text, _ = stage_render.render(template.read_text(), values)
        (out / name).write_text(text)
        rendered.append(name)
    (out / 'compose.images.env').write_text(''.join(f'{k}={v}\n' for k, v in sorted(images.items())))
    rendered.append('compose.images.env')
    steps = [{'step': s, 'command': c, 'undo': u} for s, c, u in STEPS[host_role]]
    ready = all(checks.values())
    return {'ready': ready, 'executed': False, 'host_role': host_role, 'checks': checks, 'details': details, 'rendered': rendered, 'steps': steps}


def _archive():
    sys.path.insert(0, str(ROOT / 'scripts/release'))
    import archive
    return archive


def promote(release_root, release_id, allowed_signers, *, at=None):
    """After the deploy gate passed on the host: the sealed, signed release becomes current, the previous one known_good."""
    archive = _archive()
    archive.check(release_root, release_id, allowed_signers)  # signature, completeness and seal (read-only)
    return archive.promote(release_root, release_id, at=at)


def rollback_check(release_root, target_id, *, database_head=None):
    """AT-057 with the archive: the target must be the known_good release or an older sealed one whose image archives are still
    there, and must run on the current schema (release.rollback_check against the current manifest)."""
    archive = _archive()
    root = Path(release_root)
    index = archive.rebuild_index(root)
    by_id = {e['release_id']: e for e in index['releases']}
    current, good = index['current'], index['known_good']
    reasons = []
    if current is None or good is None:
        reasons.append('no_current_or_known_good')
    target = by_id.get(target_id)
    if target is None or not target['sealed']:
        reasons.append('target_unknown_or_not_sealed')
    elif not target['images_present']:
        reasons.append('target_images_pruned')  # a rebuilt image would not be the release that was tested and deployed
    if not reasons and (target_id == current or target['built_at'] > by_id[good]['built_at']):
        reasons.append('target_newer_than_known_good')
    if reasons:
        raise release.Refused(json.dumps({'reasons': reasons, 'hints': ['fix forward: release a corrected build on the current schema']}))
    return release.rollback_check(root / 'releases' / current / 'manifest.json', root / 'releases' / target_id / 'manifest.json', database_head=database_head)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('dry-run')
    p.add_argument('--host-role', choices=('app', 'data'), required=True)
    p.add_argument('--stage-env', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--release-dir', type=Path)
    p.add_argument('--allowed-signers', type=Path)
    p.add_argument('--trusted-sha')
    p.add_argument('--repo', type=Path)
    p.add_argument('--trusted-ref')
    p.add_argument('--image-var', action='append', default=[])
    p.add_argument('--compose-env', type=Path)
    p.add_argument('--current-manifest', type=Path)
    p.add_argument('--database-head')
    p.add_argument('--load-check', action='store_true')
    p = sub.add_parser('load-check')
    p.add_argument('--release-dir', type=Path, required=True)
    p = sub.add_parser('promote')
    p.add_argument('--release-root', type=Path, required=True)
    p.add_argument('--release-id', required=True)
    p.add_argument('--allowed-signers', type=Path, required=True)
    p = sub.add_parser('rollback-check')
    p.add_argument('--release-root', type=Path, required=True)
    p.add_argument('--target', required=True)
    p.add_argument('--database-head')
    a = parser.parse_args(argv)
    if a.command != 'dry-run':
        try:
            if a.command == 'load-check':
                result = release.load_check(a.release_dir)
            elif a.command == 'promote':
                result = promote(a.release_root, a.release_id, a.allowed_signers)
            else:
                result = rollback_check(a.release_root, a.target, database_head=a.database_head)
        except release.Refused as refused:
            print(json.dumps({'ok': False, 'refused': str(refused)}))
            return 1
        except release.NotInstalled as missing:
            print(json.dumps({'ok': False, 'not_installed': str(missing)}))
            return 3
        except (release.InputError, OSError, ValueError, KeyError) as error:
            print(json.dumps({'ok': False, 'error': type(error).__name__}))
            return 2
        print(json.dumps({'ok': True, **result}, sort_keys=True, default=str))
        return 0
    try:
        image_vars = dict(v.split('=', 1) for v in a.image_var) or None
        out = dry_run(host_role=a.host_role, stage_env=a.stage_env, out=a.out, release_dir=a.release_dir, allowed_signers=a.allowed_signers,
                      trusted_sha=a.trusted_sha, repo=a.repo, trusted_ref=a.trusted_ref, image_vars=image_vars, compose_env=a.compose_env,
                      current_manifest=a.current_manifest, database_head=a.database_head, load_check=a.load_check)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError, stage_render.Refused, release.InputError) as error:
        print(json.dumps({'ready': False, 'error': type(error).__name__, 'detail': str(error) if isinstance(error, (ValueError, stage_render.Refused)) else None}))
        return 2
    print(json.dumps(out, sort_keys=True))
    return 0 if out['ready'] else 1


if __name__ == '__main__':
    sys.exit(main())
