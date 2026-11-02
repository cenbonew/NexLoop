#!/usr/bin/env python3
"""D-LEDGER (2026-11-02): the bind data directories of the APP_HOST Compose file, prepared explicitly and checked, never left to Docker.

A bind source that does not exist is created by Docker as root, 0755 (OPERATIONS §8a.5); a service running as uid 10001 then
cannot write it (stage, incentive-ledger: the first issuance was decided a certain failure). Every directory under
${NEXLOOP_ROOT}/data that a service binds must exist before the first `up`, owned by that service's account, 0700.

    data_dirs.py plan  --stage-env <private stage.env> [--compose deploy/stage/compose.app.example.yaml]
        prints one `install -d -o <uid> -g <uid> -m 0700 <dir>` line per directory (the deploy preparation step; run as root)
    data_dirs.py check --stage-env <private stage.env> [--compose ...]
        read only: each directory exists, is a directory (not a symbolic link), is owned by the account of every service binding it
        and that owner has rwx. One JSON object; exit 0 pass, 1 fail, 2 input error.

The account of a service is its Compose `user:` (variables from the stage env) or, without one, the backend image's USER 10001
(deploy/community/Dockerfile). No directory found is a failure, never a pass (an empty or unreadable Compose file checks nothing).
post_deploy_check.py runs the same check as `data_dirs`. Standard library and PyYAML only.

Order before every APP_HOST `up` (OPERATIONS §8a.5): plan (install -d the new directories as root) -> check, exit 0 required -> up;
after it, post_deploy_check item 8 checks again.

Scope: only the ${NEXLOOP_ROOT}/data/* binds of the APP_HOST Compose file (deploy/stage/compose.app.example.yaml). The bind
directories of the DATA_HOST Compose file and of the reference endpoint Compose files are not covered; a new bind there must be
wired into a check for its own Compose file.
"""
import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy/stage/compose.app.example.yaml'
IMAGE_UID = 10001
SOURCE = re.compile(r'^\$\{NEXLOOP_ROOT(?::\?[^}]*)?\}/(data/[A-Za-z0-9._/-]+)(?::|$)')
VARIABLE = re.compile(r'\$\{([A-Z0-9_]+)(?::[?-][^}]*)?\}')


class InputError(ValueError):
    pass


def _uid(service, env):
    user = (service or {}).get('user')
    if user is None:
        return IMAGE_UID
    value = VARIABLE.sub(lambda m: env.get(m.group(1), ''), str(user)).split(':', 1)[0]
    if not value.isdigit():
        raise InputError('a service user is not a numeric uid in the stage env')
    return int(value)


def bind_dirs(compose_path, env):
    """[{path, uid, services}] for every ${NEXLOOP_ROOT}/data bind source of the Compose file, one entry per directory."""
    import yaml
    root = env.get('NEXLOOP_ROOT', '')
    if not root.startswith('/'):
        raise InputError('the stage env names NEXLOOP_ROOT (absolute)')
    value = yaml.safe_load(Path(compose_path).read_text()) or {}
    out = {}
    for name, service in sorted((value.get('services') or {}).items()):
        for volume in (service or {}).get('volumes') or []:
            # A short form is <source>:<target>[:mode]; its source holds the ':' of ${NEXLOOP_ROOT:?...}, so match the whole string.
            match = SOURCE.match(str((volume.get('source') if isinstance(volume, dict) else volume) or ''))
            if not match:
                continue
            path = root.rstrip('/') + '/' + match.group(1).rstrip('/')
            entry = out.setdefault(path, {'path': path, 'uid': _uid(service, env), 'services': []})
            if entry['uid'] != _uid(service, env):
                raise InputError(f'{match.group(1)} is bound by services running as different accounts')
            entry['services'].append(name)
    return [out[p] for p in sorted(out)]


def plan(dirs):
    return [f'install -d -o {d["uid"]} -g {d["uid"]} -m 0700 {d["path"]}' for d in dirs]


def live_stat(path):
    """{kind, uid, mode} of a path without following a symbolic link; None when it does not exist."""
    try:
        s = os.lstat(path)
    except FileNotFoundError:
        return None
    kind = 'directory' if stat.S_ISDIR(s.st_mode) else 'symlink' if stat.S_ISLNK(s.st_mode) else 'other'
    return {'kind': kind, 'uid': s.st_uid, 'mode': stat.S_IMODE(s.st_mode)}


def check(dirs, stat_of=live_stat):
    """Pass only when there is at least one directory and every one is a directory owned (rwx) by its services' account."""
    failing = []
    for d in dirs:
        s = stat_of(d['path'])
        reason = ('missing' if s is None else 'not_a_directory' if s['kind'] != 'directory'
                  else 'owned_by_root' if s['uid'] == 0 and d['uid'] != 0 else 'wrong_owner' if s['uid'] != d['uid']
                  else 'owner_cannot_write' if s['mode'] & 0o700 != 0o700 else None)
        if reason:
            failing.append({'path': d['path'], 'services': d['services'], 'expected_uid': d['uid'], 'reason': reason,
                            **({'uid': s['uid'], 'mode': oct(s['mode'])} if s and s['kind'] == 'directory' else {})})
    result = {'pass': bool(dirs) and not failing, 'checked': len(dirs), 'failing': failing}
    return result if dirs else {**result, 'reason': 'no_data_directories'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='APP_HOST bind data directories: preparation lines and the read-only check')
    parser.add_argument('command', choices=['plan', 'check'])
    parser.add_argument('--stage-env', required=True)
    parser.add_argument('--compose', default=str(COMPOSE))
    args = parser.parse_args(argv)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from deploy import _env
        dirs = bind_dirs(args.compose, _env(args.stage_env))
    except (InputError, OSError, ValueError) as error:
        print(json.dumps({'pass': False, 'error': type(error).__name__, 'detail': str(error) if isinstance(error, InputError) else None}))
        return 2
    if args.command == 'plan':
        if not dirs:
            print('no data directories found', file=sys.stderr)
            return 1
        print('\n'.join(plan(dirs)))
        return 0
    result = check(dirs)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
