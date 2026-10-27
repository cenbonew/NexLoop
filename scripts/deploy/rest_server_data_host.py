#!/usr/bin/env python3
"""NX-035 repo1 append-only on DATA_HOST (owner, root; owner decision 2026-10-27; docs/implementation/NX-035-file-backup.md §6.5).
Run through rest_server_data_host.sh, one private configuration, one command:

    printf 'rest\\t%s\\nfiles\\t%s\\n' "$REST_PW" "$FILES_REPO_PW" | sudo bash rest_server_data_host.sh /path/rest-server.data-host.json
    sudo bash rest_server_data_host.sh /path/rest-server.data-host.json              a rerun: htpasswd and the files password kept
    sudo bash rest_server_data_host.sh /path/rest-server.data-host.json --uninstall

restic's rest-server serves the file backup's own repository (files repo1) to APP_HOST with --append-only: new data and snapshots can
be written, nothing existing can be deleted or overwritten (lock files excepted). The restore journal's repository is no longer shared
with APP_HOST at all (L6 F1 of the rest-server review: with the journal's key APP_HOST could add a future-dated `journal` snapshot and
make the maintenance forget every real one): this installer creates the files repository - a new directory, a new restic password,
`restic init` as nexloop-backup once - and serves only it. forget/prune stay with nexloop-restic-maint on the directory itself
(restic_maint_data_host.sh --files-repo adds its lines, each behind the future-snapshot guard). DATA_HOST step 1b, its own entry beside
step 1a (file_backup_data_host.sh: the recorder's pg_hba line only - D4 as changed on 2026-10-27 installs no sftp account): the
rest-server has its own binary, certificate, passwords and lifecycle, and either step can be rerun or undone without the other.

Private configuration (JSON, 0600 root; deploy/examples/rest-server-data-host.example.json): schema nexloop-rest-server-data-host/1,
release_checkout + release_sha (the unit template and versions.lock.json come from `git show` of it), stage_env (APP_HOST_ADDR and
DATA_HOST_ADDR are read from it), files_repo_path (the files repository: created and initialised when it does not exist, owned by
nexloop-backup; on the backup volume), journal_repo_path (the restore journal's repository, never served: the files repository must
be neither it nor inside or above it), restic (DATA_HOST's restic binary, run as nexloop-backup for init and a cat config),
rest_server_archive (the official rest-server_<version>_linux_amd64.tar.gz whose sha256 versions.lock.json names), tls_cert_file and
tls_key_file (issue_certs.py server --san IP:<DATA_HOST_ADDR> on the management machine: <name>.chain.crt and <name>.key, the key a
0600 file), port (1024-65535) and max_size_bytes (rest-server --max-size: the repository may not grow past it; at least 1 GiB).
stdin (optional on a rerun): `rest<TAB>password` - the one htpasswd user's (nexloop-file-backup), 24-128 of A-Z a-z 0-9 _ -, as APP_HOST's
repository URL carries it; stored only as its bcrypt hash (`htpasswd -niB`, apache2-utils) - and `files<TAB>password`, the files
repository's restic password (16-1024 printable, no space; APP_HOST's `repo1` line is the same one), into
/srv/nexloop/secrets/files-repo/restic.pass (root's 0711 directory, the file 0600 nexloop-backup).

Trust as the other root installers (file_backup_install): stdlib only; the checkout and its .git root's alone, lstat on every component,
no alternates/commondir/pointer file, git --no-replace-objects in a minimal environment; every directory root writes in is trusted
before its chmod. Nothing secret is printed or passed as an argument: the password reaches htpasswd on stdin, the key is a file.

Steps (each idempotent):
  R1 rest-server from a root-owned copy of the gzip tar archive whose size and sha256 the release's versions.lock.json names
     (deployment_tools.rest_server; an empty digest - not yet verified - is refused); the one regular rest-server file read out of it in
     memory (never extracted by name: no traversal; a link or a second candidate refused), /opt/nexloop/rest-server/<version>/rest-server
     (root, 0555), its --version read back
  R2 /srv/nexloop/secrets/rest-server (root, 0711): server.key (0600 nexloop-backup), server.chain.crt (0644; its SAN names
     DATA_HOST_ADDR, its public key the key's), htpasswd (0600 nexloop-backup)
  R3 the files repository (init once if absent, then cat config as nexloop-backup), /srv/nexloop-rest (root, 0755) with restic/
     bind-mounted from it by a mount unit; nothing else there
  R4 /etc/nexloop/rest-server.nft (0600 root): table inet nexloop_rest_server dropping every source but APP_HOST_ADDR on the port
     (IPv6 entirely), checked with nft -c; the unit loads it before the server starts and deletes it after it stops
  R5 the unit from the release (tokens filled), daemon-reload, enable and (re)start; then active, and the table loaded, read back
--uninstall: the unit stopped and removed (its stop removes the table), the nft file, the mount unit, the secrets, the binary; the
serving directory removed when empty. repo1 itself is never touched. nexloop-restic-maint's files repository block stops and starts
this unit around its guard and forget (L6 F2): while it is there (or the maintenance unit is a link) --uninstall refuses (L6 S6) -
take it out first (restic_maint_data_host.sh --revert, then without --files-repo).
Before R1 (L6 S7): `systemd-run --wait --quiet --collect -p ExecStopPost=/bin/false /bin/true` must exit non-zero - this host's systemd
fails a unit whose ExecStopPost fails, which the maintenance's check that the rest-server is active again relies on.
Output: step lines only. Exit 0 done, 1 a step failed, 2 refused.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import file_backup_install as A  # noqa: E402  (the same directory: trust checks, atomic writes, git show)
import file_backup_data_host as D  # noqa: E402  (the mount unit name)
import restic_maint_data_host as M  # noqa: E402  (the files repository block of nexloop-restic-maint)

Refused, Failed, run, under = A.Refused, A.Failed, A.run, A.under
SCHEMA = 'nexloop-rest-server-data-host/1'
USER = 'nexloop-backup'
REST_USER = 'nexloop-file-backup'
SERVE = '/srv/nexloop-rest'
MOUNT = SERVE + '/restic'
SECRETS = '/srv/nexloop/secrets/rest-server'
FILES_SECRETS = '/srv/nexloop/secrets/files-repo'  # the files repository's restic password (not in the rest-server's sandbox)
BASE = '/opt/nexloop/rest-server'
STOP_POST_PROBE = ['systemd-run', '--wait', '--quiet', '--collect', '-p', 'ExecStopPost=/bin/false', '/bin/true']
UNIT = 'nexloop-rest-server.service'
UNIT_SOURCE = 'deploy/stage/systemd/nexloop-rest-server.service.example'
NFT = '/etc/nexloop/rest-server.nft'
TABLE = 'nexloop_rest_server'
ADDR = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}')
PASSWORD = re.compile(r'[A-Za-z0-9_-]{24,128}')
HTPASSWD = re.compile(r'nexloop-file-backup:\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}')


def load_config(path):
    c = A.read_private_json(path)
    keys = {'schema', 'release_checkout', 'release_sha', 'stage_env', 'files_repo_path', 'journal_repo_path', 'restic', 'rest_server_archive',
            'tls_cert_file', 'tls_key_file', 'port', 'max_size_bytes', '_comment'}
    if type(c) is not dict or c.get('schema') != SCHEMA or set(c) - keys or keys - set(c) - {'_comment'}:
        raise Refused(f'configuration: schema {SCHEMA} with ' + ', '.join(sorted(keys - {'schema', '_comment'})))
    for key in ('release_checkout', 'stage_env', 'files_repo_path', 'journal_repo_path', 'restic', 'rest_server_archive', 'tls_cert_file', 'tls_key_file'):
        if type(c[key]) is not str or not A.PATH.fullmatch(c[key]) or '..' in c[key]:
            raise Refused(f'configuration: {key} (absolute path)')
    if type(c['release_sha']) is not str or not A.SHA.fullmatch(c['release_sha']):
        raise Refused('configuration: release_sha (40 hex)')
    if type(c['port']) is not int or not 1024 <= c['port'] <= 65535:
        raise Refused('configuration: port (1024-65535; the service user binds it without a capability)')
    if type(c['max_size_bytes']) is not int or c['max_size_bytes'] < 1 << 30:
        raise Refused('configuration: max_size_bytes (an integer, at least 1 GiB: rest-server --max-size)')
    files, journal = os.path.realpath(c['files_repo_path']), os.path.realpath(c['journal_repo_path'])  # L6 S2: through any link
    if os.path.commonpath([files, journal]) in (files, journal):
        raise Refused('configuration: files_repo_path must be its own repository - not the journal\'s, nor inside or above it (L6 F1)')
    return c


def addresses(config):
    """APP_HOST_ADDR and DATA_HOST_ADDR of the deployed stage.env (the addresses the stage templates render)."""
    try:
        lines = Path(config['stage_env']).read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        raise Refused('stage_env: unreadable') from None
    out = {}
    for key in ('APP_HOST_ADDR', 'DATA_HOST_ADDR'):
        values = [line.partition('=')[2].strip() for line in lines if line.startswith(key + '=')]
        if len(values) != 1 or not ADDR.fullmatch(values[0]):
            raise Refused(f'stage_env: one {key}=<IPv4 address> line')
        out[key] = values[0]
    if out['APP_HOST_ADDR'] == out['DATA_HOST_ADDR']:
        raise Refused('stage_env: APP_HOST_ADDR and DATA_HOST_ADDR are one address')
    return out


def read_passwords(stream):
    """{'rest': htpasswd password, 'files': the files repository's restic password}, each optional (a rerun keeps the files)."""
    text = '' if stream is None else stream.read(4096)
    out = {}
    for line in (line for line in text.splitlines() if line.strip()):
        name, sep, value = line.partition('\t')
        ok = (name == 'rest' and PASSWORD.fullmatch(value)) or (name == 'files' and 16 <= len(value) <= 1024 and value.isprintable()
                                                               and not any(c.isspace() for c in value))
        if not sep or name in out or not ok:
            raise Refused('stdin: rest<TAB>password (24-128 of A-Z a-z 0-9 _ -) and files<TAB>password (16-1024 printable, no space) lines')
        out[name] = value
    return out


def private_file(path, *, what, key=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused(f'{what}: unreadable (a regular file, not a link)') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (key and (info.st_mode & 0o077 or info.st_uid != os.geteuid())):
            raise Refused(f'{what}: a regular file' + (' of the caller, 0600' if key else ''))
        return stream.read(1 << 20)


def lock_entry(config, runner):
    try:
        lock = json.loads(A.git_show(config, 'versions.lock.json', runner))['deployment_tools']['rest_server']
        version, archive, digest, size = lock['version'], lock['archive'], lock['archive_sha256'], lock.get('archive_bytes')
    except (ValueError, KeyError, TypeError):
        raise Failed('versions.lock.json at the release commit has no deployment_tools.rest_server') from None
    if size is not None and (type(size) is not int or size <= 0):
        raise Failed('versions.lock.json: deployment_tools.rest_server.archive_bytes malformed')
    if not (type(version) is str and re.fullmatch(r'\d+\.\d+\.\d+', version) and archive == f'rest-server_{version}_linux_amd64.tar.gz'):
        raise Failed('versions.lock.json: deployment_tools.rest_server malformed')
    if type(digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise Refused('versions.lock.json: deployment_tools.rest_server.archive_sha256 is not filled yet (the dispatcher records it once the '
                      'official archive is downloaded and checked); nothing installed')
    return version, archive, digest, size


def _candidate(name):
    """rest-server itself: `rest-server` at the top or one directory down, relative, no `..` and no empty or `.` part."""
    parts = name.split('/')
    return (not name.startswith('/') and parts[-1] == 'rest-server' and len(parts) <= 2
            and all(part not in ('', '.', '..') for part in parts))


def rest_server_binary(config, runner):
    """The executable out of the gzip tar archive, in memory: it is never extracted by name (no path of the archive reaches the file
    system), exactly one candidate must exist and it must be a regular file - a link, a hard link or a device is refused."""
    version, archive, digest, size = lock_entry(config, runner)
    if Path(config['rest_server_archive']).name != archive:
        raise Refused(f'rest_server_archive: the lock names {archive}')
    data = _read_archive(config['rest_server_archive'])
    if (size is not None and len(data) != size) or hashlib.sha256(data).hexdigest() != digest:
        raise Refused(f'rest_server_archive: size or sha256 differs from versions.lock.json ({archive})')
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            members = [m for m in tar.getmembers() if _candidate(m.name)]
            if len(members) != 1 or not members[0].isreg() or members[0].size > 1 << 27:
                raise Refused('rest_server_archive: not exactly one regular rest-server file in it (a link or a second one is refused)')
            return version, tar.extractfile(members[0]).read()
    except (tarfile.TarError, EOFError, OSError):
        raise Refused('rest_server_archive: not a gzip tar archive') from None


def _read_archive(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused('rest_server_archive: unreadable (a regular file, not a link)') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        # The dispatcher's download lives in another account's home: root works from its own copy (root's, not group/other-writable).
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise Refused('rest_server_archive: a regular file of the caller (root), not group/other-writable - copy the download first '
                          '(install -o root -g root -m 0644 <download> <path>)')
        return stream.read(1 << 28)


def install_binary(root, version, binary, runner, trust):
    target = under(root, f'{BASE}/{version}/rest-server')
    target.parent.mkdir(parents=True, exist_ok=True)
    trust(target.parent, what='rest-server directory')
    changed = not (target.exists() and not target.is_symlink() and target.read_bytes() == binary)
    if changed:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.rest-server')
        with os.fdopen(fd, 'wb') as stream:
            stream.write(binary)
        os.chmod(tmp, 0o555)
        os.replace(tmp, target)
    else:
        trust(target, what='rest-server')
    os.chmod(target.parent, 0o555)
    shown = runner([f'{BASE}/{version}/rest-server', '--version'])
    if version not in shown:
        raise Failed(f'rest-server --version does not name {version}')
    return changed


def secrets(root, config, address, password, runner, trust):
    directory = under(root, SECRETS)
    if not under(root, '/srv/nexloop/secrets').is_dir() or os.path.islink(under(root, '/srv/nexloop/secrets')):
        raise Refused('/srv/nexloop/secrets missing (the encrypted volume is not mounted)')
    if os.path.islink(directory):
        raise Refused(f'{SECRETS} is a symbolic link')
    directory.mkdir(exist_ok=True)
    trust(directory, what='rest-server secrets')
    os.chmod(directory, 0o711)
    key = private_file(config['tls_key_file'], what='tls_key_file', key=True).decode('utf-8', 'replace')
    cert = private_file(config['tls_cert_file'], what='tls_cert_file').decode('utf-8', 'replace')
    if 'PRIVATE KEY' not in key or 'CERTIFICATE' in key or '-----BEGIN CERTIFICATE-----' not in cert or 'PRIVATE KEY' in cert:
        raise Refused('tls_key_file / tls_cert_file: a PEM key and a PEM certificate chain (issue_certs.py server: <name>.key, <name>.chain.crt)')
    done = {'server.key': A.write_private(directory / 'server.key', key, mode=0o600),
            'server.chain.crt': A.write_private(directory / 'server.chain.crt', cert, mode=0o644)}
    runner(['chown', f'{USER}:{USER}', str(directory / 'server.key')])
    # The certificate is DATA_HOST's (its SAN names the address APP_HOST connects to) and belongs to this key.
    san = runner(['openssl', 'x509', '-in', str(directory / 'server.chain.crt'), '-noout', '-ext', 'subjectAltName'])
    if f'IP Address:{address}' not in san.replace('IP Address: ', 'IP Address:'):
        raise Failed(f'tls_cert_file: its subjectAltName does not name DATA_HOST_ADDR (issue it with --san IP:<DATA_HOST_ADDR>)')
    if runner(['openssl', 'x509', '-in', str(directory / 'server.chain.crt'), '-noout', '-pubkey']).strip() != \
            runner(['openssl', 'pkey', '-in', str(directory / 'server.key'), '-pubout']).strip():
        raise Failed('tls_cert_file and tls_key_file do not belong together')
    htpasswd = directory / 'htpasswd'
    if password is not None:
        line = runner(['htpasswd', '-niB', REST_USER], stdin=password + '\n').strip()
        if not HTPASSWD.fullmatch(line):
            raise Failed('htpasswd -niB gave no bcrypt line for nexloop-file-backup')
        # A bcrypt hash is salted: written when the password line was given, kept otherwise.
        done['htpasswd'] = A.write_private(htpasswd, line + '\n', mode=0o600)
    elif not htpasswd.is_file() or htpasswd.is_symlink():
        raise Refused('stdin: htpasswd is not written yet - give rest<TAB>password')
    else:
        done['htpasswd'] = False
    runner(['chown', f'{USER}:{USER}', str(htpasswd)])
    return done


def mount_unit(repo):
    return (f'# NX-035 repo1 append-only (rest_server_data_host.py; do not edit): the restore journal\'s restic repository as {MOUNT},\n'
            f'# the only thing in the rest-server\'s --path.\n'
            f'[Unit]\nDescription=NexLoop rest-server repository (repo1)\nRequiresMountsFor={repo}\n\n'
            f'[Mount]\nWhat={repo}\nWhere={MOUNT}\nType=none\nOptions=bind,nosuid,nodev,noexec\n\n[Install]\nWantedBy=multi-user.target\n')


def files_repository(root, config, password, runner, trust, trust_python):
    """L6 F1 (b): the file backup's own repository - never the journal's. Its password in a root-owned directory (0600 nexloop-backup);
    the directory created for nexloop-backup and `restic init` run as it when there is no repository yet; then `cat config` must work.
    L6 S1: a password from stdin is written to restic.pass.new first and replaces restic.pass only after `cat config` opened the
    repository with it: a rerun with a wrong password fails and leaves the one in use as it was."""
    uid = runner(['id', '-u', USER], check=False).strip()
    if not uid.isdigit():
        raise Refused(f'user {USER} missing (the restore journal backup is not installed here)')
    trust_python(config['restic'], what='restic')
    directory = under(root, FILES_SECRETS)
    if os.path.islink(directory):
        raise Refused(f'{FILES_SECRETS} is a symbolic link')
    directory.mkdir(exist_ok=True)
    trust(directory, what='files repository secrets')
    os.chmod(directory, 0o711)
    pass_file, candidate = directory / 'restic.pass', directory / 'restic.pass.new'
    if os.path.lexists(candidate):  # a run that stopped before its check: never used
        candidate.unlink()
    if password is not None:
        A.write_private(candidate, password + '\n', mode=0o600)
        use = candidate
    elif pass_file.is_file() and not pass_file.is_symlink():
        use = pass_file
    else:
        raise Refused('stdin: the files repository password is not written yet - give files<TAB>password')
    try:
        runner(['chown', f'{USER}:{USER}', str(use)])
        repo = Path(config['files_repo_path'])
        if os.path.islink(repo) or os.path.islink(repo.parent) or not repo.parent.is_dir():
            raise Refused('files_repo_path: its parent must be a directory (the backup volume) and neither may be a link')
        as_user = ['runuser', '-u', USER, '--', config['restic'], '--no-cache', '-r', str(repo), '--password-file', f'{FILES_SECRETS}/{use.name}']
        created = False
        if not (repo / 'config').is_file():
            if repo.exists() and (not repo.is_dir() or os.listdir(repo)):
                raise Refused('files_repo_path: exists and is neither a restic repository nor an empty directory')
            repo.mkdir(mode=0o700, exist_ok=True)
            runner(['chown', f'{USER}:{USER}', str(repo)])
            runner([*as_user, 'init'])
            created = True
        if os.stat(repo).st_uid != int(uid):
            raise Refused(f'files_repo_path: owned by {USER}')
        try:
            runner([*as_user, 'cat', 'config'])  # the password opens it
        except Failed:
            raise Failed('the files repository did not open with ' + ('the password given (restic.pass kept as it was)' if use is candidate
                         else 'restic.pass')) from None
    except BaseException:
        if use is candidate and os.path.lexists(candidate):
            candidate.unlink()
        raise
    written = False
    if use is candidate:
        written = not (pass_file.is_file() and not pass_file.is_symlink() and pass_file.read_text() == password + '\n')
        os.replace(candidate, pass_file)  # the same directory, owner and mode: checked, now the one in use
    return created, written


def serve(root, config, runner, trust):
    repo = Path(config['files_repo_path'])
    if not (repo / 'config').is_file():
        raise Failed('files_repo_path: no restic repository')
    directory, mount = under(root, SERVE), under(root, MOUNT)
    if os.path.islink(directory):
        raise Refused(f'{SERVE} is a symbolic link')
    directory.mkdir(exist_ok=True)
    trust(directory, what='serving directory')  # root's alone: the service user can create no other repository in it
    os.chmod(directory, 0o755)
    if os.path.islink(mount):
        raise Refused(f'{MOUNT} is a symbolic link')
    mount.mkdir(exist_ok=True)
    unit = under(root, '/etc/systemd/system') / D.unit_name(MOUNT)
    A.write_private(unit, mount_unit(config['files_repo_path']), mode=0o644)
    runner(['systemctl', 'daemon-reload'])
    runner(['systemctl', 'enable', '--now', unit.name])
    if not (mount / 'config').is_file():
        raise Failed(f'{MOUNT}: the bind mount does not show the repository (systemctl status {unit.name})')
    others = sorted(n for n in os.listdir(directory) if n != 'restic')
    if others:
        raise Refused(f'{SERVE} holds {", ".join(others[:5])}: only restic/ may be served')
    return unit.name


def nft_rules(app, data, port):
    # Replaces the table atomically: declare, delete, define (a reload never doubles the rules).
    return (f'# NX-035 repo1 append-only (rest_server_data_host.py; do not edit): only APP_HOST reaches the rest-server port.\n'
            f'table inet {TABLE}\ndelete table inet {TABLE}\n'
            f'table inet {TABLE} {{\n  chain input {{\n    type filter hook input priority filter - 1; policy accept;\n'
            f'    meta nfproto ipv6 tcp dport {port} drop\n    ip daddr {data} tcp dport {port} ip saddr != {app} drop\n  }}\n}}\n')


def render_unit(text, version, address, config):
    for token, value in (('@REST_SERVER@', f'{BASE}/{version}/rest-server'), ('@LISTEN@', f'{address}:{config["port"]}'),
                         ('@MAX_SIZE@', str(config['max_size_bytes']))):
        text = text.replace(token, value)
    if re.search(r'@[A-Z_]+@', ''.join(line for line in text.splitlines() if not line.startswith('#'))):  # @system-service is systemd's
        raise Failed('rest-server unit template: an unfilled token')
    return text


def stop_post_fails_the_unit(runner):
    """L6 S7: nexloop-restic-maint relies on a failing ExecStopPost failing the unit (the rest-server not active again after the
    files repository's maintenance must alert). Checked on this host's systemd before anything is installed: a transient unit whose
    main process succeeds and whose ExecStopPost fails must end non-zero."""
    try:
        runner(STOP_POST_PROBE)
    except Failed:
        return
    raise Refused('this systemd reports a unit whose ExecStopPost failed as successful: nexloop-restic-maint could not alert when the '
                  'rest-server does not come back after its maintenance - nothing installed')


def install(root, config, passwords, runner, out, trust, trust_python):
    step = lambda n, text: print(f'step {n}: {text}', file=out, flush=True)  # noqa: E731
    A.trust_release(config, trust)
    stop_post_fails_the_unit(runner)
    step('R0', 'systemd: a failing ExecStopPost fails the unit (systemd-run --wait -p ExecStopPost=/bin/false /bin/true exited non-zero)')
    addr = addresses(config)
    version, binary = rest_server_binary(config, runner)
    changed = install_binary(root, version, binary, runner, trust)
    step('R1', f'rest-server {version} ' + ('installed (sha256 as versions.lock.json)' if changed else 'unchanged'))
    done = secrets(root, config, addr['DATA_HOST_ADDR'], passwords.get('rest'), runner, trust)
    step('R2', 'secrets: ' + ', '.join(f'{n} {"written" if c else "unchanged"}' for n, c in sorted(done.items())) + '; the certificate names DATA_HOST')
    created, written = files_repository(root, config, passwords.get('files'), runner, trust, trust_python)
    mount = serve(root, config, runner, trust)
    step('R3', f'files repository {"initialised" if created else "kept"} (password {"written" if written else "kept"}); {SERVE} serves it as '
               f'restic/ only ({mount}); the journal\'s repository is not served')
    rules = under(root, NFT)
    rules_changed = A.write_private(rules, nft_rules(addr['APP_HOST_ADDR'], addr['DATA_HOST_ADDR'], config['port']), mode=0o600)
    runner(['nft', '-c', '-f', str(rules)])
    step('R4', f'{NFT} ' + ('written' if rules_changed else 'unchanged') + ' (checked with nft -c)')
    unit_path = under(root, '/etc/systemd/system') / UNIT
    unit_changed = A.write_private(unit_path, render_unit(A.git_show(config, UNIT_SOURCE, runner), version, addr['DATA_HOST_ADDR'], config), mode=0o644)
    runner(['systemctl', 'daemon-reload'])
    runner(['systemctl', 'enable', UNIT])
    runner(['systemctl', 'restart' if unit_changed or rules_changed or changed or any(done.values()) else 'start', UNIT])
    state = runner(['systemctl', 'is-active', UNIT], check=False).strip()
    table = runner(['nft', 'list', 'table', 'inet', TABLE], check=False)
    if state != 'active' or f'ip saddr != {addr["APP_HOST_ADDR"]} drop' not in table:
        raise Failed(f'{UNIT} is {state or "not active"}' + ('' if 'drop' in table else ', the firewall table is not loaded') + f' (journalctl -u {UNIT})')
    step('R5', f'{UNIT} active on {addr["DATA_HOST_ADDR"]}:{config["port"]}, --append-only, only APP_HOST admitted; next: '
               f'restic_maint_data_host.sh --files-repo {config["files_repo_path"]} --files-password-file {FILES_SECRETS}/restic.pass, then APP_HOST '
               f'file_backup_install.sh with repo1.transport "rest" and this files password as repo1 (NX-035-file-backup.md §6.5)')


def uninstall(root, runner, out):
    maint = under(root, M.UNIT)  # L6 S6: its block stops and starts this unit; removing the unit first would fail every maintenance run
    if os.path.lexists(maint) and (os.path.islink(maint) or M.FILES_MARK in maint.read_text()
                                   or any(UNIT in value for _, _, value in M.sections(maint.read_text()))):  # comments do not count
        raise Refused(f'{M.UNIT} still maintains the files repository (it stops and starts {UNIT}): run restic_maint_data_host.sh --revert '
                      'first (then restic_maint_data_host.sh without --files-repo); nothing removed')
    for path in (under(root, SERVE), under(root, SECRETS), under(root, BASE)):
        if os.path.islink(path):
            raise Refused(f'{path} is a symbolic link: nothing removed')
    runner(['systemctl', 'disable', '--now', UNIT], check=False)
    runner(['nft', 'delete', 'table', 'inet', TABLE], check=False)  # its stop does this; also when the unit is already gone
    mount = under(root, '/etc/systemd/system') / D.unit_name(MOUNT)
    runner(['systemctl', 'disable', '--now', mount.name], check=False)
    for path in (under(root, '/etc/systemd/system') / UNIT, mount, under(root, NFT), *(under(root, SECRETS) / n for n in ('server.key', 'server.chain.crt', 'htpasswd'))):
        if os.path.lexists(path):
            path.unlink()
    base = under(root, BASE)
    if base.exists():
        for path in [base, *base.rglob('*')]:
            if not path.is_symlink():
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
        shutil.rmtree(base)
    runner(['systemctl', 'daemon-reload'], check=False)
    for directory in (under(root, MOUNT), under(root, SERVE), under(root, SECRETS)):
        if directory.is_dir() and not directory.is_symlink() and not os.listdir(directory):
            directory.rmdir()
    print('uninstalled: the rest-server unit, its firewall table and file, the mount unit, its secrets and binary removed; the files repository '
          'and its password stay (they hold the file backups)',
          file=out)


def main(argv=None, stdin=sys.stdin, out=sys.stdout, runner=run, root=Path('/'), trust=A.root_owned, trust_python=A.interpreter_owned):
    p = argparse.ArgumentParser(description='NX-035 repo1 rest-server --append-only on DATA_HOST (owner, root, one command)')
    p.add_argument('config')
    p.add_argument('--uninstall', action='store_true')
    a = p.parse_args(argv)
    os.umask(0o022)
    try:
        config = load_config(a.config)
        if a.uninstall:
            uninstall(root, runner, out)
        else:
            install(root, config, read_passwords(stdin), runner, out, trust, trust_python)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
