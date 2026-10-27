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
  handles  the errno of open_by_handle_at and name_to_handle_at (L6 F1): the unit filters both, so EPERM; anything else (EINVAL,
           ESTALE, a success) means the capability could open inodes past the sandbox's mounts
  append_only  for each --append-only-probe repository (repo1 over DATA_HOST's rest-server): a new snapshot taken (backup 0), forgetting
           it refused and the snapshot still listed afterwards. restic 0.18.0 exits 0 when the server refuses the DELETE (cmd_forget.go:
           ParallelRemove's callback prints `unable to remove snapshot/<id> from the repository` and returns nil; the 403 itself is not
           printed), so `refused` is whether that line names this probe's own snapshot id; `forget` is restic's exit code, reported only.
           A forget of an id restic does not find also exits 0 (`Ignoring "<id>": ...`), with no such line
  restic   the first line of `restic version`; repos: restic's exit code of `cat config` per --repo (0 the repository opens with
           its password, 10 it does not exist, 12 the password is wrong); init: the exit code of `restic init` for --init repos that
           did not exist (then `cat config` again)

Paths and exit codes only: nothing read from a file, nor restic's output past its version line, is printed.
"""
import argparse
import ctypes
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from nexloop_eios.file_backup import _environment, _restic, _snapshot_id  # noqa: E402

PROBE_TAG = 'nexloop-append-only-probe'


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


def handle_calls(libc=None):
    """Each handle system call once, with a harmless argument; its errno's name ('EPERM' when the filter holds)."""
    try:
        libc = libc or ctypes.CDLL(None, use_errno=True)
        functions = {name: getattr(libc, name) for name in ('open_by_handle_at', 'name_to_handle_at')}
    except (OSError, AttributeError):
        return {'open_by_handle_at': 'unavailable', 'name_to_handle_at': 'unavailable'}
    out = {}
    # struct file_handle { unsigned int handle_bytes; int handle_type; unsigned char f_handle[]; }: 8 bytes of a zero handle.
    handle = ctypes.create_string_buffer(8 + 128)
    ctypes.c_uint.from_buffer(handle).value = 8
    mount_id = ctypes.c_int(0)
    directory = os.open('/', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        for name, call in (('open_by_handle_at', lambda: functions['open_by_handle_at'](directory, handle, os.O_RDONLY)),
                           ('name_to_handle_at', lambda: functions['name_to_handle_at'](-100, b'/', handle, ctypes.byref(mount_id), 0))):
            ctypes.set_errno(0)
            result = call()
            if result >= 0:
                if name == 'open_by_handle_at':
                    os.close(result)
                out[name] = 'succeeded'
            else:
                out[name] = errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
    finally:
        os.close(directory)
    return out


def listing(path):
    try:
        return sorted(os.listdir(path))
    except OSError as error:
        return 'error: ' + type(error).__name__


def append_only(restic, common, probe_dir):
    """A new snapshot of a one-file directory is taken; forgetting it must be refused, and it must still be listed afterwards (the
    rest-server's --append-only). The probe snapshot stays (DATA_HOST's restic-maint forgets it with its tag's group). Exit codes and a
    boolean only."""
    if probe_dir is None:
        return {'error': 'no --probe-dir'}
    work = Path(probe_dir) / ('probe-' + uuid.uuid4().hex)
    try:
        work.mkdir(mode=0o700)
        (work / 'append-only-probe').write_text('nexloop append-only probe\n')
        saved = _restic(restic, ['backup', '--json', '--tag', PROBE_TAG, '--host', 'nexloop', '--retry-lock', '5m', '.'], cwd=work, **common)
        snapshot = _snapshot_id(saved.stdout) if saved.returncode == 0 else None
        if snapshot is None:
            return {'backup': saved.returncode or 'no snapshot id'}
        forget = _restic(restic, ['forget', '--retry-lock', '1m', snapshot], **common)
        # The removal of exactly this snapshot was attempted and failed: restic's own line, never its other output.
        refused = f'unable to remove snapshot/{snapshot} from the repository' in (forget.stdout or '') + (forget.stderr or '')
        listed = _restic(restic, ['snapshots', '--json', '--tag', PROBE_TAG], **common)
        try:
            still = listed.returncode == 0 and any(s.get('id') == snapshot for s in json.loads(listed.stdout or '[]'))
        except (ValueError, AttributeError):
            still = False
        return {'backup': 0, 'forget': forget.returncode, 'refused': refused, 'still_listed': still}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'error': type(error).__name__}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def repository(spec):
    name, *rest = spec.split(',')
    if len(rest) not in (2, 3, 4, 5):
        raise SystemExit('--repo NAME,REPOSITORY_FILE,PASSWORD_FILE[,ENVIRONMENT_FILE[,BUCKET_LOOKUP[,CACERT]]]')
    return name, rest


def main(argv=None):
    p = argparse.ArgumentParser(description='nexloop-file-backup sandbox check (installer step 5)')
    p.add_argument('--restic', required=True)
    p.add_argument('--list', action='append', default=[])
    p.add_argument('--read', action='append', default=[])
    p.add_argument('--hidden', action='append', default=[])
    p.add_argument('--repo', action='append', default=[], type=repository)
    p.add_argument('--init', action='append', default=[])
    p.add_argument('--append-only-probe', action='append', default=[])
    p.add_argument('--probe-dir', type=Path)
    a = p.parse_args(argv)
    out = {'listing': {d: listing(d) for d in a.list}, 'read': {f: readable(f) for f in a.read}, 'hidden': {f: hidden(f) for f in a.hidden},
           'handles': handle_calls(), 'repos': {}, 'init': {}, 'append_only': {}}
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
                  'options': (('-o', 's3.bucket-lookup=' + more[1]) if len(more) > 1 and more[1] else ())
                  + (('--cacert', more[2]) if len(more) > 2 and more[2] else ())}
        try:
            code = _restic(a.restic, ['cat', 'config'], **common).returncode
            if code == 10 and name in a.init:
                out['init'][name] = _restic(a.restic, ['init'], **common).returncode
                code = _restic(a.restic, ['cat', 'config'], **common).returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            code = 'error: ' + type(error).__name__
        out['repos'][name] = code
        if code == 0 and name in a.append_only_probe:
            out['append_only'][name] = append_only(a.restic, common, a.probe_dir)
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
