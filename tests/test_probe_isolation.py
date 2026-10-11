"""NX-032 §3.3 (AT-058): the isolation probe script, shared by the simulation and the real deployment. PostgreSQL probes run against a
disposable TLS PostgreSQL 18 (test_deploy_tls: TLS 1.3, SCRAM, clientcert=verify-full); Valkey probes against an in-process TLS
stand-in that answers like Valkey (no Valkey here; the real server runs in scripts/deploy/netns_sim). Synthetic certificates."""
import json
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import probe_isolation as probe  # noqa: E402
from test_deploy_tls import pki, tls_pg  # noqa: E402,F401


def pg_probe(pki, code, expect, **kw):
    base = {'code': code, 'kind': 'pg', 'target': 'DATA_PG', 'expect': expect, 'ca': str(pki / 'root/ca.crt'), 'server_name': '127.0.0.1',
            'cert': str(pki / 'clients/pg-client-nexloop_api.chain.crt'), 'key': str(pki / 'clients/pg-client-nexloop_api.key'), 'user': 'nexloop_api'}
    return {**base, **kw}


def test_postgres_probes_classify_every_refusal(pki, tls_pg, tmp_path):
    password = tmp_path / 'pw'
    password.write_text(tls_pg['password'])
    wrong = tmp_path / 'wrong'
    wrong.write_text('x' * 32)
    spec = {'location': 'APP_HOST', 'targets': {'DATA_PG': {'host': '127.0.0.1', 'port': tls_pg['port']}}, 'probes': [
        pg_probe(pki, 'pg_ok', 'authenticated', password_file=str(password)),
        pg_probe(pki, 'pg_reaches_auth', 'reached_authentication'),
        pg_probe(pki, 'pg_plaintext', 'server_refused', sslmode='disable'),
        pg_probe(pki, 'pg_wrong_ca', 'client_refused', ca=str(pki / 'other-root/ca.crt')),
        pg_probe(pki, 'pg_san_mismatch', 'client_refused', server_name='wrong-name.invalid'),
        pg_probe(pki, 'pg_no_client_cert', 'server_refused', cert=None, key=None),
        pg_probe(pki, 'pg_other_ca_client_cert', 'server_refused', cert=str(pki / 'other/pg-client-nexloop_api.chain.crt'), key=str(pki / 'other/pg-client-nexloop_api.key')),
        # The CN is checked against the role after the password (clientcert=verify-full), and PostgreSQL then answers 28P01 like
        # a wrong password. The probe brings the right password (pg_ok proves it), so the refusal is the certificate's.
        pg_probe(pki, 'pg_other_role_cert', 'auth_failed', cert=str(pki / 'clients/pg-client-nexloop_metrics.chain.crt'),
                 key=str(pki / 'clients/pg-client-nexloop_metrics.key'), password_file=str(password)),
        pg_probe(pki, 'pg_wrong_password', 'auth_failed', password_file=str(wrong)),
    ]}
    report = probe.run(spec)
    assert report['passed'] is True, [(r['probe'], r['actual'], r['reason']) for r in report['results'] if not r['passed']]
    reasons = {r['probe']: r['reason'] for r in report['results']}
    assert reasons['pg_ok'] == 'scram_sha_256' and reasons['pg_plaintext'] == 'sqlstate_28000' and reasons['pg_san_mismatch'] == 'certificate_not_verified'
    assert reasons['pg_wrong_password'] == 'sqlstate_28P01' and reasons['pg_other_role_cert'] == 'sqlstate_28P01'
    text = json.dumps(report)
    assert '127.0.0.1' not in text and str(pki) not in text and tls_pg['password'] not in text and str(tls_pg['port']) not in text


def test_tcp_probes(tmp_path):
    with socket.socket() as listening:
        listening.bind(('127.0.0.1', 0))
        listening.listen()
        open_port = listening.getsockname()[1]
        closed = socket.socket()
        closed.bind(('127.0.0.1', 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        spec = {'location': 'OUTSIDER', 'targets': {'GATEWAY': {'host': '127.0.0.1', 'port': open_port}, 'APP_API': {'host': '127.0.0.1', 'port': closed_port},
                                                    'NOWHERE': {'host': 'nowhere.invalid', 'port': 5432}},
                'probes': [{'code': 'gateway_reachable', 'kind': 'tcp', 'target': 'GATEWAY', 'expect': 'reachable'},
                           {'code': 'api_internal', 'kind': 'tcp', 'target': 'APP_API', 'expect': 'unreachable'},
                           {'code': 'unknown_host', 'kind': 'tcp', 'target': 'NOWHERE', 'expect': 'unreachable'}]}
        report = probe.run(spec)
    assert report['passed'] is True and [r['reason'] for r in report['results']] == ['connected', 'refused', 'dns']


class FakeValkey:
    """A TLS listener that requires a client certificate and answers AUTH/PING like Valkey with ACLs (default user disabled)."""

    def __init__(self, pki, password):
        self.password = password
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(pki / 'pg/pg-server.chain.crt', pki / 'pg/pg-server.key')
        self.ctx.load_verify_locations(pki / 'ca/bundle.crt')
        self.ctx.verify_mode = ssl.CERT_REQUIRED
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(raw,), daemon=True).start()

    def handle(self, raw):
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        authed = False
        try:
            buf = b''
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                parts = buf.split(b'\r\n')
                if not buf.startswith(b'*') or len(parts) < 2 * int(parts[0][1:]) + 1:
                    continue
                args = [p.decode() for p in parts[2::2][:int(parts[0][1:])]]
                buf = b''
                if args[0] == 'AUTH':
                    authed = args[2] == self.password
                    conn.sendall(b'+OK\r\n' if authed else b'-WRONGPASS invalid username-password pair\r\n')
                elif args[0] == 'PING':
                    conn.sendall(b'+PONG\r\n' if authed else b'-NOAUTH Authentication required.\r\n')
        except (ssl.SSLError, OSError):
            return
        finally:
            conn.close()


def test_valkey_probes_classify_tls_and_acl_refusals(pki, tmp_path):
    good, bad = tmp_path / 'good', tmp_path / 'bad'
    good.write_text('cache-secret')
    bad.write_text('wrong')
    server = FakeValkey(pki, 'cache-secret')
    base = {'kind': 'valkey', 'target': 'DATA_VALKEY', 'ca': str(pki / 'root/ca.crt'), 'server_name': '127.0.0.1',
            'cert': str(pki / 'clients/pg-client-nexloop_api.chain.crt'), 'key': str(pki / 'clients/pg-client-nexloop_api.key')}
    spec = {'location': 'APP_HOST', 'targets': {'DATA_VALKEY': {'host': '127.0.0.1', 'port': server.port}}, 'probes': [
        {**base, 'code': 'valkey_ok', 'expect': 'pong', 'user': 'nexloop-cache', 'password_file': str(good)},
        {**base, 'code': 'valkey_default_user', 'expect': 'acl_refused'},
        {**base, 'code': 'valkey_wrong_password', 'expect': 'acl_refused', 'user': 'nexloop-cache', 'password_file': str(bad)},
        {**base, 'code': 'valkey_no_client_cert', 'expect': 'server_refused', 'cert': None, 'key': None},
        {**base, 'code': 'valkey_wrong_ca', 'expect': 'client_refused', 'ca': str(pki / 'other-root/ca.crt')},
        {**base, 'code': 'valkey_plaintext', 'expect': 'server_refused', 'plaintext': True},
    ]}
    report = probe.run(spec)
    assert report['passed'] is True, [r for r in report['results'] if not r['passed']]
    assert {r['probe']: r['reason'] for r in report['results']}['valkey_wrong_password'] == 'wrongpass'


def test_cli_report_file_and_exit_codes(tmp_path):
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'location': 'HOST', 'targets': {'X': {'host': 'nowhere.invalid', 'port': 1}},
                                'probes': [{'code': 'x', 'kind': 'tcp', 'target': 'X', 'expect': 'reachable'}]}))
    script = ROOT / 'scripts/deploy/probe_isolation.py'
    run = subprocess.run([sys.executable, '-I', str(script), '--spec', str(spec), '--out', str(tmp_path / 'r.json')], capture_output=True, text=True)
    assert run.returncode == 1 and json.loads((tmp_path / 'r.json').read_text())['results'][0]['actual'] == 'unreachable'
    assert oct((tmp_path / 'r.json').stat().st_mode & 0o777) == '0o600'
    bad = tmp_path / 'bad.json'
    bad.write_text('{}')
    assert subprocess.run([sys.executable, '-I', str(script), '--spec', str(bad)], capture_output=True, text=True).returncode == 2
