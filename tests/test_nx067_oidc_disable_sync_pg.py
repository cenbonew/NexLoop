"""NX-067 disable sync (docs/design/nx067-oidc-group-roles-and-disable-sync.md §3, Z3) on PostgreSQL through the actual app and the
in-process IdP (nothing leaves the process): an SSO session ends at most the provider's max_session_seconds after sign-in (a password
session keeps its 30 days); a valid back-channel logout token ends every session of the linked member at once; every invalid, replayed or
unlinked token changes nothing; the endpoint exists only for a provider that enabled it, is rate limited and fails closed without its
policy. The JWKS refetch for an unknown kid is rate limited per provider and both keys of a rotation verify."""
from datetime import datetime, timedelta
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from nexloop_eios import workbench_oidc as O
from support.mock_oidc_idp import MockIdp, cached_keypair, jws, _int_b64
from test_browser_http import ORIGIN
from test_workbench_oidc_pg import app, link, sign_in, private, sso  # noqa: F401
from test_workbench_read_pg import workbench_login
from test_real_dispatch_switch_pg import switching  # noqa: F401
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

SUBJECT = 'idp-sub-nx067-operator'
PATH = '/api/v1/workbench/auth/oidc/backchannel/corp'


def provider_file(c, **changes):
    document = {'version': 1, 'providers': [{**c['idp'].provider(secret_file=c['providers'].parent / 'oidc-client-secret'), **changes}]}
    c['providers'].write_text(json.dumps(document))


def linked_member(c, admin, client, owner_client):
    csrf = workbench_login(owner_client, c['owner_member']).json()['csrf_token']
    assert link(owner_client, csrf, c['operator_member']['principal_id'], SUBJECT).status_code == 200
    done = sign_in(client, c['idp'], SUBJECT)
    assert done.status_code == 303 and done.headers['location'] == '/workbench', done.text
    assert client.get('/api/v1/workbench/auth/session').status_code == 200


def post(client, token, **extra):
    return client.post(PATH, content=urlencode({'logout_token': token, **extra}), headers={'content-type': 'application/x-www-form-urlencoded'})


def failures(admin):
    return [r for (r,) in admin.execute("select payload->>'reason' from runtime.audit_events where action='nexloop.workbench_member.sso_login_failed' order by created_at")]


def test_an_sso_session_ends_at_the_providers_maximum_and_a_password_session_keeps_its_30_days(sso, admin):
    c = sso
    provider_file(c, max_session_seconds=900)
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member, TestClient(app(c), base_url=ORIGIN) as password:
        linked_member(c, admin, member, owner)
        assert workbench_login(password, c['operator_member']).status_code == 200
    rows = admin.execute("""select payload->'authentication_methods',(payload->>'absolute_expires_at')::timestamptz-(payload->>'created_at')::timestamptz,
        (payload->>'idle_expires_at')::timestamptz-(payload->>'created_at')::timestamptz from control.nexloop_browser_sessions
        where payload->>'principal_id'=%s order by payload->>'created_at'""", (c['operator_member']['principal_id'],)).fetchall()
    assert rows == [(['oidc'], timedelta(seconds=900), timedelta(seconds=900)), (['password'], timedelta(days=30), timedelta(days=7))]
    event = admin.execute("select details from control.nexloop_browser_login_events where event_type='oidc_login'").fetchone()[0]
    assert event['max_session_seconds'] == 900
    # Only the bounded function creates SSO sessions now.
    assert admin.execute("""select has_function_privilege('nexloop_identity','control.nexloop_create_workbench_oidc_session(text,text,text,text,text,text,jsonb,bytea,bytea,text)','execute'),
        has_function_privilege('nexloop_identity','control.nexloop_create_workbench_oidc_session_bounded(text,text,text,text,text,text,jsonb,bytea,bytea,text,integer,text,jsonb)','execute')""").fetchone() == (False, True)


def test_the_default_maximum_is_eight_hours():
    provider = O.Provider(provider_id='corp', issuer='https://idp.example.test/r', client_id='c', authorization_endpoint='https://idp.example.test/a',
                          token_endpoint='https://idp.example.test/t', jwks_uri='https://idp.example.test/j', client_secret_env='X')
    assert provider.max_session_seconds == O.MAX_SESSION_SECONDS_DEFAULT == 28800 and provider.backchannel_logout is False


@pytest.mark.parametrize('value', [899, 43201, '900', True, None])
def test_a_maximum_outside_900_to_43200_is_refused(value):
    with pytest.raises(O.OidcConfigurationError):
        O.Provider(provider_id='corp', issuer='https://idp.example.test/r', client_id='c', authorization_endpoint='https://idp.example.test/a',
                   token_endpoint='https://idp.example.test/t', jwks_uri='https://idp.example.test/j', client_secret_env='X', max_session_seconds=value)


def test_a_valid_logout_token_ends_every_session_of_the_member_at_once(sso, admin):
    c, idp = sso, sso['idp']
    provider_file(c, backchannel_logout=True)
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member, \
            TestClient(app(c), base_url=ORIGIN) as password, TestClient(app(c), base_url=ORIGIN) as idp_server:
        linked_member(c, admin, member, owner)
        assert workbench_login(password, c['operator_member']).status_code == 200
        answer = post(idp_server, idp.logout_token(SUBJECT))
        assert answer.status_code == 200 and answer.json() == {} and answer.headers['cache-control'] == 'no-store'
        assert member.get('/api/v1/workbench/auth/session').status_code == 401
        assert password.get('/api/v1/workbench/auth/session').status_code == 401  # design 3.3: password sessions end too
        assert owner.get('/api/v1/workbench/auth/session').status_code == 200  # nobody else
        # The member signs in again (the link stays): a logout is not an unlink.
        assert sign_in(member, idp, SUBJECT).headers['location'] == '/workbench'
    event = admin.execute("select details,request_id from control.nexloop_browser_login_events where event_type='oidc_backchannel_logout'").fetchone()
    assert event[0] == {'provider_id': 'corp', 'principal_id': c['operator_member']['principal_id']} and SUBJECT not in json.dumps(event)
    stored = admin.execute('select jti_hmac from control.nexloop_workbench_oidc_logout_jti').fetchall()
    assert len(stored) == 1 and len(stored[0][0]) == 64


def other_key():
    return cached_keypair('other')


INVALID = {
    'signature': lambda idp: idp.logout_token(SUBJECT, sign_with=other_key()),
    'alg': lambda idp: idp.logout_token(SUBJECT, header={'alg': 'HS256', 'kid': idp.kid}),
    'issuer': lambda idp: idp.logout_token(SUBJECT, iss='https://elsewhere.example.test/realm'),
    'audience': lambda idp: idp.logout_token(SUBJECT, aud='another-client'),
    'no_events': lambda idp: idp.logout_token(SUBJECT, events=None),
    'wrong_event': lambda idp: idp.logout_token(SUBJECT, events={'http://schemas.openid.net/event/other': {}}),
    'event_not_object': lambda idp: idp.logout_token(SUBJECT, events={O.BACKCHANNEL_EVENT: True}),
    'nonce': lambda idp: idp.logout_token(SUBJECT, nonce='n'),
    'sid_only': lambda idp: idp.logout_token(None, sid='idp-session-1'),
    'old': lambda idp: idp.logout_token(SUBJECT, iat=int(time.time()) - 700, exp=None),
    'future': lambda idp: idp.logout_token(SUBJECT, iat=int(time.time()) + 300),
    'expired': lambda idp: idp.logout_token(SUBJECT, exp=int(time.time()) - 120),
    'no_jti': lambda idp: idp.logout_token(SUBJECT, jti=None),
    'empty_jti': lambda idp: idp.logout_token(SUBJECT, jti=''),
}


def test_invalid_tokens_are_400_and_end_nothing(sso, admin):
    c, idp = sso, sso['idp']
    provider_file(c, backchannel_logout=True)
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member, TestClient(app(c), base_url=ORIGIN) as idp_server:
        linked_member(c, admin, member, owner)
        for name, make in INVALID.items():
            answer = post(idp_server, make(idp))
            assert answer.status_code == 400 and answer.json() == {'error': 'invalid_request'}, name
        # Malformed requests: a second field, a repeated token, another content type, an oversized body.
        token = idp.logout_token(SUBJECT)
        assert post(idp_server, token, extra='1').status_code == 400
        assert idp_server.post(PATH, content=f'logout_token={token}&logout_token={token}', headers={'content-type': 'application/x-www-form-urlencoded'}).status_code == 400
        assert idp_server.post(PATH, json={'logout_token': token}).status_code == 400
        assert idp_server.post(PATH, content='logout_token=' + 'a' * 17000, headers={'content-type': 'application/x-www-form-urlencoded'}).status_code == 400
        assert member.get('/api/v1/workbench/auth/session').status_code == 200
    # Audited: every invalid token, the extra field and the repeated token (the content type and the size are refused before parsing).
    assert failures(admin) == ['logout_token_invalid'] * (len(INVALID) + 2)
    assert admin.execute("select count(*) from control.nexloop_browser_login_events where event_type='oidc_backchannel_logout'").fetchone() == (0,)


def test_a_replayed_token_is_400_and_an_unlinked_subject_is_200_with_no_effect(sso, admin):
    c, idp = sso, sso['idp']
    provider_file(c, backchannel_logout=True)
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member, TestClient(app(c), base_url=ORIGIN) as idp_server:
        linked_member(c, admin, member, owner)
        nobody = post(idp_server, idp.logout_token('idp-sub-nobody'))
        assert nobody.status_code == 200 and member.get('/api/v1/workbench/auth/session').status_code == 200
        token = idp.logout_token(SUBJECT)
        assert post(idp_server, token).status_code == 200
        assert sign_in(member, idp, SUBJECT).headers['location'] == '/workbench'
        assert post(idp_server, token).status_code == 400  # the same jti again: nothing ends
        assert member.get('/api/v1/workbench/auth/session').status_code == 200
    assert failures(admin) == ['logout_token_invalid']


def test_the_endpoint_is_404_unless_the_provider_enabled_it(sso, admin):
    c, idp = sso, sso['idp']
    with TestClient(app(c), base_url=ORIGIN) as idp_server:
        assert post(idp_server, idp.logout_token(SUBJECT)).status_code == 404
        provider_file(c, backchannel_logout=True)
    with TestClient(app(c), base_url=ORIGIN) as idp_server:
        assert idp_server.post('/api/v1/workbench/auth/oidc/backchannel/other', content='logout_token=x',
                               headers={'content-type': 'application/x-www-form-urlencoded'}).status_code == 404


def test_the_endpoint_is_rate_limited_and_fails_closed_without_the_policy(sso, admin):
    c, idp = sso, sso['idp']
    provider_file(c, backchannel_logout=True)
    admin.execute("update control.nexloop_browser_rate_policies set maximum_attempts=2 where tenant_id=%s and action='oidc_start'", (TENANT,))
    with TestClient(app(c), base_url=ORIGIN) as idp_server:
        assert [post(idp_server, idp.logout_token('idp-sub-nobody')).status_code for _ in range(3)] == [200, 200, 429]
    admin.execute("delete from control.nexloop_browser_rate_policies where tenant_id=%s and action='oidc_start'", (TENANT,))
    with TestClient(app(c), base_url=ORIGIN) as idp_server:
        assert post(idp_server, idp.logout_token('idp-sub-nobody')).status_code == 503


class Jwks:
    """A JWKS endpoint that counts its fetches and serves whatever keys the test sets."""

    def __init__(self, idp):
        self.idp, self.fetches, self.keys = idp, 0, [(idp.key, idp.kid)]

    def handle(self, request):
        self.fetches += 1
        return httpx.Response(200, json={'keys': [{'kty': 'RSA', 'use': 'sig', 'alg': 'RS256', 'kid': kid, 'n': _int_b64(k[0]), 'e': _int_b64(k[1])}
                                                  for k, kid in self.keys]})


def test_z3_unknown_kids_refetch_at_most_once_a_minute_and_both_rotation_keys_verify():
    idp = MockIdp()
    provider = O.Provider(**{**idp.provider(), 'client_secret_file': None, 'client_secret_env': 'UNUSED'})
    jwks, now = Jwks(idp), [1000.0]
    client = O.OidcClient({'corp': provider}, transport=httpx.MockTransport(jwks.handle), clock=lambda: now[0])
    keys = client.keys(provider)
    assert keys(idp.kid) and jwks.fetches == 1
    # Forged kids within the interval: no fetch at all.
    for i in range(20):
        assert keys(f'forged-{i}') == []
    assert jwks.fetches == 1
    # Rotation: the IdP publishes the new key beside the old one; after the interval the new kid fetches once and both verify.
    new = cached_keypair('rotated')
    jwks.keys = [(idp.key, idp.kid), (new, 'kid-new')]
    now[0] += O.JWKS_MIN_REFETCH_SECONDS + 1
    assert keys('kid-new') and jwks.fetches == 2 and keys(idp.kid)
    token_old = idp.logout_token(SUBJECT)
    token_new = jws(new, 'kid-new', json.loads(O._unb64(token_old.split('.')[1])))
    for token in (token_old, token_new):
        assert O.verify_logout_token(token, provider=provider, keys=keys, now=time.time())['subject'] == SUBJECT
    # The old key leaves the JWKS: after the cache lifetime the next lookup refetches and the old kid no longer verifies.
    jwks.keys = [(new, 'kid-new')]
    now[0] += O.JWKS_CACHE_SECONDS + 1
    with pytest.raises(O.OidcFailure):
        O.verify_logout_token(token_old, provider=provider, keys=keys, now=time.time())
    assert jwks.fetches == 3
