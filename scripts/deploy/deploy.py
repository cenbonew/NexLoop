#!/usr/bin/env python3
"""NX-032 slice 3: the deployment dry-run (docs/implementation/NX-032-design.md §5, §7; AT-055 and AT-057 on the deploy side).

It decides whether a release may be deployed to one host and prints what would be done; it never executes a deployment step,
never connects to a host and never calls docker. Every step it lists is a B-class step the owner runs (NX-032-deploy-runbook.md).

    deploy.py dry-run --host-role app|data --stage-env <private.env> --out <new dir>
                      [--release-dir R --allowed-signers F --trusted-sha SHA | --repo DIR --trusted-ref REF]
                      [--image-var BACKEND_IMAGE=<manifest image name> ...] [--compose-env <private compose env>]
                      [--current-manifest <deployed manifest.json> --database-head NNNN]

- APP_HOST: the signed release must verify (release.py verify, AT-055): the manifest's commit equals the trusted SHA (given, or
  resolved from the trusted ref of the local repository), every manifest image is deployed and every archive is unchanged. The
  Compose image variables are rendered from the manifest; a private Compose env naming any other digest is refused.
- DATA_HOST: the PostgreSQL and Valkey images are the digests pinned in versions.lock.json.
- With the currently deployed manifest: release.py rollback-check refuses a blind rollback (AT-057) and says how to move forward.
- The configuration templates of the host are rendered (whitelist renderer) into --out with the Compose image env.
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
        ('load_images', 'docker load -i <release>/images/<archive> for each manifest image; compare the loaded digest with the manifest', 'docker image rm <digest>'),
        ('install_configuration', 'scp <out>/{Caddyfile,compose.images.env} "$SSH_ALIAS_APP:$NEXLOOP_ROOT/config/"', 'restore the previous files'),
        ('firewall', 'ssh "$SSH_ALIAS_APP" sudo nft -c -f <app-host.nft> && sudo nft -f <app-host.nft>', 'sudo nft delete table inet nexloop_stage'),
        ('backup_gate', 'verified backup before any migration (D5; nexloop-migrate refuses without one, AT-056)', None),
        ('stop_old_agent_host', 'docker compose -p nexloop-app stop agent-host (one SQLite owner at a time)', 'docker compose -p nexloop-app start agent-host'),
        ('start_app', 'docker compose -p nexloop-app -f compose.app.yaml --env-file compose.app.env --env-file compose.images.env up -d (REAL_DISPATCH_ENABLED=false)',
         'docker compose -p nexloop-app up -d with the previous compose.images.env'),
        ('inventory_after_and_gate', 'inventory.py --phase after …; nexloop-doctor --deploy-gate <app spec>; doctor --release (running digests = manifest)', None)],
}


class Refused(Exception):
    pass


def _env(path):
    return dict(line.split('=', 1) for line in Path(path).read_text().splitlines() if line and not line.startswith('#') and '=' in line)


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def dry_run(*, host_role, stage_env, out, release_dir=None, allowed_signers=None, trusted_sha=None, repo=None, trusted_ref=None, image_vars=None,
            compose_env=None, current_manifest=None, database_head=None, lock=None):
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
    a = parser.parse_args(argv)
    try:
        image_vars = dict(v.split('=', 1) for v in a.image_var) or None
        out = dry_run(host_role=a.host_role, stage_env=a.stage_env, out=a.out, release_dir=a.release_dir, allowed_signers=a.allowed_signers,
                      trusted_sha=a.trusted_sha, repo=a.repo, trusted_ref=a.trusted_ref, image_vars=image_vars, compose_env=a.compose_env,
                      current_manifest=a.current_manifest, database_head=a.database_head)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError, stage_render.Refused, release.InputError) as error:
        print(json.dumps({'ready': False, 'error': type(error).__name__, 'detail': str(error) if isinstance(error, (ValueError, stage_render.Refused)) else None}))
        return 2
    print(json.dumps(out, sort_keys=True))
    return 0 if out['ready'] else 1


if __name__ == '__main__':
    sys.exit(main())
