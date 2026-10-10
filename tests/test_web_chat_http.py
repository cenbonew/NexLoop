"""Real PG browser authentication boundary; no successful business-port stub."""
import pytest
import json
import secrets
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from nexloop_eios.http_api import ApiConfiguration, create_app
from test_conversation_messages import conversations, browser_business, published_action, uow
from test_browser_http import browser, identity, login, ORIGIN
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.web_chat_http import router


@pytest.fixture
def chat(browser, identity, tmp_path):
    seen = []
    def unavailable(request, session):
        # Observe genuine inspected PG identity, then fail closed. This factory
        # deliberately grants nothing and supplies no mocked Conversation data.
        seen.append(session.principal_id)
        raise RuntimeError('synthetic-sensitive-detail-must-not-escape')
    config = BrowserConfiguration(tmp_path/'unused-dsn', tmp_path/'unused-key',
        'synthetic-a', 'synthetic-browser-app', ORIGIN)
    # Separate boundary app reuses the actual open PG identity store; the
    # production app's included routers remain untouched. FastAPI wraps its
    # included routers, so filtering path attributes would not remove them.
    from nexloop_eios.browser_http import router as authentication_router
    boundary = FastAPI()
    boundary.state.browser_store = browser.app.state.browser_store
    boundary.state.browser_rate_key = browser.app.state.browser_rate_key
    boundary.include_router(authentication_router(config))
    boundary.include_router(router(config, ports_for_browser=unavailable))
    with TestClient(boundary, base_url=ORIGIN) as client:
        yield client, seen


def test_anonymous_and_cross_origin_never_reach_business_port(chat):
    client, seen = chat
    assert client.get('/api/v1/conversations').status_code == 401
    assert client.get('/api/v1/conversations', headers={'Host': 'other.example'}).status_code == 403
    assert client.post('/api/v1/conversations', headers={'Idempotency-Key': 'synthetic-create-key'}, json={}).status_code == 403
    assert seen == []


def test_actual_cookie_csrf_and_current_session_required(chat, identity, admin):
    client, seen = chat
    logged = login(client, identity)
    assert logged.status_code == 200
    headers = {'Origin': ORIGIN, 'Idempotency-Key': 'synthetic-create-key'}
    assert client.post('/api/v1/conversations', headers=headers, json={}).status_code == 401
    headers['X-CSRF-Token'] = logged.json()['csrf_token']
    accepted_boundary = client.post('/api/v1/conversations', headers=headers, json={})
    assert accepted_boundary.status_code == 503
    assert seen == [identity[2].principal_id]
    assert 'synthetic-sensitive-detail' not in accepted_boundary.text
    admin.execute('update control.nexloop_browser_applications set active=false')
    assert client.get('/api/v1/conversations').status_code == 401
    assert len(seen) == 1


def test_strict_body_key_and_sse_cursor_reject_before_business(chat, identity):
    client, seen = chat
    logged = login(client, identity)
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': logged.json()['csrf_token'], 'Idempotency-Key': 'synthetic-message-key'}
    path = '/api/v1/conversations/'+'a'*64+'/messages'
    for body in ({'body': 'hello', 'tenant_id': 'other'}, {'body': ''}, {'body': 1}):
        assert client.post(path, headers=headers, json=body).status_code == 422
    assert client.post(path, headers={**headers, 'Idempotency-Key': 'short'}, json={'body': 'hello'}).status_code == 422
    assert client.post(path, headers={**headers, 'Content-Type': 'application/json'}, content='{"body":"x","body":"y"}').status_code == 422
    assert client.get(path.replace('/messages','/events'), headers={'Last-Event-ID': '-1'}).status_code == 422
    assert seen == []


@pytest.fixture
def actual_chat(conversations, admin, tmp_path):
    fixture = conversations
    def private(name, content):
        path = tmp_path/name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(0o600)
        return path
    dsn = private('business-dsn', make_conninfo(fixture['pg'], user='nexloop_api'))
    identity_dsn = private('identity-dsn', fixture['base']['identity'][0])
    key = private('authority-key', fixture['reader'].signer.material)
    rate_key = private('rate-key', secrets.token_hex(32))
    admin.execute("insert into control.nexloop_browser_rate_policies values('synthetic-a','local_login','nexloop_identity',10,60,true)")
    config = ApiConfiguration(dsn, key, tmp_path/'artifacts', fixture['reader'].signer.key_id,
        BrowserConfiguration(identity_dsn, rate_key, 'synthetic-a', 'synthetic-browser-app', ORIGIN),
        execution_profile='deterministic-test', conversation_stream_seconds=1)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        signed_in = login(client, fixture['base']['identity'])
        assert signed_in.status_code == 200
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': signed_in.json()['csrf_token']}
        yield client, headers, fixture


def create_actual(client, headers, key='synthetic-http-conversation'):
    response = client.post('/api/v1/conversations', headers={**headers, 'Idempotency-Key': key}, json={})
    assert response.status_code == 200
    assert response.json()['execution_profile'] == 'deterministic-test'
    return response.json()


def test_actual_http_governed_message202_replay409_persisted_before_ack(actual_chat, admin):
    client, headers, fixture = actual_chat
    created = create_actual(client, headers)
    path = '/api/v1/conversations/'+created['id']+'/messages'
    request_headers = {**headers, 'Idempotency-Key': 'synthetic-http-message-key'}
    accepted = client.post(path, headers=request_headers, json={'body': 'consumer statement'})
    assert accepted.status_code == 202
    message = accepted.json()['message']
    assert message['actor'] == fixture['principal'] and message['status'] == 'accepted'
    assert admin.execute('select count(*) from runtime.nexloop_message_outbox').fetchone() == (1,)
    replay = client.post(path, headers=request_headers, json={'body': 'consumer statement'})
    assert replay.status_code == 202 and replay.json() == {**accepted.json(), 'created': False}
    conflict = client.post(path, headers=request_headers, json={'body': 'different statement'})
    assert conflict.status_code == 409 and conflict.json()['code'] == 'conversation_payload_conflict'
    # NX-051 read projection: server-default provider facts, no reply link.
    assert client.get(path).json()['items'] == [{**message,**{'reply_to_message_id':None,'provider':{'namespace':'nexloop.api','message_ref':None,'sequence':None,'sent_at':None,'trust':'server','skewed':False}}}]
    assert admin.execute('select count(*) from ontology.objects where type_name=%s', ('Message',)).fetchone() == (1,)


def test_actual_http_conversation_pagination_not_repeated_first_page(actual_chat):
    client, headers, _ = actual_chat
    first_created = create_actual(client, headers, 'synthetic-http-conversation-one')
    second_created = create_actual(client, headers, 'synthetic-http-conversation-two')
    first = client.get('/api/v1/conversations?limit=1')
    assert first.status_code == 200 and len(first.json()['items']) == 1
    cursor = first.json()['next_cursor']
    assert cursor
    second = client.get('/api/v1/conversations', params={'after': cursor, 'limit': 1})
    assert second.status_code == 200 and len(second.json()['items']) == 1
    assert {first.json()['items'][0]['id'], second.json()['items'][0]['id']} == {first_created['id'], second_created['id']}
    assert client.get('/api/v1/conversations?limit=101').status_code == 422
    assert client.get('/api/v1/conversations?after=foreign-invalid').status_code == 422


def test_actual_http_sse_last_event_id_only_committed_pg_messages(actual_chat):
    client, headers, _ = actual_chat
    created = create_actual(client, headers)
    path = '/api/v1/conversations/'+created['id']
    receipts = []
    for index in range(3):
        response = client.post(path+'/messages', headers={**headers, 'Idempotency-Key': 'synthetic-http-sse-message-'+str(index)}, json={'body': 'message '+str(index)})
        assert response.status_code == 202
        receipts.append(response.json()['message'])
    streamed = client.get(path+'/events', headers={'Last-Event-ID': '2'})
    assert streamed.status_code == 200 and streamed.headers['content-type'].startswith('text/event-stream')
    data = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith('data: ')]
    assert data == [{'id': '3', 'type': 'message.accepted', 'data': receipts[2]}]
    assert 'id: 1\n' not in streamed.text and 'id: 2\n' not in streamed.text
    resumed = client.get(path+'/events', headers={'Last-Event-ID': '3'})
    assert resumed.status_code == 200 and 'data: ' not in resumed.text


def test_actual_http_wrong_owner_and_current_revoke_no_business_fallback(actual_chat, admin):
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    from nexloop_eios.conversation_messages import READ
    client, headers, fixture = actual_chat
    created = create_actual(client, headers)
    assert client.get('/api/v1/conversations/'+'f'*64+'/messages').status_code == 403
    assert client.get('/api/v1/conversations/'+'f'*64+'/events').status_code == 403
    replace_fact(admin, 'synthetic-a', 'grants', [fixture['principal'], 'eios:action:'+READ+':1'], F.GrantFacts, grants=[])
    assert client.get('/api/v1/conversations/'+created['id']+'/messages').status_code == 403
    assert client.get('/api/v1/conversations/'+created['id']+'/events').status_code == 403
