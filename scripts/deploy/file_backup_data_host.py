#!/usr/bin/env python3
"""NX-035 slice 3, step 1 on DATA_HOST (owner, root): what the APP_HOST file backup needs here (docs/implementation/NX-035-file-backup.md
§6.2). Run through file_backup_data_host.sh, one private configuration, one command:

    sudo bash file_backup_data_host.sh /path/file-backup.data-host.json
    sudo bash file_backup_data_host.sh /path/file-backup.data-host.json --uninstall

Owner decisions: D2 (2026-10-26) - one hostssl pg_hba line, nexloop_backup_recorder from APP_HOST only, scram-sha-256 (its existing
password, not rotated: nothing is set here). D4 changed on 2026-10-27: the file backup no longer shares the restore journal's
repository and is not written over sftp; its own repository is served by rest-server --append-only, installed by
rest_server_data_host.sh (step 1b). This entry is step 1a only; no sftp account, chroot or sshd drop-in is installed any more, and
--uninstall still removes those of an earlier version wherever they exist (idempotent).

Private configuration (JSON, 0600 root; deploy/examples/file-backup-data-host.example.json): schema nexloop-file-backup-data-host/1,
release_checkout (render.py and the pg_hba template come from it), python (root runs render.py with it: a real file, root's),
nexloop_root (config/pg_hba.conf is under it) and stage_env (the deployed private stage.env; APP_HOST_ADDR is read from it); the
earlier repo_path and app_host_public_key are accepted and ignored; optionally trusted_owner (the deployment user, see 1a).

Trust (root runs this code and render.py): stdlib only; the checkout, its .git, the trees root executes from and the interpreter are
root's alone, every component lstat'ed (file_backup_install.trust_release and root_owned); nothing secret is read or printed.

Step (idempotent; a rerun converges):
  1a pg_hba: rendered from the release's template with the deployed stage.env; the rendered file may differ from the installed one
     by this one hostssl line added and nothing removed. Neither file is a link (L6 I2). Written in place (same inode: the
     container bind-mounts the single file; opened without following a link; the old content kept, atomically and 0600, as
     config/pg_hba.conf.before-file-backup), checked from inside the container, PostgreSQL HUP'ed and the reload read back from
     its log; otherwise the old content is written back in place and reloaded.
     Who writes (dispatcher 2026-10-27, stage: /srv/nexloop, config/ and pg_hba.conf belong to the deployment user): config/
     root's alone (L6 I2) - root writes; or, with trusted_owner configured (file_backup_install.trusted_owner: named explicitly,
     never assumed), config/ and pg_hba.conf that user's, neither group/other-writable nor a link, every directory above root's or
     that user's - the write of pg_hba, the backup and a write-back are done in a child process with that user's uid and gid and no
     supplementary group, so a link or file that user puts in config/ gets nothing that user could not write already. Root reads
     pg_hba without following a link and only from the inode, owner and type lstat saw; it renders in its own scratch directory
     (root's, as before).
--uninstall: the D4 sftp parts of an earlier version - the sshd drop-in, the authorized key, the mount unit and the chroot directory
(when empty) - removed wherever they exist, sshd reloaded only when its drop-in was there. The pg_hba line stays (the template has
it; without APP_HOST's DSN nobody uses it).
Output: step lines only. Exit 0 done, 1 a step failed, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import stat
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import file_backup_install as A  # noqa: E402  (the same directory: the trust checks, atomic writes and refusals)

Refused, Failed, run, under = A.Refused, A.Failed, A.run, A.under
SCHEMA = 'nexloop-file-backup-data-host/1'
USER = 'nexloop-backup'
CHROOT = '/srv/nexloop-sftp'  # the D4 sftp parts: removed by --uninstall only, never installed any more
MOUNT = CHROOT + '/restic'
KEYS = '/etc/ssh/nexloop-authorized-keys'
DROPIN = '/etc/ssh/sshd_config.d/00-nexloop-file-backup.conf'
PROJECT = 'nexloop-data'
HBA_LINE = re.compile(r'hostssl [a-z_][a-z0-9_]* nexloop_backup_recorder (\d{1,3}(?:\.\d{1,3}){3})/32 scram-sha-256')
ADDR = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}')
SCRATCH = '/var/lib/nexloop/file-backup-data-host'
# What sshd must apply to nexloop-backup from APP_HOST (sshd -T -C), keyword -> value.

def load_config(path):
    c = A.read_private_json(path)
    need = {'schema', 'release_checkout', 'python', 'nexloop_root', 'stage_env'}
    keys = need | {'repo_path', 'app_host_public_key', 'trusted_owner', '_comment'}  # repo_path, app_host_public_key: D4 sftp, ignored
    if type(c) is not dict or c.get('schema') != SCHEMA or set(c) - keys or need - set(c):
        raise Refused(f'configuration: schema {SCHEMA} with ' + ', '.join(sorted(need - {'schema'})))
    for key in ('release_checkout', 'python', 'nexloop_root', 'stage_env'):
        if type(c[key]) is not str or not A.PATH.fullmatch(c[key]) or '..' in c[key]:
            raise Refused(f'configuration: {key} (absolute path)')
    return c


def app_host_addr(config):
    """APP_HOST_ADDR of the deployed stage.env: the address the pg_hba template renders and the only source sshd accepts."""
    try:
        lines = Path(config['stage_env']).read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        raise Refused('stage_env: unreadable') from None
    values = [line.partition('=')[2].strip() for line in lines if line.startswith('APP_HOST_ADDR=')]
    if len(values) != 1 or not ADDR.fullmatch(values[0]):
        raise Refused('stage_env: one APP_HOST_ADDR=<IPv4 address> line')
    return values[0]


def trusted_code(config, trust):
    A.trust_release(config, trust)
    trust(config['python'], what='python')
    checkout = Path(config['release_checkout'])
    for tree, what in ((HERE, 'installer'), (checkout / 'scripts/deploy', 'scripts/deploy'), (checkout / 'packages/eios-core/src', 'packages/eios-core/src')):
        trust(tree, what=what)
        for path in sorted(tree.rglob('*')):
            trust(path, what=what)


def rules(text):
    return [' '.join(line.split()) for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]


def hba_writer(directory, hba, trust, owner):
    """Who writes in config/: None - root, config/ root's alone (L6 I2) - or the configured trusted_owner (uid, gid, name) when config/
    and pg_hba.conf are that user's, neither group/other-writable nor a link, and every directory above root's or that user's."""
    try:
        trust(directory, what='pg_hba directory')
        return None
    except Refused as error:
        if owner is None:
            raise Refused(f'{error} (config/ of the deployment user: name that user as trusted_owner in the configuration)') from None
    # config/ is not root's alone: pg_hba.conf must be trusted_owner's, and config/ above it (root's or theirs - so theirs) too.
    trust(hba, what='pg_hba.conf of trusted_owner', own=(owner[0],))
    return owner


def as_user(writer, action):
    """action() as root (writer None), or in a child process with writer's uid and gid and no supplementary group; a failure of the
    child is raised here as Failed with its message."""
    if writer is None:
        return action()
    uid, gid, name = writer
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: never returns into the caller's code
        os.close(read)
        code, message = 0, ''
        try:
            if os.geteuid() == 0:
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)
            if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (uid, uid, gid, gid):
                raise Failed(f'not running as {name}')
            if uid != 0:
                try:
                    os.setuid(0)
                except PermissionError:
                    pass
                else:
                    raise Failed('root could be taken back')
            action()
        except BaseException as error:  # noqa: B036  (the child reports everything and exits)
            code, message = 1, str(error) if isinstance(error, (Failed, Refused)) else type(error).__name__
        os.write(write, message.encode()[:4000])
        os._exit(code)
    os.close(write)
    with os.fdopen(read, 'rb') as stream:
        message = stream.read().decode(errors='replace')
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise Failed(f'as {name}: {message or "exit status " + str(os.waitstatus_to_exitcode(status))}')
    return None


def read_seen(path, seen):
    """The text of path opened without following a link, only when it is the regular file lstat saw (inode, device, owner)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_ino, info.st_dev, info.st_uid) != (seen.st_ino, seen.st_dev, seen.st_uid):
            raise Failed('pg_hba: not the file checked (replaced meanwhile)')
        return stream.read()


def write_in_place(path, text):
    """The container bind-mounts the single file: same inode, owner and mode; refused when the inode changed. Opened without
    following a link (L6 I2), and the file opened is checked to be the one lstat saw."""
    before = os.lstat(path).st_ino
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r+') as stream:
        if os.fstat(fd).st_ino != before:
            raise Failed('pg_hba: the inode changed while opening')
        stream.seek(0)
        stream.write(text)
        stream.truncate()
    if os.lstat(path).st_ino != before:
        raise Failed('pg_hba: the inode changed while writing')


def container_hba(runner):
    return runner(['docker', 'compose', '-p', PROJECT, 'exec', '-T', 'postgres', 'cat', '/etc/postgresql/pg_hba.conf'], check=False)


def reload_postgres(runner, sleep):
    since = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    runner(['docker', 'compose', '-p', PROJECT, 'kill', '-s', 'HUP', 'postgres'], check=False)
    sleep(3)
    logs = runner(['docker', 'compose', '-p', PROJECT, 'logs', '--since', since, 'postgres'], check=False)
    return 'received SIGHUP' in logs and not re.search(r'pg_hba\.conf.*(not reloaded|invalid)|could not load pg_hba', logs, re.I)


def step_hba(root, config, address, runner, out, trust, sleep, owner=None, as_user=as_user):
    hba = Path(config['nexloop_root']) / 'config/pg_hba.conf'
    backup = hba.parent / 'pg_hba.conf.before-file-backup'
    checkout = Path(config['release_checkout'])
    # L6 I2: config/ root's alone, or the deployment user's with pg_hba.conf (then that user writes); neither file is a link.
    writer = hba_writer(hba.parent, hba, trust, owner)
    if os.path.islink(hba) or not hba.is_file():
        raise Refused(f'{hba}: a regular file')
    seen = os.lstat(hba)
    if os.path.islink(backup):
        raise Refused(f'{backup}: a symbolic link')
    scratch = under(root, SCRATCH)
    scratch.mkdir(parents=True, exist_ok=True)
    trust(scratch, what='scratch directory')  # L6 I1: no link on the way and root's, before the chmod and before root writes in it
    os.chmod(scratch, 0o700)
    work = Path(tempfile.mkdtemp(dir=scratch, prefix='hba-'))
    try:
        runner([config['python'], '-I', '-B', str(checkout / 'scripts/deploy/render.py'), '--env', config['stage_env'],
                '--template', str(checkout / 'deploy/stage/config/pg_hba.conf.template'), '--out', str(work / 'pg_hba.conf')])
        rendered = (work / 'pg_hba.conf').read_text()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    installed = read_seen(hba, seen)
    now, new = Counter(rules(installed)), Counter(rules(rendered))
    removed, added = list((now - new).elements()), list((new - now).elements())
    if removed:
        raise Failed(f'pg_hba: the rendered file removes {len(removed)} rule line(s); render with the deployed stage.env and this release')
    present = [line for line in rules(installed) if HBA_LINE.fullmatch(line)]
    if not added and present:
        if HBA_LINE.fullmatch(present[0]).group(1) != address:
            raise Failed('pg_hba: the recorder line names another address than APP_HOST_ADDR')
        print('step 1a: pg_hba recorder line already installed', file=out)
        return False
    if len(added) != 1 or not HBA_LINE.fullmatch(added[0]) or HBA_LINE.fullmatch(added[0]).group(1) != address:
        raise Failed(f'pg_hba: expected exactly one added rule, the recorder hostssl line from APP_HOST (found {len(added)})')
    def write(text, keep=False):
        if keep:  # atomically, 0600, the writer's: a link put there meanwhile is replaced, never followed
            A.write_private(backup, installed)
        write_in_place(hba, text)
    as_user(writer, lambda: write(rendered, keep=True))
    if container_hba(runner) != rendered:
        as_user(writer, lambda: write(installed))
        raise Failed(f'pg_hba: the container does not see the new file (its bind mount holds another inode); the previous content is '
                     f'written back - restart the postgres container (docker compose -p {PROJECT} restart postgres) and rerun')
    if not reload_postgres(runner, sleep):
        as_user(writer, lambda: write(installed))
        reload_postgres(runner, sleep)
        raise Failed('pg_hba: the reload was not clean; the previous content is written back in place and reloaded')
    print('step 1a: pg_hba recorder hostssl line installed and reloaded (backup config/pg_hba.conf.before-file-backup, written as '
          + ('root' if writer is None else writer[2]) + ')', file=out)
    return True


def unit_name(path):
    """systemd-escape --path --suffix=mount."""
    out = []
    for i, ch in enumerate(path.strip('/')):
        if ch == '/':
            out.append('-')
        elif ch.isascii() and (ch.isalnum() or ch in ':_' or (ch == '.' and i > 0)):
            out.append(ch)
        else:
            out.append('\\x%02x' % ord(ch))
    return ''.join(out) + '.mount'


def install(root, config, runner, out, trust, sleep, owner, as_user):
    trusted_code(config, trust)
    address = app_host_addr(config)
    step_hba(root, config, address, runner, out, trust, sleep, owner, as_user)
    print('done: DATA_HOST step 1a; next: rest_server_data_host.sh (step 1b, the files repository and rest-server; NX-035-file-backup.md §6.2)',
          file=out)


def uninstall(root, runner, out):
    """The D4 sftp parts an earlier version installed, wherever they exist; nothing else (idempotent: none of them is required)."""
    unit = under(root, '/etc/systemd/system') / unit_name(MOUNT)
    dropin = under(root, DROPIN)
    had_dropin = os.path.lexists(dropin)
    removed = []
    for path in (dropin, under(root, KEYS) / USER):
        if os.path.lexists(path):
            path.unlink()
            removed.append(path.name)
    if had_dropin:
        runner(['sshd', '-t'], check=False)
        runner(['systemctl', 'try-reload-or-restart', 'ssh.service'], check=False)
    if os.path.lexists(unit):
        runner(['systemctl', 'disable', '--now', unit.name], check=False)
        unit.unlink()
        runner(['systemctl', 'daemon-reload'], check=False)
        removed.append(unit.name)
    for directory in (under(root, MOUNT), under(root, CHROOT), under(root, KEYS)):
        if directory.is_dir() and not directory.is_symlink() and not os.listdir(directory):
            directory.rmdir()
            removed.append(str(directory.name))
    print('uninstalled: ' + (', '.join(removed) if removed else 'no D4 sftp part was there') + '; the pg_hba line stays', file=out)


def main(argv=None, out=sys.stdout, runner=run, root=Path('/'), trust=A.root_owned, sleep=time.sleep, account=pwd.getpwnam, as_user=as_user):
    p = argparse.ArgumentParser(description='NX-035 file backup, DATA_HOST step 1 (owner, root, one command)')
    p.add_argument('config')
    p.add_argument('--uninstall', action='store_true')
    a = p.parse_args(argv)
    os.umask(0o022)
    try:
        config = load_config(a.config)
        owner = A.trusted_owner(config, (USER,), account)
        trust = A.trusting(trust, owner)
        if a.uninstall:
            uninstall(root, runner, out)
        else:
            install(root, config, runner, out, trust, sleep, owner, as_user)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
