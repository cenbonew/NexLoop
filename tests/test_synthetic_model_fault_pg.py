"""ADR-045 (0423, stage only): the test-Consumer model fault switch on PostgreSQL. The guard's decision injects a message Run of a
synthetic / simulated_agent Consumer named by the live switch, once per Run (a resume keeps the start's decision, D2); a real Consumer
is never injected; nothing is written without a live switch; a lapsed switch never blocks a new one (D1); the owner's handler refuses
a real Consumer and a second live switch; the audit is append-only; an erasure removes the Consumer from the switch. Synthetic data."""
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A as TENANT
from test_evolution_traces_pg import hexid, run


def replica(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(sql, params)


@pytest.fixture
def fault(admin, pg):
    bootstrap(admin)
    token, _ = seed_authority(admin, TENANT, 'eios:artifact:' + 'e' * 32, identity_suffix='-fault-guard')
    digest = hexid(token)
    consumers = {name: hexid('fault-' + name) for name in ('synthetic', 'agent', 'real', 'other')}
    for name, c in consumers.items():
        replica(admin, "insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at) values(%s,'real','Consumer',%s,1,'{}','synthetic','synthetic',now(),now())", (TENANT, c))
        if name in ('synthetic', 'agent', 'other'):
            admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,%s,1,'synthetic')",
                          (TENANT, c, 'synthetic' if name == 'synthetic' else 'simulated_agent'))

    def api():
        return psycopg.connect(make_conninfo(pg, user='nexloop_api'), autocommit=True)

    def decide(run_id, operation='start'):
        with api() as db:
            return db.execute('select authz.nexloop_synthetic_fault_decide(%s,%s,%s,%s)', (digest, 'real', run_id, operation)).fetchone()[0]

    def message_run(name):
        return run(admin, digest, datetime.now(UTC), consumer=consumers[name], link='message', tenant=TENANT)

    def governed(state, ids=(), remaining=0, seconds=0, intent=None):
        admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
        return admin.execute('select control.nexloop_governed_synthetic_fault_set(%s,%s,%s,%s,%s,%s)',
                             (TENANT, 'real', 'owner-principal', 'owner-subject', intent or str(uuid.uuid4()),
                              Jsonb({'operation': 'set_synthetic_fault', 'request_id': 'r-' + uuid.uuid4().hex[:8], 'state': state,
                                     'consumer_ids': [consumers[n] for n in ids], 'remaining': remaining, 'duration_seconds': seconds,
                                     'reason': 'NX-038 S6 P5 (ADR-045, synthetic)'}))).fetchone()[0]

    def count(table):
        return admin.execute(f'select count(*) from {table}').fetchone()[0]
    return dict(admin=admin, decide=decide, message_run=message_run, governed=governed, count=count, consumers=consumers, digest=digest)


def events(admin):
    return [k for (k,) in admin.execute('select kind from runtime.nexloop_synthetic_fault_events order by event_id')]


def snapshot(admin):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'synthetic_fault'", (TENANT,)).fetchone()[0]


def test_a_listed_synthetic_message_run_is_injected_once_and_a_resume_keeps_it(fault):
    f, admin = fault, fault['admin']
    opened = f['governed']('open', ('synthetic', 'agent'), remaining=3, seconds=600)
    assert opened['opened'] is True and opened['consumers'] == 2
    assert snapshot(admin) == {'active': 1, 'remaining': 3, 'injected_last_hour': 0}
    a, b = f['message_run']('synthetic'), f['message_run']('agent')
    assert f['decide'](a) is True and f['decide'](b) is True
    assert f['decide'](a, 'resume') is True and f['decide'](a) is True  # one decision per Run: no second take
    assert snapshot(admin) == {'active': 1, 'remaining': 1, 'injected_last_hour': 2}
    assert events(admin) == ['opened', 'injected', 'injected']
    assert f['count']('runtime.nexloop_synthetic_fault_decisions') == 2


def test_a_real_consumer_is_never_injected_whatever_the_switch_says(fault):
    f, admin = fault, fault['admin']
    f['governed']('open', ('synthetic',), remaining=5, seconds=600)
    switch = admin.execute('select switch_id from control.nexloop_synthetic_fault_switches').fetchone()[0]
    # Written past the handler (which refuses a real Consumer): even listed, live and with remaining, a real Consumer is not injected.
    replica(admin, "insert into control.nexloop_synthetic_fault_members(tenant_id,world,switch_id,consumer_id) values(%s,'real',%s,%s)",
            (TENANT, switch, f['consumers']['real']))
    assert f['decide'](f['message_run']('real')) is False
    assert f['decide'](f['message_run']('other')) is False  # simulated_agent but not listed
    assert snapshot(admin)['remaining'] == 5 and events(admin) == ['opened']


def test_without_a_live_switch_nothing_is_written(fault):
    f = fault
    for name in ('synthetic', 'real'):
        r = f['message_run'](name)
        assert f['decide'](r) is False and f['decide'](r, 'resume') is False
    assert f['count']('runtime.nexloop_synthetic_fault_decisions') == 0 and f['count']('runtime.nexloop_synthetic_fault_events') == 0
    assert snapshot(f['admin']) == {'active': 0, 'remaining': 0, 'injected_last_hour': 0}


def test_a_run_started_while_closed_is_not_injected_when_resumed_after_an_open(fault):
    f = fault
    r = f['message_run']('synthetic')
    assert f['decide'](r) is False                    # D2: start while no switch is live (no row written)
    f['governed']('open', ('synthetic',), remaining=5, seconds=600)
    assert f['decide'](r, 'resume') is False          # the resume keeps the start's decision: not injected
    assert snapshot(f['admin'])['remaining'] == 5


def test_a_lapsed_switch_without_any_run_still_lets_a_new_one_open(fault):
    f, admin = fault, fault['admin']
    f['governed']('open', ('synthetic',), remaining=5, seconds=60)
    replica(admin, "update control.nexloop_synthetic_fault_switches set opened_at=opened_at-interval '2 hours',expires_at=expires_at-interval '2 hours'")
    assert snapshot(admin)['active'] == 0                       # D1: computed, although closed_at is still empty
    opened = f['governed']('open', ('agent',), remaining=2, seconds=600)
    assert opened['opened'] is True and events(admin) == ['opened', 'expired', 'opened']
    assert admin.execute('select close_reason from control.nexloop_synthetic_fault_switches where closed_at is not null').fetchall() == [('expired',)]
    # The last remaining injection closes it as exhausted.
    assert f['decide'](f['message_run']('agent')) is True and f['decide'](f['message_run']('agent')) is True
    assert events(admin)[-1] == 'exhausted' and snapshot(admin) == {'active': 0, 'remaining': 0, 'injected_last_hour': 2}


def test_the_handler_refuses_a_real_consumer_a_second_switch_and_bad_input_and_closes_idempotently(fault):
    f, admin = fault, fault['admin']
    for args, code in ((('open', ('synthetic', 'real'), 3, 600), 'consumer_not_synthetic'), (('open', ('synthetic',), 51, 600), 'invalid'),
                       (('open', ('synthetic',), 3, 7201), 'invalid'), (('open', (), 3, 600), 'invalid'), (('closed', ('synthetic',), 0, 0), 'invalid')):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match=code), admin.transaction():
            f['governed'](*args)
    f['governed']('open', ('synthetic',), remaining=3, seconds=600)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='switch_open'), admin.transaction():
        f['governed']('open', ('agent',), remaining=3, seconds=600)
    assert f['governed']('closed')['closed'] is True and f['governed']('closed') == {'closed': False, 'reason': 'no_live_switch'}
    assert events(admin) == ['opened', 'closed'] and snapshot(admin)['active'] == 0
    for sql in ('update runtime.nexloop_synthetic_fault_events set remaining=9', 'delete from runtime.nexloop_synthetic_fault_events'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), admin.transaction():
            admin.execute(sql)


def test_a_role_run_is_never_decided_and_an_erasure_removes_the_consumer(fault):
    f, admin = fault, fault['admin']
    f['governed']('open', ('synthetic', 'agent'), remaining=5, seconds=600)
    role = run(admin, f['digest'], datetime.now(UTC), consumer=f['consumers']['synthetic'], link='role', tenant=TENANT)
    assert f['decide'](role) is False
    replica(admin, """insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','fault-erasure-1',%s,'owner','owner-principal','synthetic erasure','pending_confirmation',0)""", (TENANT, f['consumers']['synthetic']))
    # session_replication_role replica skips the trigger: fire it as the erasure would (origin role) on a second request.
    admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','fault-erasure-2',%s,'owner','owner-principal','synthetic erasure','pending_confirmation',0)""", (TENANT, f['consumers']['agent']))
    assert admin.execute('select count(*) from control.nexloop_synthetic_fault_members where consumer_id=%s', (f['consumers']['agent'],)).fetchone()[0] == 0
    assert f['decide'](f['message_run']('agent')) is False


def test_the_decision_is_the_guard_s_alone(fault, pg):
    f = fault
    with psycopg.connect(make_conninfo(pg, user='nexloop_action_worker'), autocommit=True) as db:  # not a guard role
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select authz.nexloop_synthetic_fault_decide(%s,%s,%s,%s)', (f['digest'], 'real', uuid.uuid4(), 'start'))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        f['decide'](uuid.uuid4(), 'model')
    assert f['decide'](uuid.uuid4()) is False  # an unknown Run: no live switch, nothing written
