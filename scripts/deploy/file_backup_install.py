#!/usr/bin/env python3
"""NX-035 slice 3: install the file backup on APP_HOST, one command (steps 2-6; docs/implementation/NX-035-file-backup.md §6).
Step 1 runs on DATA_HOST first (scripts/deploy/file_backup_data_host.sh: the recorder's hostssl pg_hba line and the restricted
sftp account of repo1). Run as root through file_backup_install.sh:

    sudo bash file_backup_install.sh /path/file-backup.json --prepare        the user, its SSH key (printed), secrets/backup, the env file
    printf 'recorder\\t%s\\nrepo1\\t%s\\n' "$RECORDER_PW" "$REPO1_PW" | sudo bash file_backup_install.sh /path/file-backup.json
    sudo bash file_backup_install.sh /path/file-backup.json                  a rerun: the secret files already written are kept
    sudo bash file_backup_install.sh /path/file-backup.json --uninstall

Owner decisions 2026-10-26 (D1-D6): enabled now; the recorder connects from APP_HOST over hostssl (D2); the unit's only privilege is
CAP_DAC_READ_SEARCH and its sandbox shows nothing of /srv/nexloop but the two data directories and secrets/backup (D3); repo1 is the
restore journal's restic repository on DATA_HOST, reached over a chrooted sftp account (D4); restic is the official binary of
versions.lock.json, its archive's sha256 checked (D5).

Private configuration (JSON, 0600 root; deploy/examples/file-backup-install.app-host.example.json): schema
nexloop-file-backup-install/1, release_checkout + release_sha, python (the unit's interpreter: the host-health venv's bin/python,
which has psycopg; a link chain is fine), memory_deny_write_execute (default true), restic_archive (the downloaded
restic_<version>_linux_amd64.bz2), key_ref (the label recorded with each manifest row), database {host, port, dbname, sslrootcert}
(DATA_HOST's PG over hostssl, verify-full; the CA is copied next to the DSN), repo1 {host, port, host_key, path} (D4: DATA_HOST's sshd,
its pinned host key, the repository's path inside the chroot) or {transport "rest", host, port, cacert} (owner 2026-10-27: DATA_HOST's
rest-server --append-only, the stage CA bundle pinned with --cacert; stdin then also repo1_rest<TAB>password; step 5 proves a
snapshot can be written but not forgotten, NX-035-file-backup.md §6.5), repo2_init (default false: true lets step 5 run `restic init` on a
repo2 that does not exist yet), host_health_config and host_health_install_config (both host-health files).

Order: --prepare (prints the public key DATA_HOST step 1 authorizes), DATA_HOST step 1, `install_repo2_secrets.sh files` (it reads
the repo2 file paths from the env file --prepare wrote), then the install with the passwords on stdin.
stdin (optional on a rerun): `recorder<TAB>password` (nexloop_backup_recorder's existing password) and `repo1<TAB>password` (the
restore journal's repo1 restic password: one repository, one password). A file already written is kept when its line is absent.

Trust (root runs this code; the L6 reviews of the host-health and relation-gaps installers): stdlib only. The release checkout and
its .git (every directory, every file outside objects/) are root's alone, every path component checked with lstat; .git is a
directory without objects/info/alternates or commondir; `git show` runs with --no-replace-objects in a minimal environment. The
interpreter is resolved hop by hop. A reused release is checked file by file against the commit. Nothing secret is printed,
passed as an argument or logged: the passwords arrive on stdin and leave only into 0600 files.

Steps (each idempotent):
  2 the system user nexloop-file-backup (home /var/lib/nexloop-file-backup, root's; no shell), its SSH key (generated once, kept),
    the release /opt/nexloop/file-backup/<sha>/ (the executor's modules, this sandbox check, the units; read-only; `current` -> <sha>)
    and restic at /opt/nexloop/file-backup/restic-<version>/restic from the archive whose sha256 versions.lock.json names
  3 /srv/nexloop/secrets/backup (root, 0711; refused when it holds anything this unit should not see): recorder.dsn, the repo1
    repository and password files (0600, the service user), pg-ca.crt; ~/.ssh/config and known_hosts (root's; the host key pinned)
  4 /etc/nexloop/file-backup.env (0600 root), the units and timers from the release (tokens filled): the file backup and repo2's
    daily maintenance (nexloop-file-backup-maint: forget per tag with --prune, check; repo1 is DATA_HOST's); daemon-reload
  5 inside the unit's own sandbox (systemd-run with its [Service] settings): the data roots and its own files readable, /srv/nexloop
    showing data and secrets only, secrets only backup, every InaccessiblePaths= entry and every other secret hidden, both handle
    system calls refused with EPERM (L6 F1: the capability would otherwise open any inode past those mounts), restic the
    pinned version, both repositories open (repo2 initialised when repo2_init); repo2's maintenance once with --dry-run in its own
    unit's sandbox (ok, nothing removed); then one real run of the service, all four ExecStart lines verified. Any of these
    failing disables both timers before the run stops (L6 S2: a rerun leaves nothing of an earlier install running on it)
  6 file-backup, file-backup-maint (and provider-drift, when its timer is installed here) in both host-health files (both or
    neither), then `systemctl enable --now nexloop-file-backup.timer nexloop-file-backup-maint.timer`
--uninstall: both timers, units, drop-ins, env file, release, restic, staging and the secret files removed, the file-backup and
file-backup-maint entries taken out of both host-health files; the user, its home and SSH key stay (DATA_HOST's authorized key keeps matching).
Output: step lines, the check's verdicts and the run's counts; never a password, DSN or key. Exit 0 done, 1 a step failed, 2 refused.
"""
from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = 'nexloop-file-backup-install/1'
USER = 'nexloop-file-backup'
HOME = '/var/lib/nexloop-file-backup'
STAGING = HOME + '/staging'
SECRETS = '/srv/nexloop/secrets/backup'
ENV_FILE = '/etc/nexloop/file-backup.env'
BASE = '/opt/nexloop/file-backup'
SERVICE, TIMER = 'nexloop-file-backup.service', 'nexloop-file-backup.timer'
MAINT_SERVICE, MAINT_TIMER = 'nexloop-file-backup-maint.service', 'nexloop-file-backup-maint.timer'  # repo2's daily maintenance
RECORDER = 'nexloop_backup_recorder'
REPO1_USER = 'nexloop-backup'  # DATA_HOST's owner of the restore journal's repo1 (one repository, one uid)
SSH_ALIAS = 'nexloop-repo1'
DATA_ROOTS = ('/srv/nexloop/data/artifacts', '/srv/nexloop/data/runtime')
# secrets/backup holds these and nothing else (D3: the sandbox shows the whole directory); install_repo2_secrets.sh writes the repo2
# three and keeps <file>.pre-repo2 copies for its rollback.
OWN = {'recorder.dsn': 'user', 'pg-ca.crt': 'root', 'restic-files-repo1.repository': 'user', 'restic-files-repo1.pass': 'user',
       'rest-ca.crt': 'root'}  # rest-ca.crt: repo1 over DATA_HOST's rest-server (transport "rest"), the stage CA bundle restic pins
REST_CA = 'rest-ca.crt'
REST_USER = 'nexloop-file-backup'  # the rest-server's one htpasswd user
REST_PASSWORD = re.compile(r'[A-Za-z0-9_-]{24,128}')  # it sits in the repository URL: no character that needs escaping
PROBE_TAG = 'nexloop-append-only-probe'
REPO2 = ('restic-files-repo2.repository', 'restic-files-repo2.pass', 'offsite.env')
ALLOWED = set(OWN) | set(REPO2) | {n + '.pre-repo2' for n in REPO2}
SHA = re.compile(r'[0-9a-f]{40}')
PATH = re.compile(r'/[A-Za-z0-9._/@-]+')
HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}')
HOST_KEY = re.compile(r'(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ssh-rsa) [A-Za-z0-9+/]{40,}={0,2}')
LABEL = re.compile(r'[A-Za-z0-9._:-]{1,64}')
FILES = ('scripts/deploy/file_backup_run.py', 'scripts/deploy/file_backup_sandbox_check.py', 'scripts/deploy/file_backup_maint.py',
         'packages/eios-core/src/nexloop_eios/__init__.py', 'packages/eios-core/src/nexloop_eios/backup.py',
         'packages/eios-core/src/nexloop_eios/file_backup.py', 'packages/eios-core/src/nexloop_eios/private_configuration.py')
UNITS = {SERVICE: 'deploy/stage/systemd/nexloop-file-backup.service.example', TIMER: 'deploy/stage/systemd/nexloop-file-backup.timer.example',
         MAINT_SERVICE: 'deploy/stage/systemd/nexloop-file-backup-maint.service.example',
         MAINT_TIMER: 'deploy/stage/systemd/nexloop-file-backup-maint.timer.example'}
ENTRIES = {'file-backup': {'name': 'file-backup', 'timer': TIMER, 'service': SERVICE},
           'file-backup-maint': {'name': 'file-backup-maint', 'timer': MAINT_TIMER, 'service': MAINT_SERVICE},
           'provider-drift': {'name': 'provider-drift', 'timer': 'nexloop-provider-drift.timer', 'service': 'nexloop-provider-drift.service'}}
RUNS = ('artifact/repo1', 'artifact/repo2', 'runtime/repo1', 'runtime/repo2')  # the unit's ExecStart order
SANDBOX_SKIP = {'Type', 'ExecStart', 'ExecStartPre', 'ExecStartPost', 'TimeoutStartSec'}
OPTIONS = re.compile(r'FILE_BACKUP_REPO2_OPTIONS=--s3-bucket-lookup (auto|dns|path)')


class Refused(ValueError):
    pass


class Failed(RuntimeError):
    pass


def run(command, *, stdin=None, check=True):
    done = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise Failed(f'{Path(command[0]).name} exited {done.returncode}')
    return done.stdout


def _owned(info, owners):
    return info.st_uid in owners and not info.st_mode & 0o022


def root_owned(path, *, what, owners=(0,)):
    """Root's alone: owner root, nothing group/other-writable, no symbolic link on the way (lstat, never followed)."""
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        try:
            info = os.lstat(part)
        except OSError:
            raise Refused(f'{what}: {part} unreadable') from None
        if stat.S_ISLNK(info.st_mode):
            raise Refused(f'{what}: {part} is a symbolic link (give the real path)')
        if not _owned(info, owners):
            raise Refused(f'{what}: {part} must be owned by root and not group/other-writable')


def interpreter_owned(path, *, what, owners=(0,)):
    """The unit's interpreter, run by the service user and never by root, may be a link chain (the host-health venv's bin/python ->
    python3 -> /usr/bin/python3.12, current -> <sha>): resolved hop by hop, every directory entered and every link root's and not
    group/other-writable, the real file a regular file of root's."""
    parts = list(Path(os.path.abspath(path)).parts[1:])
    current, hops = Path('/'), 0
    if not _owned(os.lstat(current), owners):
        raise Refused(f'{what}: / must be owned by root and not group/other-writable')
    while parts:
        name = parts.pop(0)
        if name in ('', '.'):
            continue
        if name == '..':
            current = current.parent
            continue
        entry = current / name
        try:
            info = os.lstat(entry)
        except OSError:
            raise Refused(f'{what}: {entry} unreadable') from None
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            if hops > 40 or info.st_uid not in owners:
                raise Refused(f'{what}: {entry} (a link) must be owned by root')
            target = Path(os.readlink(entry))
            if target.is_absolute():
                current = Path('/')
                parts = list(target.parts[1:]) + parts
            else:
                parts = list(target.parts) + parts
            continue
        if not _owned(info, owners):
            raise Refused(f'{what}: {entry} must be owned by root and not group/other-writable')
        if parts:
            if not stat.S_ISDIR(info.st_mode):
                raise Refused(f'{what}: {entry} is not a directory')
            current = entry
        elif not stat.S_ISREG(info.st_mode):
            raise Refused(f'{what}: {entry} is not a regular file')


def trust_release(config, trust):
    """The checkout and its .git: every directory, and every file outside objects/ (refs, config, hooks, packed-refs)."""
    checkout = Path(config['release_checkout'])
    trust(checkout, what='release_checkout')
    git = checkout / '.git'
    if os.path.islink(git) or not os.path.isdir(git):
        raise Refused('release .git: a directory (a worktree pointer file or a link is refused)')
    if os.path.lexists(git / 'objects/info/alternates') or os.path.lexists(git / 'commondir'):
        raise Refused('release .git: objects from elsewhere (objects/info/alternates, commondir) are refused')
    trust(git, what='release .git')
    for path in sorted(git.rglob('*')):
        if path.is_dir() or not str(path.relative_to(git)).startswith('objects/'):
            trust(path, what='release .git')


def read_private_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused(f'{path}: unreadable') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused(f'{path}: a regular 0600 file of the caller')
        try:
            return json.loads(stream.read(1 << 16))
        except ValueError:
            raise Refused(f'{path}: not JSON') from None


def write_private(path, text, mode=0o600):
    """Atomically (a temporary file in the same directory, then rename); returns whether the content changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_symlink() and path.read_text() == text and stat.S_IMODE(path.stat().st_mode) == mode:
        return False
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
    return True


def load_config(path):
    c = read_private_json(path)
    keys = {'schema', 'release_checkout', 'release_sha', 'python', 'memory_deny_write_execute', 'restic_archive', 'key_ref', 'database',
            'repo1', 'repo2_init', 'host_health_config', 'host_health_install_config', '_comment'}
    if type(c) is not dict or c.get('schema') != SCHEMA or set(c) - keys:
        raise Refused(f'configuration: schema {SCHEMA} with ' + ', '.join(sorted(keys - {'schema', '_comment'})))
    for key in ('release_checkout', 'python', 'restic_archive', 'host_health_config', 'host_health_install_config'):
        if type(c.get(key)) is not str or not PATH.fullmatch(c[key]) or '..' in c[key]:
            raise Refused(f'configuration: {key} (absolute path)')
    if type(c.get('release_sha')) is not str or not SHA.fullmatch(c['release_sha']):
        raise Refused('configuration: release_sha (40 hex)')
    if type(c.get('key_ref')) is not str or not LABEL.fullmatch(c['key_ref']):
        raise Refused('configuration: key_ref (a label, 1-64 of A-Z a-z 0-9 . _ : -)')
    for key in ('memory_deny_write_execute', 'repo2_init'):
        if type(c.get(key, key == 'memory_deny_write_execute')) is not bool:
            raise Refused(f'configuration: {key} true | false')
    db = c.get('database')
    if (type(db) is not dict or set(db) != {'host', 'port', 'dbname', 'sslrootcert'} or type(db['host']) is not str or not HOST.fullmatch(db['host'])
            or type(db['port']) is not int or not 0 < db['port'] < 65536 or type(db['dbname']) is not str
            or not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', db['dbname']) or type(db['sslrootcert']) is not str or not PATH.fullmatch(db['sslrootcert'])):
        raise Refused('configuration: database {host, port, dbname, sslrootcert} (DATA_HOST over hostssl, verify-full)')
    repo1 = c.get('repo1')
    if type(repo1) is not dict or repo1.get('transport', 'sftp') not in ('sftp', 'rest'):
        raise Refused('configuration: repo1.transport sftp (D4, the default) | rest (DATA_HOST\'s rest-server, owner 2026-10-27)')
    if repo1.get('transport', 'sftp') == 'sftp':
        if (set(repo1) - {'transport'} != {'host', 'port', 'host_key', 'path'} or type(repo1['host']) is not str or not HOST.fullmatch(repo1['host'])
                or type(repo1['port']) is not int or not 0 < repo1['port'] < 65536 or type(repo1['host_key']) is not str
                or not HOST_KEY.fullmatch(repo1['host_key']) or type(repo1['path']) is not str or not PATH.fullmatch(repo1['path']) or '..' in repo1['path']):
            raise Refused('configuration: repo1 {host, port, host_key ("<type> <base64>"), path (inside the chroot)}')
    elif (set(repo1) != {'transport', 'host', 'port', 'cacert'} or type(repo1['host']) is not str or not HOST.fullmatch(repo1['host'])
            or type(repo1['port']) is not int or not 1024 <= repo1['port'] < 65536 or type(repo1['cacert']) is not str
            or not PATH.fullmatch(repo1['cacert']) or '..' in repo1['cacert']):
        raise Refused('configuration: repo1 {transport "rest", host (DATA_HOST\'s address, as its certificate names it), port, cacert (the stage CA bundle)}')
    return c


def rest(config):
    return config['repo1'].get('transport', 'sftp') == 'rest'


def read_secrets(stream):
    out = {}
    text = '' if stream is None else stream.read(1 << 14)
    for line in text.splitlines():
        if not line.strip():
            continue
        name, sep, value = line.partition('\t')
        if not sep or name not in ('recorder', 'repo1', 'repo1_rest') or name in out or not 16 <= len(value) <= 1024 or not value.isprintable() \
                or any(c.isspace() for c in value) or (name == 'repo1_rest' and not REST_PASSWORD.fullmatch(value)):
            raise Refused('stdin: recorder<TAB>password, repo1<TAB>password (16-1024 printable characters, no space) and, for repo1 over the '
                          'rest-server, repo1_rest<TAB>password (24-128 of A-Z a-z 0-9 _ -) lines')
        out[name] = value
    return out


GIT = ['env', '-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent', 'GIT_CONFIG_NOSYSTEM=1', 'GIT_CONFIG_GLOBAL=/dev/null', 'git', '--no-replace-objects']


def git_show(config, path, runner):
    # A minimal environment: no GIT_* variable of root's session, no system or global configuration.
    return runner([*GIT, '-C', config['release_checkout'], 'show', f'{config["release_sha"]}:{path}'])


def under(root, path):
    return root / path.lstrip('/')


def restic_lock(config, runner):
    """deployment_tools.restic of versions.lock.json at the release commit: version, archive name and its sha256."""
    try:
        lock = json.loads(git_show(config, 'versions.lock.json', runner))['deployment_tools']['restic']
        version, archive, digest = lock['version'], lock['archive'], lock['archive_sha256']
    except (ValueError, KeyError, TypeError):
        raise Failed('versions.lock.json at the release commit has no deployment_tools.restic') from None
    if not (type(version) is str and re.fullmatch(r'\d+\.\d+\.\d+', version) and archive == f'restic_{version}_linux_amd64.bz2'
            and type(digest) is str and re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise Failed('versions.lock.json: deployment_tools.restic malformed')
    return version, archive, digest


def restic_path(version):
    return f'{BASE}/restic-{version}/restic'


def restic_binary(config, runner):
    """D5: the official binary, from the archive whose sha256 the lock names (checked here, on the host, before anything is
    unpacked or written)."""
    version, archive, digest = restic_lock(config, runner)
    if Path(config['restic_archive']).name != archive:
        raise Refused(f'restic_archive: the lock names {archive}')
    try:
        fd = os.open(config['restic_archive'], os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused('restic_archive: unreadable (a regular file, not a link)') from None
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Refused('restic_archive: a regular file')
        data = stream.read(1 << 27)
    if hashlib.sha256(data).hexdigest() != digest:
        raise Refused(f'restic_archive: sha256 differs from versions.lock.json ({archive})')
    try:
        binary = bz2.decompress(data)
    except (OSError, ValueError, EOFError):
        raise Refused('restic_archive: not a bzip2 archive') from None
    return version, binary


def install_restic(root, version, binary, trust):
    """root's, 0555; an installed copy is kept only when it is byte for byte the archive's content."""
    target = under(root, restic_path(version))
    target.parent.mkdir(parents=True, exist_ok=True)
    trust(target.parent, what='restic directory')
    if target.exists() and not target.is_symlink() and target.read_bytes() == binary:
        trust(target, what='restic')
        return False
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.restic')
    with os.fdopen(fd, 'wb') as stream:
        stream.write(binary)
    os.chmod(tmp, 0o555)
    os.replace(tmp, target)
    os.chmod(target.parent, 0o555)
    return True


def release(root, config, runner, trust):
    base = under(root, BASE)
    target = base / config['release_sha']
    base.mkdir(parents=True, exist_ok=True)
    trust(base, what='release base')
    wanted = {**{f: git_show(config, f, runner) for f in FILES}, **{'units/' + u: git_show(config, s, runner) for u, s in UNITS.items()}}
    if (target / '.complete').exists():
        for name, text in wanted.items():  # a reused release, file by file against the commit (and root's alone)
            trust(target / name, what='release')
            if (target / name).read_text() != text:
                raise Failed(f'release {config["release_sha"][:12]}: {name} differs from the commit (remove {target} and rerun)')
        reused = True
    else:
        if target.exists():
            for path in [target, *target.rglob('*')]:
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            shutil.rmtree(target)
        work = Path(tempfile.mkdtemp(dir=base, prefix='.build-'))
        for name, text in wanted.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(text)
        (work / '.complete').write_text(config['release_sha'] + '\n')
        for path in sorted(work.rglob('*'), reverse=True):
            os.chmod(path, 0o555 if path.is_dir() else 0o444)
        os.chmod(work, 0o555)
        os.replace(work, target)
        reused = False
    current = base / 'current'
    if not (current.is_symlink() and os.readlink(current) == config['release_sha']):
        tmp = base / '.current'
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(config['release_sha'], tmp)
        os.replace(tmp, current)
    return target, reused


def ensure_user(root, runner, trust):
    if runner(['id', '-u', USER], check=False) == '':
        runner(['useradd', '--system', '--no-create-home', '--home-dir', HOME, '--shell', '/usr/sbin/nologin', USER])
    home = under(root, HOME)
    for directory, mode in ((home, 0o755), (home / '.ssh', 0o755)):
        directory.mkdir(parents=True, exist_ok=True)
        trust(directory, what='home')  # no link on the way, root's, before any chmod
        os.chmod(directory, mode)
    staging = home / 'staging'
    if os.path.islink(staging):
        raise Refused(f'{staging} is a symbolic link')
    staging.mkdir(exist_ok=True)
    os.chmod(staging, 0o700)
    runner(['chown', f'{USER}:{USER}', str(staging)])


def ensure_key(root, runner):
    """The service user's SSH key, generated once (never overwritten: DATA_HOST's authorized key names it); the public half root's."""
    ssh = under(root, HOME) / '.ssh'
    key, public = ssh / 'id_ed25519', ssh / 'id_ed25519.pub'
    created = False
    if not os.path.lexists(key):
        work = Path(tempfile.mkdtemp(dir=ssh, prefix='.keygen-'))
        try:
            runner(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', USER, '-f', str(work / 'id_ed25519')])
            os.chmod(work / 'id_ed25519', 0o600)
            os.chmod(work / 'id_ed25519.pub', 0o644)
            runner(['chown', f'{USER}:{USER}', str(work / 'id_ed25519')])
            os.link(work / 'id_ed25519', key)  # fails when a key appeared meanwhile: an existing key is never replaced
            os.replace(work / 'id_ed25519.pub', public)
            created = True
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if os.path.islink(key) or os.path.islink(public) or not public.exists():
        raise Refused(f'{ssh}: the key files must be regular files (id_ed25519 and id_ed25519.pub)')
    os.chmod(key, 0o600)  # the user's alone (ssh refuses a key others can read), on every run
    runner(['chown', f'{USER}:{USER}', str(key)])
    line = public.read_text().strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}( \S+)?', line):
        raise Refused(f'{public}: not one ssh-ed25519 public key line')
    return line, created


def _quote(value):
    return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"


def recorder_dsn(config, password):
    db = config['database']
    parts = {'host': db['host'], 'port': db['port'], 'dbname': db['dbname'], 'user': RECORDER, 'password': password,
             'sslmode': 'verify-full', 'sslrootcert': SECRETS + '/pg-ca.crt', 'connect_timeout': 10}
    return ' '.join(f'{k}={_quote(v)}' for k, v in parts.items()) + '\n'


def foreign_secrets(root):
    directory = under(root, SECRETS)
    if not under(root, '/srv/nexloop/secrets').is_dir() or os.path.islink(under(root, '/srv/nexloop/secrets')):
        raise Refused('/srv/nexloop/secrets missing (the encrypted volume is not mounted)')
    if os.path.islink(directory):
        raise Refused(f'{SECRETS} is a symbolic link')
    if not directory.is_dir():
        return
    other = sorted(n for n in os.listdir(directory) if n not in ALLOWED)
    if other:
        # D3: the unit sees the whole directory; nothing but its own files may be in it.
        raise Refused(f'{SECRETS} holds {len(other)} other entr' + ('y' if len(other) == 1 else 'ies') + ' (' + ', '.join(other[:5]) +
                      '): the file backup unit would see them; move them away')


def secrets_directory(root, trust):
    foreign_secrets(root)
    directory = under(root, SECRETS)
    directory.mkdir(exist_ok=True)
    trust(directory, what='secrets/backup')
    os.chmod(directory, 0o711)
    return directory


def write_secrets(root, config, secrets, runner, trust):
    directory = secrets_directory(root, trust)
    done = {}
    contents = {'recorder.dsn': recorder_dsn(config, secrets['recorder']) if 'recorder' in secrets else None,
                'restic-files-repo1.repository': repo1_location(directory, config, secrets.get('repo1_rest')),
                'restic-files-repo1.pass': secrets['repo1'] + '\n' if 'repo1' in secrets else None}
    if rest(config):
        try:
            bundle = Path(config['repo1']['cacert']).read_text()
        except (OSError, UnicodeDecodeError):
            raise Refused('repo1.cacert: unreadable') from None
        if '-----BEGIN CERTIFICATE-----' not in bundle or 'PRIVATE KEY' in bundle:
            raise Refused('repo1.cacert: the stage CA bundle, PEM certificates (and no key)')
        contents[REST_CA] = bundle
    try:
        ca = Path(config['database']['sslrootcert']).read_text()
    except (OSError, UnicodeDecodeError):
        raise Refused('database.sslrootcert: unreadable') from None
    if '-----BEGIN CERTIFICATE-----' not in ca or 'PRIVATE KEY' in ca:
        raise Refused('database.sslrootcert: a PEM certificate (and no key)')
    contents['pg-ca.crt'] = ca
    for name, text in contents.items():
        path = directory / name
        if text is None:
            if not path.exists() or path.is_symlink():
                line = {'recorder.dsn': 'recorder', 'restic-files-repo1.pass': 'repo1'}.get(name, 'repo1_rest')
                raise Refused(f'stdin: {name} is not written yet - give its line ({line}<TAB>password)')
            done[name] = 'kept'
        else:
            done[name] = 'written' if write_private(path, text, mode=0o600 if OWN[name] == 'user' else 0o644) else 'unchanged'
        if OWN[name] == 'user':
            runner(['chown', f'{USER}:{USER}', str(path)])
    return done


def repo1_location(directory, config, rest_password):
    """repo1's repository file: the D4 sftp alias, or DATA_HOST's rest-server (the one htpasswd user's password in the URL; the
    file is 0600 and only its path reaches restic). None keeps a rest location already written for this host and port."""
    repo1 = config['repo1']
    if not rest(config):
        return f'sftp:{SSH_ALIAS}:{repo1["path"]}\n'
    url = f'rest:https://{REST_USER}:{{}}@{repo1["host"]}:{repo1["port"]}/restic/'
    if rest_password is not None:
        return url.format(rest_password) + '\n'
    path = directory / 'restic-files-repo1.repository'
    try:
        current = path.read_text().strip() if path.is_file() and not path.is_symlink() else ''
    except OSError:
        current = ''
    head, _, tail = url.partition('{}')
    if not (current.startswith(head) and current.endswith(tail) and REST_PASSWORD.fullmatch(current[len(head):-len(tail)] or '')):
        raise Refused('stdin: repo1 is not on this rest-server yet - give the repo1_rest<TAB>password line')
    return None


def ssh_files(root, config):
    """~/.ssh/config and known_hosts, root's 0644: the alias, the key, the pinned host key, nothing interactive. Over the rest-server
    there is no sftp: both files are removed (the key stays, as --uninstall keeps it)."""
    repo1 = config['repo1']
    ssh = under(root, HOME) / '.ssh'
    if rest(config):
        removed = False
        for name in ('config', 'known_hosts'):
            if os.path.lexists(ssh / name):
                (ssh / name).unlink()
                removed = True
        return removed
    conf = (f'# nexloop-file-backup: repo1 on DATA_HOST over the restricted sftp account (file_backup_install.py; do not edit)\n'
            f'Host {SSH_ALIAS}\n  HostName {repo1["host"]}\n  Port {repo1["port"]}\n  User {REPO1_USER}\n'
            f'  IdentityFile {HOME}/.ssh/id_ed25519\n  IdentitiesOnly yes\n  HostKeyAlias {SSH_ALIAS}\n'
            f'  UserKnownHostsFile {HOME}/.ssh/known_hosts\n  GlobalKnownHostsFile /dev/null\n  StrictHostKeyChecking yes\n'
            f'  UpdateHostKeys no\n  BatchMode yes\n  ForwardAgent no\n  ForwardX11 no\n  ClearAllForwardings yes\n  ConnectTimeout 20\n'
            f'  ServerAliveInterval 30\n')
    a = write_private(ssh / 'config', conf, mode=0o644)
    b = write_private(ssh / 'known_hosts', f'{SSH_ALIAS} {repo1["host_key"]}\n', mode=0o644)
    return a or b


def env_file(root, config):
    path = under(root, ENV_FILE)
    kept = []
    if path.exists():
        # FILE_BACKUP_REPO2_OPTIONS belongs to install_repo2_secrets.sh files (BUCKET_LOOKUP): kept as it wrote it.
        kept = [line for line in path.read_text().splitlines() if OPTIONS.fullmatch(line)]
    lines = [f'BACKUP_SECRETS={SECRETS}', f'FILE_BACKUP_STAGING={STAGING}', f'FILE_BACKUP_KEY_REF={config["key_ref"]}',
             f'RESTIC_REPO1_REPOSITORY_FILE={SECRETS}/restic-files-repo1.repository', f'RESTIC_REPO1_PASSWORD_FILE={SECRETS}/restic-files-repo1.pass',
             f'RESTIC_REPO2_REPOSITORY_FILE={SECRETS}/{REPO2[0]}', f'RESTIC_REPO2_PASSWORD_FILE={SECRETS}/{REPO2[1]}',
             f'OFFSITE_ENVIRONMENT_FILE={SECRETS}/{REPO2[2]}', *kept[:1],
             # repo1 over DATA_HOST's rest-server: restic trusts the stage CA bundle alone (--cacert); empty over the D4 sftp account.
             *([f'FILE_BACKUP_REPO1_OPTIONS=--cacert {SECRETS}/{REST_CA}'] if rest(config) else [])]
    return write_private(path, '# nexloop-file-backup (file_backup_install.py); FILE_BACKUP_REPO2_OPTIONS from install_repo2_secrets.sh files\n'
                         + '\n'.join(lines) + '\n')


def render_service(text, config, restic):
    for token, value in (('@PYTHON@', config['python']), ('@RESTIC@', restic),
                         ('@MDWE@', 'true' if config.get('memory_deny_write_execute', True) else 'false')):
        text = text.replace(token, value)
    if '@' in ''.join(line for line in text.splitlines() if not line.startswith('#')):
        raise Failed('service template: an unfilled token')
    return text


def sandbox(service_text):
    """(key, value) of the unit's [Service] section, as transient properties: the check runs in exactly the unit's sandbox."""
    out, section = [], None
    for line in service_text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.endswith('\\') or '"' in line or "'" in line:
            raise Failed('the unit uses a continuation line or a quoted value: the sandbox check cannot mirror it')
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        key, sep, value = line.partition('=')
        if section == 'Service' and sep and key.strip() not in SANDBOX_SKIP:
            out.append((key.strip(), value.strip()))
    return out


def hidden_paths(root, properties):
    """Everything the check must find hidden: each InaccessiblePaths= entry that exists here, and every entry of /srv/nexloop,
    its secrets and its data besides the ones the unit is given."""
    paths = []
    for key, value in properties:
        if key == 'InaccessiblePaths':
            paths += [p.lstrip('-') for p in value.split()]
    for directory, given in (('/srv/nexloop', {'data', 'secrets'}), ('/srv/nexloop/secrets', {'backup'}),
                             ('/srv/nexloop/data', {'artifacts', 'runtime'})):
        if under(root, directory).is_dir():
            paths += [f'{directory}/{n}' for n in sorted(os.listdir(under(root, directory))) if n not in given]
    return [p for p in dict.fromkeys(paths) if os.path.lexists(under(root, p))]


def repo2_lookup(root):
    for line in under(root, ENV_FILE).read_text().splitlines():
        m = OPTIONS.fullmatch(line)
        if m:
            return m.group(1)
    return ''


def check_sandbox(root, config, service, restic_version, runner, out):
    properties = sandbox(service)
    missing = [n for n in REPO2 if not under(root, f'{SECRETS}/{n}').exists()]
    if missing:
        raise Failed('repo2 files missing (' + ', '.join(missing) + '): run install_repo2_secrets.sh files, then rerun this installer')
    hidden = hidden_paths(root, properties)
    reads = [*DATA_ROOTS, *(f'{SECRETS}/{n}' for n in [*OWN, *REPO2] if n != REST_CA or rest(config))]
    reads += [] if rest(config) else [f'{HOME}/.ssh/id_ed25519']
    lists = ['/srv/nexloop', '/srv/nexloop/secrets', '/srv/nexloop/data']
    command = [config['python'], '-I', '-B', f'{BASE}/current/scripts/deploy/file_backup_sandbox_check.py', '--restic', restic_path(restic_version),
               *[a for p in lists for a in ('--list', p)], *[a for p in reads for a in ('--read', p)], *[a for p in hidden for a in ('--hidden', p)],
               '--repo', f'repo1,{SECRETS}/restic-files-repo1.repository,{SECRETS}/restic-files-repo1.pass'
               + (f',,,{SECRETS}/{REST_CA}' if rest(config) else ''),
               # Over the rest-server, repo1 must take a new snapshot and refuse to forget it (--append-only).
               *(['--append-only-probe', 'repo1', '--probe-dir', STAGING] if rest(config) else []),
               '--repo', f'repo2,{SECRETS}/{REPO2[0]},{SECRETS}/{REPO2[1]},{SECRETS}/{REPO2[2]},{repo2_lookup(root)}',
               *(['--init', 'repo2'] if config.get('repo2_init') else [])]
    answer = runner(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec',
                     *[a for key, value in properties for a in ('-p', f'{key}={value}')], *command], check=False)
    try:
        doc = json.loads(answer.strip().splitlines()[-1])
        assert type(doc) is dict
    except (ValueError, IndexError, AssertionError):
        raise Failed('sandbox check gave no answer (the interpreter, the release or a sandbox setting): timer not enabled') from None
    problems = []
    expected = {'/srv/nexloop': ['data', 'secrets'], '/srv/nexloop/secrets': ['backup'], '/srv/nexloop/data': ['artifacts', 'runtime']}
    problems += [f'{d} shows {doc.get("listing", {}).get(d)}' for d, names in expected.items() if doc.get('listing', {}).get(d) != names]
    problems += [f'{p} unreadable' for p in reads if doc.get('read', {}).get(p) is not True]
    problems += [f'{p} visible' for p in hidden if doc.get('hidden', {}).get(p) is not True]
    # L6 F1: the handle calls filtered (EPERM), or the capability opens any inode past the mounts above.
    problems += [f'{name} is not filtered ({doc.get("handles", {}).get(name)})' for name in ('open_by_handle_at', 'name_to_handle_at')
                 if doc.get('handles', {}).get(name) != 'EPERM']
    if doc.get('restic') != f'restic {restic_version}':
        problems.append(f'restic is {doc.get("restic")!r}, not {restic_version}')
    for name in ('repo1', 'repo2'):
        code = doc.get('repos', {}).get(name)
        if code != 0:
            why = {10: 'does not exist' + (' (repo2_init true creates it)' if name == 'repo2' else ' (DATA_HOST step 1, repo1.path)'),
                   12: 'wrong password'}.get(code, f'restic {code}')
            problems.append(f'{name}: {why}')
    if rest(config):
        # Owner 2026-10-27: a new snapshot is taken, forgetting it is refused and it is still there - APP_HOST cannot delete repo1.
        probe = doc.get('append_only', {}).get('repo1')
        if not (type(probe) is dict and probe.get('backup') == 0 and probe.get('forget') not in (0, None) and probe.get('still_listed') is True):
            problems.append(f'repo1 is not append-only ({json.dumps(probe, sort_keys=True)}): a forget was not refused')
    print('sandbox: ' + json.dumps({'hidden': len(hidden), 'readable': len(reads), 'listing': 'as expected' if not any('shows' in p for p in problems) else 'NOT',
                                    'restic': doc.get('restic'), 'handles': doc.get('handles'), 'repos': doc.get('repos'), 'init': doc.get('init'),
                                    **({'append_only': doc.get('append_only')} if rest(config) else {})}, sort_keys=True), file=out)
    if problems:
        raise Failed('sandbox check: ' + '; '.join(problems)[:600] + ': timer not enabled')
    return len(hidden)


def real_run(runner, out):
    """One run of the service itself (all four ExecStart lines); each line's JSON result read back from its invocation's journal."""
    runner(['systemctl', 'start', SERVICE], check=False)
    invocation = runner(['systemctl', 'show', '-p', 'InvocationID', '--value', SERVICE], check=False).strip()
    result = runner(['systemctl', 'show', '-p', 'Result', '--value', SERVICE], check=False).strip()
    lines = runner(['journalctl', '--no-pager', '-o', 'cat', f'_SYSTEMD_INVOCATION_ID={invocation}'], check=False) if re.fullmatch(r'[0-9a-f]{32}', invocation) else ''
    results = []
    for line in lines.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if type(doc) is dict and 'ok' in doc:
            results.append(doc)
    for label, doc in zip(RUNS, results):
        print(f'run {label}: ' + json.dumps({k: doc.get(k) for k in ('verify_status', 'file_count', 'listed', 'skipped', 'absent', 'restic', 'check', 'error')
                                             if k in doc}, sort_keys=True), file=out)
    if result != 'success' or len(results) != len(RUNS) or not all(d.get('ok') is True and d.get('verify_status') == 'verified' for d in results):
        raise Failed(f'the service run is not verified ({result or "no result"}, {len(results)} of {len(RUNS)} lines; '
                     f'journalctl -u {SERVICE}): timer not enabled')


def timers_of(value, nested, path):
    probe = value.get('probe') if nested else value
    if type(probe) is not dict or type(probe.get('timers', [])) is not list or not all(type(t) is dict for t in probe.get('timers', [])):
        raise Refused(f'{path}: no probe timers list')
    return probe


def host_health_files(root, config):
    return [(under(root, config['host_health_config']), False), (under(root, config['host_health_install_config']), True)]


def rewrite_timers(files, change):
    """Both host-health files or neither: the first is restored when the second cannot be written."""
    plans = []
    for path, nested in files:
        before = path.read_text() if path.exists() else None
        value = read_private_json(path)
        if change(timers_of(value, nested, path)):
            plans.append((path, before, json.dumps(value, indent=2) + '\n'))
    done = []
    try:
        for path, before, text in plans:
            write_private(path, text)
            done.append((path, before))
    except OSError:
        for path, before in done:
            if before is not None:
                write_private(path, before)
        raise
    return [path for path, _, _ in plans]


def adder(names):
    def add(probe):
        timers = probe.setdefault('timers', [])
        missing = [n for n in names if not any(t.get('name') == n for t in timers)]
        timers.extend(dict(ENTRIES[n]) for n in missing)
        return bool(missing)
    return add


def remove_file_backup(probe):
    """This installer's two timers out (provider-drift is its own unit and stays)."""
    ours = {'file-backup', 'file-backup-maint'}
    timers = probe.get('timers', [])
    if not any(t.get('name') in ours for t in timers):
        return False
    probe['timers'] = [t for t in timers if t.get('name') not in ours]
    return True


def maint_dry_run(root, config, maint_service, restic_version, runner, out):
    """repo2's maintenance once with --dry-run (forget --dry-run, nothing removed, no check) in its own unit's sandbox: what fails
    under its timer - the repository, the keys, the option, the sandbox - fails here, before the timers are enabled. The paths are
    the env file's own (step 4); a run that is not ok, or no answer, stops the install."""
    lookup = repo2_lookup(root)
    command = [config['python'], '-I', '-B', f'{BASE}/current/scripts/deploy/file_backup_maint.py', '--restic', restic_path(restic_version),
               '--repository-file', f'{SECRETS}/{REPO2[0]}', '--password-file', f'{SECRETS}/{REPO2[1]}', '--environment-file', f'{SECRETS}/{REPO2[2]}',
               *(['--s3-bucket-lookup', lookup] if lookup else []), '--dry-run']
    answer = runner(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec',
                     *[a for key, value in sandbox(maint_service) for a in ('-p', f'{key}={value}')], *command], check=False)
    try:
        doc = json.loads(answer.strip().splitlines()[-1])
        assert type(doc) is dict
    except (ValueError, IndexError, AssertionError):
        raise Failed('repo2 maintenance dry run gave no answer (the interpreter, the release or a sandbox setting): timers not enabled') from None
    print('maint dry-run: ' + json.dumps({k: doc.get(k) for k in ('ok', 'forget', 'removed', 'error')}, sort_keys=True), file=out)
    if doc.get('ok') is not True or doc.get('forget') != 'ok':
        raise Failed('repo2 maintenance dry run is not ok (' + str(doc.get('error') or doc.get('forget')) + '): timers not enabled')
    return doc.get('removed')


def preflight(root, config, trust, trust_python):
    trust_release(config, trust)
    trust_python(config['python'], what='python')
    for path, nested in host_health_files(root, config):  # malformed or missing: refused before anything is written
        timers_of(read_private_json(path), nested, path)
    for directory in DATA_ROOTS:
        if os.path.islink(under(root, directory)) or not under(root, directory).is_dir():
            raise Refused(f'{directory}: a directory (the unit binds it read-only)')
    foreign_secrets(root)


def install(root, config, secrets, runner, out, trust, trust_python):
    step = lambda n, text: print(f'step {n}: {text}', file=out, flush=True)  # noqa: E731
    preflight(root, config, trust, trust_python)
    version, binary = restic_binary(config, runner)
    ensure_user(root, runner, trust)
    public, created = ensure_key(root, runner)
    target, reused = release(root, config, runner, trust)
    unpacked = install_restic(root, version, binary, trust)
    step(2, f'user {USER}, SSH key ' + ('generated' if created else 'kept') + f', release {config["release_sha"][:12]} ' + ('reused' if reused else 'built')
         + f', restic {version} ' + ('installed (sha256 as versions.lock.json)' if unpacked else 'unchanged'))
    done = write_secrets(root, config, secrets, runner, trust)
    ssh_changed = ssh_files(root, config)
    step(3, 'secrets/backup: ' + ', '.join(f'{n} {s}' for n, s in sorted(done.items())) + '; ssh config ' + ('written' if ssh_changed else 'unchanged'))
    env_changed = env_file(root, config)
    systemd = under(root, '/etc/systemd/system')
    service = render_service((target / 'units' / SERVICE).read_text(), config, restic_path(version))
    maint = render_service((target / 'units' / MAINT_SERVICE).read_text(), config, restic_path(version))
    for name, text in ((SERVICE, service), (TIMER, (target / 'units' / TIMER).read_text()), (MAINT_SERVICE, maint),
                       (MAINT_TIMER, (target / 'units' / MAINT_TIMER).read_text())):
        write_private(systemd / name, text, mode=0o644)
    runner(['systemctl', 'daemon-reload'])
    step(4, f'{ENV_FILE} ' + ('written' if env_changed else 'unchanged') + '; units installed')
    try:
        hidden = check_sandbox(root, config, service, version, runner, out)
        maint_dry_run(root, config, maint, version, runner, out)
        real_run(runner, out)
    except Failed as error:
        # L6 S2: a rerun whose gate fails leaves no timer of an earlier install running on what failed the gate.
        runner(['systemctl', 'disable', '--now', TIMER, MAINT_TIMER], check=False)
        print(f'step 5 failed: {TIMER} and {MAINT_TIMER} disabled (an earlier install may have enabled them)', file=out)
        raise Failed(f'{error}; both timers disabled') from None
    step(5, f'sandbox as the unit ({hidden} paths hidden, the data roots and its own files readable, both repositories open); repo2 maintenance '
         f'dry run ok; one run verified')
    names = ['file-backup', 'file-backup-maint'] + (['provider-drift'] if 'nexloop-provider-drift.timer' in runner(
        ['systemctl', 'list-unit-files', '--no-legend', 'nexloop-provider-drift.timer'], check=False) else [])
    added = rewrite_timers(host_health_files(root, config), adder(names))
    runner(['systemctl', 'enable', '--now', TIMER, MAINT_TIMER])
    step(6, 'host-health timers ' + ', '.join(names) + ': ' + ('added to ' + ', '.join(p.name for p in added) if added else 'already listed')
         + ('' if 'provider-drift' in names else ' (no nexloop-provider-drift.timer here)') + '; both timers enabled')


def prepare(root, config, runner, out, trust):
    ensure_user(root, runner, trust)
    public, created = ensure_key(root, runner)
    secrets_directory(root, trust)
    changed = env_file(root, config)
    print(f'prepared: user {USER}, SSH key ' + ('generated' if created else 'kept') + f', {SECRETS}, {ENV_FILE} ' + ('written' if changed else 'unchanged'),
          file=out)
    print('public key (for DATA_HOST step 1): ' + public, file=out)


def uninstall(root, config, runner, out):
    files = [(p, n) for p, n in host_health_files(root, config) if p.exists()]
    for path, nested in files:  # a malformed host-health file is refused before anything is removed
        timers_of(read_private_json(path), nested, path)
    for path in (under(root, SECRETS), under(root, BASE), under(root, STAGING)):
        if os.path.islink(path):
            raise Refused(f'{path} is a symbolic link: nothing removed')
    runner(['systemctl', 'disable', '--now', TIMER, MAINT_TIMER], check=False)
    systemd = under(root, '/etc/systemd/system')
    for d in (TIMER + '.d', SERVICE + '.d', MAINT_TIMER + '.d', MAINT_SERVICE + '.d'):
        if (systemd / d).is_dir():
            shutil.rmtree(systemd / d)
    for path in (systemd / TIMER, systemd / SERVICE, systemd / MAINT_TIMER, systemd / MAINT_SERVICE, under(root, ENV_FILE), under(root, ENV_FILE + '.pre-repo2'),
                 *(under(root, f'{SECRETS}/{n}') for n in sorted(ALLOWED))):
        if path.exists() or path.is_symlink():
            path.unlink()
    # L6 S2: the staging tree is the service user's: removed as it is (rmtree does not follow links), never chmod'ed first.
    if under(root, STAGING).exists():
        shutil.rmtree(under(root, STAGING))
    base = under(root, BASE)
    if base.exists():
        for path in [base, *base.rglob('*')]:  # root's own read-only release tree (and restic): writable before removal
            if not path.is_symlink():
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
        shutil.rmtree(base)
    rewrite_timers(files, remove_file_backup)
    runner(['systemctl', 'daemon-reload'], check=False)
    print(f'uninstalled: timers, units, env file, release, restic, staging and the secret files removed; file-backup and file-backup-maint taken out of the '
          f'host-health timers; the user {USER}, its home and SSH key stay', file=out)


def main(argv=None, stdin=sys.stdin, out=sys.stdout, runner=run, root=Path('/'), trust=root_owned, trust_python=interpreter_owned):
    p = argparse.ArgumentParser(description='NX-035 file backup install on APP_HOST (steps 2-6, root, one command)')
    p.add_argument('config')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--uninstall', action='store_true')
    a = p.parse_args(argv)
    os.umask(0o022)
    try:
        config = load_config(a.config)
        if a.uninstall:
            uninstall(root, config, runner, out)
        elif a.prepare:
            prepare(root, config, runner, out, trust)
        else:
            install(root, config, read_secrets(stdin), runner, out, trust, trust_python)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
