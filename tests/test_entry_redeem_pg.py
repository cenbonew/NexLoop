"""S81 / ADR-043 C2: the WebChat redemption of an entry ticket (POST /api/v1/auth/entry, consumer realm) on real PostgreSQL: the
pinned synthetic test customer's ordinary session after one take (T9 its own data only), 303 to the WebChat; replay (T1), expiry (T2),
another target (T3), a target no longer synthetic at redemption (T13), concurrent takes (T11), malformed bodies and other origins
(T6: one fixed page, byte for byte, never the ticket), the switch off. Synthetic data only."""
import dataclasses
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import entry_tickets as E
from nexloop_eios.http_api import create_app
from test_browser_http import ORIGIN
from test_entry_tickets_pg import STAFF_URL, ask, consumer_of, events, persona
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, private, provision, published_action, uow,  # noqa: F401
                                    workbench)


@pytest.fixture
def entry(workbench, tmp_path):
    import secrets
    w = workbench
    provision(w)
    key_file = private(tmp_path, 'entry-ticket-key', secrets.token_bytes(32).hex())
    persona(w['admin'], consumer_of(w['admin'], w['customer']['principal_id']), 'simulated_agent')

    def client(on=True):
        bench = dataclasses.replace(w['config'].workbench, entry_tickets=on, entry_test_webchat_principal=w['customer']['principal_id'],
                                    entry_ticket_key_file=key_file, reference_endpoint_staff_url=STAFF_URL)
        browser = dataclasses.replace(w['config'].browser, entry_tickets=on)
        return TestClient(create_app(dataclasses.replace(w['config'], workbench=bench, browser=browser)), base_url=ORIGIN)
    return dict(w=w, client=client)


def ticket(entry, target='webchat'):
    with entry['client']() as c:
        r = ask(c, entry['w']['owner'], target)
        assert r.status_code == 200, r.text
        return r.json()['ticket']


def redeem(c, value, *, origin=ORIGIN, content_type='application/x-www-form-urlencoded', body=None, path='/api/v1/auth/entry', fetch=None):
    return c.post(path, headers={'Origin': origin, 'Content-Type': content_type, **(fetch or {})}, content=body if body is not None else 'ticket=' + value,
                  follow_redirects=False)


def refused(r, value=''):
    assert r.status_code == 401 and r.content == E.FAILED_PAGE and r.headers['cache-control'] == 'no-store'
    assert 'set-cookie' not in r.headers and (not value or value not in r.text)


def test_the_ticket_opens_the_test_customers_own_session_once(entry):  # T1, T9
    w, admin = entry['w'], entry['w']['admin']
    value = ticket(entry)
    with entry['client']() as c:
        r = redeem(c, value)
        assert r.status_code == 303 and r.headers['location'] == '/' and '__Host-nexloop_session=' in r.headers['set-cookie']
        assert value not in r.headers['location'] and 'httponly' in r.headers['set-cookie'].lower()
        session = c.get('/api/v1/auth/session')
        assert session.status_code == 200 and session.json()['principal_id'] == w['customer']['principal_id']
        # T9: the customer's own conversations, through the ordinary consumer authorization.
        own = c.get('/api/v1/conversations')
        assert own.status_code == 200 and {x['consumer_id'] for x in own.json()['items']} <= {consumer_of(admin, w['customer']['principal_id'])}
        refused(redeem(c, value), value)  # T1: once
    methods = admin.execute("select payload->'authentication_methods' from control.nexloop_browser_sessions where payload->>'principal_id'=%s"
                            " order by payload->>'created_at' desc limit 1", (w['customer']['principal_id'],)).fetchone()[0]
    assert methods == ['entry_ticket']
    assert events(admin) == ['issue:webchat:issued', 'redeem:webchat:session', 'redeem:webchat:used']
    assert admin.execute('select outcome from control.nexloop_entry_tickets').fetchall() == [('session',)]


def test_expired_other_target_and_no_longer_synthetic_are_refused(entry):  # T2, T3, T13
    w, admin = entry['w'], entry['w']['admin']
    late = ticket(entry)
    admin.execute("update control.nexloop_entry_tickets set issued_at=issued_at-interval '31 seconds',expires_at=expires_at-interval '31 seconds'")
    other = ticket(entry)
    admin.execute("update control.nexloop_entry_tickets set target='reference_staff' where ticket_digest=%s", (E.digest(other),))
    changed = ticket(entry)
    persona(admin, consumer_of(admin, w['customer']['principal_id']), 'real')
    with entry['client']() as c:
        for value in (late, other, changed):
            refused(redeem(c, value), value)
        refused(redeem(c, 'x' * 43))  # unknown
    assert events(admin)[-4:] == ['redeem:webchat:expired', 'redeem:webchat:wrong_target', 'redeem:webchat:not_synthetic', 'redeem:webchat:unknown']
    assert not admin.execute("select count(*) from control.nexloop_browser_sessions where payload->>'principal_id'=%s and payload->'authentication_methods' ? 'entry_ticket'",
                             (w['customer']['principal_id'],)).fetchone()[0]


def test_concurrent_redemptions_take_the_ticket_once(entry):  # T11
    value = ticket(entry)
    with entry['client']() as c1, entry['client']() as c2:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda c: redeem(c, value).status_code, (c1, c2)))
    assert sorted(results) == [303, 401]


def test_malformed_requests_and_other_origins_get_the_same_fixed_page(entry):  # T6, T8
    value = ticket(entry)
    staff = ticket(entry, 'reference_staff')
    with entry['client']() as c:
        for r in (redeem(c, value, origin='https://elsewhere.example.invalid'), redeem(c, value, content_type='application/json'),
                  redeem(c, value, body='ticket=' + value + '&extra=1'), redeem(c, value, body='ticket=' + value + '&ticket=' + value),
                  redeem(c, value, body='ticket=' + value[:-1]), redeem(c, value, body='ticket=' + value + 'A'), redeem(c, value, body='x' * 200),
                  redeem(c, value, body='%ff%fe'), redeem(c, value, path='/api/v1/auth/entry?ticket=' + value), redeem(c, staff)):
            refused(r, value)
        # Still unused: none of those touched it.
        assert redeem(c, value).status_code == 303
    with entry['client'](on=False) as c:
        refused(redeem(c, ticket(entry)))


NAVIGATE = {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'navigate'}


def test_origin_null_passes_only_as_a_same_origin_navigation(entry):  # R67: the workbench's no-referrer policy makes the form's Origin null
    value = ticket(entry)
    with entry['client']() as c:
        for fetch in ({}, {**NAVIGATE, 'Sec-Fetch-Site': 'cross-site'}, {**NAVIGATE, 'Sec-Fetch-Site': 'same-site'}, {**NAVIGATE, 'Sec-Fetch-Site': 'none'},
                      {**NAVIGATE, 'Sec-Fetch-Mode': 'cors'}, {'Sec-Fetch-Mode': 'navigate'}):
            refused(redeem(c, value, origin='null', fetch=fetch), value)
        refused(redeem(c, value, origin='https://elsewhere.example.invalid', fetch=NAVIGATE), value)  # an explicit other origin stays refused
        assert redeem(c, value, origin='null', fetch=NAVIGATE).status_code == 303
