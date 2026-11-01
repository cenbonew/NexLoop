"""S81 / ADR-043 C1 (0392, nexloop_eios.entry_tickets) on real PostgreSQL over the workbench HTTP realm: the owner's entry ticket for
the pinned synthetic test customer (WebChat) or the reference endpoint test staff; what is refused and recorded (T13 not synthetic,
T16 a synthetic principal that is not the pinned one, T4 not owner / no session / no CSRF / other origin, the switch off); the
ticket's sha256 only in the database; erasure of the customer and of the issuing member (T14); the 30-day retention clearing; the
switch outside the stage (T15). Synthetic data only."""
import dataclasses
import hashlib
import hmac
import json
import secrets

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import entry_tickets as E
from nexloop_eios.http_api import create_app
from test_browser_http import ORIGIN
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, private, provision, published_action, uow,  # noqa: F401
                                    workbench, workbench_login)

STAFF_URL = 'https://endpoint.example.invalid:8443/staff'


def consumer_of(admin, principal):
    return admin.execute("select consumer_id from control.nexloop_consumer_owners where tenant_id=%s and world='real' and principal_id=%s",
                         (TENANT, principal)).fetchone()[0]


def persona(admin, consumer, value):
    admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,%s,1,'test')"
                  " on conflict(tenant_id,world,consumer_id) do update set persona=excluded.persona,revision=control.nexloop_consumer_personas.revision+1",
                  (TENANT, consumer, value))


@pytest.fixture
def entry(workbench, tmp_path):
    w = workbench
    provision(w)
    key = secrets.token_bytes(32)
    key_file = private(tmp_path, 'entry-ticket-key', key.hex())

    def client(**changes):
        bench = dataclasses.replace(w['config'].workbench, **{'entry_tickets': True, 'entry_test_webchat_principal': w['customer']['principal_id'],
                                                               'entry_ticket_key_file': key_file, 'reference_endpoint_staff_url': STAFF_URL, **changes})
        return TestClient(create_app(dataclasses.replace(w['config'], workbench=bench)), base_url=ORIGIN)
    return dict(w=w, key=key, client=client, consumer=consumer_of(w['admin'], w['customer']['principal_id']))


def ask(client, member, target='webchat', *, csrf=True, origin=ORIGIN, body=None):
    headers = {'Origin': origin}
    if member is not None:
        signed = workbench_login(client, member)
        assert signed.status_code == 200
        if csrf:
            headers['X-CSRF-Token'] = signed.json()['csrf_token']
    return client.post('/api/v1/workbench/entry-tickets', headers=headers, json=body if body is not None else {'target': target})


def events(admin):
    return [r[0] for r in admin.execute("select kind||':'||target||':'||outcome from control.nexloop_entry_ticket_events where tenant_id=%s order by event_id",
                                        (TENANT,))]


def test_the_owner_gets_a_webchat_ticket_for_the_pinned_synthetic_customer_only(entry):
    w, admin = entry['w'], entry['w']['admin']
    persona(admin, entry['consumer'], 'simulated_agent')
    with entry['client']() as c:
        r = ask(c, w['owner'])
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-store'
    value = r.json()
    assert set(value) == {'target', 'action', 'ticket', 'expires_at'} and value['action'] == '/api/v1/auth/entry' and value['target'] == 'webchat'
    row = admin.execute('select ticket_digest,target_principal,issued_by_principal,client_digest,expires_at-issued_at,used_at from control.nexloop_entry_tickets').fetchall()
    assert len(row) == 1 and row[0][0] == hashlib.sha256(value['ticket'].encode()).hexdigest()
    assert row[0][1] == w['customer']['principal_id'] and row[0][2] == w['owner']['principal_id'] and len(row[0][3]) == 64
    assert row[0][4].total_seconds() == 30 and row[0][5] is None
    # The ticket itself is nowhere in the database.
    assert not admin.execute("select count(*) from control.nexloop_entry_ticket_events e where e::text like '%%'||%s||'%%'", (value['ticket'],)).fetchone()[0]
    assert events(admin) == ['issue:webchat:issued']


@pytest.mark.parametrize('state', ['real', 'unmarked', 'erasing', 'no_consumer'])
def test_a_target_that_is_not_a_synthetic_test_customer_is_refused_and_recorded(entry, state):  # T13
    w, admin = entry['w'], entry['w']['admin']
    changes = {}
    if state == 'real':
        persona(admin, entry['consumer'], 'real')
    elif state == 'erasing':
        # simulated_agent, but an erasure of that Consumer is under way (0383 runtime.nexloop_consumer_erasing).
        persona(admin, entry['consumer'], 'simulated_agent')
        admin.execute("insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)"
                      " values(%s,'real','entry-ticket-t13',%s,'owner','test','t13','blocking',1)", (TENANT, entry['consumer']))
    elif state == 'no_consumer':
        changes = {'entry_test_webchat_principal': w['operator']['principal_id']}  # a member, no Consumer
    with entry['client'](**changes) as c:
        r = ask(c, w['owner'])
    assert r.status_code == 403 and r.json()['code'] == 'entry_target_refused'
    assert admin.execute('select count(*) from control.nexloop_entry_tickets').fetchone()[0] == 0
    assert events(admin) == ['issue:webchat:not_synthetic']


def test_a_synthetic_principal_that_is_not_the_pinned_one_cannot_be_reached(entry):  # T16
    w, admin = entry['w'], entry['w']['admin']
    # Another principal whose Consumer is simulated_agent; the pinned one is real.
    other = 'f' * 64
    admin.execute("insert into control.nexloop_consumer_owners(tenant_id,world,principal_id,consumer_id,ownership_id,idempotency_key) values(%s,'real',%s,%s,%s,%s)",
                  (TENANT, w['outsider']['principal_id'], other, 'own-' + other[:8], 'entry-ticket-t16'))
    persona(admin, other, 'simulated_agent')
    persona(admin, entry['consumer'], 'real')
    with entry['client']() as c:
        assert ask(c, w['owner']).status_code == 403  # the pinned principal is checked, not some synthetic one
        for body in ({'target': 'webchat', 'principal': w['outsider']['principal_id']}, {'target': 'webchat', 'principal_id': other}, {'target': 'other'}, {}):
            assert ask(c, w['owner'], body=body).status_code == 422  # the client never names the account
    assert admin.execute('select count(*) from control.nexloop_entry_tickets').fetchone()[0] == 0


def test_not_owner_no_session_no_csrf_other_origin_and_switch_off(entry):  # T4, T8
    w, admin = entry['w'], entry['w']['admin']
    persona(admin, entry['consumer'], 'simulated_agent')
    with entry['client']() as c:
        r = ask(c, w['operator'])
        assert r.status_code == 403 and r.json()['code'] == 'not_owner'
    with entry['client']() as c:
        assert ask(c, None).status_code == 401
    with entry['client']() as c:
        assert ask(c, w['owner'], csrf=False).status_code in (401, 403)
    with entry['client']() as c:
        assert ask(c, w['owner'], origin='https://elsewhere.example.invalid').status_code == 403
    with entry['client'](entry_tickets=False) as c:
        assert ask(c, w['owner']).status_code == 404
    with entry['client'](entry_test_webchat_principal=None) as c:
        assert ask(c, w['owner']).status_code == 404
    with entry['client'](entry_ticket_key_file=None) as c:
        assert ask(c, w['owner'], 'reference_staff').status_code == 404
    assert admin.execute('select count(*) from control.nexloop_entry_tickets').fetchone()[0] == 0
    assert events(admin) == ['issue:webchat:not_owner']
    # The role is the member's current one, read in SQL at every issue (control.nexloop_workbench_role), never the session's.
    assert admin.execute('select control.nexloop_workbench_role(%s,%s)', (TENANT, w['operator']['principal_id'])).fetchone()[0] == 'operator'


def test_the_reference_staff_ticket_is_signed_for_the_pinned_label(entry):
    w, admin = entry['w'], entry['w']['admin']
    with entry['client']() as c:
        r = ask(c, w['owner'], 'reference_staff')
    assert r.status_code == 200
    value = r.json()
    assert value['action'] == 'https://endpoint.example.invalid:8443/staff/entry'
    import base64
    head, mac = value['ticket'].split('.')
    payload = base64.urlsafe_b64decode(head + '=' * (-len(head) % 4))
    assert hmac.compare_digest(base64.urlsafe_b64decode(mac + '=' * (-len(mac) % 4)), hmac.new(entry['key'], E.HMAC_CONTEXT + payload, 'sha256').digest())
    claims = json.loads(payload)
    assert claims['aud'] == 'reference_staff' and claims['sub'] == 'entry-test' and claims['exp'] - claims['iat'] == 30 and len(claims['jti']) == 32
    assert admin.execute('select target,target_principal from control.nexloop_entry_tickets').fetchall() == [('reference_staff', 'entry-test')]


def test_erasure_of_the_customer_and_of_the_issuing_member_and_the_retention_clearing(entry):  # T14
    w, admin = entry['w'], entry['w']['admin']
    persona(admin, entry['consumer'], 'simulated_agent')
    with entry['client']() as c:
        assert ask(c, w['owner']).status_code == 200
        assert ask(c, w['owner']).status_code == 200
    customer, owner = w['customer']['principal_id'], w['owner']['principal_id']
    # The NX-029 erasure deletes the Consumer's owner row (0306/0388): the tickets lose the principal and the client digest, an unused one ends.
    admin.execute("set eios.tenant_id=%s" % ("'" + TENANT + "'"))
    admin.execute("delete from control.nexloop_consumer_owners where tenant_id=%s and principal_id=%s", (TENANT, customer))
    rows = admin.execute('select target_principal,client_digest,used_at is not null,outcome from control.nexloop_entry_tickets').fetchall()
    assert all(r[0].startswith('pseudonym:') and r[0] != customer and r[1] is None and r[2] and r[3] == 'erased' for r in rows) and len(rows) == 2
    assert not admin.execute('select count(*) from control.nexloop_entry_ticket_events where target_principal=%s or client_digest is not null',
                             (customer,)).fetchone()[0]
    # A member erasure (0352 ledger) does the same for the issuing member.
    admin.execute("insert into control.nexloop_workbench_member_erasures(tenant_id,subject_id,principals,erased_at) values(%s,%s,%s,clock_timestamp())",
                  (TENANT, w['owner']['subject_id'], [owner]))
    assert not admin.execute('select count(*) from control.nexloop_entry_tickets where issued_by_principal=%s', (owner,)).fetchone()[0]
    assert not admin.execute('select count(*) from control.nexloop_entry_ticket_events where issued_by_principal=%s', (owner,)).fetchone()[0]
    # Retention: 30 days.
    assert admin.execute('select control.nexloop_entry_tickets_clear(%s,100)', (TENANT,)).fetchone()[0] == {'tickets': 0, 'events': 0}
    admin.execute("update control.nexloop_entry_ticket_events set at=at-interval '31 days'")
    admin.execute("alter table control.nexloop_entry_tickets drop constraint nexloop_entry_tickets_check")  # test only: age the rows
    admin.execute("update control.nexloop_entry_tickets set issued_at=issued_at-interval '31 days'")
    assert admin.execute('select control.nexloop_entry_tickets_clear(%s,100)', (TENANT,)).fetchone()[0] == {'tickets': 2, 'events': 2}


def test_only_identity_calls_the_ticket_functions(entry):
    import psycopg
    admin = entry['w']['admin']
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_runtime'):
        for sql in ("select control.nexloop_entry_ticket_issue('t','a','\\x00','webchat','p','%s',null,30)" % ('a' * 64),
                    "select control.nexloop_entry_ticket_take('t','a','%s',null)" % ('a' * 64)):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
                admin.execute(f'set local role {role}')
                admin.execute(sql)


def test_the_switch_is_stage_only(tmp_path):  # T15
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('check_publication', root / 'scripts/check_publication.py')
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    on = 'NEXLOOP_ENTRY_TICKETS=' + 'st' + 'age'
    assert P.violation('deploy/stage/compose.app.env.example', on.encode()) == 'entry ticket switch enabled'
    assert P.violation('docs/release/x.md', (on + '\n').encode()) == 'entry ticket switch enabled'
    assert P.violation('deploy/community/compose.test.yaml', b'      NEXLOOP_ENTRY_TICKETS: ${NEXLOOP_ENTRY_TICKETS:-off}\n') == \
        'entry ticket switch outside the stage compose'
    assert P.violation('deploy/stage/compose.app.example.yaml', b'      NEXLOOP_ENTRY_TICKETS: ${NEXLOOP_ENTRY_TICKETS:-off}\n') is None
    # Every tracked deployment file outside the stage compose leaves the switch out; none turns it on.
    import subprocess
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    named = [n for n in tracked if n and (n.endswith(('.yaml', '.yml', '.template', '.env.example'))) and 'NEXLOOP_ENTRY_TICKETS' in (root / n).read_text(errors='replace')]
    assert set(named) <= {'deploy/stage/compose.app.example.yaml'}
    # The process: only the value stage turns it on; another value refuses the start.
    assert E.switch_enabled({}) is False and E.switch_enabled({'NEXLOOP_ENTRY_TICKETS': 'off'}) is False
    assert E.switch_enabled({'NEXLOOP_ENTRY_TICKETS': 'st' + 'age'}) is True
    for bad in ('on', 'true', 'production', 'STAGE'):
        with pytest.raises(ValueError):
            E.switch_enabled({'NEXLOOP_ENTRY_TICKETS': bad})
