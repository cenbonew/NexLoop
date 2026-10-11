"""NX-032 slice 1: certificates from scripts/deploy/issue_certs.py (D1) and the doctor TLS checks, with negative cases on a
disposable PostgreSQL 18 over TCP on 127.0.0.1 with TLS, SCRAM and client certificates (D2): a wrong client certificate, a wrong
CA, a SAN mismatch, no client certificate, a client CN for another role, plaintext. Every key is generated in the test's
temporary directory and removed with it. Valkey is not installed on this machine; its server-side negative cases are not run
here (the configuration check is)."""
import json
import os
import re
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import issue_certs  # noqa: E402
from nexloop_eios import deploy_checks  # noqa: E402
import conftest  # noqa: E402

PG_BIN = pathlib.Path(os.environ.get('NEXLOOP_TEST_PG_BIN') or conftest.DEFAULT_PG_BIN)


@pytest.fixture(scope='module')
def pki():
    root = pathlib.Path(tempfile.mkdtemp(prefix='nexloop-pki-'))
    try:
        issue_certs.root(root / 'root')
        issue_certs.intermediate(root / 'root', root / 'ca')
        issue_certs.leaf('server', root / 'ca', root / 'pg', 'pg-server', sans=['IP:127.0.0.1', 'DNS:localhost'])
        issue_certs.leaf('client', root / 'ca', root / 'clients', 'pg-client-nexloop_api', cn='nexloop_api')
        issue_certs.leaf('client', root / 'ca', root / 'clients', 'pg-client-nexloop_metrics', cn='nexloop_metrics')
        # Another, unrelated CA with a client certificate for the same role name.
        issue_certs.root(root / 'other-root')
        issue_certs.intermediate(root / 'other-root', root / 'other-ca')
        issue_certs.leaf('client', root / 'other-ca', root / 'other', 'pg-client-nexloop_api', cn='nexloop_api')
        issue_certs.leaf('server', root / 'other-ca', root / 'other', 'pg-server', sans=['IP:127.0.0.1'])
        yield root
    finally:
        assert root.name.startswith('nexloop-pki-') and root.parent == pathlib.Path(tempfile.gettempdir())
        shutil.rmtree(root)


@pytest.fixture(scope='module')
def tls_pg(pki):
    root = pathlib.Path(tempfile.mkdtemp(prefix='nexloop-pg-'))
    data, sock = root / 'data', root / 'socket'
    sock.mkdir()
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    subprocess.run([str(PG_BIN / 'initdb'), '-D', str(data), '-U', 'nexloop_bootstrap', '--auth=trust', '--no-locale', '-E', 'UTF8'], check=True, stdout=subprocess.DEVNULL)
    for name in ('pg-server.chain.crt', 'pg-server.key'):
        shutil.copy(pki / 'pg' / name, data / name)
        (data / name).chmod(0o600)
    shutil.copy(pki / 'ca' / 'bundle.crt', data / 'client-ca.crt')
    with open(data / 'postgresql.conf', 'a') as conf:
        conf.write("ssl = on\nssl_min_protocol_version = 'TLSv1.3'\nssl_cert_file = 'pg-server.chain.crt'\nssl_key_file = 'pg-server.key'\n"
                   "ssl_ca_file = 'client-ca.crt'\npassword_encryption = 'scram-sha-256'\n")
    # The stage pg_hba shape (deploy/stage/config/pg_hba.conf.template) with the loopback address standing in for APP_HOST.
    (data / 'pg_hba.conf').write_text('local all nexloop_bootstrap trust\n'
                                      'hostssl postgres nexloop_api,nexloop_metrics 127.0.0.1/32 scram-sha-256 clientcert=verify-full\n'
                                      'hostnossl all all 0.0.0.0/0 reject\nhost all all 0.0.0.0/0 reject\n')
    password = secrets.token_hex(16)
    started = False
    try:
        subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(data), '-l', str(root / 'pg.log'), '-o', f"-k {sock} -p {port} -c listen_addresses=127.0.0.1",
                        '-w', 'start'], check=True, stdout=subprocess.DEVNULL)
        started = True
        with psycopg.connect(f'host={sock} port={port} dbname=postgres user=nexloop_bootstrap', autocommit=True) as admin:
            for role in ('nexloop_api', 'nexloop_metrics'):
                admin.execute(psycopg.sql.SQL('create role {} login password {}').format(psycopg.sql.Identifier(role), psycopg.sql.Literal(password)))
        yield {'port': port, 'password': password}
    finally:
        if started:
            subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'], check=True, stdout=subprocess.DEVNULL)
        assert root.name.startswith('nexloop-pg-') and root.parent == pathlib.Path(tempfile.gettempdir())
        shutil.rmtree(root)


def dsn(pki, pg, *, user='nexloop_api', cert='clients/pg-client-nexloop_api', ca='root/ca.crt', host='127.0.0.1', sslmode='verify-full'):
    parts = [f'host={host}', 'hostaddr=127.0.0.1', f"port={pg['port']}", 'dbname=postgres', f'user={user}', f"password={pg['password']}",
             f'sslmode={sslmode}', f'sslrootcert={pki / ca}', 'connect_timeout=5']
    if cert:
        parts += [f'sslcert={pki / cert}.chain.crt', f'sslkey={pki / cert}.key']
    return ' '.join(parts)


def connects(conninfo):
    try:
        with psycopg.connect(conninfo) as c:
            return c.execute('select current_user').fetchone()[0]
    except psycopg.OperationalError as error:
        return 'refused:' + type(error).__name__


def test_issued_files_follow_d1(pki):
    for key in pki.rglob('*.key'):
        assert oct(key.stat().st_mode & 0o777) == '0o600', key.name
    text = subprocess.run(['openssl', 'x509', '-in', str(pki / 'pg/pg-server.crt'), '-noout', '-text'], capture_output=True, text=True).stdout
    assert 'IP Address:127.0.0.1' in text and 'TLS Web Server Authentication' in text and 'CA:FALSE' in text
    end = subprocess.run(['openssl', 'x509', '-in', str(pki / 'ca/ca.crt'), '-noout', '-text'], capture_output=True, text=True).stdout
    assert 'pathlen:0' in end
    # Validity limits of D1 are enforced, and a key is never overwritten.
    with pytest.raises(issue_certs.Refused, match='1..90'):
        issue_certs.leaf('server', pki / 'ca', pki / 'x', 'too-long', sans=['DNS:localhost'], days=91)
    with pytest.raises(issue_certs.Refused, match='1..365'):
        issue_certs.intermediate(pki / 'root', pki / 'ca2', days=400)
    with pytest.raises(issue_certs.Refused, match='key exists'):
        issue_certs.leaf('client', pki / 'ca', pki / 'clients', 'pg-client-nexloop_api', cn='nexloop_api')
    with pytest.raises(issue_certs.Refused, match='SAN'):
        issue_certs.leaf('server', pki / 'ca', pki / 'y', 'bad-san', sans=['URI:x'])


def test_postgres_accepts_only_verify_full_with_the_roles_own_client_certificate(pki, tls_pg):
    assert connects(dsn(pki, tls_pg)) == 'nexloop_api'
    negatives = {
        'wrong CA on the client': dsn(pki, tls_pg, ca='other-root/ca.crt'),
        'SAN mismatch': dsn(pki, tls_pg, host='wrong-name.invalid'),
        'client certificate from another CA': dsn(pki, tls_pg, cert='other/pg-client-nexloop_api'),
        'no client certificate': dsn(pki, tls_pg, cert=None),
        'client certificate of another role': dsn(pki, tls_pg, cert='clients/pg-client-nexloop_metrics'),
        'plaintext': dsn(pki, tls_pg, sslmode='disable', cert=None),
        'wrong password': dsn(pki, tls_pg).replace(tls_pg['password'], 'x' * 32),
    }
    for case, conninfo in negatives.items():
        assert connects(conninfo).startswith('refused:'), case
    assert connects(dsn(pki, tls_pg, user='nexloop_metrics', cert='clients/pg-client-nexloop_metrics')) == 'nexloop_metrics'


def test_doctor_certificate_check(pki, tmp_path):
    ca = pki / 'root/ca.crt'
    good = deploy_checks.check_certificate(chain=pki / 'pg/pg-server.chain.crt', key=pki / 'pg/pg-server.key', ca=ca, purpose='server', names=['127.0.0.1', 'localhost'])
    assert good['passed'] is True and good['problems'] == [] and 80 <= good['days_left'] <= 90
    assert deploy_checks.check_certificate(chain=pki / 'pg/pg-server.chain.crt', key=pki / 'pg/pg-server.key', ca=ca, purpose='server',
                                           names=['data.stage.invalid'])['problems'] == ['chain_or_name_not_verified:expected_name_0']
    assert deploy_checks.check_certificate(chain=pki / 'other/pg-server.chain.crt', key=pki / 'other/pg-server.key', ca=ca, purpose='server',
                                           names=['127.0.0.1'])['passed'] is False
    assert deploy_checks.check_certificate(chain=pki / 'pg/pg-server.chain.crt', key=pki / 'other/pg-server.key', ca=ca, purpose='server',
                                           names=['127.0.0.1'])['problems'] == ['key_does_not_match_certificate']
    client = deploy_checks.check_certificate(chain=pki / 'clients/pg-client-nexloop_api.chain.crt', key=pki / 'clients/pg-client-nexloop_api.key', ca=ca,
                                             purpose='client', cn='nexloop_api')
    assert client['passed'] is True
    assert deploy_checks.check_certificate(chain=pki / 'clients/pg-client-nexloop_api.chain.crt', key=pki / 'clients/pg-client-nexloop_api.key', ca=ca,
                                           purpose='client', cn='nexloop_metrics')['problems'] == ['client_cn_mismatch']
    assert 'client_chain_not_verified' in deploy_checks.check_certificate(chain=pki / 'other/pg-client-nexloop_api.chain.crt',
        key=pki / 'other/pg-client-nexloop_api.key', ca=ca, purpose='client', cn='nexloop_api')['problems']
    # Validity: < 30 days warns, < 7 days fails.
    later = datetime.now(UTC) + timedelta(days=70)
    assert deploy_checks.check_certificate(chain=pki / 'pg/pg-server.chain.crt', key=pki / 'pg/pg-server.key', ca=ca, purpose='server', names=['127.0.0.1'],
                                           now=later)['warnings'] == ['expires_within_30_days']
    assert 'expires_within_7_days' in deploy_checks.check_certificate(chain=pki / 'pg/pg-server.chain.crt', key=pki / 'pg/pg-server.key', ca=ca,
        purpose='server', names=['127.0.0.1'], now=datetime.now(UTC) + timedelta(days=85))['problems']
    # Key file: permissions, symlink.
    loose = tmp_path / 'loose.key'
    loose.write_bytes((pki / 'pg/pg-server.key').read_bytes())
    loose.chmod(0o644)
    assert 'key_permissions' in deploy_checks.check_certificate(chain=pki / 'pg/pg-server.chain.crt', key=loose, ca=ca, purpose='server', names=['127.0.0.1'])['problems']
    link = tmp_path / 'link.key'
    link.symlink_to(pki / 'pg/pg-server.key')
    assert 'key_not_regular_file' in deploy_checks.check_certificate(chain=pki / 'pg/pg-server.chain.crt', key=link, ca=ca, purpose='server', names=['127.0.0.1'])['problems']


def test_doctor_dsn_and_valkey_checks(pki, tmp_path, capsys):
    full = 'host=db.stage.invalid port=55432 dbname=nexloop_stage user=nexloop_api sslmode=verify-full sslrootcert=/run/secrets/ca sslcert=/run/secrets/c sslkey=/run/secrets/k'
    assert deploy_checks.check_dsn(full) == []
    assert deploy_checks.check_dsn(full.replace('verify-full', 'require')) == ['sslmode_not_verify_full']
    assert deploy_checks.check_dsn(full.replace('verify-full', 'verify-ca')) == ['sslmode_not_verify_full']
    assert deploy_checks.check_dsn(full.replace(' sslmode=verify-full', '')) == ['sslmode_not_verify_full']
    assert 'missing_sslcert' in deploy_checks.check_dsn(full.replace(' sslcert=/run/secrets/c', ''))
    valkey = (ROOT / 'deploy/stage/config/valkey.conf.template').read_text()
    assert deploy_checks.check_valkey_conf(valkey) == []
    assert deploy_checks.check_valkey_conf(valkey.replace('port 0', 'port 6379')) == ['plaintext_port_open']
    assert deploy_checks.check_valkey_conf(valkey.replace('tls-auth-clients yes', 'tls-auth-clients no')) == ['client_certificates_not_required']
    # Through the doctor CLI: a spec of certificate, DSN files and the Valkey configuration; names and codes only in the output.
    good, weak = tmp_path / 'api.dsn', tmp_path / 'scheduler.dsn'
    good.write_text(full)
    weak.write_text(full.replace('verify-full', 'require'))
    for f in (good, weak):
        f.chmod(0o600)
    spec = {'ca': str(pki / 'root/ca.crt'), 'valkey_conf': str(ROOT / 'deploy/stage/config/valkey.conf.template'),
            'certificates': [{'name': 'pg-server', 'chain': str(pki / 'pg/pg-server.chain.crt'), 'key': str(pki / 'pg/pg-server.key'), 'purpose': 'server',
                              'names': ['127.0.0.1']}],
            'dsn_files': [{'name': 'api', 'path': str(good)}, {'name': 'scheduler', 'path': str(weak)}]}
    spec_file = tmp_path / 'tls.json'
    spec_file.write_text(json.dumps(spec))
    from nexloop_eios.doctor import main
    sys.argv = ['nexloop-doctor', '--tls-spec', str(spec_file)]
    assert main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out['checks'] == {'tls_certificates': True, 'tls_dsn_verify_full': False, 'tls_valkey_only': True}
    assert out['details']['tls']['dsn'] == {'api': [], 'scheduler': ['sslmode_not_verify_full']}
    assert str(pki) not in json.dumps(out) and 'db.stage.invalid' not in json.dumps(out)


def test_no_private_key_or_certificate_is_tracked_in_the_repository():
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split('\0')
    suspicious = [p for p in tracked if p.endswith(('.key', '.pem', '.p12', '.pfx', '.crt', '.srl', '.csr'))]
    assert suspicious == [], suspicious
    # A PEM private key block in any tracked text file (scanners name the marker without a full block).
    block = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n[A-Za-z0-9+/=\s]{64,}')
    for path in tracked:
        p = ROOT / path
        if p.is_file() and p.stat().st_size < 2_000_000:
            try:
                text = p.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            assert not block.search(text), path
