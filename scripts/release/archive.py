#!/usr/bin/env python3
"""NX-034 §2.4 release archive on the release host (D3 retention). Local only: nothing is pushed, copied or backed up here
(manifests, signatures and SBOMs reach the other host through NX-035's file backup; image archives are not backed up).

Layout under <RELEASE_ROOT> (a parameter; never a path written in the repository):

    releases/<release_id>/manifest.json, manifest.json.sig, images/<name>.tar, sbom/<name>.{spdx,findings}.json,
                          preflight/{preflight,licenses,source.spdx}.json
    index.json            one entry per sealed release, rebuilt from the directories (derived; safe to delete)
    deployments.json      append-only deployment records written by the deploy tool (release_id, deployed_at, known_good)
    current               symbolic link to releases/<release_id>: the release deployed now
    known_good            symbolic link to releases/<release_id>: the release that was current before it (the rollback target)
                          Both are switched only by promote (deploy.py promote, after the deploy gate passed), atomically.

    archive.py seal   --root R --release-id ID --allowed-signers F   check completeness and signature, make read-only, index
    archive.py check  --root R --release-id ID --allowed-signers F   read-only: signature, completeness, sealed (verify-release)
    archive.py index  --root R                                       rebuild and print the index
    archive.py record-deployment --root R --release-id ID [--known-good] [--at ISO]
    archive.py prune  --root R [--apply] [--now ISO]                 image archives outside retention (dry run by default)

Each command prints one JSON object. Exit codes: 0 ok, 1 refused, 2 input or environment error, 3 a required tool is missing.
"""
import argparse
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402

RETENTION = ROOT / 'deploy/release/retention.v1.json'
_RELEASE_ID = re.compile(r'[0-9]{8}-[0-9a-f]{12}-[0-9]{1,6}')
_SBOM_FILE = re.compile(r'sbom/[a-z0-9][a-z0-9._-]{0,127}\.(spdx|findings)\.json')
_IMAGE_FILE = re.compile(r'images/[a-z0-9][a-z0-9._-]{0,127}\.tar')
PREFLIGHT_FILES = {'preflight/' + name for name in release.PREFLIGHT_KEEP}


def _release_dir(root, release_id):
    if not _RELEASE_ID.fullmatch(release_id or ''):
        raise release.InputError('release_id format')
    return Path(root) / 'releases' / release_id


def _write_atomic(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'w') as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _files(directory):
    return sorted(str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file() or p.is_symlink())


def completeness_problems(directory):
    """Everything the manifest names is present and unchanged, and nothing else is in the release directory."""
    manifest = json.loads((directory / 'manifest.json').read_text())
    problems = []
    if not (directory / 'manifest.json.sig').is_file():
        problems.append('signature_missing')
    problems += release.preflight_failures(directory, manifest)
    expected = {'manifest.json', 'manifest.json.sig'}
    for image in manifest['images']:
        expected.add(image['archive'])
        path = directory / image['archive']
        if not path.is_file() or release.sha256_file(path) != image['archive_sha256']:
            problems.append('archive_changed:' + image['name'])
        if image.get('sbom_sha256'):
            sbom = directory / 'sbom' / (image['name'].replace('/', '_') + '.spdx.json')
            expected.add(str(sbom.relative_to(directory)))
            if not sbom.is_file() or release.sha256_file(sbom) != image['sbom_sha256']:
                problems.append('sbom_changed:' + image['name'])
    for name in _files(directory):
        if name in expected or name in PREFLIGHT_FILES or _SBOM_FILE.fullmatch(name):
            continue
        problems.append('unexpected_file:' + name)
    if any(p.is_symlink() for p in directory.rglob('*')):
        problems.append('symbolic_link_in_release')
    return problems


def _read_only(directory):
    for path in sorted(directory.rglob('*'), key=lambda p: -len(p.parts)):
        path.chmod(0o555 if path.is_dir() else 0o444)
    directory.chmod(0o555)


def _is_sealed(directory):
    return not any(p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) for p in [directory, *directory.rglob('*')])


def seal(root, release_id, allowed_signers):
    directory = _release_dir(root, release_id)
    if not (directory / 'manifest.json').is_file():
        raise release.InputError('no such release')
    if _is_sealed(directory):
        raise release.InputError('release already sealed')
    release.verify_signature(directory, allowed_signers)
    problems = completeness_problems(directory)
    if problems:
        raise release.Refused(';'.join(problems))
    _read_only(directory)
    return {'sealed': release_id, 'index': rebuild_index(root)['count']}


def check(root, release_id, allowed_signers):
    directory = _release_dir(root, release_id)
    if not (directory / 'manifest.json').is_file():
        raise release.InputError('no such release')
    release.verify_signature(directory, allowed_signers)
    problems = completeness_problems(directory) + ([] if _is_sealed(directory) else ['not_sealed'])
    if problems:
        raise release.Refused(';'.join(problems))
    manifest = json.loads((directory / 'manifest.json').read_text())
    return {'release_id': release_id, 'commit': manifest['commit'], 'images': {i['name']: i['reference'] for i in manifest['images']},
            'preflight': manifest['preflight']['category']}


def _deployments(root):
    path = Path(root) / 'deployments.json'
    return json.loads(path.read_text()) if path.is_file() else []


def rebuild_index(root):
    root = Path(root)
    entries = []
    for directory in sorted((root / 'releases').iterdir()) if (root / 'releases').is_dir() else []:
        if not _RELEASE_ID.fullmatch(directory.name) or not (directory / 'manifest.json').is_file():
            continue
        manifest = json.loads((directory / 'manifest.json').read_text())
        images = [directory / image['archive'] for image in manifest['images']]
        entries.append({'release_id': manifest['release_id'], 'commit': manifest['commit'], 'built_at': manifest['built_at'],
                        'manifest_sha256': release.sha256_file(directory / 'manifest.json'),
                        'signature_sha256': release.sha256_file(directory / 'manifest.json.sig') if (directory / 'manifest.json.sig').is_file() else None,
                        'preflight': {k: (manifest.get('preflight') or {}).get(k) for k in ('category', 'ok')},
                        'sealed': _is_sealed(directory), 'images_present': all(p.is_file() for p in images),
                        'images': {image['name']: image['reference'] for image in manifest['images']}})
    index = {'schema': 'nexloop-release-index/1', 'count': len(entries), 'releases': entries, 'current': current(root), 'known_good': known_good(root)}
    _write_atomic(root / 'index.json', index)
    return index


def _link_target(root, name):
    link = Path(root) / name
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    rid = Path(target).name
    return rid if _RELEASE_ID.fullmatch(rid) and Path(target).parent.name == 'releases' and not Path(target).is_absolute() else None


def known_good(root):
    return _link_target(root, 'known_good')


def _switch_link(root, name, release_id):
    """Atomic: a new relative link under a temporary name, then rename over the old one (never a moment without it)."""
    root = Path(root)
    temporary = root / f'.{name}.tmp-{os.getpid()}-{os.urandom(4).hex()}'
    os.symlink(f'releases/{release_id}', temporary)
    try:
        os.replace(temporary, root / name)
    finally:
        if temporary.is_symlink():
            temporary.unlink()
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def promote(root, release_id, *, at=None, _fail_after=None):
    """After the deploy gate passed: the previous current becomes known_good, release_id becomes current, the deployment is
    recorded as known good. Each link is switched atomically; if a later step fails, the links are put back as they were.
    _fail_after (tests only): raise after the named step to prove nothing is left half-done."""
    root = Path(root)
    directory = _release_dir(root, release_id)
    if not (directory / 'manifest.json').is_file() or not _is_sealed(directory):
        raise release.Refused('only a sealed release can be promoted')
    before = {'current': current(root), 'known_good': known_good(root)}
    if before['current'] == release_id:
        raise release.InputError('release is already current')
    try:
        if before['current'] is not None:
            _switch_link(root, 'known_good', before['current'])
        if _fail_after == 'known_good':
            raise OSError('injected failure')
        _switch_link(root, 'current', release_id)
        if _fail_after == 'current':
            raise OSError('injected failure')
        record = record_deployment(root, release_id, known_good=True, at=at)
    except BaseException:
        for name, previous in before.items():
            if previous is None:
                if (root / name).is_symlink():
                    (root / name).unlink()
            else:
                _switch_link(root, name, previous)
        raise
    rebuild_index(root)
    return {'current': release_id, 'known_good': before['current'], 'deployment': record}


def current(root):
    link = Path(root) / 'current'
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    name = Path(target).name
    return name if _RELEASE_ID.fullmatch(name) and Path(target).parent.name == 'releases' else None


def record_deployment(root, release_id, *, known_good=False, at=None):
    directory = _release_dir(root, release_id)
    if not _is_sealed(directory):
        raise release.Refused('only a sealed release can be recorded as deployed')
    records = _deployments(root)
    at = at or datetime.now(UTC)
    records.append({'release_id': release_id, 'deployed_at': at.isoformat().replace('+00:00', 'Z'), 'known_good': bool(known_good)})
    _write_atomic(Path(root) / 'deployments.json', records)
    return records[-1]


def retention_plan(root, now=None, policy=None):
    """Which image archives retention keeps (and why) and which it would remove. Manifests, signatures, SBOMs and preflight
    reports are never removed."""
    policy = policy or json.loads(RETENTION.read_text())
    now = now or datetime.now(UTC)
    index = rebuild_index(root)
    releases = [e for e in index['releases'] if e['sealed']]
    reasons = {e['release_id']: [] for e in releases}
    for entry in sorted(releases, key=lambda e: (e['built_at'], e['release_id']), reverse=True)[:policy['keep_last']]:
        reasons[entry['release_id']].append('recent')
    deployments = _deployments(root)
    horizon = now - timedelta(days=policy['deployed_within_days'])
    for record in deployments:
        if record['release_id'] in reasons and datetime.fromisoformat(record['deployed_at'].replace('Z', '+00:00')) >= horizon:
            reasons[record['release_id']].append('deployed_recently')
    live = index['current'] or (deployments[-1]['release_id'] if deployments else None)
    if policy['keep_current_and_previous_known_good']:
        if live in reasons:
            reasons[live].append('current')
        previous = next((r['release_id'] for r in reversed(deployments) if r['known_good'] and r['release_id'] != live), None)
        if previous in reasons:
            reasons[previous].append('previous_known_good')
    # The current and known_good links are the deploy tool's own state: their releases are never pruned, whatever the policy.
    for name, rid in (('current_link', index['current']), ('known_good_link', index['known_good'])):
        if rid in reasons:
            reasons[rid].append(name)
    keep = {rid: sorted(set(why)) for rid, why in reasons.items() if why}
    remove = sorted(e['release_id'] for e in releases if e['release_id'] not in keep and e['images_present'])
    return {'keep': keep, 'remove_images': remove, 'current': live}


def prune(root, *, apply=False, now=None, policy=None):
    plan = retention_plan(root, now, policy)
    removed = []
    if apply:
        for release_id in plan['remove_images']:
            if release_id in (plan['current'], known_good(root)):
                continue  # never, whatever the plan says
            directory = _release_dir(root, release_id)
            images = directory / 'images'
            images.chmod(0o755)
            try:
                for archive in sorted(images.glob('*.tar')):
                    archive.unlink()
            finally:
                images.chmod(0o555)
            removed.append(release_id)
        rebuild_index(root)
    return {'dry_run': not apply, **plan, 'removed': removed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('seal', 'check', 'index', 'record-deployment', 'prune'):
        p = sub.add_parser(name)
        p.add_argument('--root', required=True, type=Path)
        if name in ('seal', 'check', 'record-deployment'):
            p.add_argument('--release-id', required=True)
        if name in ('seal', 'check'):
            p.add_argument('--allowed-signers', required=True, type=Path)
        if name == 'record-deployment':
            p.add_argument('--known-good', action='store_true')
            p.add_argument('--at')
        if name == 'prune':
            p.add_argument('--apply', action='store_true')
            p.add_argument('--now')
    args = parser.parse_args(argv)
    try:
        if args.command == 'seal':
            result = seal(args.root, args.release_id, args.allowed_signers)
        elif args.command == 'check':
            result = check(args.root, args.release_id, args.allowed_signers)
        elif args.command == 'index':
            result = rebuild_index(args.root)
        elif args.command == 'record-deployment':
            at = datetime.fromisoformat(args.at.replace('Z', '+00:00')) if args.at else None
            result = record_deployment(args.root, args.release_id, known_good=args.known_good, at=at)
        else:
            now = datetime.fromisoformat(args.now.replace('Z', '+00:00')) if args.now else None
            result = prune(args.root, apply=args.apply, now=now)
    except release.Refused as error:
        print(json.dumps({'ok': False, 'refused': str(error)}))
        return 1
    except release.NotInstalled as error:
        print(json.dumps({'ok': False, 'not_installed': str(error)}))
        return 3
    except (release.InputError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__, 'detail': str(error)}))
        return 2
    print(json.dumps({'ok': True, **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
