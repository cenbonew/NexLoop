#!/usr/bin/env python3
"""NX-035 on APP_HOST: the daily maintenance of the file backup's repo2 (TOS nexloop/restic-files), the entry of
nexloop-file-backup-maint.service (docs/implementation/NX-035-file-backup.md §6.4; dispatcher 2026-10-26, option 甲).

repo1 is the restore journal's repository on DATA_HOST and is maintained there (nexloop-restic-maint); this entry touches repo2 only and
refuses a repository location that is not an S3 one. One run:

  0 snapshots --json: any snapshot dated more than an hour ahead stops the run before forget (L6 F1: keep-within counts from a group's
    newest snapshot, so a future-dated one would make forget delete every real one; restic_future_guard.py)
  1 forget --retry-lock 10m --group-by host,tags --keep-within 14d --keep-monthly 1 --prune: each file kind (files_artifact,
    files_runtime: their tags) keeps D4's window. Under restic's default grouping (host,paths) every file snapshot, each from a new
    staging directory, is a group of one and is never forgotten.
  2 check --retry-lock 10m; on Sundays (host time) check --read-data-subset 1/10 as well.

--retry-lock: prune takes the exclusive lock; a file backup round (every 15 minutes, its shared lock) is waited for instead of failing
this run, and the timer runs between two rounds. --dry-run (installer step 5): forget --dry-run only, nothing removed, no check.
The repository location, the password and the object storage keys stay in their 0600 files (read only to prove they are private and
the location is S3); restic's environment is PATH, LANG and the three AWS keys (nexloop_eios.file_backup._environment). It prints one
JSON line of states and counts, never a secret or restic's own output; exit 0 only when every step succeeded.

    file_backup_maint.py --restic BIN --repository-file F --password-file F --environment-file F [--s3-bucket-lookup auto|dns|path]
        [--dry-run] [--deep auto|always|never]
"""
import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # restic_future_guard.py, its sibling in the release

from nexloop_eios.file_backup import BUCKET_LOOKUPS, _environment, _restic  # noqa: E402
from nexloop_eios.private_configuration import read_private_text  # noqa: E402
import restic_future_guard as guard  # noqa: E402

RETENTION = ['--group-by', 'host,tags', '--keep-within', '14d', '--keep-monthly', '1']
LOCK = ['--retry-lock', '10m']


class NotRepo2(ValueError):
    pass


class OffsiteKeysMissing(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, 'configuration unavailable\n')


def _removed(stdout):
    """Snapshots forget removed (or would remove), from its JSON groups; None when the output is not that shape."""
    for line in reversed(stdout.splitlines()):
        try:
            groups = json.loads(line)
        except ValueError:
            continue
        if type(groups) is list and all(type(g) is dict for g in groups):
            return sum(len(g.get('remove') or []) for g in groups)
    return None


def maintain(*, restic, repository_file, password_file, environment_file, s3_bucket_lookup=None, dry_run=False, deep='auto', clock=time.localtime):
    location = read_private_text(repository_file, maximum=4096)
    read_private_text(password_file, maximum=4096)
    if not location.startswith('s3:'):
        # repo1 (sftp, DATA_HOST) is maintained by DATA_HOST's nexloop-restic-maint; this unit never prunes it.
        raise NotRepo2('not an S3 repository')
    environment = _environment(environment_file)
    if not {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'} <= set(environment):
        raise OffsiteKeysMissing('object storage keys missing')
    common = {'repository_file': repository_file, 'password_file': password_file, 'environment': environment, 'timeout': 3 * 3600,
              'options': ('-o', 's3.bucket-lookup=' + s3_bucket_lookup) if s3_bucket_lookup else ()}
    # L6 F1: a snapshot dated in the future would make keep-within forget every real one of its group: no forget then.
    listed = _restic(restic, ['snapshots', '--json', '--no-lock'], **common)
    try:
        verdict = guard.check(json.loads(listed.stdout or 'null'), datetime.now(UTC), 3600) if listed.returncode == 0 else None
    except ValueError:
        verdict = None
    if verdict is None or not verdict['ok']:
        return {'forget': 'refused_future_snapshots' if verdict else f'snapshots exit {listed.returncode}', 'removed': None, 'dry_run': dry_run,
                'check': 'not_run', **({'future': verdict['future'], 'unreadable': verdict['unreadable']} if verdict else {})}
    forget = _restic(restic, ['forget', *LOCK, *RETENTION, *(['--dry-run'] if dry_run else ['--prune']), '--json'], **common)
    result = {'forget': 'ok' if forget.returncode == 0 else f'exit {forget.returncode}', 'removed': _removed(forget.stdout) if forget.returncode == 0 else None,
              'dry_run': dry_run}
    if dry_run or forget.returncode != 0:
        result['check'] = 'not_run'
        return result
    checked = _restic(restic, ['check', *LOCK], **common)
    result['check'] = 'ok' if checked.returncode == 0 else f'exit {checked.returncode}'
    result['deep'] = deep == 'always' or (deep == 'auto' and clock().tm_wday == 6)
    if result['deep'] and checked.returncode == 0:
        subset = _restic(restic, ['check', *LOCK, '--read-data-subset', '1/10'], **common)
        result['deep_check'] = 'ok' if subset.returncode == 0 else f'exit {subset.returncode}'
    return result


def main(argv=None, clock=time.localtime):
    p = _Parser(description='NexLoop file backup repo2 maintenance (APP_HOST)')
    p.add_argument('--restic', required=True)
    for name in ('repository-file', 'password-file', 'environment-file'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--s3-bucket-lookup', choices=BUCKET_LOOKUPS)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--deep', choices=('auto', 'always', 'never'), default='auto')
    a = p.parse_args(argv)
    try:
        result = maintain(restic=a.restic, repository_file=a.repository_file, password_file=a.password_file, environment_file=a.environment_file,
                          s3_bucket_lookup=a.s3_bucket_lookup, dry_run=a.dry_run, deep=a.deep, clock=clock)
    except Exception as error:  # noqa: BLE001 - the class name only (NotRepo2, OffsiteKeysMissing, PermissionError, ...), never a message
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 1
    ok = all(v == 'ok' for k, v in result.items() if k in ('forget', 'check', 'deep_check') and not (k == 'check' and a.dry_run))
    print(json.dumps({'ok': ok, **result}, sort_keys=True))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
