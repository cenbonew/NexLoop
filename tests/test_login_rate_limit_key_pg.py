"""L6 C11 (2026-10-22): the login rate limit's keys.

Behind the gateway every browser login reached the API from loopback and the limit keyed on request.client.host only, so one source's
attempts could lock the whole tenant out. Now: the client address from the gateway's X-NexLoop-Client-IP, trusted only from a
configured proxy peer (eios.identity.client_ip, vendored), never a client-supplied header; and a second dimension per account name
(NFKC, trimmed, case-folded). Either over the limit is 429. Real identity PostgreSQL, the actual application; synthetic accounts only
(addresses from the RFC 5737 documentation ranges).
"""
import secrets

import pytest
from fastapi.testclient import TestClient

from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app
from support.tenants import TENANT_A
from test_browser_identity_reads import identity  # noqa: F401

ORIGIN = 'https://synthetic.example'
GATEWAY = ('127.0.0.1', 50000)
HEADER = 'X-NexLoop-Client-IP'
LIMIT = 3


@pytest.fixture
def apps(identity, admin, tmp_path):  # noqa: F811
    dsn = tmp_path / 'identity-dsn'
    dsn.write_text(identity[0])
    dsn.chmod(0o600)
    key = tmp_path / 'rate-key'
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',%s,60,true)", (TENANT_A, LIMIT))

    def app(peers=()):
        browser = BrowserConfiguration(dsn, key, TENANT_A, 'synthetic-browser-app', ORIGIN, trusted_proxy_peers=tuple(peers))
        return create_app(ApiConfiguration(tmp_path / 'missing', tmp_path / 'missing-key', tmp_path / 'unused', 'missing', browser))
    return {'proxied': app(('127.0.0.1',)), 'direct': app(), 'identity': identity}


def login(app, username, password, *, peer=GATEWAY, headers=None):
    with TestClient(app, base_url=ORIGIN, client=peer) as client:
        return client.post('/api/v1/auth/login', headers={'Origin': ORIGIN, **(headers or {})}, json={'username': username, 'password': password}).status_code


def test_two_sources_behind_the_gateway_do_not_share_a_bucket(apps):
    a = apps
    account = a['identity'][3].username
    for n in range(LIMIT):
        assert login(a['proxied'], f'nobody-{n}', 'wrong', headers={HEADER: '198.51.100.7'}) == 401
    assert login(a['proxied'], 'nobody-x', 'wrong', headers={HEADER: '198.51.100.7'}) == 429  # that source is limited
    # Another source through the same gateway (the same loopback peer) is not: before the fix both were one bucket.
    assert login(a['proxied'], account, a['identity'][-1], headers={HEADER: '198.51.100.8'}) == 200


def test_client_supplied_addresses_are_never_trusted(apps):
    a = apps
    # Directly (no trusted peer): the socket peer only; a changing X-NexLoop-Client-IP or X-Forwarded-For does not open new buckets.
    for n in range(LIMIT):
        assert login(a['direct'], f'nobody-d{n}', 'wrong', peer=('203.0.113.5', 40000),
                     headers={HEADER: f'198.51.100.{n + 10}', 'X-Forwarded-For': f'198.51.100.{n + 20}'}) == 401
    assert login(a['direct'], 'nobody-dx', 'wrong', peer=('203.0.113.5', 40000), headers={HEADER: '198.51.100.99'}) == 429
    # Behind the gateway X-Forwarded-For is ignored: the gateway's own header decides.
    for n in range(LIMIT):
        assert login(a['proxied'], f'nobody-p{n}', 'wrong', headers={HEADER: '198.51.100.30', 'X-Forwarded-For': f'198.51.100.{n + 40}'}) == 401
    assert login(a['proxied'], 'nobody-px', 'wrong', headers={HEADER: '198.51.100.30', 'X-Forwarded-For': '198.51.100.99'}) == 429
    # A peer that is not the gateway cannot speak for a client: refused (503), never keyed on its header.
    assert login(a['proxied'], 'nobody-q', 'wrong', peer=('203.0.113.9', 40000), headers={HEADER: '198.51.100.31'}) == 503
    # The gateway's header must be one canonical address.
    assert login(a['proxied'], 'nobody-r', 'wrong', headers={HEADER: '198.51.100.32, 198.51.100.33'}) == 503
    assert login(a['proxied'], 'nobody-s', 'wrong') == 503  # the gateway always sets it; without it nothing is keyed


def test_one_account_name_is_limited_across_sources_and_others_are_not(apps):
    a = apps
    account, password = a['identity'][3].username, a['identity'][-1]
    variants = [account.upper(), f'  {account} ', account]  # case and spaces fold into one account bucket
    for n, name in enumerate(variants):
        assert login(a['proxied'], name, 'wrong', headers={HEADER: f'198.51.100.{n + 50}'}) == 401
    assert login(a['proxied'], account, password, headers={HEADER: '198.51.100.60'}) == 429  # a fresh source: the name is limited
    # Another account from that same fresh source logs in (the address bucket holds 1 of 3).
    assert login(a['proxied'], 'nobody-else', 'wrong', headers={HEADER: '198.51.100.60'}) == 401


def test_the_stage_gateway_writes_the_header_and_the_api_trusts_only_it():
    from pathlib import Path
    import yaml
    stage = Path(__file__).resolve().parents[1] / 'deploy/stage'
    caddy = (stage / 'config/Caddyfile.template').read_text()
    assert f'header_up {HEADER} {{remote_host}}' in caddy and 'header_up -X-Forwarded-For' in caddy
    compose = yaml.safe_load((stage / 'compose.app.example.yaml').read_text())['services']
    apis = [s for s in compose.values() if s.get('labels', {}).get('nexloop.budget-service') == 'api,api-browser-identity'
            or (isinstance(s.get('command'), list) and '--identity-pool-max' in s['command'])]
    assert len(apis) == 2
    for spec in apis:
        command = [str(c) for c in spec['command']]
        assert command[command.index('--trusted-proxy-peer') + 1] == '127.0.0.1' and command.count('--trusted-proxy-peer') == 1
