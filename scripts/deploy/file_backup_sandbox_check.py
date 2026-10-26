#!/usr/bin/env python3
"""NX-035 slice 3 on APP_HOST: what nexloop-file-backup.service can see, from inside its own sandbox (D3; installer step 5).

scripts/deploy/file_backup_install.py runs this with systemd-run and the unit's [Service] settings as properties, as the unit's user
with the unit's single capability (CAP_DAC_READ_SEARCH, which reads past every file mode): so what this reports is exactly what a
timer run could read. It prints one JSON line and nothing else:

  listing  the entries of each --list directory (the installer expects /srv/nexloop to show data and secrets only, secrets only
           backup, data only artifacts and runtime: no other service's secret is mounted at all)
  read     for each --read path: true when its content can be read (the two data roots and this unit's own files)
  hidden   for each --hidden path: true when nothing of it can be seen - absent, refused, an empty directory or a file that reads
           empty (InaccessiblePaths= mounts an empty node over it; the capability opens that node, but there is nothing in it)
  restic   the first line of `restic version`; repos: restic's exit code of `cat config` per --repo (0 the repository opens with
           its password, 10 it does not exist, 12 the password is wrong); init: the exit code of `restic init` for --init repos that
           did not exist (then `cat config` again)

Paths and exit codes only: nothing read from a file, nor restic's output past its version line, is printed.
"""
import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from nexloop_eios.file_backup import _environment, _restic  # noqa: E402


def readable(path):
    try:
        if os.path.isdir(path):
            os.listdir(path)
            return True
        with open(path, 'rb') as stream:
            stream.read(1)
        return True
    except OSError:
        return False


def hidden(path):
    try:
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode):
            return os.listdir(path) == []
        with open(path, 'rb') as stream:
            return stream.read(1) == b''
    except OSError:
        return True


def listing(path):
    try:
        return sorted(os.listdir(path))
    except OSError as error:
        return 'error: ' + type(error).__name__


def repository(spec):
    name, *rest = spec.split(',')
    if len(rest) not in (2, 3, 4):
        raise SystemExit('--repo NAME,REPOSITORY_FILE,PASSWORD_FILE[,ENVIRONMENT_FILE[,BUCKET_LOOKUP]]')
    return name, rest


def main(argv=None):
    p = argparse.ArgumentParser(description='nexloop-file-backup sandbox check (installer step 5)')
    p.add_argument('--restic', required=True)
    p.add_argument('--list', action='append', default=[])
    p.add_argument('--read', action='append', default=[])
    p.add_argument('--hidden', action='append', default=[])
    p.add_argument('--repo', action='append', default=[], type=repository)
    p.add_argument('--init', action='append', default=[])
    a = p.parse_args(argv)
    out = {'listing': {d: listing(d) for d in a.list}, 'read': {f: readable(f) for f in a.read}, 'hidden': {f: hidden(f) for f in a.hidden},
           'repos': {}, 'init': {}}
    try:
        version = subprocess.run([a.restic, 'version'], env=_environment(None), capture_output=True, text=True, timeout=60)
        out['restic'] = ' '.join(version.stdout.split()[:2]) if version.returncode == 0 else f'exit {version.returncode}'
    except OSError as error:
        out['restic'] = 'error: ' + type(error).__name__
    for name, (repository_file, password_file, *more) in a.repo:
        try:
            environment = _environment(more[0] if more and more[0] else None)
        except Exception as error:  # an unreadable or malformed environment file: the name only
            out['repos'][name] = 'error: ' + type(error).__name__
            continue
        common = {'repository_file': repository_file, 'password_file': password_file, 'environment': environment, 'timeout': 300,
                  'options': ('-o', 's3.bucket-lookup=' + more[1]) if len(more) > 1 and more[1] else ()}
        try:
            code = _restic(a.restic, ['cat', 'config'], **common).returncode
            if code == 10 and name in a.init:
                out['init'][name] = _restic(a.restic, ['init'], **common).returncode
                code = _restic(a.restic, ['cat', 'config'], **common).returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            code = 'error: ' + type(error).__name__
        out['repos'][name] = code
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
