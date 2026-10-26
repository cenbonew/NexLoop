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
BINARY = b'#!/bin/sh\necho rest-server version 0.13.0\n'
OWNERS = (0, os.getuid())
KEY_MARK = 'PRIVATE' + ' KEY'  # built at runtime: no key-shaped literal in the repository
KEY = f'-----BEGIN {KEY_MARK}-----\nsynthetic\n-----END {KEY_MARK}-----\n'
CERT = '-----BEGIN CERTIFICATE-----\nsynthetic leaf\n-----END CERTIFICATE-----\n-----BEGIN CERTIFICATE-----\nsynthetic intermediate\n-----END CERTIFICATE-----\n'
BCRYPT = 'nexloop-file-backup:$2y$05$' + 'a' * 53


@pytest.fixture(autouse=True)
def trust_root_at_tmp_path(tmp_path, monkeypatch):
    above = set()
    for base in (Path(tmp_path), Path(os.path.realpath(tmp_path))):
        above |= {str(p) for p in base.parents}
    lstat = os.lstat

    def fake(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) in above:
            return os.stat_result((st.S_IFDIR | 0o755, info.st_ino, info.st_dev, info.st_nlink, 0, 0, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return info
    monkeypatch.setattr(os, 'lstat', fake)
    yield
    make_writable(tmp_path)


def archive(binary=BINARY, member='rest-server_0.13.0_linux_amd64/rest-server', link=False):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as tar:
        info = tarfile.TarInfo(member)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            tar.addfile(info)
        else:
            info.size, info.mode = len(binary), 0o755
            tar.addfile(info, io.BytesIO(binary))
        readme = tarfile.TarInfo('rest-server_0.13.0_linux_amd64/README.md')
        readme.size = 6
        tar.addfile(readme, io.BytesIO(b'readme'))
    return out.getvalue()


class Runner:
    def __init__(self, root, *, digest=None, san=None, pubkey=('PUB', 'PUB'), active='active', table=None, version='0.13.0', htpasswd=BCRYPT,
                 uid=None):
        self.calls, self.stdin, self.root, self.uid = [], [], root, uid
        self.digest, self.san, self.pubkey, self.active, self.table, self.version, self.htpasswd = digest, san, pubkey, active, table, version, htpasswd

    def __call__(self, command, *, stdin=None, check=True):
        self.calls.append(list(command))
        self.stdin.append(stdin)
        if command[:2] == ['env', '-i']:
            assert command[:8] == R.A.GIT
            path = command[-1].split(':', 1)[1]
            if path == 'versions.lock.json':
                lock = json.loads((ROOT / path).read_text())
                if self.digest is not None:
                    lock['deployment_tools']['rest_server']['archive_sha256'] = self.digest
                return json.dumps(lock)
            return (ROOT / path).read_text()
        if command[:2] == ['id', '-u']:
            return f'{os.getuid() if self.uid is None else self.uid}\n'
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
    repo = tmp_path / 'backup/files/restic'
    repo.mkdir(parents=True)
    (repo / 'config').write_text('synthetic\n')
    blob = archive() if data is None else data
    cfg = {'schema': R.SCHEMA, 'release_checkout': str(checkout), 'release_sha': SHA,
           'stage_env': str(private(tmp_path / 'stage.env', f'APP_HOST_ADDR={APP}\nDATA_HOST_ADDR={DATA}\n')), 'repo_path': str(repo),
           'rest_server_archive': str(private(tmp_path / 'download/rest-server_0.13.0_linux_amd64.tar.gz', blob)),
           'tls_cert_file': str(private(tmp_path / 'pki/rest-server.chain.crt', CERT, 0o644)), 'tls_key_file': str(private(tmp_path / 'pki/rest-server.key', KEY)),
           'port': 8000, 'max_size_bytes': 100 << 30, **over}
    config = private(tmp_path / 'config.json', json.dumps(cfg))
    return root, str(config), hashlib.sha256(blob).hexdigest()


def main(root, config, *args, runner=None, stdin=f'rest\t{PASSWORD}\n', digest=None, **kw):
    out, runner = io.StringIO(), runner or Runner(root, digest=digest, **kw)
    code = R.main([config, *args], stdin=io.StringIO(stdin), out=out, runner=runner, root=root,
                  trust=functools.partial(R.A.root_owned, owners=OWNERS))
    return code, out.getvalue(), runner


def test_installs_the_append_only_server_and_a_rerun_changes_nothing(tmp_path):
    root, config, digest = setup(tmp_path)
    code, text, runner = main(root, config, digest=digest)
    assert code == 0, text
    binary = root / 'opt/nexloop/rest-server/0.13.0/rest-server'
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
    assert f'What={tmp_path}/backup/files/restic\nWhere=/srv/nexloop-rest/restic\nType=none\nOptions=bind,nosuid,nodev,noexec' in mount
    # The firewall: IPv6 dropped on the port, IPv4 only from APP_HOST; checked before use; the table replaced atomically.
    rules = (root / 'etc/nexloop/rest-server.nft').read_text()
    assert f'ip daddr {DATA} tcp dport 8000 ip saddr != {APP} drop' in rules and 'meta nfproto ipv6 tcp dport 8000 drop' in rules
    assert rules.index('delete table inet nexloop_rest_server') < rules.index('table inet nexloop_rest_server {')
    assert ['nft', '-c', '-f', str(root / 'etc/nexloop/rest-server.nft')] in runner.calls
    unit = (root / 'etc/systemd/system/nexloop-rest-server.service').read_text()
    assert (f'ExecStart=/opt/nexloop/rest-server/0.13.0/rest-server --path /srv/nexloop-rest --listen {DATA}:8000 --append-only --tls '
            f'--tls-cert /srv/nexloop/secrets/rest-server/server.chain.crt') in unit and f'--max-size {100 << 30}' in unit
    assert ['systemctl', 'restart', 'nexloop-rest-server.service'] in runner.calls and 'step R5' in text
    code, text, runner = main(root, config, stdin='', digest=digest)
    assert code == 0, text
    assert 'htpasswd unchanged' in text and 'unchanged (checked with nft -c)' in text and ['systemctl', 'start', 'nexloop-rest-server.service'] in runner.calls
    assert not any(c[0] == 'htpasswd' for c in runner.calls)


def test_an_unfilled_lock_digest_or_another_archive_is_refused_before_anything(tmp_path):
    root, config, digest = setup(tmp_path)
    code, text, runner = main(root, config)  # the lock as it is in the repository: pending, no digest
    assert code == 2 and 'not filled yet' in text and not (root / 'opt').exists()
    code, text, _ = main(root, config, digest='0' * 64)
    assert code == 2 and 'sha256 differs' in text and not (root / 'opt').exists()
    for n, blob in enumerate((archive(member='rest-server'), archive(link=True), b'not a tar archive')):
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
    assert code == 2 and 'the lock names rest-server_0.13.0_linux_amd64.tar.gz' in text
    root, config, digest = setup(tmp_path / 'v')
    code, text, _ = main(root, config, digest=digest, version='0.12.1')
    assert code == 1 and 'does not name 0.13.0' in text


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
    for bad in ('rest\tshort\n', f'rest\t{PASSWORD}:x\n', f'repo1\t{PASSWORD}\n', f'rest\t{PASSWORD}\nrest\t{PASSWORD}\n'):
        code, text, _ = main(root, config, stdin=bad, digest=digest)
        assert code == 2 and 'stdin' in text and PASSWORD not in text
    code, text, _ = main(root, config, stdin='', digest=digest)
    assert code == 2 and 'htpasswd is not written yet' in text


def test_the_repository_its_owner_and_the_serving_directory_are_checked(tmp_path):
    root, config, digest = setup(tmp_path)
    os.unlink(Path(json.loads(Path(config).read_text())['repo_path']) / 'config')
    code, text, _ = main(root, config, digest=digest)
    assert code == 2 and 'restic config file' in text
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
    assert (tmp_path / 'backup/files/restic').is_dir() and (tmp_path / 'backup/files/restic/config').read_text() == 'synthetic\n'  # repo1 untouched


def test_the_unit_is_append_only_tls_and_sandboxed():
    text = (ROOT / 'deploy/stage/systemd/nexloop-rest-server.service.example').read_text()
    service = dict(R.A.sandbox(text))
    command = next(l for l in text.splitlines() if l.startswith('ExecStart='))
    for flag in ('--append-only', '--tls', '--tls-cert', '--tls-key', '--htpasswd-file', '--max-size @MAX_SIZE@', '--path /srv/nexloop-rest',
                 '--listen @LISTEN@'):
        assert flag in command, flag
    assert '--no-auth' not in command and '--private-repos' not in command  # one repository in a root-owned --path (see the unit's comment)
    assert service['User'] == 'nexloop-backup' and service['CapabilityBoundingSet'] == '' and service['NoNewPrivileges'] == 'true'
    assert service['TemporaryFileSystem'] == '/srv/nexloop:ro' and service['BindReadOnlyPaths'] == '/srv/nexloop/secrets/rest-server'
    assert service['ReadWritePaths'] == '/srv/nexloop-rest/restic' and service['ProtectSystem'] == 'strict' and service['UMask'] == '0077'
    hidden = service['InaccessiblePaths'].split()
    assert {'/etc/nexloop', '-/srv/nexloop-backup', '-/etc/shadow'} <= set(hidden)
    assert 'ExecStartPre=+/usr/sbin/nft -f /etc/nexloop/rest-server.nft' in text and 'ExecStopPost=-+/usr/sbin/nft delete table inet nexloop_rest_server' in text
    assert 'SystemCallFilter=~open_by_handle_at name_to_handle_at' in text and 'MemoryDenyWriteExecute=true' in text
    with pytest.raises(R.Failed, match='unfilled token'):
        R.render_unit(text.replace('@MAX_SIZE@', '@OTHER@'), '0.13.0', DATA, {'port': 8000, 'max_size_bytes': 1 << 30})
