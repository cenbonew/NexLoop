"""Synthetic customer driver (scripts/acceptance/synthetic_customer.py) offline: a scripted WebChat server behind the consumer
client's transport and a scripted read-only persona lookup. Covers the login back-off on 429, the persona preflight (a real Consumer
refuses the whole run before anything is sent; an erased one is skipped), the 403 skip, idempotent replay, the reply wait and that no
password, username, body or id reaches the output - including a password line given in the wrong place. No network, no database."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/acceptance'))
import synthetic_customer as S  # noqa: E402
from nexloop_eios.consumer_channel import ConsumerClient  # noqa: E402

ORIGIN = 'https://app.example.invalid'
SECRET = 'Pw-zq9!synthetic'
USERS = {'customer-1': 'user-one@example.invalid', 'customer-2': 'user-two@example.invalid', 'customer-4': 'user-four@example.invalid'}
PRINCIPAL = {u: f'principal-{a}' for a, u in USERS.items()}
BODY = '有没有适合初学者的网球拍'


class Server:
    """WebChat stand-in: login (optionally throttled), conversations, idempotent messages, replies, logout."""
    def __init__(self, throttle=0, forbidden=(), reply=True):
        self.throttle, self.forbidden, self.reply = throttle, set(forbidden), reply
        self.sessions, self.conversations, self.messages, self.keys, self.requests = {}, {}, {}, {}, []

    def __call__(self, method, origin, path, headers, body):
        self.requests.append((method, path, dict(headers), body))
        data = json.loads(body) if body else {}
        cookie = (headers.get('Cookie') or '').partition('=')[2]
        principal = self.sessions.get(cookie)
        if path == '/api/v1/auth/login':
            if self.throttle:
                self.throttle -= 1
                return 429, {'retry-after': '3'}, b'{"code":"rate_limited"}'
            principal = PRINCIPAL.get(data['username']) if data.get('password') == SECRET else None
            if principal is None:
                return 401, {}, b'{"code":"unauthenticated"}'
            token = f'c{len(self.sessions):031d}'
            self.sessions[token] = principal
            return 200, {'set-cookie': f'__Host-nexloop_session={token}; Path=/; Secure; HttpOnly'}, json.dumps(
                {'csrf_token': 't' * 32, 'principal_id': principal, 'tenant_id': 'tenant-x'}).encode()
        if principal is None:
            return 401, {}, b'{"code":"unauthenticated"}'
        if path == '/api/v1/auth/logout':
            self.sessions.pop(cookie)
            return 200, {}, b'{}'
        if path.startswith('/api/v1/conversations?') or path == '/api/v1/conversations':
            if method == 'GET':
                mine = [{'id': c} for c, p in self.conversations.items() if p == principal]
                return 200, {}, json.dumps({'items': mine[:1]}).encode()
            if principal in self.forbidden:
                return 403, {}, b'{"code":"conversation_unavailable"}'
            cid = f'{len(self.conversations) + 1:064x}'
            self.conversations[cid] = principal
            self.messages[cid] = []
            return 200, {}, json.dumps({'id': cid}).encode()
        cid = path.split('/')[4]
        if method == 'POST':
            key = headers['Idempotency-Key']
            if key in self.keys:
                return 200, {}, json.dumps({'created': False, 'message': self.keys[key]}).encode()
            message = {'sequence': len(self.messages[cid]) + 1, 'actor': principal}
            self.messages[cid].append(message)
            if self.reply:
                self.messages[cid].append({'sequence': len(self.messages[cid]) + 1, 'actor': 'nexloop'})
            self.keys[key] = message
            return 200, {}, json.dumps({'created': True, 'message': message}).encode()
        after = int(path.split('after=')[1].split('&')[0])
        return 200, {}, json.dumps({'items': [m for m in self.messages[cid] if m['sequence'] > after]}).encode()


class Reader:
    """The read-only persona lookup: principal -> (persona, erasing) or no owner row."""
    def __init__(self, personas):
        self.personas, self.principal = personas, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=()):
        if 'set_config' in sql:
            return self
        self.principal = args[1]
        return self

    def fetchall(self):
        got = self.personas.get(self.principal)
        return [] if got is None else [got]


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def private(path, value):
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)
    return str(path)


def files(tmp_path, turns, plan_id='s6-local-1'):
    config = private(tmp_path / 'config.json', {'schema': S.CONFIG_SCHEMA, 'origin': ORIGIN, 'ca_file': None, 'reader_dsn_file': '/unused',
                                                'accounts': USERS})
    plan = private(tmp_path / f'{plan_id}.json', {'schema': S.PLAN_SCHEMA, 'plan_id': plan_id, 'turns': turns})
    return config, plan


def run(tmp_path, server, personas, turns, stdin=None, plan_id='s6-local-1'):
    config, plan = files(tmp_path, turns, plan_id)
    clock, out = Clock(), io.StringIO()
    aliases = list(dict.fromkeys(t['account'] for t in turns))
    stdin = stdin if stdin is not None else ''.join(f'{a}\t{SECRET}\n' for a in aliases)
    code = S.main(['--config', config, '--plan', plan], stdin=io.StringIO(stdin), out=out,
                  client_factory=lambda: ConsumerClient(ORIGIN, transport=server, clock=clock, sleep=clock.sleep),
                  connect=lambda: Reader(personas), sleep=clock.sleep, clock=clock)
    text = out.getvalue()
    return code, [json.loads(line) for line in text.splitlines()], text, clock


def no_secret(text):
    for value in (SECRET, BODY, *USERS.values(), *PRINCIPAL.values(), 'c0000000', 't' * 32):
        assert value not in text


SYNTHETIC = {PRINCIPAL[USERS['customer-1']]: ('simulated_agent', False), PRINCIPAL[USERS['customer-2']]: ('synthetic', False)}


def test_turns_are_spoken_replies_awaited_and_a_rerun_replays(tmp_path):
    server = Server(throttle=2)
    turns = [{'account': 'customer-1', 'body': BODY, 'wait_reply_seconds': 30}, {'account': 'customer-2', 'body': BODY},
             {'account': 'customer-1', 'body': BODY + '?', 'wait_reply_seconds': 30}]
    code, lines, text, clock = run(tmp_path, server, SYNTHETIC, turns)
    assert code == 0, lines
    assert lines[0] == {'preflight': {'customer-1': 'simulated_agent', 'customer-2': 'synthetic'}}
    assert [(x['turn'], x['account'], x['status'], x['replied']) for x in lines[1:4]] == [
        (1, 'customer-1', 'sent', True), (2, 'customer-2', 'sent', None), (3, 'customer-1', 'sent', True)]
    assert clock.slept[:2] == [3, 3]  # the two 429s backed off by Retry-After
    assert lines[-1]['summary']['sent_or_replayed'] == 3
    keys = [h['Idempotency-Key'] for m, p, h, _ in server.requests if m == 'POST' and p.endswith('/messages')]
    assert keys == ['s6-local-1-1-customer-1', 's6-local-1-2-customer-2', 's6-local-1-3-customer-1']
    assert server.sessions == {}  # every login logged out
    no_secret(text)
    # The same plan again: every message replays (no new message), the replies are already there.
    before = sum(len(m) for m in server.messages.values())
    code, lines, text, _ = run(tmp_path, server, SYNTHETIC, turns)
    assert code == 0 and [x['status'] for x in lines[1:4]] == ['replayed'] * 3
    assert sum(len(m) for m in server.messages.values()) == before
    no_secret(text)


def test_a_real_consumer_refuses_the_whole_run_before_anything_is_sent(tmp_path):
    server = Server()
    personas = {**SYNTHETIC, PRINCIPAL[USERS['customer-2']]: ('real', False)}
    code, lines, text, _ = run(tmp_path, server, personas, [{'account': 'customer-1', 'body': BODY}, {'account': 'customer-2', 'body': BODY}])
    assert code == 2 and 'customer-2 is not a simulated_agent / synthetic Consumer' in lines[0]['refused']
    assert not any(m == 'POST' and 'conversations' in p for m, p, _, _ in server.requests)
    no_secret(text)


def test_an_erased_consumer_and_a_forbidden_conversation_are_skipped(tmp_path):
    server = Server(forbidden={PRINCIPAL[USERS['customer-2']]})
    personas = {PRINCIPAL[USERS['customer-1']]: ('simulated_agent', False), PRINCIPAL[USERS['customer-2']]: ('simulated_agent', False)}
    # customer-4: no owner row any more (erased): skipped in the preflight, never logged in again or created.
    turns = [{'account': 'customer-4', 'body': BODY}, {'account': 'customer-2', 'body': BODY}, {'account': 'customer-2', 'body': BODY},
             {'account': 'customer-1', 'body': BODY}]
    code, lines, text, _ = run(tmp_path, server, personas, turns)
    assert code == 1
    assert lines[0]['preflight']['customer-4'] == 'conversation_unavailable'
    assert [(x['account'], x['status'], x['http']) for x in lines[1:5]] == [
        ('customer-4', 'conversation_unavailable', None), ('customer-2', 'conversation_unavailable', 403),
        ('customer-2', 'conversation_unavailable', None), ('customer-1', 'sent', 200)]
    creates = [p for m, p, _, _ in server.requests if m == 'POST' and p == '/api/v1/conversations']
    assert len(creates) == 2  # customer-2 once (403, then skipped), customer-1 once; never customer-4
    assert lines[-1]['summary']['not_sent'] == {'conversation_unavailable': 3}
    no_secret(text)


def test_a_reply_that_never_comes_is_a_timeout_and_is_not_resent(tmp_path):
    server = Server(reply=False)
    code, lines, text, clock = run(tmp_path, server, SYNTHETIC, [{'account': 'customer-1', 'body': BODY, 'wait_reply_seconds': 10}])
    assert code == 1 and lines[1]['status'] == 'timeout' and lines[1]['replied'] is False
    assert sum(1 for m, p, _, _ in server.requests if m == 'POST' and p.endswith('/messages')) == 1
    no_secret(text)


@pytest.mark.parametrize('stdin', [f'{SECRET}\n', f'customer-1 {SECRET}\n', f'customer-9\t{SECRET}\n', '', f'customer-1\t{SECRET}\ncustomer-1\t{SECRET}\n',
                                   f'import os; print("{SECRET}")\n'])
def test_a_password_in_the_wrong_shape_is_refused_without_being_echoed(tmp_path, stdin):
    """Including stdin given a script (as if run with `python -`): refused as a password line, never executed or printed."""
    server = Server()
    code, lines, text, _ = run(tmp_path, server, SYNTHETIC, [{'account': 'customer-1', 'body': BODY}], stdin=stdin)
    assert code == 2 and lines[0]['refused'].startswith('stdin:')
    assert server.requests == []
    no_secret(text)


def test_a_wrong_password_names_the_alias_and_code_only(tmp_path):
    server = Server()
    config, plan = files(tmp_path, [{'account': 'customer-1', 'body': BODY}])
    out = io.StringIO()
    code = S.main(['--config', config, '--plan', plan], stdin=io.StringIO('customer-1\tnot-the-password\n'), out=out,
                  client_factory=lambda: ConsumerClient(ORIGIN, transport=server), connect=lambda: Reader(SYNTHETIC))
    assert code == 2 and json.loads(out.getvalue())['refused'] == 'preflight: customer-1 login unauthenticated'
    assert 'not-the-password' not in out.getvalue()


def test_input_files_must_be_private_and_well_formed(tmp_path):
    config, plan = files(tmp_path, [{'account': 'customer-1', 'body': BODY}])
    os.chmod(plan, 0o644)
    out = io.StringIO()
    assert S.main(['--config', config, '--plan', plan], stdin=io.StringIO(''), out=out) == 2
    config, plan = files(tmp_path, [{'account': 'customer-unknown', 'body': BODY}], plan_id='bad')
    assert S.main(['--config', config, '--plan', plan], stdin=io.StringIO(''), out=io.StringIO()) == 2


class OwnerWorkbench:
    """The owner's erasure precheck (GET /api/v1/workbench/erasure-precheck/<consumer>): consumer_id -> (persona, erasure state)."""
    def __init__(self, prechecks, password=SECRET + '-owner'):
        self.prechecks, self.password, self.calls, self.logged_in, self.logged_out = prechecks, password, [], None, False

    def login(self, username, password):
        if password != self.password:
            raise PermissionError('login refused (401)')
        self.logged_in = username

    def logout(self):
        self.logged_out = True

    def _call(self, method, path, body=None, extra=None):
        self.calls.append((method, path))
        consumer = path.rsplit('/', 1)[1]
        if consumer not in self.prechecks:
            return 404, {'error': 'not_found'}
        persona, state = self.prechecks[consumer]
        return 200, {'consumer_id': consumer, 'persona': persona, 'erasure': None if state is None else {'state': state}}


CONSUMER = {'customer-1': 'a' * 64, 'customer-2': 'b' * 64, 'customer-4': 'c' * 63 + 'd'}


def run_workbench(tmp_path, server, workbench, turns, stdin=None):
    owner_file = tmp_path / 'owner'
    owner_file.write_text('owner@example.invalid\n')
    os.chmod(owner_file, 0o600)
    config = private(tmp_path / 'config.json', {'schema': S.CONFIG_SCHEMA, 'origin': ORIGIN, 'ca_file': None, 'accounts': USERS,
                                                'persona_check': {'via': 'workbench', 'owner_username_file': str(owner_file), 'consumers': CONSUMER}})
    plan = private(tmp_path / 'plan.json', {'schema': S.PLAN_SCHEMA, 'plan_id': 'nx054-perf-1', 'turns': turns})
    clock, out = Clock(), io.StringIO()
    aliases = list(dict.fromkeys(t['account'] for t in turns))
    stdin = stdin if stdin is not None else ''.join(f'{a}\t{SECRET}\n' for a in aliases) + f'owner\t{SECRET}-owner\n'
    code = S.main(['--config', config, '--plan', plan], stdin=io.StringIO(stdin), out=out,
                  client_factory=lambda: ConsumerClient(ORIGIN, transport=server, clock=clock, sleep=clock.sleep),
                  workbench_factory=lambda: workbench, sleep=clock.sleep, clock=clock)
    text = out.getvalue()
    return code, [json.loads(line) for line in text.splitlines()], text


def test_the_persona_check_through_the_owners_workbench_precheck(tmp_path):
    """NX-054 §8: no read-only DSN - the owner's governed precheck reads the persona; the owner logs out; nothing secret in output."""
    server, workbench = Server(), OwnerWorkbench({CONSUMER['customer-1']: ('simulated_agent', None)})
    code, lines, text = run_workbench(tmp_path, server, workbench, [{'account': 'customer-1', 'body': BODY}])
    assert code == 0 and lines[0] == {'preflight': {'customer-1': 'simulated_agent'}}
    assert workbench.calls == [('GET', '/api/v1/workbench/erasure-precheck/' + CONSUMER['customer-1'])] and workbench.logged_out
    assert SECRET + '-owner' not in text and 'owner@example.invalid' not in text and CONSUMER['customer-1'] not in text
    no_secret(text)


@pytest.mark.parametrize('precheck,expected', [(('real', None), 'refused'), (('simulated_agent', 'purging'), 'skipped'), (None, 'skipped')])
def test_the_workbench_persona_check_refuses_real_and_skips_erasing(tmp_path, precheck, expected):
    server = Server()
    workbench = OwnerWorkbench({} if precheck is None else {CONSUMER['customer-1']: precheck})
    code, lines, text = run_workbench(tmp_path, server, workbench, [{'account': 'customer-1', 'body': BODY}])
    if expected == 'refused':
        assert code == 2 and 'not a simulated_agent / synthetic Consumer' in lines[0]['refused']
        assert not any(m == 'POST' and 'conversations' in p for m, p, _, _ in server.requests)
    else:
        assert lines[0] == {'preflight': {'customer-1': 'conversation_unavailable'}}


def test_the_workbench_persona_check_needs_the_owner_line_and_a_full_mapping(tmp_path):
    server, workbench = Server(), OwnerWorkbench({CONSUMER['customer-1']: ('simulated_agent', None)})
    code, lines, _ = run_workbench(tmp_path, server, workbench, [{'account': 'customer-1', 'body': BODY}], stdin=f'customer-1\t{SECRET}\n')
    assert code == 2 and 'stdin' in lines[0]['refused']
    code, lines, _ = run_workbench(tmp_path, server, OwnerWorkbench({}, password='other'), [{'account': 'customer-1', 'body': BODY}])
    assert code == 2 and lines[0]['refused'] == 'preflight: owner login refused'
    owner_file = tmp_path / 'owner'
    owner_file.write_text('o\n')
    os.chmod(owner_file, 0o600)
    for check in ({'via': 'workbench', 'owner_username_file': str(owner_file), 'consumers': {'customer-1': 'a' * 64}},  # not every account
                  {'via': 'database', 'owner_username_file': str(owner_file), 'consumers': CONSUMER}):
        config = private(tmp_path / 'bad.json', {'schema': S.CONFIG_SCHEMA, 'origin': ORIGIN, 'accounts': USERS, 'persona_check': check})
        plan = private(tmp_path / 'p.json', {'schema': S.PLAN_SCHEMA, 'plan_id': 'x', 'turns': [{'account': 'customer-1', 'body': BODY}]})
        out = io.StringIO()
        assert S.main(['--config', config, '--plan', plan], stdin=io.StringIO(''), out=out) == 2 and 'persona_check' in out.getvalue()
