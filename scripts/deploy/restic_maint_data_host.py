#!/usr/bin/env python3
"""NX-035 on DATA_HOST (owner, root): restic retention per tag in the installed nexloop-restic-maint.service, one command
(docs/implementation/NX-035-restic-maint-groupby.md). Run through restic_maint_data_host.sh:

    sudo bash restic_maint_data_host.sh            preview, then rewrite the installed unit
    sudo bash restic_maint_data_host.sh --revert   the forget lines back to restic's default grouping

The installed unit is the deploy days' copy of deploy/stage/systemd/nexloop-restic-maint.service.example (its paths may be the host's
own), so it is not replaced: in its forget lines, and only there, `forget --keep-within 14d --keep-monthly 1 --prune` becomes
`forget --retry-lock 10m --group-by host,tags --keep-within 14d --keep-monthly 1 --prune` (L6 S2: prune's exclusive lock waits for a
backup's shared one instead of failing the run). Under restic's default grouping (host,paths) every file
snapshot of the APP_HOST file backup is a group of its own (each comes from a new staging directory) and is never forgotten; grouped
by tag the restore journal (journal) and the two file kinds (files_artifact, files_runtime) each keep D4's window.

Steps:
  1 preflight: the unit a regular file, it and every directory above it root's and not group/other-writable (lstat, no link); exactly
    the expected forget lines (one per repository, at least repo1), nothing else changed
  2 preview: each forget line, with --dry-run --json instead of --prune, run in the unit's own sandbox (systemd-run with its [Service]
    settings: its user, its EnvironmentFile; --expand-environment=yes, so its ${VAR} paths are expanded as in the unit - systemd 254 or
    later, an older systemd-run refuses the option and the preview fails); per group its host, tags, kept and removed counts. A preview
    that exits non-zero or whose last line is not restic's JSON stops the run before anything is written (L6 S1, S3)
  3 the unit rewritten atomically (the previous text kept as <unit>.before-groupby, 0644 root), then `systemctl daemon-reload`
Output: step lines and the preview's counts (snapshot ids, tags and counts; never a password or a repository location). Exit 0 done,
1 a step failed, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

UNIT = '/etc/systemd/system/nexloop-restic-maint.service'
OLD = 'forget --keep-within 14d --keep-monthly 1 --prune'
NEW = 'forget --retry-lock 10m --group-by host,tags --keep-within 14d --keep-monthly 1 --prune'
SANDBOX_SKIP = {'Type', 'ExecStart', 'ExecStartPre', 'ExecStartPost', 'TimeoutStartSec'}


class Refused(ValueError):
    pass


class Failed(RuntimeError):
    pass


def run(command, *, check=True):
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise Failed(f'{Path(command[0]).name} exited {done.returncode}')
    return done.stdout


def root_owned(path, *, what, owners=(0,)):
    """Root's alone: owner root, nothing group/other-writable, no symbolic link on the way (lstat, never followed)."""
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        try:
            info = os.lstat(part)
        except OSError:
            raise Refused(f'{what}: {part} unreadable') from None
        if stat.S_ISLNK(info.st_mode):
            raise Refused(f'{what}: {part} is a symbolic link')
        if info.st_uid not in owners or info.st_mode & 0o022:
            raise Refused(f'{what}: {part} must be owned by root and not group/other-writable')


def sections(text):
    """[(section, key, value)] of a unit; continuation lines and quotes (other than the sh -c lines of the deep check) refused."""
    out, section = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.endswith('\\'):
            raise Refused('the unit uses a continuation line: rewrite it by hand')
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        key, sep, value = line.partition('=')
        if sep:
            out.append((section, key.strip(), value.strip()))
    return out


def forget_lines(text, form):
    lines = [v for s, k, v in sections(text) if s == 'Service' and k == 'ExecStart' and ' forget ' in f' {v} ']
    if not 1 <= len(lines) <= 2 or not all(form in v and v.count(' forget ') == 1 for v in lines):
        raise Refused(f'the unit\'s forget lines are not the expected {len(lines)} of "{form}": change them by hand')
    return lines


def sandbox(text):
    out = []
    for section, key, value in sections(text):
        if section == 'Service' and key not in SANDBOX_SKIP:
            if '"' in value or "'" in value:
                raise Refused('a quoted [Service] setting: the preview sandbox cannot mirror it')
            out.append((key, value))
    return out


def preview(text, lines, runner, out):
    properties = [a for key, value in sandbox(text) for a in ('-p', f'{key}={value}')]
    for line in lines:
        argv = line.replace(NEW, NEW.replace('--prune', '--dry-run --json')).split()
        try:
            # L6 S1: the unit's ${VAR} paths expanded explicitly, not by a systemd-run default (systemd 254+); S3: exit 0 required.
            answer = runner(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec', '--expand-environment=yes',
                             *properties, *argv])
        except Failed as error:
            raise Failed(f'the forget preview failed ({error}; a lock held for 10 minutes, the repository, its password, or a systemd older '
                         'than 254 refusing --expand-environment): nothing written') from None
        try:
            groups = json.loads(answer.strip().splitlines()[-1])
            assert type(groups) is list
        except (ValueError, IndexError, AssertionError):
            raise Failed('the forget preview gave no answer (a lock held for 10 minutes, the repository or its password): nothing written') from None
        repository = 'repo2' if 'REPO2' in line else 'repo1'
        for g in groups:
            print(f'preview {repository}: ' + json.dumps({'host': g.get('host'), 'tags': g.get('tags'), 'keep': len(g.get('keep') or []),
                                                         'remove': len(g.get('remove') or [])}, sort_keys=True), file=out)
        if not groups:
            print(f'preview {repository}: no snapshot', file=out)


def write_atomic(path, text, mode=0o644):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None, out=sys.stdout, runner=run, unit=UNIT, trust=root_owned):
    p = argparse.ArgumentParser(description='restic retention per tag in nexloop-restic-maint.service (DATA_HOST, owner, root)')
    p.add_argument('--revert', action='store_true')
    a = p.parse_args(argv)
    os.umask(0o022)
    path = Path(unit)
    before, after = (NEW, OLD) if a.revert else (OLD, NEW)
    try:
        trust(path, what='unit')
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise Refused(f'{path}: a regular file')
        text = path.read_text()
        if before not in text and after in text:
            forget_lines(text, after)
            print('done: the forget lines already read "' + after + '" (nothing to do)', file=out)
            return 0
        lines = forget_lines(text, before)
        if text.count(before) != len(lines) or after in text:
            raise Refused('the forget text appears outside the forget lines, or both forms are there: change the unit by hand')
        new_text = text.replace(before, after)
        print(f'step 1: {len(lines)} forget line(s) of {path.name}', file=out)
        if not a.revert:
            preview(text, [line.replace(OLD, NEW) for line in lines], runner, out)
            print('step 2: preview done (nothing removed)', file=out)
        backup = path.with_name(path.name + '.before-groupby')
        if os.path.islink(backup):
            raise Refused(f'{backup} is a symbolic link')
        write_atomic(backup, text)
        write_atomic(path, new_text)
        runner(['systemctl', 'daemon-reload'])
        print(f'step 3: {path.name} rewritten ({"restic default grouping" if a.revert else "--group-by host,tags"}), daemon-reload; '
              f'the previous text kept as {backup.name}', file=out)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
