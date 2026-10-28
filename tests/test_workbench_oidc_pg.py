"""NX-058 slice F (docs/design/account-self-service.md §5; owner 2026-10-28) on PostgreSQL through the actual app, against the in-process
IdP of tests/support/mock_oidc_idp.py (self-signed JWKS; nothing leaves the process): the owner links an operator's IdP subject by the
governed Action; the operator signs in by authorization code with PKCE, state and nonce; an unlinked subject, a tampered state, nonce or
signature, an expired token and a wrong audience are refused with the same redirect and an audit reason; sso_only stops the password and
reset codes (a member without it is unaffected); unlinking and revoking end the link. Synthetic members, keys and secrets only."""
from datetime import timedelta
import dataclasses
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import workbench_oidc as O
from nexloop_eios.http_api import create_app
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from support.mock_oidc_idp import MockIdp, cached_keypair
from support.single_clock import issued_once
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from nexloop_eios.workbench_roles import apply_members
from test_workbench_read_pg import ROLES, members_file, workbench_login

ACTIONS = ('nexloop.workbench_member.link_external', 'nexloop.workbench_member.unlink_external', 'nexloop.workbench_member.issue_reset',
           'nexloop.workbench_member.revoke')
START = '/api/v1/workbench/auth/oidc/start'
CALLBACK = '/api/v1/workbench/auth/oidc/callback'


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def sso(switching, admin, tmp_path):
    c = switching
    publish_registry_actions(admin, ACTIONS)
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'oidc_start','nexloop_identity',100,60,true) on conflict do nothing", (TENANT,))
    idp = MockIdp()
    secret = private(tmp_path / 'oidc-client-secret', idp.client_secret)
    providers = private(tmp_path / 'oidc-providers.json', json.dumps({'version': 1, 'providers': [idp.provider(secret_file=secret)]}))
    key = private(tmp_path / 'identity-token-key', secrets.token_hex(32))
    c['config'] = dataclasses.replace(c['config'], workbench=dataclasses.replace(c['config'].workbench, identity_token_key_file=key,
                                                                                    oidc_providers_file=providers))
    c['idp'], c['providers'] = idp, providers
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    return c


def app(c):
    a = create_app(c['config'])
    a.state.workbench_oidc = O.OidcClient(O.load_providers(c['providers']), transport=c['idp'].transport)
    return a


def sign_in(client, idp, subject, *, provider='corp', back=None):
    """Start, sign in at the IdP as `subject`, come back; returns the callback response (and the query sent back)."""
    started = client.get(START, params={'provider': provider}, follow_redirects=False)
    assert started.status_code == 302, started.text
    query = idp.authorize(started.headers['location'], subject)
    if back is not None:
        query = back(query)
    return client.get(CALLBACK, params=query, follow_redirects=False)


def failures(admin):
    return [r for (r,) in admin.execute("select payload->>'reason' from runtime.audit_events where action='nexloop.workbench_member.sso_login_failed' order by created_at")]


def link(client, csrf, principal, subject, mode='sso_or_password', provider='corp'):
    return act(client, csrf, 'link_workbench_external', {'target_principal': principal, 'provider_id': provider, 'external_subject': subject, 'login_mode': mode})


def test_a_linked_member_signs_in_by_sso_and_the_flow_is_one_time(sso, admin):
    c, idp = sso, sso['idp']
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(app(c), base_url=ORIGIN) as client, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(client, owner).json()['csrf_token']
        assert member.get('/api/v1/workbench/auth/oidc/providers').json() == {'providers': [{'provider_id': 'corp', 'display_name': 'Test IdP'}]}
        linked = link(client, csrf, operator['principal_id'], 'idp-sub-operator')
        assert linked.status_code == 200 and linked.json()['login_mode'] == 'sso_or_password', linked.json()
        started = member.get(START, params={'provider': 'corp'}, follow_redirects=False)
        location = started.headers['location']
        q = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert q['code_challenge_method'] == 'S256' and q['redirect_uri'] == ORIGIN + CALLBACK and O.valid_state(q['state'])
        assert started.headers['referrer-policy'] == 'no-referrer' and 'samesite=lax' in started.headers['set-cookie'].lower()
        # Only HMACs are stored: neither the state nor the browser binding is in the database.
        stored = admin.execute('select state_hmac,binding_hmac from control.nexloop_workbench_oidc_states').fetchall()
        assert len(stored) == 1 and q['state'] not in stored[0] and member.cookies.get('__Host-nexloop_workbench_oidc') not in stored[0]
        issued_once(admin, TENANT, 'control.nexloop_workbench_oidc_states', 'state_hmac', stored[0][0], timedelta(minutes=5), start='created_at')
        # Both times come from one statement and nothing else records the instant, so two adjacent readings often agree to the
        # microsecond: the deployed definition itself must read the clock once.
        assert admin.execute("""select (length(d)-length(replace(d,'clock_timestamp()','')))/length('clock_timestamp()')
            from pg_get_functiondef('control.nexloop_workbench_oidc_begin(text,text,text,text)'::regprocedure) d""").fetchone() == (1,)
        back = idp.authorize(location, 'idp-sub-operator')
        done = member.get(CALLBACK, params=back, follow_redirects=False)
        assert done.status_code == 303 and done.headers['location'] == '/workbench', done.text
        view = member.get('/api/v1/workbench/auth/session').json()
        assert view['principal_id'] == operator['principal_id'] and view['authenticated'] is True
        assert view['authentication_methods'] == ['oidc']  # L6 S1: an SSO session is told apart from a password one
        csrf_member = member.post('/api/v1/workbench/auth/csrf', headers={'Origin': ORIGIN})
        assert csrf_member.status_code == 200
        # The PKCE verifier reached the IdP and matched (the mock refuses otherwise); the secret went in the Authorization header only.
        assert idp.token_requests[-1]['code_verifier'] and 'client_secret' not in idp.token_requests[-1]
        event = admin.execute("select details from control.nexloop_browser_login_events where event_type='oidc_login'").fetchone()[0]
        assert event['provider_id'] == 'corp' and event['principal_id'] == operator['principal_id'] and len(event['provider_configuration_fingerprint']) == 64
        assert 'idp-sub-operator' not in json.dumps(event)
        session = admin.execute("select payload from control.nexloop_browser_sessions where payload->>'principal_id'=%s and payload->'authentication_methods'='[\"oidc\"]'",
                                (operator['principal_id'],)).fetchone()
        assert session is not None
        # The same callback again (the state is taken once): refused, no new session.
        before = admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0]
        again = member.get(CALLBACK, params=back, follow_redirects=False)
        assert again.status_code == 303 and again.headers['location'] == '/workbench?sso=failed'
        assert admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0] == before
        assert failures(admin) == ['state_invalid']
        # sso_or_password: the password still opens a session (and says so).
        assert workbench_login(member, operator).status_code == 200
        assert member.get('/api/v1/workbench/auth/session').json()['authentication_methods'] == ['password']
    audit = admin.execute("select payload from runtime.audit_events where action='nexloop.workbench_member.link_external'").fetchone()[0]
    assert audit == {'target_principal': operator['principal_id'], 'provider_id': 'corp', 'login_mode': 'sso_or_password'}


def test_an_unlinked_subject_is_refused_with_the_same_answer(sso, admin):
    c, idp = sso, sso['idp']
    with TestClient(app(c), base_url=ORIGIN) as member:
        refused = sign_in(member, idp, 'idp-sub-nobody')
        assert refused.status_code == 303 and refused.headers['location'] == '/workbench?sso=failed'
        assert '__Host-nexloop_workbench=' not in refused.headers.get('set-cookie', '')
        assert member.get('/api/v1/workbench/auth/session').status_code == 401
    assert failures(admin) == ['not_linked']
    # Nothing was created: no first-login account (design §5).
    assert admin.execute("select count(*) from control.nexloop_browser_login_events where event_type='oidc_login'").fetchone()[0] == 0


def test_tampered_state_nonce_signature_expiry_and_audience_are_refused(sso, admin):
    c, idp = sso, sso['idp']
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(app(c), base_url=ORIGIN) as client, TestClient(app(c), base_url=ORIGIN) as member, TestClient(app(c), base_url=ORIGIN) as other:
        csrf = workbench_login(client, owner).json()['csrf_token']
        assert link(client, csrf, operator['principal_id'], 'idp-sub-operator').status_code == 200

        def refused(response):
            assert response.status_code == 303 and response.headers['location'] == '/workbench?sso=failed', response.text
            assert member.get('/api/v1/workbench/auth/session').status_code == 401

        # state changed on the way back
        refused(sign_in(member, idp, 'idp-sub-operator', back=lambda q: {**q, 'state': O.new_state()}))
        # the right state in another browser (no binding cookie of this flow): login CSRF / fixation
        started = member.get(START, params={'provider': 'corp'}, follow_redirects=False)
        back = idp.authorize(started.headers['location'], 'idp-sub-operator')
        out = other.get(CALLBACK, params=back, follow_redirects=False)
        assert out.headers['location'] == '/workbench?sso=failed' and other.get('/api/v1/workbench/auth/session').status_code == 401
        # ... and in a browser holding its own binding cookie (of a flow it started itself): the database compares the binding
        started = member.get(START, params={'provider': 'corp'}, follow_redirects=False)
        back = idp.authorize(started.headers['location'], 'idp-sub-operator')
        assert other.get(START, params={'provider': 'corp'}, follow_redirects=False).status_code == 302
        out = other.get(CALLBACK, params=back, follow_redirects=False)
        assert out.headers['location'] == '/workbench?sso=failed' and other.get('/api/v1/workbench/auth/session').status_code == 401
        # the victim's own flow was spent by that attempt (a state is taken once, whoever brings it)
        refused(member.get(CALLBACK, params=back, follow_redirects=False))
        # the IdP reports an error instead of a code
        refused(sign_in(member, idp, 'idp-sub-operator', back=lambda q: {'state': q['state'], 'error': 'access_denied'}))
        # ID tokens: another nonce, another signing key, expired, another audience
        for tamper in ({'nonce': 'n-' + secrets.token_hex(4)}, {'sign_with': cached_keypair('attacker')},
                       {'exp': 1_700_000_000, 'iat': 1_699_999_000}, {'aud': 'another-client'}):
            idp.tamper = tamper
            refused(sign_in(member, idp, 'idp-sub-operator'))
        idp.tamper = {}
        assert sign_in(member, idp, 'idp-sub-operator').headers['location'] == '/workbench'
    assert failures(admin) == ['state_invalid'] * 4 + ['provider_error'] + ['id_token_invalid'] * 4
    # The audit names the provider and the reason only.
    for (payload,) in admin.execute("select payload from runtime.audit_events where action='nexloop.workbench_member.sso_login_failed'"):
        assert set(payload) == {'provider_id', 'reason'}


def test_sso_only_stops_the_password_and_reset_codes_and_others_are_unaffected(sso, admin):
    c, idp = sso, sso['idp']
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(app(c), base_url=ORIGIN) as client, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(client, owner).json()['csrf_token']
        # A member without sso_only: reset codes as before (also under sso_or_password).
        assert act(client, csrf, 'issue_workbench_reset', {'target_principal': operator['principal_id']}).status_code == 200
        assert link(client, csrf, operator['principal_id'], 'idp-sub-operator').status_code == 200
        assert act(client, csrf, 'issue_workbench_reset', {'target_principal': operator['principal_id']}).status_code == 200
        assert act(client, csrf, 'unlink_workbench_external', {'target_principal': operator['principal_id']}).status_code == 200
        assert workbench_login(member, operator).status_code == 200
        # sso_only: the password session ends, the password opens none, no reset code can be issued, unused ones are revoked.
        linked = link(client, csrf, operator['principal_id'], 'idp-sub-operator', mode='sso_only')
        assert linked.status_code == 200, linked.json()
        assert member.get('/api/v1/workbench/auth/session').status_code == 401
        assert workbench_login(member, operator).status_code == 401
        assert admin.execute("select count(*) from control.nexloop_workbench_reset_codes where state='issued'").fetchone()[0] == 0
        refused = act(client, csrf, 'issue_workbench_reset', {'target_principal': operator['principal_id']})
        assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'target_sso_only'}
        # SSO still works, and the owner (not linked) logs in with the password as before.
        assert sign_in(member, idp, 'idp-sub-operator').headers['location'] == '/workbench'
        assert member.get('/api/v1/workbench/auth/session').json()['principal_id'] == operator['principal_id']
        with TestClient(app(c), base_url=ORIGIN) as third:
            assert workbench_login(third, owner).status_code == 200
        # Unlinking ends the SSO session, SSO no longer signs the member in, and reset codes are possible again.
        assert act(client, csrf, 'unlink_workbench_external', {'target_principal': operator['principal_id']}).status_code == 200
        assert member.get('/api/v1/workbench/auth/session').status_code == 401
        assert sign_in(member, idp, 'idp-sub-operator').headers['location'] == '/workbench?sso=failed'
        assert act(client, csrf, 'issue_workbench_reset', {'target_principal': operator['principal_id']}).status_code == 200
    assert failures(admin) == ['not_linked']


def test_links_are_governed_owner_actions_for_operators_and_reviewers(sso, admin):
    c, idp = sso, sso['idp']
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(app(c), base_url=ORIGIN) as client, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(client, owner).json()['csrf_token']
        csrf_op = workbench_login(member, operator).json()['csrf_token']
        # Only the owner holds the Action; an owner target waits for an approval policy; the requester is never the target.
        assert link(member, csrf_op, owner['principal_id'], 'idp-sub-owner').status_code == 403
        assert link(client, csrf, owner['principal_id'], 'idp-sub-owner').json()['details'] == {'reason': 'target_is_requester'}
        # Only a configured provider; the issuer is the provider's (the body has none to give).
        unknown = link(client, csrf, operator['principal_id'], 'idp-sub-operator', provider='other')
        assert unknown.status_code == 409 and unknown.json()['details'] == {'reason': 'provider_unknown'}
        assert act(client, csrf, 'link_workbench_external', {'target_principal': operator['principal_id'], 'provider_id': 'corp',
                                                            'external_subject': 'x', 'login_mode': 'sso_only', 'issuer': 'https://evil.example.test'}).status_code == 422
        assert link(client, csrf, operator['principal_id'], 'idp-sub-operator').status_code == 200
        assert admin.execute('select issuer from control.nexloop_workbench_external_identities').fetchone()[0] == idp.issuer
        # One link per member and per IdP subject.
        assert link(client, csrf, operator['principal_id'], 'idp-sub-2').json()['details'] == {'reason': 'already_linked'}
        # Revoking the member removes its link in the same transaction (erasure then finds none).
        revoked = act(client, csrf, 'revoke_workbench_member', {'target_principal': operator['principal_id'], 'reason': 'synthetic offboarding'})
        assert revoked.status_code == 200, revoked.json()
    assert admin.execute('select count(*) from control.nexloop_workbench_external_identities').fetchone()[0] == 0
    assert [k for (k,) in admin.execute('select kind from control.nexloop_workbench_external_identity_events order by created_at')] == ['linked', 'member_revoked']
    # The link history never holds the IdP subject.
    assert 'idp-sub-operator' not in json.dumps(admin.execute('select json_agg(e) from control.nexloop_workbench_external_identity_events e').fetchone()[0])
    with pytest.raises(Exception):
        admin.execute('delete from control.nexloop_workbench_external_identity_events')


def test_sso_is_off_without_a_provider_file(sso, admin):
    c = sso
    config = dataclasses.replace(c['config'], workbench=dataclasses.replace(c['config'].workbench, oidc_providers_file=None))
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        assert client.get(START, params={'provider': 'corp'}, follow_redirects=False).status_code == 404
        assert client.get('/api/v1/workbench/auth/oidc/providers').json() == {'providers': []}
        assert client.get(CALLBACK, params={'state': O.new_state(), 'code': 'x'}, follow_redirects=False).status_code == 404
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert link(client, csrf, c['operator_member']['principal_id'], 'idp-sub-operator').json()['details'] == {'reason': 'provider_unknown'}
    assert admin.execute('select count(*) from control.nexloop_workbench_oidc_states').fetchone()[0] == 0


def test_identity_role_only_for_the_login_functions(sso, admin):
    login = ('control.nexloop_workbench_oidc_begin(text,text,text,text)', 'control.nexloop_workbench_oidc_take(text,text,text)',
             'control.nexloop_workbench_oidc_resolve(text,text,text,text,text)',
             'control.nexloop_create_workbench_oidc_session(text,text,text,text,text,text,jsonb,bytea,bytea,text)',
             'control.nexloop_workbench_oidc_failed(text,text,text,text)')
    internal = ('control.nexloop_governed_link_workbench_external(text,text,text,text,text,jsonb)',
                'control.nexloop_governed_unlink_workbench_external(text,text,text,text,text,jsonb)', 'control.nexloop_workbench_password_allowed(text,text)')
    can = lambda role, fn: admin.execute("select has_function_privilege(%s,%s,'execute')", (role, fn)).fetchone()[0]  # noqa: E731
    assert all(can('nexloop_identity', f) and not can('nexloop_api', f) for f in login)
    assert not any(can(r, f) for f in internal for r in ('nexloop_identity', 'nexloop_api'))
    # Any other session user (here the test administrator) is refused by the function itself.
    with pytest.raises(Exception):
        admin.execute("select control.nexloop_workbench_oidc_failed(%s,'corp','not_linked','t')", (TENANT,))


def test_a_promoted_member_loses_its_link_and_no_owner_signs_in_by_sso(sso, admin):
    """L6 F1: link first, promote later must not give an owner an unapproved SSO login. Promotion deletes the link in the same transaction
    (every path to owner writes the owner role's start in the role history; here the configurator's); the resolve refuses an owner even
    with a link left in place; an owner target can be unlinked (clean-up)."""
    c, idp = sso, sso['idp']
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(app(c), base_url=ORIGIN) as client, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(client, owner).json()['csrf_token']
        assert link(client, csrf, operator['principal_id'], 'idp-sub-operator').status_code == 200
        assert sign_in(member, idp, 'idp-sub-operator').headers['location'] == '/workbench'
        apply_members(ROLES, members_file((owner, 'owner'), (operator, 'owner')), database_url_file=c['configurator'])
        assert admin.execute('select count(*) from control.nexloop_workbench_external_identities').fetchone()[0] == 0
        assert [k for (k,) in admin.execute('select kind from control.nexloop_workbench_external_identity_events order by created_at')] == ['linked', 'owner_promoted']
        assert sign_in(member, idp, 'idp-sub-operator').headers['location'] == '/workbench?sso=failed'
        # Second guard: a link present for an owner anyway (written here directly) still signs nobody in.
        admin.execute("""insert into control.nexloop_workbench_external_identities values(%s,'corp',%s,'idp-sub-operator',%s,%s,'sso_or_password',%s,'synthetic-direct',now())""",
                      (TENANT, idp.issuer, operator['principal_id'], operator['subject_id'], owner['principal_id']))
        assert sign_in(member, idp, 'idp-sub-operator').headers['location'] == '/workbench?sso=failed'
        # ... and an owner target can be unlinked.
        csrf = workbench_login(client, owner).json()['csrf_token']
        unlinked = act(client, csrf, 'unlink_workbench_external', {'target_principal': operator['principal_id']})
        assert unlinked.status_code == 200, unlinked.json()
        assert admin.execute('select count(*) from control.nexloop_workbench_external_identities').fetchone()[0] == 0
    assert failures(admin) == ['not_linked', 'member_unavailable']
