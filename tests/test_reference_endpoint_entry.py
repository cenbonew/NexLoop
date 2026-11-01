"""S81 / ADR-043 C3: the reference endpoint's staff entry ticket (POST /staff/entry): a ticket NexLoop signs
(nexloop_eios.entry_tickets.sign_staff_ticket) opens a read-only staff session once (jti kept in the endpoint store, also across a
restart), the read-only session cannot mark; replay, expiry, another audience, a forged or truncated signature, a malformed body:
one fixed page that names nothing of the ticket (T6); the local audit keeps 30 days; no key file, no route. Local TLS, synthetic."""
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

from nexloop_eios import entry_tickets as E
from test_reference_endpoint import endpoint_module, http, request, running  # noqa: F401


def post(h, ticket=None, body=None):
    return h.post('/staff/entry', content=body if body is not None else 'ticket=' + ticket,
                  headers={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'https://gateway.example.invalid'})


def forge(key, **claims):
    payload = json.dumps({'v': 1, 'aud': 'reference_staff', 'sub': 'entry-test', 'jti': secrets.token_hex(16), 'iat': int(time.time()),
                          'exp': int(time.time()) + 30, **claims}, separators=(',', ':')).encode()
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=').decode()  # noqa: E731
    return b64(payload) + '.' + b64(hmac.new(key, E.HMAC_CONTEXT + payload, 'sha256').digest())


def uses(e):
    with sqlite3.connect(e['data'] / endpoint_module.DATABASE) as db:
        return [r[0] for r in db.execute('select outcome from entry_ticket_uses order by sequence')]


def test_a_signed_ticket_opens_a_read_only_staff_session_once(tmp_path):
    key = secrets.token_bytes(32)
    with running(tmp_path, entry_key=key) as e, http(e) as h:
        endpoint_module.Store(e['data'])  # schema present
        # A real ticket waiting to be marked.
        ticket_request = request()
        assert h.post('/v1/effects', json=ticket_request, headers={'Authorization': 'Bearer ' + e['service'],
                      'Idempotency-Key': ticket_request['intent_id']}).status_code == 200
        ticket = E.sign_staff_ticket(key, label='entry-test', ttl=30)
        r = post(h, ticket)
        assert r.status_code == 200 and 'url=/staff' in r.text and ticket not in r.text
        cookie = r.headers['set-cookie']
        assert cookie.startswith('re_session=') and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Secure' in cookie
        session = cookie.split(';')[0]
        page = h.get('/staff', headers={'Cookie': session})
        assert page.status_code == 200 and '只读测试员工（entry-test）' in page.text and 'action="/staff/mark"' not in page.text and '只读会话，不能标记' in page.text
        intent = ticket_request['intent_id']
        mark = h.post('/staff/mark', content='csrf=x&intent_id=' + intent + '&state=fulfilled&handled_by=z', headers={'Cookie': session,
                      'Content-Type': 'application/x-www-form-urlencoded'})
        assert mark.status_code == 403 and intent  # T10: read only
        # T1: once; the replay is refused with the fixed page.
        again = post(h, ticket)
        assert again.status_code == 401 and 'set-cookie' not in again.headers
    # A restart on the same store keeps the used jti.
    with running(tmp_path, entry_key=key) as e2, http(e2) as h2:
        assert post(h2, ticket).status_code == 401
    assert uses(e) == ['session', 'used', 'used']


def test_every_refusal_is_the_same_page_and_names_nothing_of_the_ticket(tmp_path):  # T2, T3, T6
    key = secrets.token_bytes(32)
    with running(tmp_path, entry_key=key) as e, http(e) as h:
        good = E.sign_staff_ticket(key, label='entry-test', ttl=30)
        head, mac = good.split('.')
        cases = [forge(key, iat=int(time.time()) - 100, exp=int(time.time()) - 70),           # expired
                 forge(key, aud='webchat'),                                                     # another audience
                 forge(key, exp=int(time.time()) + 120),                                        # longer than 60 s
                 forge(secrets.token_bytes(32)),                                                # another key
                 head + '.' + ('B' if mac[0] == 'A' else 'A') + mac[1:],                        # forged signature (first char: the last one carries padding bits)
                 head[:-2] + '.' + mac, head, 'x' * 43, secrets.token_urlsafe(32),              # truncated, no signature, a WebChat-shaped one
                 forge(key, sub='Entry Test'), forge(key, extra=1)]
        pages = [post(h, c) for c in cases] + [post(h, body='ticket=' + good + '&x=1'), post(h, body='%ff'), post(h, body='')]
        bodies = {p.content for p in pages}
        assert len(bodies) == 1 and all(p.status_code in (400, 401) for p in pages)
        fixed = pages[0].text
        assert '自动登录失败' in fixed and all(part not in fixed for part in (head, mac, 'reference_staff', 'entry-test', 'jti', 'exp'))
        assert all('set-cookie' not in p.headers for p in pages)
        # The good ticket is still unused after all that.
        assert post(h, good).status_code == 200
    assert uses(e)[:5] == ['expired', 'wrong_target', 'expired', 'signature', 'signature'] and uses(e)[-1] == 'session'


def test_no_key_file_no_route_and_the_audit_keeps_30_days(tmp_path):
    with running(tmp_path) as e, http(e) as h:
        assert post(h, 'x.y').status_code == 404
    store = endpoint_module.Store(tmp_path / 'aged')
    with store.connect() as db:
        db.execute("insert into entry_ticket_uses(jti,staff_label,outcome,at) values('a','entry-test','session','2020-01-01T00:00:00+00:00')")
    assert store.entry_use('b' * 32, 'entry-test', 'session') is True
    with store.connect() as db:
        assert [r[0] for r in db.execute('select jti from entry_ticket_uses')] == ['b' * 32]
    assert hashlib  # no address or ticket is stored: the columns are jti, label, outcome, time
    with store.connect() as db:
        assert {r[1] for r in db.execute('pragma table_info(entry_ticket_uses)')} == {'sequence', 'jti', 'staff_label', 'outcome', 'at'}
