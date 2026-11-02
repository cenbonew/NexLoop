"""AUTH-FP (0403): the browser directory hash covers only what decides authority. Activity of the same session (a CSRF refresh,
any read touching last_seen_at / idle_expires_at) leaves it unchanged, so a send and a read of one browser running at once both
succeed; a sign-out, a revoked session or a changed authority (tenant revision, membership) still changes or refuses it, and a claim
signed with the old hash is refused. Real PostgreSQL, the actual app and browser session; synthetic data only."""
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from nexloop_eios.browser_authorization import authenticate_browser_business
from support.tenants import TENANT_A
from test_browser_http import ORIGIN, browser, identity  # noqa: F401
from test_conversation_messages import browser_business, conversations, published_action, uow  # noqa: F401
from test_web_chat_http import actual_chat, create_actual  # noqa: F401


def digest(admin, principal):
    return admin.execute("select encode(session_token_digest,'hex') from control.nexloop_browser_sessions where payload->>'principal_id'=%s"
                         " and payload->>'revoked_at' is null order by payload->>'created_at' desc limit 1", (principal,)).fetchone()[0]


def fact_hash(fixture, token_digest, session_id):
    with psycopg.connect(make_conninfo(fixture['pg'], user='nexloop_api'), autocommit=True) as api:
        return api.execute("select authz.nexloop_browser_authority_fact_snapshot(%s,'real','browser_authentication',array[%s])->>'record_hash'",
                           (token_digest, session_id)).fetchone()[0]


def snapshot(fixture, token_digest):
    with psycopg.connect(make_conninfo(fixture['pg'], user='nexloop_api'), autocommit=True) as api:
        return api.execute("select authz.nexloop_browser_identity_snapshot(%s,'real')", (token_digest,)).fetchone()[0]


def seen(admin, token_digest):
    return admin.execute("select payload->>'last_seen_at',payload->>'idle_expires_at' from control.nexloop_browser_sessions"
                         " where session_token_digest=decode(%s,'hex')", (token_digest,)).fetchone()


def inspect(client):
    from eios.identity.ports import TrustedIdentityOperator
    from eios.identity.sessions import BrowserSessionService
    store = client.app.state.browser_store
    return BrowserSessionService(store, store, application_id='synthetic-browser-app',
        operator=TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id='00000000-0000-4000-8000-0000000000f1',
                                         trace_id='00000000-0000-4000-8000-0000000000f2')).inspect(client.cookies.get('__Host-nexloop_session'))


def test_activity_of_the_same_session_keeps_the_fingerprint(actual_chat, admin):
    client, headers, fixture = actual_chat
    token = digest(admin, fixture['principal'])
    first = snapshot(fixture, token)
    before, activity = first['directory_hash'], seen(admin, token)
    session_id = first['binding']['session_id']
    authentication = fact_hash(fixture, token, session_id)
    refreshed = client.post('/api/v1/auth/csrf', headers={'Origin': ORIGIN})
    assert refreshed.status_code == 200
    assert seen(admin, token) != activity  # the session row really changed (last_seen_at / idle_expires_at)
    assert snapshot(fixture, token)['directory_hash'] == before
    assert fact_hash(fixture, token, session_id) == authentication  # the browser_authentication fact of a claim, too


def test_a_send_and_a_read_of_one_session_at_once_both_succeed(actual_chat, admin):
    client, headers, fixture = actual_chat
    created = create_actual(client, headers)
    path = '/api/v1/conversations/'+created['id']+'/messages'
    reader = TestClient(client.app, base_url=ORIGIN, cookies=dict(client.cookies))

    def send(index):
        csrf = client.post('/api/v1/auth/csrf', headers={'Origin': ORIGIN})  # as the WebChat does before each send: touches the session
        assert csrf.status_code == 200
        return client.post(path, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf.json()['csrf_token'],
                                          'Idempotency-Key': f'synthetic-fp-message-{index}'}, json={'body': f'synthetic {index}'}).status_code

    def read(_):
        return reader.get(path).status_code
    # The race made deterministic: a read authenticated before a send's CSRF refresh completes after it.
    from nexloop_eios.conversation_messages import ConversationMessagePort
    backend = client.app.state.backend
    before = authenticate_browser_business(backend._pool, inspect(client))
    assert client.post('/api/v1/auth/csrf', headers={'Origin': ORIGIN}).status_code == 200
    assert ConversationMessagePort(backend._pool, before, backend._signer).read_messages(conversation_id=created['id'])['items'] == []
    with ThreadPoolExecutor(2) as pool:
        sends = pool.submit(lambda: [send(i) for i in range(8)])
        reads = pool.submit(lambda: [read(i) for i in range(40)])
        assert sends.result() == [202]*8
        assert reads.result() == [200]*40


def test_sign_out_revocation_and_authority_changes_still_count(actual_chat, admin):
    from nexloop_eios.conversation_messages import ConversationMessagePort, ConversationUnavailable
    client, headers, fixture = actual_chat
    created = create_actual(client, headers)
    path = '/api/v1/conversations/'+created['id']+'/messages'
    token = digest(admin, fixture['principal'])
    backend = client.app.state.backend
    stale = authenticate_browser_business(backend._pool, inspect(client))  # the old fingerprint, held across the change below
    port = ConversationMessagePort(backend._pool, stale, backend._signer)
    assert port.read_messages(conversation_id=created['id'])['items'] == []
    first = snapshot(fixture, token)['directory_hash']
    # An authority change (a governed role or grant change raises the tenant revision): the fingerprint changes ...
    admin.execute('update control.nexloop_tenants set authority_revision=authority_revision+1 where tenant_id=%s', (TENANT_A,))
    assert snapshot(fixture, token)['directory_hash'] != first
    # ... and a claim signed with the old one is refused (the reverse check), while a fresh read of the session passes.
    with pytest.raises(ConversationUnavailable):
        port.read_messages(conversation_id=created['id'])
    assert client.get(path).status_code == 200
    # Sign-out: the session is revoked; reads fail and the snapshot refuses.
    assert client.post('/api/v1/auth/logout', headers=headers).status_code in (200, 204)
    assert client.get(path).status_code == 401
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        snapshot(fixture, token)


def account_payload(admin, principal):
    return admin.execute("select a.payload from control.nexloop_browser_accounts a join control.nexloop_browser_sessions s"
                         " on s.payload->>'credential_id'=a.local_account_id and s.tenant_id=a.tenant_id where s.payload->>'principal_id'=%s limit 1",
                         (principal,)).fetchone()[0]


def test_lockout_restriction_and_revocation_refuse_a_held_claim(actual_chat, admin):
    # A failed login of someone else is not tested as "the claim stays valid": 0023 raises the account revision on every failure,
    # and the snapshot then refuses every live session of the account as stale (reported separately, AUTH-FP follow-up).
    from nexloop_eios.conversation_messages import ConversationMessagePort, ConversationUnavailable
    client, headers, fixture = actual_chat
    created = create_actual(client, headers)
    path = '/api/v1/conversations/'+created['id']+'/messages'
    backend = client.app.state.backend
    token = digest(admin, fixture['principal'])
    held = ConversationMessagePort(backend._pool, authenticate_browser_business(backend._pool, inspect(client)), backend._signer)
    account = account_payload(admin, fixture['principal'])
    assert held.read_messages(conversation_id=created['id'])['items'] == [] and client.get(path).status_code == 200

    def refused():
        assert client.get(path).status_code != 200
        with pytest.raises(ConversationUnavailable):
            held.read_messages(conversation_id=created['id'])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            snapshot(fixture, token)
    # Locked (locked_until in the future): refused.
    admin.execute("update control.nexloop_browser_accounts set payload=payload||jsonb_build_object('locked_until',to_jsonb(now()+interval '10 minutes'))"
                  " where local_account_id=%s", (account['local_account_id'],))
    refused()
    admin.execute("update control.nexloop_browser_accounts set payload=payload||jsonb_build_object('locked_until',null) where local_account_id=%s",
                  (account['local_account_id'],))
    assert client.get(path).status_code == 200
    # restricted=true, then revoked_at set: refused.
    for change in ("jsonb_build_object('restricted',true)", "jsonb_build_object('revoked_at',to_jsonb(now()))"):
        admin.execute(f"update control.nexloop_browser_sessions set payload=payload||{change} where session_token_digest=decode(%s,'hex')", (token,))
        refused()
        admin.execute("update control.nexloop_browser_sessions set payload=payload||jsonb_build_object('restricted',false,'revoked_at',null)"
                      " where session_token_digest=decode(%s,'hex')", (token,))
