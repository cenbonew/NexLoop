"""Consumer channel phase 1 on the real stack: the client, CLI and MCP server drive the actual WebChat HTTP API (create_app with
the real browser login, cookie session, CSRF and governed Conversation Actions on PostgreSQL), through an in-process transport.

Covers: login, conversation, send (202, replay, governed persistence), history, receipt and run reads, the SSE watch, wait for a
reply (timeout here: no runtime replies on this stack), refusal cases (wrong password, rate limit, expired session, another
consumer's conversation), and that neither the password nor the session cookie or CSRF token reaches any output."""
import io
import json
import secrets

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from nexloop_eios import consumer_cli, consumer_mcp
from nexloop_eios.browser_http import BrowserConfiguration, COOKIE
from nexloop_eios.consumer_channel import ConsumerChannelError, ConsumerClient
from nexloop_eios.http_api import ApiConfiguration, create_app
from support.tenants import TENANT_A
from test_browser_http import ORIGIN, identity  # noqa: F401
from test_conversation_messages import conversations, browser_business, published_action, uow  # noqa: F401


@pytest.fixture
def stack(conversations, admin, tmp_path):  # noqa: F811
    fixture = conversations

    def private(name, content):
        path = tmp_path / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(0o600)
        return path
    dsn = private('business-dsn', make_conninfo(fixture['pg'], user='nexloop_api'))
    identity_dsn = private('identity-dsn', fixture['base']['identity'][0])
    key = private('authority-key', fixture['reader'].signer.material)
    rate_key = private('rate-key', secrets.token_hex(32))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',4,60,true)", (TENANT_A,))
    config = ApiConfiguration(dsn, key, tmp_path / 'artifacts', fixture['reader'].signer.key_id,
                              BrowserConfiguration(identity_dsn, rate_key, TENANT_A, 'synthetic-browser-app', ORIGIN),
                              execution_profile='deterministic-test', conversation_stream_seconds=1)
    identity = fixture['base']['identity']
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        yield {'http': client, 'username': identity[3].username, 'password': identity[-1], 'principal': fixture['principal'], 'tmp': tmp_path}


def transport_for(http):
    """The client's own headers and cookie only: the test client's cookie jar is cleared before every request."""
    def send(method, origin, path, headers, body):
        assert origin == ORIGIN
        http.cookies.clear()
        response = http.request(method, path, headers=headers, content=body)
        return response.status_code, {k.lower(): v for k, v in response.headers.items()}, response.content
    return send


def test_login_send_history_receipt_and_watch_through_the_real_api(stack, admin):
    s = stack
    client = ConsumerClient(ORIGIN, transport=transport_for(s['http']))
    signed_in = client.login(s['username'], s['password'])
    assert signed_in['ok'] and signed_in['principal_id'] == s['principal'] and signed_in['authenticated']
    assert s['password'] not in json.dumps(signed_in) and client.session.cookie not in json.dumps(signed_in)
    sent = client.send('我想预约下周三上门维修。')
    message = sent['message']
    assert sent['created'] is True and message['actor'] == s['principal'] and message['status'] == 'accepted'
    assert admin.execute('select count(*) from runtime.nexloop_message_outbox').fetchone() == (1,)  # governed path, persisted
    replay = client.send('我想预约下周三上门维修。', idempotency_key='fixed-replay-key-0001')
    again = client.send('我想预约下周三上门维修。', idempotency_key='fixed-replay-key-0001')
    assert replay['created'] is True and again['created'] is False and again['message']['id'] == replay['message']['id']
    history = client.history()
    assert [m['id'] for m in history['items']] == [message['id'], replay['message']['id']]
    receipt = client.receipt(message['id'])
    assert receipt['ok'] and set(receipt) == {'ok', 'message_id', 'receipt', 'run'}
    watched = client.watch(after=0)
    assert [e['data']['id'] for e in watched['events']] == [message['id'], replay['message']['id']] and watched['last_event_id'] == '2'
    waited = client.wait_reply(after_sequence=2, timeout=0)
    assert waited == {'ok': True, 'replied': False, 'timed_out': True, 'conversation_id': sent['conversation_id'], 'last_sequence': 2}


def test_wrong_password_then_rate_limit_are_codes(stack):
    s = stack
    client = ConsumerClient(ORIGIN, transport=transport_for(s['http']))
    codes = []
    for _ in range(6):  # the stack's policy admits 4 login attempts per minute
        with pytest.raises(ConsumerChannelError) as refused:
            client.login(s['username'], 'not-the-password-' + secrets.token_hex(4))
        codes.append((refused.value.code, refused.value.status))
    assert codes[0] == ('unauthenticated', 401) and codes[-1] == ('rate_limited', 429)
    assert refused.value.retry_after is not None and not client.session.cookie


def test_expired_session_and_invalid_ids_are_refused_before_any_write(stack, admin):
    s = stack
    transport = transport_for(s['http'])
    client = ConsumerClient(ORIGIN, transport=transport)
    client.login(s['username'], s['password'])
    sent = client.send('一条消息')
    stale = ConsumerClient(ORIGIN, transport=transport, session=type(client.session)(**client.session.__dict__))
    client.logout()
    with pytest.raises(ConsumerChannelError) as expired:
        stale.history()
    assert expired.value.code == 'session_expired' and expired.value.status == 401
    with pytest.raises(ConsumerChannelError) as expired_write:
        stale.send('写入也被拒绝')
    assert expired_write.value.status == 401
    for bad in ({'conversation_id': 'not-an-id'},):
        with pytest.raises(ConsumerChannelError) as invalid:
            stale.send('x', **bad)
        assert invalid.value.code == 'conversation_id_invalid'
    with pytest.raises(ConsumerChannelError):
        stale.receipt('../etc')
    assert admin.execute('select count(*) from runtime.nexloop_message_outbox').fetchone() == (1,) and sent['created']


def test_another_consumers_conversation_is_forbidden(stack):
    s = stack
    client = ConsumerClient(ORIGIN, transport=transport_for(s['http']))
    client.login(s['username'], s['password'])
    with pytest.raises(ConsumerChannelError) as foreign:
        client.send('不属于我的会话', conversation_id='f' * 64)
    assert foreign.value.status in (403, 404, 409, 503) and foreign.value.code != 'ok'


def test_cli_session_file_and_no_secret_in_output(stack):
    s = stack
    session_file = s['tmp'] / 'consumer' / 'session.json'
    transport = transport_for(s['http'])

    def cli(*argv, stdin=''):
        out = io.StringIO()
        code = consumer_cli.main(['--origin', ORIGIN, '--session-file', str(session_file), *argv], stdin=io.StringIO(stdin), stdout=out, transport=transport)
        return code, json.loads(out.getvalue())
    code, signed_in = cli('login', '--username', s['username'], '--password-stdin', stdin=s['password'] + '\n')
    assert code == 0 and signed_in['ok'] and (session_file.stat().st_mode & 0o777) == 0o600
    stored = session_file.read_text()
    assert s['password'] not in stored
    code, sent = cli('send', '--text', '你好，我想咨询')
    assert code == 0 and sent['message']['actor'] == s['principal']
    code, history = cli('history')
    assert code == 0 and len(history['items']) == 1
    code, waited = cli('wait-reply', '--after-sequence', '1', '--timeout', '0')
    assert code == 0 and waited['timed_out'] is True
    cookie = json.loads(stored)['cookie']
    for output in (signed_in, sent, history, waited):
        text = json.dumps(output, ensure_ascii=False)
        assert s['password'] not in text and cookie not in text and 'csrf' not in text
    code, out = cli('logout')
    assert code == 0 and out == {'ok': True, 'authenticated': False}
    code, refused = cli('send', '--text', 'after logout')
    assert code == 1 and refused['code'] == 'login_required'


def test_mcp_tools_over_the_real_api_with_a_server_side_password(stack):
    s = stack
    passwords = s['tmp'] / 'passwords'
    passwords.mkdir(mode=0o700)
    (passwords / s['username']).write_text(s['password'])
    (passwords / s['username']).chmod(0o600)
    lines = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
             {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
             {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
             {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'send_message', 'arguments': {'text': 'before login'}}},
             {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'login', 'arguments': {'username': s['username']}}},
             {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'send_message', 'arguments': {'text': '请问营业时间？'}}},
             {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call', 'params': {'name': 'wait_for_reply', 'arguments': {'after_sequence': 1, 'timeout_seconds': 1}}},
             {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'get_history', 'arguments': {}}}]
    out = io.StringIO()
    consumer_mcp.main(['--origin', ORIGIN, '--password-dir', str(passwords)], transport=transport_for(s['http']),
                      stdin=io.StringIO('\n'.join(json.dumps(line) for line in lines) + '\n'), stdout=out)
    responses = {r['id']: r for r in map(json.loads, out.getvalue().splitlines())}
    assert set(responses) == {1, 2, 3, 4, 5, 6, 7}  # the notification got no response
    assert responses[1]['result']['serverInfo']['name'] == 'nexloop-consumer'
    assert {t['name'] for t in responses[2]['result']['tools']} == {'login', 'new_conversation', 'send_message', 'wait_for_reply', 'get_history', 'get_receipt'}
    login_schema = next(t for t in responses[2]['result']['tools'] if t['name'] == 'login')['inputSchema']
    assert set(login_schema['properties']) == {'username'}  # no password argument exists
    result = lambda i: (json.loads(responses[i]['result']['content'][0]['text']), responses[i]['result']['isError'])
    assert result(3) == ({'ok': False, 'code': 'login_required'}, True)
    assert result(4)[0]['principal_id'] == s['principal'] and result(4)[1] is False
    assert result(5)[0]['message']['actor'] == s['principal']
    assert result(6)[0]['timed_out'] is True
    assert len(result(7)[0]['items']) == 1
    assert s['password'] not in out.getvalue() and COOKIE not in out.getvalue()
