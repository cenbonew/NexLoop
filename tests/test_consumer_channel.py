"""Consumer channel client offline: reply detection while waiting, the password sources (private file, stdin, Keychain without a
shell), the private session file, origin checks and the MCP protocol envelope. No network, no database."""
import io
import json
import subprocess

import pytest

from nexloop_eios import consumer_cli, consumer_mcp
from nexloop_eios.consumer_channel import ConsumerChannelError, ConsumerClient, Session, load_session, read_password, save_session

ORIGIN = 'https://consumer.example.invalid'
ME = 'principal-consumer'


class Pages:
    """Serves conversation pages from a script; records the requests."""
    def __init__(self, *pages):
        self.pages, self.requests = list(pages), []

    def __call__(self, method, origin, path, headers, body):
        self.requests.append((method, path, dict(headers)))
        return 200, {}, json.dumps(self.pages.pop(0) if self.pages else {'items': []}).encode()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def signed_in(transport, clock=None):
    clock = clock or Clock()
    return ConsumerClient(ORIGIN, transport=transport, session=Session(origin=ORIGIN, principal_id=ME, cookie='c' * 32, csrf='t' * 32,
                                                                       conversation_id='a' * 64), clock=clock, sleep=clock.sleep)


def test_wait_reply_returns_messages_from_others_and_advances_the_cursor():
    mine = {'id': '1' * 64, 'sequence': 3, 'actor': ME, 'body': 'q'}
    reply = {'id': '2' * 64, 'sequence': 4, 'actor': 'nexloop-agent', 'body': 'a'}
    pages = Pages({'items': [mine]}, {'items': []}, {'items': [reply]})
    result = signed_in(pages).wait_reply(after_sequence=2, timeout=60, poll=2)
    assert result['replied'] is True and result['replies'] == [reply] and result['last_sequence'] == 4
    assert [p for _, p, _ in pages.requests] == ['/api/v1/conversations/' + 'a' * 64 + '/messages?after=' + a + '&limit=100' for a in ('2', '3', '3')]


def test_wait_reply_times_out_on_the_deadline():
    clock = Clock()
    result = signed_in(Pages(), clock).wait_reply(timeout=5, poll=2)
    assert result['timed_out'] is True and clock.now == 5


def test_requests_carry_origin_cookie_and_csrf_only_on_writes():
    pages = Pages({'created': True, 'message': {'id': '3' * 64}}, {'items': []})
    client = signed_in(pages)
    client.send('hello', idempotency_key='fixed-key-0000000001')
    client.history()
    (_, _, write), (_, _, read) = pages.requests
    assert write['Origin'] == ORIGIN and write['X-CSRF-Token'] == 't' * 32 and write['Idempotency-Key'] == 'fixed-key-0000000001'
    assert 'X-CSRF-Token' not in read and read['Cookie'] == '__Host-nexloop_session=' + 'c' * 32


@pytest.mark.parametrize('origin', ['http://consumer.example.invalid', 'https://consumer.example.invalid/path', 'https://u:p@consumer.example.invalid'])
def test_only_an_https_origin_is_accepted(origin):
    with pytest.raises(ConsumerChannelError):
        ConsumerClient(origin, transport=Pages())


def test_password_sources_never_argv(tmp_path):
    secret = tmp_path / 'password'
    secret.write_text('synthetic-password-value\n')
    secret.chmod(0o600)
    assert read_password(password_file=secret) == 'synthetic-password-value'
    assert read_password(stdin=io.StringIO('from-a-keychain-pipe\nignored\n')) == 'from-a-keychain-pipe'
    secret.chmod(0o644)
    with pytest.raises(PermissionError):
        read_password(password_file=secret)
    with pytest.raises(ConsumerChannelError):
        read_password()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='keychain-value\n', stderr='')
    source = consumer_mcp.PasswordSource(keychain_service='nexloop/stage-consumer', run=run)
    assert source('customer-1') == 'keychain-value'
    assert calls == [['security', 'find-generic-password', '-w', '-s', 'nexloop/stage-consumer', '-a', 'customer-1']]
    with pytest.raises(ConsumerChannelError):
        source('customer; rm -rf /')
    assert len(calls) == 1


def test_session_file_is_private_and_cli_refuses_a_loose_one(tmp_path):
    path = tmp_path / 'consumer' / 'session.json'
    save_session(path, Session(origin=ORIGIN, cookie='c' * 32, csrf='t' * 32))
    assert (path.stat().st_mode & 0o777) == 0o600 and load_session(path).cookie == 'c' * 32
    path.chmod(0o644)
    out = io.StringIO()
    assert consumer_cli.main(['--session-file', str(path), 'history'], stdout=out, transport=Pages()) == 1
    assert json.loads(out.getvalue()) == {'ok': False, 'code': 'session_file_not_private'}


def test_mcp_protocol_envelope():
    server = consumer_mcp.Server(signed_in(Pages()), lambda username: 'unused')
    assert server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None
    assert server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'nope'})['error']['code'] == -32601
    assert server.handle({'id': 1})['error']['code'] == -32600
    bad = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'send_message', 'arguments': {}}})
    assert bad['result']['isError'] is True and json.loads(bad['result']['content'][0]['text'])['code'] == 'arguments_invalid'
    out = io.StringIO()
    server.serve(io.StringIO('not json\n'), out)
    assert json.loads(out.getvalue())['error']['code'] == -32700
