#!/usr/bin/env python3
"""NX-035 slice 3, step 1 on DATA_HOST (owner, root): what the APP_HOST file backup needs here (docs/implementation/NX-035-file-backup.md
§6.2). Run through file_backup_data_host.sh, one private configuration, one command:

    sudo bash file_backup_data_host.sh /path/file-backup.data-host.json
    sudo bash file_backup_data_host.sh /path/file-backup.data-host.json --uninstall

Owner decisions 2026-10-26: D2 - one hostssl pg_hba line, nexloop_backup_recorder from APP_HOST only, scram-sha-256 (its existing
password, not rotated: nothing is set here); D4 - repo1 is the restore journal's restic repository, written over a restricted,
chrooted sftp account: the login is the repository's own owner nexloop-backup (one repository, one uid), and for that user sshd
allows nothing but internal-sftp inside /srv/nexloop-sftp, where the repository is bind-mounted as /restic and nothing else is.

Private configuration (JSON, 0600 root; deploy/examples/file-backup-data-host.example.json): schema nexloop-file-backup-data-host/1,
release_checkout (render.py and the pg_hba template come from it), python (root runs render.py with it: a real file, root's),
nexloop_root (config/pg_hba.conf is under it), stage_env (the deployed private stage.env; APP_HOST_ADDR is read from it),
repo_path (the restore journal's restic repository: a directory with its config file, owned by nexloop-backup) and
app_host_public_key (the line `file_backup_install.sh --prepare` printed on APP_HOST).

Trust (root runs this code and render.py): stdlib only; the checkout, its .git, the trees root executes from and the interpreter are
root's alone, every component lstat'ed (file_backup_install.trust_release and root_owned); nothing secret is read or printed.

Steps (each idempotent; a rerun converges):
  1a pg_hba: rendered from the release's template with the deployed stage.env; the rendered file may differ from the installed one
     by this one hostssl line added and nothing removed. config/ is root's alone and neither file a link (L6 I2). Written in place
     (same inode: the container bind-mounts the single file; opened without following a link; the old content kept, atomically
     and 0600, as config/pg_hba.conf.before-file-backup), checked from inside the container, PostgreSQL HUP'ed and the
     reload read back from its log; otherwise the old content is written back in place and reloaded.
  1b sftp: /srv/nexloop-sftp (root, 0755, every component root's) with restic/ bind-mounted from repo_path by a mount unit; the
     authorized key /etc/ssh/nexloop-authorized-keys/nexloop-backup (root, 0644; `restrict,from="<APP_HOST_ADDR>"`); the sshd
     drop-in /etc/ssh/sshd_config.d/00-nexloop-file-backup.conf (Match User nexloop-backup: that key file only, public key only,
     ChrootDirectory, ForceCommand internal-sftp -u 0077, no tty and no forwarding of any kind). `sshd -t`, then the effective
     settings for that user from APP_HOST read back with `sshd -T -C` (an earlier setting elsewhere wins in sshd: then refused,
     the drop-in removed); then sshd reloaded.
--uninstall: the drop-in, the authorized key and the mount unit removed (sshd reloaded); the chroot directory removed when empty.
The pg_hba line stays (the template has it; without APP_HOST's DSN nobody uses it).
Output: step lines only. Exit 0 done, 1 a step failed, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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
CHROOT = '/srv/nexloop-sftp'
MOUNT = CHROOT + '/restic'
KEYS = '/etc/ssh/nexloop-authorized-keys'
DROPIN = '/etc/ssh/sshd_config.d/00-nexloop-file-backup.conf'
PROJECT = 'nexloop-data'
HBA_LINE = re.compile(r'hostssl [a-z_][a-z0-9_]* nexloop_backup_recorder (\d{1,3}(?:\.\d{1,3}){3})/32 scram-sha-256')
ADDR = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}')
PUBLIC_KEY = re.compile(r'ssh-ed25519 [A-Za-z0-9+/]{40,}={0,2}( [A-Za-z0-9@._-]{1,64})?')
SCRATCH = '/var/lib/nexloop/file-backup-data-host'
# What sshd must apply to nexloop-backup from APP_HOST (sshd -T -C), keyword -> value.
EFFECTIVE = {'chrootdirectory': CHROOT, 'forcecommand': 'internal-sftp -u 0077', 'authorizedkeysfile': KEYS + '/%u',
             'authenticationmethods': 'publickey', 'passwordauthentication': 'no', 'kbdinteractiveauthentication': 'no',
             'permittty': 'no', 'allowtcpforwarding': 'no', 'allowstreamlocalforwarding': 'no', 'allowagentforwarding': 'no',
             'x11forwarding': 'no', 'permittunnel': 'no', 'gatewayports': 'no', 'permituserrc': 'no'}


def load_config(path):
    c = A.read_private_json(path)
    keys = {'schema', 'release_checkout', 'python', 'nexloop_root', 'stage_env', 'repo_path', 'app_host_public_key', '_comment'}
    if type(c) is not dict or c.get('schema') != SCHEMA or set(c) - keys or set(keys) - set(c) - {'_comment'}:
        raise Refused(f'configuration: schema {SCHEMA} with ' + ', '.join(sorted(keys - {'schema', '_comment'})))
    for key in ('release_checkout', 'python', 'nexloop_root', 'stage_env', 'repo_path'):
        if type(c[key]) is not str or not A.PATH.fullmatch(c[key]) or '..' in c[key]:
            raise Refused(f'configuration: {key} (absolute path)')
    if type(c['app_host_public_key']) is not str or not PUBLIC_KEY.fullmatch(c['app_host_public_key'].strip()):
        raise Refused('configuration: app_host_public_key (the one ssh-ed25519 line file_backup_install.sh --prepare printed)')
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


def step_hba(root, config, address, runner, out, trust, sleep):
    hba = Path(config['nexloop_root']) / 'config/pg_hba.conf'
    backup = hba.parent / 'pg_hba.conf.before-file-backup'
    checkout = Path(config['release_checkout'])
    # L6 I2: the directory root writes in (the backup) and rewrites a file of is root's alone; neither file is a link.
    trust(hba.parent, what='pg_hba directory')
    if os.path.islink(hba) or not hba.is_file():
        raise Refused(f'{hba}: a regular file')
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
    installed = hba.read_text()
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
    A.write_private(backup, installed)  # atomically, 0600 root: a link put there meanwhile is replaced, never followed
    write_in_place(hba, rendered)
    if container_hba(runner) != rendered:
        write_in_place(hba, installed)
        raise Failed(f'pg_hba: the container does not see the new file (its bind mount holds another inode); the previous content is '
                     f'written back - restart the postgres container (docker compose -p {PROJECT} restart postgres) and rerun')
    if not reload_postgres(runner, sleep):
        write_in_place(hba, installed)
        reload_postgres(runner, sleep)
        raise Failed('pg_hba: the reload was not clean; the previous content is written back in place and reloaded')
    print('step 1a: pg_hba recorder hostssl line installed and reloaded (backup config/pg_hba.conf.before-file-backup)', file=out)
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


def mount_unit(repo):
    return (f'# NX-035 D4 (file_backup_data_host.py; do not edit): the restore journal\'s restic repository inside the chroot of the\n'
            f'# restricted sftp account, as {MOUNT}; nothing else of the backup volume is there.\n'
            f'[Unit]\nDescription=NexLoop file backup repo1 for the restricted sftp account\nRequiresMountsFor={repo}\n\n'
            f'[Mount]\nWhat={repo}\nWhere={MOUNT}\nType=none\nOptions=bind,nosuid,nodev,noexec\n\n[Install]\nWantedBy=multi-user.target\n')


def sshd_dropin():
    lines = [f'# NX-035 D4 (file_backup_data_host.py; do not edit): {USER} logs in only for internal-sftp inside {CHROOT}, with the',
             '# one APP_HOST key (its from= option names the address); first in sshd_config.d, so these values are obtained first.',
             f'Match User {USER}', f'    AuthorizedKeysFile {KEYS}/%u', '    AuthenticationMethods publickey', '    PubkeyAuthentication yes',
             '    PasswordAuthentication no', '    KbdInteractiveAuthentication no', f'    ChrootDirectory {CHROOT}',
             '    ForceCommand internal-sftp -u 0077', '    PermitTTY no', '    AllowTcpForwarding no', '    AllowStreamLocalForwarding no',
             '    AllowAgentForwarding no', '    X11Forwarding no', '    PermitTunnel no', '    GatewayPorts no', '    PermitUserRC no', 'Match all']
    return '\n'.join(lines) + '\n'


def effective(runner, address):
    text = runner(['sshd', '-T', '-C', f'user={USER},host={address},addr={address}'])
    values = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(' ')
        values.setdefault(key.lower(), []).append(value.strip())
    return values


def check_effective(values, groups=()):
    problems = [f'{k} is {" ".join(values.get(k, ["unset"]))!r}' for k, v in EFFECTIVE.items() if values.get(k) != [v]]
    allowed = [u for v in values.get('allowusers', []) for u in v.split()]
    if allowed and not any(u.split('@')[0] in (USER, '*') for u in allowed):
        problems.append(f'AllowUsers does not name {USER}')
    if any(u.split('@')[0] in (USER, '*') for v in values.get('denyusers', []) for u in v.split()):
        problems.append(f'DenyUsers names {USER}')
    # L6 S4: the groups too (the user's groups from `id -nG`).
    allowed = [g for v in values.get('allowgroups', []) for g in v.split()]
    if allowed and not any(g in (*groups, '*') for g in allowed):
        problems.append(f'AllowGroups names none of {USER}\'s groups')
    if any(g in (*groups, '*') for v in values.get('denygroups', []) for g in v.split()):
        problems.append(f'DenyGroups names a group of {USER}')
    return problems


def restore(path, before):
    if before is None:
        if os.path.lexists(path):
            path.unlink()
    else:
        A.write_private(path, before, mode=0o644)


def step_sftp(root, config, address, runner, out, trust):
    uid = runner(['id', '-u', USER], check=False).strip()
    if not uid.isdigit():
        raise Refused(f'user {USER} missing (the restore journal backup is not installed here)')
    repo = Path(config['repo_path'])
    if os.path.islink(repo) or not repo.is_dir() or not (repo / 'config').is_file() or os.stat(repo).st_uid != int(uid):
        raise Refused(f'repo_path: a directory with a restic config file, owned by {USER}')
    chroot, mount = under(root, CHROOT), under(root, MOUNT)
    chroot.mkdir(exist_ok=True)
    trust(chroot, what='chroot')  # sshd's own rule too: every component root's and not group/other-writable
    os.chmod(chroot, 0o755)
    if os.path.islink(mount):
        raise Refused(f'{MOUNT} is a symbolic link')
    mount.mkdir(exist_ok=True)
    unit = under(root, '/etc/systemd/system') / unit_name(MOUNT)
    A.write_private(unit, mount_unit(config['repo_path']), mode=0o644)
    runner(['systemctl', 'daemon-reload'])
    runner(['systemctl', 'enable', '--now', unit.name])
    if not (mount / 'config').is_file():
        raise Failed(f'{MOUNT}: the bind mount does not show the repository (systemctl status {unit.name})')
    keys = under(root, KEYS)
    keys.mkdir(parents=True, exist_ok=True)
    trust(keys, what='authorized keys')
    os.chmod(keys, 0o755)
    keyfile, dropin = keys / USER, under(root, DROPIN)
    before = {p: (p.read_text() if p.is_file() and not p.is_symlink() else None) for p in (keyfile, dropin)}
    changed = A.write_private(keyfile, f'restrict,from="{address}" {config["app_host_public_key"].strip()}\n', mode=0o644)
    changed = A.write_private(dropin, sshd_dropin(), mode=0o644) or changed
    try:
        runner(['sshd', '-t'])
        problems = check_effective(effective(runner, address), runner(['id', '-nG', USER], check=False).split())
        if problems:
            raise Failed('sshd: for nexloop-backup from APP_HOST ' + '; '.join(problems)[:500] + ' (an earlier setting wins in sshd)')
    except (Failed, OSError):
        for path, text in before.items():
            restore(path, text)
        raise
    if changed:
        runner(['systemctl', 'try-reload-or-restart', 'ssh.service'], check=False)
    print(f'step 1b: sftp chroot {CHROOT} with restic/ bound from repo_path ({unit.name}), the APP_HOST key, the sshd drop-in '
          + ('installed, sshd reloaded' if changed else 'unchanged'), file=out)


def install(root, config, runner, out, trust, sleep):
    trusted_code(config, trust)
    address = app_host_addr(config)
    step_hba(root, config, address, runner, out, trust, sleep)
    step_sftp(root, config, address, runner, out, trust)
    print('done: DATA_HOST step 1; next on APP_HOST: file_backup_install.sh (NX-035-file-backup.md §6.1)', file=out)


def uninstall(root, runner, out):
    unit = under(root, '/etc/systemd/system') / unit_name(MOUNT)
    for path in (under(root, DROPIN), under(root, KEYS) / USER):
        if os.path.lexists(path):
            path.unlink()
    runner(['sshd', '-t'], check=False)
    runner(['systemctl', 'try-reload-or-restart', 'ssh.service'], check=False)
    runner(['systemctl', 'disable', '--now', unit.name], check=False)
    if os.path.lexists(unit):
        unit.unlink()
    runner(['systemctl', 'daemon-reload'], check=False)
    for directory in (under(root, MOUNT), under(root, CHROOT)):
        if directory.is_dir() and not directory.is_symlink() and not os.listdir(directory):
            directory.rmdir()
    print('uninstalled: the sshd drop-in, the authorized key and the mount unit removed; the pg_hba line stays', file=out)


def main(argv=None, out=sys.stdout, runner=run, root=Path('/'), trust=A.root_owned, sleep=time.sleep):
    p = argparse.ArgumentParser(description='NX-035 file backup, DATA_HOST step 1 (owner, root, one command)')
    p.add_argument('config')
    p.add_argument('--uninstall', action='store_true')
    a = p.parse_args(argv)
    os.umask(0o022)
    try:
        config = load_config(a.config)
        if a.uninstall:
            uninstall(root, runner, out)
        else:
            install(root, config, runner, out, trust, sleep)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
