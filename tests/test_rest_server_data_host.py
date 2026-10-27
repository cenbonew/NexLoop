"""scripts/deploy/rest_server_data_host.py (DATA_HOST, owner): repo1 behind restic's rest-server --append-only, into a temporary root with a
scripted runner (no root, no systemd, no nft, no openssl, no rest-server). The binary only from the archive the release's versions.lock.json
names (an unfilled digest refused), the TLS files checked against DATA_HOST's address and each other, the password only on stdin and only
as its bcrypt hash, a serving directory with nothing but the repository, the firewall table, the unit's sandbox, uninstall. The trust
checks run for real against this test's own uid; the directories above tmp_path stand for root's."""
import functools
import hashlib
import io
import json
import os
import stat as st
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import rest_server_data_host as R  # noqa: E402
from support.writable_tree import make_writable  # noqa: E402

SHA = 'c' * 40
APP = '.'.join(('192', '0', '2', '10'))   # TEST-NET-1, built at runtime
DATA = '.'.join(('192', '0', '2', '20'))
PASSWORD = 'Rest-Pw_synthetic_0123456789abcdef'
FILES_PW = 'files-repo-Pw-synthetic-0123456789'
STDIN = f'rest\t{PASSWORD}\nfiles\t{FILES_PW}\n'
LOCK = json.loads((ROOT / 'versions.lock.json').read_text())['deployment_tools']['rest_server']
VERSION = LOCK['version']
BINARY = f'#!/bin/sh\necho rest-server version {VERSION}\n'.encode()
OWNERS = (0, os.getuid())
KEY_MARK = 'PRIVATE' + ' KEY'  # built at runtime: no key-shaped literal in the repository
KEY = f'-----BEGIN {KEY_MARK}-----\nsynthetic\n-----END {KEY_MARK}-----\n'
CERT = '-----BEGIN CERTIFICATE-----\nsynthetic leaf\n-----END CERTIFICATE-----\n-----BEGIN CERTIFICATE-----\nsynthetic intermediate\n-----END CERTIFICATE-----\n'
BCRYPT = 'nexloop-file-backup:$2y$05$' + 'a' * 53


UIDS = {}  # test only: inode -> the uid lstat reports (the deployment user's directories without root)
DEPLOY = 4242


@pytest.fixture(autouse=True)
def trust_root_at_tmp_path(tmp_path, monkeypatch):
    above = set()
    for base in (Path(tmp_path), Path(os.path.realpath(tmp_path))):
        above |= {str(p) for p in base.parents}
    lstat = os.lstat
    UIDS.clear()

    def fake(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) in above:
            return os.stat_result((st.S_IFDIR | 0o755, info.st_ino, info.st_dev, info.st_nlink, 0, 0, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        if info.st_ino in UIDS:
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, UIDS[info.st_ino], DEPLOY, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return info
    monkeypatch.setattr(os, 'lstat', fake)
    yield
    make_writable(tmp_path)


def archive(binary=BINARY, member=f'rest-server_{VERSION}_linux_amd64/rest-server', link=False, extra=()):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as tar:
        info = tarfile.TarInfo(member)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            tar.addfile(info)
        else:
            info.size, info.mode = len(binary), 0o755
            tar.addfile(info, io.BytesIO(binary))
        for name, kind in extra:
            other = tarfile.TarInfo(name)
            if kind == 'hard':
                other.type, other.linkname = tarfile.LNKTYPE, member
                tar.addfile(other)
            else:
                other.size = len(binary)
                tar.addfile(other, io.BytesIO(binary))
        readme = tarfile.TarInfo(f'rest-server_{VERSION}_linux_amd64/README.md')
        readme.size = 6
        tar.addfile(readme, io.BytesIO(b'readme'))
    return out.getvalue()


class Runner:
    def __init__(self, root, *, digest=None, san=None, pubkey=('PUB', 'PUB'), active='active', table=None, version=VERSION, htpasswd=BCRYPT,
                 uid=None, stop_post_fails=True):
        self.calls, self.stdin, self.root, self.uid, self.stop_post_fails = [], [], root, uid, stop_post_fails
        self.digest, self.san, self.pubkey, self.active, self.table, self.version, self.htpasswd = digest, san, pubkey, active, table, version, htpasswd

    def __call__(self, command, *, stdin=None, check=True):
        self.calls.append(list(command))
        self.stdin.append(stdin)
        if command[:2] == ['env', '-i']:
            assert command[:8] == R.A.GIT
            path = command[-1].split(':', 1)[1]
            if path == 'versions.lock.json':
                lock = json.loads((ROOT / path).read_text())
                if self.digest is not None:  # a synthetic archive: its digest, and no size (the official archive's size is the lock's)
                    lock['deployment_tools']['rest_server']['archive_sha256'] = self.digest
                    lock['deployment_tools']['rest_server'].pop('archive_bytes', None)
                return json.dumps(lock)
            return (ROOT / path).read_text()
        if command[:2] == ['id', '-u']:
            return f'{os.getuid() if self.uid is None else self.uid}\n'
        if command == R.STOP_POST_PROBE:  # a real systemd: the failing ExecStopPost fails the transient unit, so systemd-run exits 1
            if self.stop_post_fails:
                raise R.Failed('systemd-run exited 1')
            return ''
        if command[-1] == '--version':
            return f'rest-server version {self.version} (synthetic)\n'
        if command[:2] == ['openssl', 'x509'] and '-ext' in command:
            return f'X509v3 Subject Alternative Name: \n    IP Address:{self.san or DATA}\n'
        if command[:2] == ['openssl', 'x509'] and '-pubkey' in command:
            return self.pubkey[0] + '\n'
        if command[:2] == ['openssl', 'pkey']:
            return self.pubkey[1] + '\n'
        if command[0] == 'htpasswd':
            assert command == ['htpasswd', '-niB', 'nexloop-file-backup'] and stdin == PASSWORD + '\n'
            return self.htpasswd + '\n'
        if command[:3] == ['runuser', '-u', 'nexloop-backup'] and command[-1] in ('init', 'config'):
            repo = Path(command[command.index('-r') + 1])
            given = (self.root / command[command.index('--password-file') + 1].lstrip('/')).read_text()
            if command[-1] == 'init':  # the repository keeps the password it was made with (a synthetic key file)
                (repo / 'config').write_text('restic repository config (synthetic)\n')
                (repo / 'keys').mkdir()
                (repo / 'keys/synthetic').write_text(given)
            elif (repo / 'keys/synthetic').read_text() != given:
                raise R.Failed('runuser exited 12')  # restic: wrong password or no key found
            return ''
        if command[:3] == ['systemctl', 'enable', '--now'] and command[3].endswith('.mount'):
            (self.root / 'srv/nexloop-rest/restic/config').write_text('restic repository config (synthetic)\n')
            return ''
        if command[:2] == ['systemctl', 'is-active']:
            return self.active + '\n'
        if command[:3] == ['nft', 'list', 'table']:
            return self.table if self.table is not None else f'table inet nexloop_rest_server {{ ip saddr != {APP} drop }}\n'
        return ''


def private(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text) if isinstance(text, str) else path.write_bytes(text)
    os.chmod(path, mode)
    return path


def setup(tmp_path, data=None, **over):
    root = tmp_path / 'root'
    (root / 'srv/nexloop/secrets').mkdir(parents=True)
    checkout = tmp_path / 'release'
    (checkout / '.git/refs/heads').mkdir(parents=True)
    (checkout / '.git/HEAD').write_text('ref: refs/heads/main\n')
    journal = tmp_path / 'backups/file-repo1/restic'
    journal.mkdir(parents=True)
    (journal / 'config').write_text('synthetic journal\n')
    (tmp_path / 'backups/files-repo1').mkdir(parents=True)  # the files repository's parent: it does not exist yet
    restic = private(tmp_path / 'usr/local/bin/restic', '#!/bin/sh\n', 0o755)
    blob = archive() if data is None else data
    cfg = {'schema': R.SCHEMA, 'release_checkout': str(checkout), 'release_sha': SHA,
           'stage_env': str(private(tmp_path / 'stage.env', f'APP_HOST_ADDR={APP}\nDATA_HOST_ADDR={DATA}\n')),
           'files_repo_path': str(tmp_path / 'backups/files-repo1/restic'), 'journal_repo_path': str(journal), 'restic': str(restic),
           'rest_server_archive': str(private(tmp_path / f'download/rest-server_{VERSION}_linux_amd64.tar.gz', blob)),
           'tls_cert_file': str(private(tmp_path / 'pki/rest-server.chain.crt', CERT, 0o644)), 'tls_key_file': str(private(tmp_path / 'pki/rest-server.key', KEY)),
           'port': 8000, 'max_size_bytes': 100 << 30, **over}
    config = private(tmp_path / 'config.json', json.dumps(cfg))
    return root, str(config), hashlib.sha256(blob).hexdigest()


def account(name):
    """Test only: the account database."""
    uids = {'deploy-user': DEPLOY, 'nexloop-backup': DEPLOY + 3, 'nexloop-file-backup': DEPLOY + 4}
    if name not in uids:
        raise KeyError(name)
    return type('Entry', (), {'pw_uid': uids[name], 'pw_gid': uids[name], 'pw_name': name})


def main(root, config, *args, runner=None, stdin=STDIN, digest=None, **kw):
    out, runner = io.StringIO(), runner or Runner(root, digest=digest, **kw)
    code = R.main([config, *args], stdin=io.StringIO(stdin), out=out, runner=runner, root=root,
                  trust=functools.partial(R.A.root_owned, owners=OWNERS), trust_python=functools.partial(R.A.interpreter_owned, owners=OWNERS),
                  account=account)
    return code, out.getvalue(), runner


def reconfigure(config, **over):
    cfg = {**json.loads(Path(config).read_text()), **over}
    private(Path(config), json.dumps({k: v for k, v in cfg.items() if v is not None}))


def test_installs_the_append_only_server_and_a_rerun_changes_nothing(tmp_path):
    root, config, digest = setup(tmp_path)
    code, text, runner = main(root, config, digest=digest)
    assert code == 0, text
    binary = root / f'opt/nexloop/rest-server/{VERSION}/rest-server'
    assert binary.read_bytes() == BINARY and st.S_IMODE(binary.stat().st_mode) == 0o555
    secrets = root / 'srv/nexloop/secrets/rest-server'
    assert st.S_IMODE(secrets.stat().st_mode) == 0o711
    assert (secrets / 'server.key').read_text() == KEY and st.S_IMODE((secrets / 'server.key').stat().st_mode) == 0o600
    assert (secrets / 'htpasswd').read_text() == BCRYPT + '\n' and st.S_IMODE((secrets / 'htpasswd').stat().st_mode) == 0o600
    for name in ('server.key', 'htpasswd'):
        assert ['chown', 'nexloop-backup:nexloop-backup', str(secrets / name)] in runner.calls
    # The password reached htpasswd on stdin only: never an argument, never printed.
    assert PASSWORD not in text and not any(PASSWORD in ' '.join(c) for c in runner.calls) and KEY_MARK not in text
    # Only the repository is served, from a root-owned directory.
    serve = root / 'srv/nexloop-rest'
    assert sorted(os.listdir(serve)) == ['restic'] and st.S_IMODE(serve.stat().st_mode) == 0o755
    mount = (root / 'etc/systemd/system/srv-nexloop\\x2drest-restic.mount').read_text()
    assert f'What={tmp_path}/backups/files-repo1/restic\nWhere=/srv/nexloop-rest/restic\nType=none\nOptions=bind,nosuid,nodev,noexec' in mount
    # The firewall: IPv6 dropped on the port, IPv4 only from APP_HOST; checked before use; the table replaced atomically.
    rules = (root / 'etc/nexloop/rest-server.nft').read_text()
    assert f'ip daddr {DATA} tcp dport 8000 ip saddr != {APP} drop' in rules and 'meta nfproto ipv6 tcp dport 8000 drop' in rules
    assert rules.index('delete table inet nexloop_rest_server') < rules.index('table inet nexloop_rest_server {')
    assert ['nft', '-c', '-f', str(root / 'etc/nexloop/rest-server.nft')] in runner.calls
    unit = (root / 'etc/systemd/system/nexloop-rest-server.service').read_text()
    assert (f'ExecStart=/opt/nexloop/rest-server/{VERSION}/rest-server --path /srv/nexloop-rest --listen {DATA}:8000 --append-only --tls '
            f'--tls-cert /srv/nexloop/secrets/rest-server/server.chain.crt') in unit and f'--max-size {100 << 30}' in unit
    assert ' --append-only ' in unit.split('ExecStart=', 1)[1].splitlines()[0]  # and in the unit as installed
    assert ['systemctl', 'restart', 'nexloop-rest-server.service'] in runner.calls and 'step R5' in text
    code, text, runner = main(root, config, stdin='', digest=digest)
    assert code == 0, text
    assert 'htpasswd unchanged' in text and 'unchanged (checked with nft -c)' in text and ['systemctl', 'start', 'nexloop-rest-server.service'] in runner.calls
    assert not any(c[0] == 'htpasswd' for c in runner.calls)


def test_an_unfilled_lock_digest_or_another_archive_is_refused_before_anything(tmp_path):
    root, config, digest = setup(tmp_path)
    code, text, runner = main(root, config, digest='')  # a pending entry: no digest yet
    assert code == 2 and 'not filled yet' in text and not (root / 'opt').exists()
    code, text, _ = main(root, config)  # the real lock's digest is the official archive's, not this synthetic one
    assert code == 2 and 'sha256 differs' in text and not (root / 'opt').exists()
    code, text, _ = main(root, config, digest='0' * 64)
    assert code == 2 and 'sha256 differs' in text and not (root / 'opt').exists()
    for n, blob in enumerate((archive(member='../rest-server'), archive(member='/rest-server'), archive(member='a/b/rest-server'), archive(link=True),
                              archive(extra=[('rest-server', 'file')]), archive(extra=[('rest-server', 'hard')]), b'not a tar archive')):
        root, config, digest = setup(tmp_path / str(n), data=blob)
        code, text, _ = main(root, config, digest=digest)
        assert code == 2 and 'rest_server_archive' in text and not (root / 'opt').exists(), text
    root, config, digest = setup(tmp_path / 'r')
    renamed = Path(json.loads(Path(config).read_text())['rest_server_archive']).with_name('rest-server.tar.gz')
    os.rename(json.loads(Path(config).read_text())['rest_server_archive'], renamed)
    cfg = json.loads(Path(config).read_text())
    cfg['rest_server_archive'] = str(renamed)
    private(Path(config), json.dumps(cfg))
    code, text, _ = main(root, config, digest=digest)  # the same bytes under another name: not the archive the lock names
    assert code == 2 and f'the lock names rest-server_{VERSION}_linux_amd64.tar.gz' in text
    root, config, digest = setup(tmp_path / 'v')
    code, text, _ = main(root, config, digest=digest, version='0.12.1')
    assert code == 1 and f'does not name {VERSION}' in text


@pytest.mark.parametrize('kw, why', [
    ({'san': '.'.join(('192', '0', '2', '99'))}, 'does not name DATA_HOST_ADDR'),
    ({'pubkey': ('PUB-A', 'PUB-B')}, 'do not belong together'),
    ({'htpasswd': 'nexloop-file-backup:{SHA}synthetic'}, 'no bcrypt line'),
    ({'active': 'failed'}, 'is failed'),
    ({'table': ''}, 'the firewall table is not loaded'),
])
def test_a_wrong_certificate_a_weak_hash_or_a_server_not_running_fails(tmp_path, kw, why):
    root, config, digest = setup(tmp_path)
    code, text, _ = main(root, config, digest=digest, **kw)
    assert code == 1 and why in text, text


def test_the_key_must_be_a_private_file_and_the_password_a_plain_token(tmp_path):
    root, config, digest = setup(tmp_path)
    os.chmod(json.loads(Path(config).read_text())['tls_key_file'], 0o644)
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and 'tls_key_file' in text
    root, config, digest = setup(tmp_path / 'p')
    for bad in ('rest\tshort\n', f'rest\t{PASSWORD}:x\n', f'repo1\t{PASSWORD}\n', f'rest\t{PASSWORD}\nrest\t{PASSWORD}\n', f'files\t{FILES_PW} x\n',
                'files\tshort\n'):
        code, text, _ = main(root, config, stdin=bad, digest=digest)
        assert code == 2 and 'stdin' in text and PASSWORD not in text
    code, text, _ = main(root, config, stdin='', digest=digest)
    assert code == 2 and 'htpasswd is not written yet' in text
    code, text, _ = main(root, config, stdin=f'rest\t{PASSWORD}\n', digest=digest)
    assert code == 2 and 'files repository password is not written yet' in text


def test_the_repository_its_owner_and_the_serving_directory_are_checked(tmp_path):
    root, config, digest = setup(tmp_path)
    files = Path(json.loads(Path(config).read_text())['files_repo_path'])
    files.mkdir()
    (files / 'something').write_text('not a repository\n')
    code, text, runner = main(root, config, digest=digest)
    assert code == 2 and 'neither a restic repository nor an empty directory' in text and not any(c[-1] == 'init' for c in runner.calls)
    root, config, digest = setup(tmp_path / 'u')
    code, text, _ = main(root, config, digest=digest, uid=4242)  # the repository is not nexloop-backup's
    assert code == 2 and 'owned by nexloop-backup' in text
    root, config, digest = setup(tmp_path / 'o')
    (root / 'srv/nexloop-rest/other-repo').mkdir(parents=True)
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and 'only restic/ may be served' in text
    root, config, digest = setup(tmp_path / 'w')
    (root / 'srv/nexloop-rest').mkdir(parents=True)
    os.chmod(root / 'srv/nexloop-rest', 0o777)
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and 'serving directory' in text and st.S_IMODE((root / 'srv/nexloop-rest').stat().st_mode) == 0o777


def test_the_addresses_and_the_configuration_are_checked(tmp_path):
    for n, body in enumerate((f'APP_HOST_ADDR={APP}\n', f'APP_HOST_ADDR={APP}\nDATA_HOST_ADDR={APP}\n')):
        root, config, digest = setup(tmp_path / str(n))
        Path(json.loads(Path(config).read_text())['stage_env']).write_text(body)
        code, text, runner = main(root, config, digest=digest)
        assert code == 2 and 'stage_env' in text and not any(c[0] == 'systemctl' for c in runner.calls)
    for n, over in enumerate(({'port': 80}, {'max_size_bytes': 1 << 20}, {'unexpected': 1})):
        root, config, digest = setup(tmp_path / f'c{n}', **over)
        code, text, _ = main(root, config, digest=digest)
        assert code == 2 and 'configuration' in text, over


def test_the_release_is_trusted_before_anything(tmp_path):
    root, config, digest = setup(tmp_path)
    (tmp_path / 'release/.git/objects/info').mkdir(parents=True)
    (tmp_path / 'release/.git/objects/info/alternates').write_text('/elsewhere\n')
    code, text, runner = main(root, config, digest=digest)
    assert code == 2 and 'objects from elsewhere' in text and runner.calls == []


def test_uninstall_removes_the_server_and_never_the_repository(tmp_path):
    root, config, digest = setup(tmp_path)
    assert main(root, config, digest=digest)[0] == 0
    os.unlink(root / 'srv/nexloop-rest/restic/config')  # the bind mount gone once its unit is stopped
    code, text, runner = main(root, config, '--uninstall', stdin='', digest=digest)
    assert code == 0, text
    assert runner.calls[0] == ['systemctl', 'disable', '--now', 'nexloop-rest-server.service']
    assert ['nft', 'delete', 'table', 'inet', 'nexloop_rest_server'] in runner.calls
    for gone in ('etc/systemd/system/nexloop-rest-server.service', 'etc/systemd/system/srv-nexloop\\x2drest-restic.mount', 'etc/nexloop/rest-server.nft',
                 'srv/nexloop/secrets/rest-server', 'opt/nexloop/rest-server', 'srv/nexloop-rest'):
        assert not (root / gone).exists(), gone
    assert (tmp_path / 'backups/files-repo1/restic/config').exists() and (root / 'srv/nexloop/secrets/files-repo/restic.pass').exists()  # kept
    assert (tmp_path / 'backups/file-repo1/restic/config').read_text() == 'synthetic journal\n'


def test_uninstall_is_refused_while_the_maintenance_still_stops_and_starts_the_server(tmp_path):
    """L6 S6: nexloop-restic-maint's files repository block (or any of its lines naming this unit) must go first; a comment does not count."""
    root, config, digest = setup(tmp_path)
    assert main(root, config, digest=digest)[0] == 0
    maint = root / 'etc/systemd/system/nexloop-restic-maint.service'
    example = (ROOT / 'deploy/stage/systemd/nexloop-restic-maint.service.example').read_text()
    assert 'nexloop-rest-server.service' in example  # in its comment only
    for text in (example + R.M.FILES_MARK + '\n', example.replace('UMask=0077', 'ExecStopPost=+/usr/bin/systemctl start nexloop-rest-server.service\nUMask=0077')):
        maint.write_text(text)
        code, text, runner = main(root, config, '--uninstall', stdin='', digest=digest)
        assert code == 2 and 'restic_maint_data_host.sh --revert' in text and runner.calls == [], text
        assert (root / 'etc/systemd/system/nexloop-rest-server.service').exists()
    maint.unlink()
    os.symlink(tmp_path / 'elsewhere', maint)
    assert main(root, config, '--uninstall', stdin='', digest=digest)[0] == 2
    maint.unlink()
    maint.write_text(example)  # the example unit, without the block: uninstall goes ahead
    assert main(root, config, '--uninstall', stdin='', digest=digest)[0] == 0
    assert not (root / 'etc/systemd/system/nexloop-rest-server.service').exists()


def test_a_systemd_that_ignores_a_failing_stop_post_is_refused_before_anything(tmp_path):
    """L6 S7: the maintenance's \"the rest-server is active again\" check is an ExecStopPost; this host's systemd must fail a unit
    whose ExecStopPost fails, measured with a transient unit before anything is installed."""
    root, config, digest = setup(tmp_path)
    code, text, runner = main(root, config, digest=digest, stop_post_fails=False)
    assert code == 2 and 'ExecStopPost failed as successful' in text and 'nothing installed' in text, text
    assert runner.calls[-1] == ['systemd-run', '--wait', '--quiet', '--collect', '-p', 'ExecStopPost=/bin/false', '/bin/true']
    assert not (root / 'opt/nexloop/rest-server').exists() and not (root / 'etc/systemd/system/nexloop-rest-server.service').exists()
    code, text, runner = main(root, config, digest=digest)
    assert code == 0 and 'step R0: systemd: a failing ExecStopPost fails the unit' in text
    assert runner.calls.index(R.STOP_POST_PROBE) < min(i for i, c in enumerate(runner.calls) if c[-1] == '--version')


def test_the_unit_is_append_only_tls_and_sandboxed():
    text = (ROOT / 'deploy/stage/systemd/nexloop-rest-server.service.example').read_text()
    service = dict(R.A.sandbox(text))
    command = next(l for l in text.splitlines() if l.startswith('ExecStart='))
    for flag in ('--append-only', '--tls', '--tls-cert', '--tls-key', '--htpasswd-file', '--max-size @MAX_SIZE@', '--path /srv/nexloop-rest',
                 '--listen @LISTEN@'):
        assert flag in command, flag
    assert command.split().count('--append-only') == 1  # L6 I2: the one flag the whole design rests on, locked in the unit itself
    assert '--no-auth' not in command and '--private-repos' not in command  # one repository in a root-owned --path (see the unit's comment)
    assert service['User'] == 'nexloop-backup' and service['CapabilityBoundingSet'] == '' and service['NoNewPrivileges'] == 'true'
    assert service['TemporaryFileSystem'] == '/srv/nexloop:ro' and service['BindReadOnlyPaths'] == '/srv/nexloop/secrets/rest-server'
    assert service['ReadWritePaths'] == '/srv/nexloop-rest/restic' and service['ProtectSystem'] == 'strict' and service['UMask'] == '0077'
    hidden = service['InaccessiblePaths'].split()
    assert {'/etc/nexloop', '-/srv/nexloop-backup', '-/etc/shadow'} <= set(hidden)
    assert 'ExecStartPre=+/usr/sbin/nft -f /etc/nexloop/rest-server.nft' in text and 'ExecStopPost=-+/usr/sbin/nft delete table inet nexloop_rest_server' in text
    assert 'SystemCallFilter=~open_by_handle_at name_to_handle_at' in text and 'MemoryDenyWriteExecute=true' in text
    with pytest.raises(R.Failed, match='unfilled token'):
        R.render_unit(text.replace('@MAX_SIZE@', '@OTHER@'), VERSION, DATA, {'port': 8000, 'max_size_bytes': 1 << 30})



def test_the_binary_may_sit_at_the_top_of_the_archive_and_the_copy_must_be_roots(tmp_path):
    root, config, digest = setup(tmp_path, data=archive(member='rest-server'))
    assert main(root, config, digest=digest)[0] == 0
    root, config, digest = setup(tmp_path / 'g')
    os.chmod(json.loads(Path(config).read_text())['rest_server_archive'], 0o664)  # still another account's download
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and 'a regular file of the caller (root)' in text and not (root / 'opt').exists()


@pytest.mark.parametrize('delta, code', [(1, 2), (0, 0), ('-size', 1)])
def test_the_size_in_the_lock_is_checked(tmp_path, delta, code):
    root, config, digest = setup(tmp_path)
    runner = Runner(root, digest=digest)
    real = runner.__call__

    def lock_with_size(command, **kw):
        out = real(command, **kw)
        if command[:2] == ['env', '-i'] and command[-1].endswith(':versions.lock.json'):
            lock = json.loads(out)
            size = len(Path(json.loads(Path(config).read_text())['rest_server_archive']).read_bytes())
            lock['deployment_tools']['rest_server']['archive_bytes'] = -size if delta == '-size' else size + delta  # -size: malformed
            return json.dumps(lock)
        return out
    got, text, _ = main(root, config, runner=lock_with_size)
    assert got == code, text
    assert ('size or sha256 differs' in text) == (delta == 1) and ('archive_bytes malformed' in text) == (delta == '-size')


def test_the_repository_lock_names_the_verified_official_archive():
    assert LOCK['status'] == 'verified_not_installed' and LOCK['archive'] == f'rest-server_{VERSION}_linux_amd64.tar.gz'
    assert len(LOCK['archive_sha256']) == 64 and type(LOCK['archive_bytes']) is int and LOCK['archive_bytes'] > 0



def test_the_files_repository_is_its_own_initialised_once_and_the_journal_is_never_served(tmp_path):
    """L6 F1 (b): APP_HOST gets a repository of its own; the restore journal's is neither served nor reachable from it."""
    root, config, digest = setup(tmp_path)
    code, text, runner = main(root, config, digest=digest)
    assert code == 0, text
    files = tmp_path / 'backups/files-repo1/restic'
    pass_file = root / 'srv/nexloop/secrets/files-repo/restic.pass'
    assert pass_file.read_text() == FILES_PW + '\n' and st.S_IMODE(pass_file.stat().st_mode) == 0o600
    assert st.S_IMODE((root / 'srv/nexloop/secrets/files-repo').stat().st_mode) == 0o711
    restic = json.loads(Path(config).read_text())['restic']
    as_user = ['runuser', '-u', 'nexloop-backup', '--', restic, '--no-cache', '-r', str(files), '--password-file', '/srv/nexloop/secrets/files-repo/restic.pass.new']
    assert [c for c in runner.calls if c[:3] == ['runuser', '-u', 'nexloop-backup']] == [[*as_user, 'init'], [*as_user, 'cat', 'config']]
    candidate = pass_file.with_name('restic.pass.new')
    assert ['chown', 'nexloop-backup:nexloop-backup', str(files)] in runner.calls and ['chown', 'nexloop-backup:nexloop-backup', str(candidate)] in runner.calls
    assert not candidate.exists()  # checked, then renamed to restic.pass
    assert FILES_PW not in text and not any(FILES_PW in ' '.join(c) for c in runner.calls)
    mount = (root / 'etc/systemd/system/srv-nexloop\\x2drest-restic.mount').read_text()
    assert f'What={files}\n' in mount and str(tmp_path / 'backups/file-repo1') not in mount
    assert 'the journal\'s repository is not served' in text and '--files-repo ' + str(files) in text
    code, text, runner = main(root, config, stdin='', digest=digest)
    assert code == 0 and 'files repository kept (password kept)' in text and not any(c[-1] == 'init' for c in runner.calls)
    assert [c[-3] for c in runner.calls if c[:3] == ['runuser', '-u', 'nexloop-backup']] == ['/srv/nexloop/secrets/files-repo/restic.pass']
    code, text, runner = main(root, config, digest=digest)  # the same password again: checked, the file unchanged
    assert code == 0 and 'files repository kept (password kept)' in text and pass_file.read_text() == FILES_PW + '\n'


def test_a_rerun_with_a_wrong_files_password_fails_and_keeps_the_one_in_use(tmp_path):
    """L6 S1: the password from stdin opens the repository before it replaces restic.pass; a wrong one changes nothing."""
    root, config, digest = setup(tmp_path)
    assert main(root, config, digest=digest)[0] == 0
    pass_file = root / 'srv/nexloop/secrets/files-repo/restic.pass'
    code, text, runner = main(root, config, stdin=f'files\t{FILES_PW}-wrong\n', digest=digest)
    assert code == 1 and 'did not open with the password given (restic.pass kept as it was)' in text, text
    assert pass_file.read_text() == FILES_PW + '\n' and sorted(p.name for p in pass_file.parent.iterdir()) == ['restic.pass']
    assert FILES_PW not in text
    assert main(root, config, stdin='', digest=digest)[0] == 0  # still opens with the kept one
    stale = pass_file.with_name('restic.pass.new')  # left by a run stopped before its check: removed, never used
    stale.write_text('stale\n')
    assert main(root, config, stdin='', digest=digest)[0] == 0 and not stale.exists()


@pytest.mark.parametrize('files', ['backups/file-repo1/restic', 'backups/file-repo1/restic/files', 'backups/file-repo1'])
def test_the_files_repository_may_be_neither_the_journals_nor_inside_or_above_it(tmp_path, files):
    root, config, digest = setup(tmp_path, files_repo_path=str(tmp_path / files))
    code, text, runner = main(root, config, digest=digest)
    assert code == 2 and 'must be its own repository' in text and runner.calls == []


@pytest.mark.parametrize('files', ['alias/file-repo1/restic', 'alias/file-repo1/restic/files', 'alias/file-repo1'])
def test_the_journal_reached_through_a_link_is_refused_too(tmp_path, files):
    """L6 S2: the paths are compared after realpath - a link above them (here alias -> backups) does not hide the journal."""
    os.symlink(tmp_path / 'backups', tmp_path / 'alias')
    root, config, digest = setup(tmp_path, files_repo_path=str(tmp_path / files))
    code, text, runner = main(root, config, digest=digest)
    assert code == 2 and 'must be its own repository' in text and runner.calls == []



def test_a_linked_or_writable_rest_server_secrets_directory_is_refused_before_any_secret_is_written(tmp_path):
    """L6 I2: R2 trusts its directory before it writes the key, the certificate and the htpasswd file into it."""
    root, config, digest = setup(tmp_path)
    secrets = root / 'srv/nexloop/secrets/rest-server'
    secrets.mkdir(parents=True)
    os.chmod(secrets, 0o777)
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and 'rest-server secrets' in text and list(secrets.iterdir()) == [] and st.S_IMODE(secrets.stat().st_mode) == 0o777
    root, config, digest = setup(tmp_path / 'l')
    elsewhere = tmp_path / 'l/elsewhere'
    elsewhere.mkdir()
    os.symlink(elsewhere, root / 'srv/nexloop/secrets/rest-server')
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and 'symbolic link' in text and list(elsewhere.iterdir()) == []



def test_a_writable_files_repository_secrets_directory_is_refused(tmp_path):
    root, config, digest = setup(tmp_path)
    (root / 'srv/nexloop/secrets/files-repo').mkdir(parents=True)
    os.chmod(root / 'srv/nexloop/secrets/files-repo', 0o777)
    code, text, runner = main(root, config, digest=digest)
    assert code == 2 and 'files repository secrets' in text and not (root / 'srv/nexloop/secrets/files-repo/restic.pass').exists()


def deployment_users_srv(root):
    """As on stage: /srv/nexloop the deployment user's, 751 (and so every path root checks under it has that user above it)."""
    UIDS[os.stat(root / 'srv/nexloop').st_ino] = DEPLOY
    os.chmod(root / 'srv/nexloop', 0o751)


def test_a_srv_nexloop_of_the_deployment_user_is_accepted_only_as_trusted_owner_and_r1_reruns_unchanged(tmp_path):
    """Dispatcher 2026-10-27: on stage R0 and R1 passed and R2 was refused on /srv/nexloop. Without trusted_owner that stays refused
    (never by default), saying what to configure; with it the rerun keeps the binary (R1 unchanged) and installs the rest, every
    directory and file it creates root's or nexloop-backup's as before."""
    root, config, digest = setup(tmp_path)
    deployment_users_srv(root)
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and 'rest-server secrets' in text and 'name that user as trusted_owner' in text, text
    binary = root / f'opt/nexloop/rest-server/{VERSION}/rest-server'
    assert f'rest-server {VERSION} installed' in text and binary.read_bytes() == BINARY
    inode = os.stat(binary).st_ino
    reconfigure(config, trusted_owner='deploy-user')
    code, text, runner = main(root, config, digest=digest)
    assert code == 0, text
    assert f'step R1: rest-server {VERSION} unchanged' in text and os.stat(binary).st_ino == inode and st.S_IMODE(binary.stat().st_mode) == 0o555
    for directory, mode in (('srv/nexloop/secrets/rest-server', 0o711), ('srv/nexloop/secrets/files-repo', 0o711)):
        info = os.lstat(root / directory)
        assert info.st_uid in OWNERS and st.S_IMODE(info.st_mode) == mode, directory
    assert st.S_IMODE((root / 'srv/nexloop/secrets/rest-server/server.key').stat().st_mode) == 0o600
    assert ['chown', 'nexloop-backup:nexloop-backup', str(root / 'srv/nexloop/secrets/rest-server/htpasswd')] in runner.calls


@pytest.mark.parametrize('directory, what', [('srv/nexloop/secrets/rest-server', 'rest-server secrets'),
                                             ('srv/nexloop/secrets/files-repo', 'files repository secrets')])
def test_a_directory_the_installer_owns_stays_roots_even_with_trusted_owner(tmp_path, directory, what):
    """trusted_owner widens only what stands above: secrets/rest-server or secrets/files-repo of the deployment user is refused."""
    root, config, digest = setup(tmp_path, trusted_owner='deploy-user')
    deployment_users_srv(root)
    (root / directory).mkdir()
    os.chmod(root / directory, 0o711)
    UIDS[os.stat(root / directory).st_ino] = DEPLOY
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and what in text and 'must be owned by root and not' in text and list((root / directory).iterdir()) == [], text


@pytest.mark.parametrize('name, why', [('nexloop-backup', 'not nexloop-backup'), ('nexloop-file-backup', 'not nexloop-backup, nexloop-file-backup'),
                                       ('nobody-here', 'no such user'), ('', "deployment user's name")])
def test_trusted_owner_is_an_existing_user_never_a_service_user(tmp_path, name, why):
    root, config, digest = setup(tmp_path, trusted_owner=name)
    code, text, runner = main(root, config, digest=digest)
    assert code == 2 and why in text and runner.calls == [], text
