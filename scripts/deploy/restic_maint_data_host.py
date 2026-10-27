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

L6 F1 of the rest-server review (2026-10-27): before every forget line a guard line runs restic_future_guard.py on the same repository
(installed root's and read-only at /opt/nexloop/restic-maint/, run with the host's /usr/bin/python3): a snapshot dated more than an
hour ahead - which keep-within would take as the group's newest and so forget every real one - stops the unit before forget/prune,
and timer_scheduled_failure alerts. --files-repo PATH --files-password-file PATH adds the files repository's maintenance (the one
rest_server_data_host.py made for APP_HOST's file backup: forget per tag, check, the Sunday deep check, its ReadWritePaths) once.
L6 F2 (2026-10-27): the guard and forget are two ExecStart lines, so APP_HOST could otherwise hold a lock, let the guard pass, add a
future-dated snapshot while forget waits for that lock, then release it. The files repository's block therefore stops the rest-server
around its guard and forget: stale locks removed (`unlock`, restic's own rule: only locks no live process refreshes), then a wait until
no lock is held (restic_future_guard.py --wait-unlocked 600: between two file backup rounds, not in one), `systemctl stop` of
nexloop-rest-server.service, the guard, forget/prune, `systemctl start`, then the checks with the server back. ExecStopPost starts it
again on every path (a failed guard, forget or wait included), waits 3 seconds and requires it active: otherwise the unit fails and
timer_scheduled_failure alerts. A file backup round that starts while the server is stopped fails (NX-035-file-backup.md §6.5).
--files-repo requires the rest-server unit active.

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
SANDBOX_SKIP = {'Type', 'ExecStart', 'ExecStartPre', 'ExecStartPost', 'ExecStopPost', 'TimeoutStartSec'}
PYTHON = '/usr/bin/python3'  # the host's own interpreter (the guard is stdlib only)
GUARD_DIR = '/opt/nexloop/restic-maint'
GUARD = GUARD_DIR + '/restic_future_guard.py'
GUARD_CALL = f'{PYTHON} -I -B {GUARD} --max-ahead-seconds 3600 -- '
REST_UNIT = 'nexloop-rest-server.service'  # rest_server_data_host.UNIT
SYSTEMCTL = '/usr/bin/systemctl'
WAIT_SECONDS = 600
FILES_KEYS = ('ExecStart=', 'ExecStopPost=', 'ReadWritePaths=')
FILES_MARK = '# NX-035 files repository (APP_HOST\'s file backup through the rest-server; restic_maint_data_host.py --files-repo)'
HERE = Path(__file__).resolve().parent


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
    if not 1 <= len(lines) <= 3 or not all(form in v and v.count(' forget ') == 1 for v in lines):
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
        # L6 F1: the guard first, exactly as the unit runs it: a future-dated snapshot stops the rewrite.
        prefix = line[:line.index(' forget ')]
        try:
            verdict = runner(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec', '--expand-environment=yes',
                              *properties, *(GUARD_CALL + prefix).split()])
        except Failed as error:
            raise Failed(f'the future-snapshot guard refused ({error}): a snapshot dated in the future, or the repository unreadable - '
                         'investigate before any forget; nothing written') from None
        print('guard: ' + (verdict.strip().splitlines()[-1] if verdict.strip() else '{}'), file=out)
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
        repository = 'repo2' if 'REPO2' in line else ('files' if ' -r ' in line else 'repo1')
        for g in groups:
            print(f'preview {repository}: ' + json.dumps({'host': g.get('host'), 'tags': g.get('tags'), 'keep': len(g.get('keep') or []),
                                                         'remove': len(g.get('remove') or [])}, sort_keys=True), file=out)
        if not groups:
            print(f'preview {repository}: no snapshot', file=out)


def is_guard(line):
    return line.startswith('ExecStart=' + GUARD_CALL)


def guarded(text):
    """Every forget line in the target form, preceded by its guard line (the same restic command line up to the subcommand)."""
    out = []
    for line in text.splitlines(keepends=True):
        bare = line.rstrip('\n')
        if bare.startswith('ExecStart=') and ' forget ' in bare and not is_guard(bare):
            bare = bare.replace(OLD, NEW)
            prefix = bare[len('ExecStart='):bare.index(' forget ')]
            guard = f'ExecStart={GUARD_CALL}{prefix}\n'
            if out and is_guard(out[-1].rstrip('\n')):
                out[-1] = guard
            else:
                out.append(guard)
            out.append(bare + '\n')
        else:
            out.append(line)
    return ''.join(out)


def files_block(restic, repo, password_file):
    """L6 F2: the rest-server stopped from before the guard until after forget/prune, so no snapshot can be added in between; started
    again by ExecStopPost on every path, which fails the unit when it is not active."""
    common = f'{restic} --no-cache -r {repo} --password-file {password_file}'
    return (f'{FILES_MARK}\nExecStart={common} unlock\n'
            f'ExecStart={PYTHON} -I -B {GUARD} --wait-unlocked {WAIT_SECONDS} -- {common}\n'
            f'ExecStart=+{SYSTEMCTL} stop {REST_UNIT}\n'
            f'ExecStart={common} {NEW}\nExecStart=+{SYSTEMCTL} start {REST_UNIT}\nExecStart={common} check --retry-lock 10m\n'
            f'ExecStart=/bin/sh -c \'if [ "$$(date +%%u)" = 7 ]; then exec {common} check --read-data-subset 1/10; fi\'\n'
            f'ExecStopPost=+{SYSTEMCTL} start {REST_UNIT}\nExecStopPost=/bin/sleep 3\nExecStopPost=+{SYSTEMCTL} is-active --quiet {REST_UNIT}\n'
            f'ReadWritePaths={repo}\n')


def without_files(text):
    """The unit without the files repository's block (its mark and the ExecStart/ExecStopPost/ReadWritePaths lines after it)."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        bare = line.rstrip('\n')
        if bare == FILES_MARK:
            skip = True
            continue
        if skip and bare.startswith(FILES_KEYS):
            continue
        skip = False
        out.append(line)
    return ''.join(out)


def with_files(text, repo, password_file):
    """The files repository's maintenance after the last line of [Service], replacing an earlier block (55271922's had no stop of the
    rest-server); the guard is added like for every forget."""
    text = without_files(text)
    first = next(v for s, k, v in sections(text) if s == 'Service' and k == 'ExecStart' and ' forget ' in f' {v} ')
    restic = first.split()[0]
    lines = text.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.strip() == '[Service]')
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith('[')), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return ''.join(lines[:end]) + files_block(restic, repo, password_file) + ''.join(lines[end:])


def reverted(text):
    return ''.join(line.replace(NEW, OLD) for line in without_files(text).splitlines(keepends=True) if not is_guard(line.rstrip('\n')))


def install_guard(root, runner, trust, out, source):
    """restic_future_guard.py from this release next to nothing else, root's and read-only."""
    trust(source, what='guard source')
    text = source.read_text()
    directory = Path(str(root).rstrip('/') + GUARD_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    trust(directory, what='guard directory')
    target = directory / 'restic_future_guard.py'
    if os.path.islink(target):
        raise Refused(f'{target} is a symbolic link')
    if not (target.exists() and target.read_text() == text):
        os.chmod(directory, 0o755)
        write_atomic(target, text, mode=0o444)
        print(f'guard: {GUARD} written', file=out)
    os.chmod(directory, 0o555)


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


def main(argv=None, out=sys.stdout, runner=run, unit=UNIT, trust=root_owned, root='/', trust_python=None, guard_source=HERE / 'restic_future_guard.py'):
    if trust_python is None:
        sys.path.insert(0, str(HERE))
        import file_backup_install  # the same directory: the interpreter's link chain checked hop by hop
        trust_python = file_backup_install.interpreter_owned
    p = argparse.ArgumentParser(description='restic retention per tag, a future-snapshot guard before every forget, the files repository '
                                            '(nexloop-restic-maint.service, DATA_HOST, owner, root)')
    p.add_argument('--revert', action='store_true')
    p.add_argument('--files-repo', type=Path)
    p.add_argument('--files-password-file', type=Path)
    a = p.parse_args(argv)
    os.umask(0o022)
    path = Path(unit)
    try:
        if (a.files_repo is None) != (a.files_password_file is None) or (a.revert and a.files_repo):
            raise Refused('--files-repo and --files-password-file go together (and not with --revert)')
        trust(path, what='unit')
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise Refused(f'{path}: a regular file')
        text = path.read_text()
        sections(text)  # continuation lines refused before anything
        if a.revert:
            new_text = reverted(text)
            forget_lines(new_text, OLD)
        else:
            current = OLD if OLD in text else NEW
            forget_lines(text, current)
            if OLD in text and NEW in text:
                raise Refused('both forget forms are in the unit: change it by hand')
            if text.count(current) != len(forget_lines(text, current)):
                raise Refused('the forget text appears outside the forget lines: change the unit by hand')
            if a.files_repo:
                if not str(a.files_repo).startswith('/') or not (a.files_repo / 'config').is_file() or os.path.islink(a.files_repo):
                    raise Refused('--files-repo: the files repository (an absolute path holding its restic config)')
                if os.path.islink(a.files_password_file) or not a.files_password_file.is_file():
                    raise Refused('--files-password-file: a regular file (rest_server_data_host.py wrote it)')
                # L6 F2: the block stops and starts the rest-server; without it (or systemctl) every run would fail.
                if not os.path.isfile(str(root).rstrip('/') + SYSTEMCTL):
                    raise Refused(f'{SYSTEMCTL} missing: the files repository\'s block stops and starts {REST_UNIT} with it')
                if runner(['systemctl', 'is-active', REST_UNIT], check=False).strip() != 'active':
                    raise Refused(f'{REST_UNIT} is not active: install it first (rest_server_data_host.sh), then add the files repository')
                text_files = with_files(text, a.files_repo, a.files_password_file)
            else:
                text_files = text
            new_text = guarded(text_files)
            trust_python(PYTHON, what='python')
            install_guard(root, runner, trust, out, Path(guard_source))
        if new_text == text:
            print('done: the unit already has the guard before every forget and the per-tag retention (nothing to do)', file=out)
            return 0
        lines = forget_lines(new_text, OLD if a.revert else NEW)
        print(f'step 1: {len(lines)} forget line(s) of {path.name}', file=out)
        if not a.revert:
            preview(new_text, lines, runner, out)
            print('step 2: preview done (guards passed, nothing removed)', file=out)
        backup = path.with_name(path.name + '.before-groupby')
        if os.path.islink(backup):
            raise Refused(f'{backup} is a symbolic link')
        write_atomic(backup, text)
        write_atomic(path, new_text)
        runner(['systemctl', 'daemon-reload'])
        print(f'step 3: {path.name} rewritten ({"restic default grouping, no guard" if a.revert else "--group-by host,tags, a guard before every forget"}'
              f'{", the files repository (the rest-server stopped around its guard and forget)" if a.files_repo else ""}), daemon-reload; the previous text kept as {backup.name}', file=out)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
