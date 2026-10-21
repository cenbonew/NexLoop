"""NX-043 X6 (design §6, §8, D6): experiment results by maturity window and the frozen report, on PostgreSQL.

Allocations (X4) are written here directly to reach 30+ per arm: X1 caps the candidate arm at 20 (D2) while D6 asks 30 matured per
arm, so a registered online experiment can only report insufficient_sample today (see the implementation document). Payments are
verified real commercial events of customers linked to the Consumers; unknown effects are effect intents left unknown. Synthetic ids.
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_nx043_experiments_pg import db, publish, ready, register, registration  # noqa: F401

EXPERIMENT = 'x6.payment'
NOW = datetime.now(UTC)
TODAY = NOW.replace(hour=0, minute=0, second=0, microsecond=0)  # report as_of: whole UTC days only


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def consumer(arm, n):
    return sha(f'x6-{arm}-{n}')


def replica(admin, sql, params):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(sql, params)


def allocate(admin, arm, n, *, days_ago=20, start=0):
    for i in range(start, start + n):
        replica(admin, """insert into control.nexloop_experiment_allocations(tenant_id,experiment_id,consumer_id,cluster_key,arm,allocated_at)
            values(%s,%s,%s,%s,%s,%s)""", (TENANT_A, EXPERIMENT, consumer(arm, i), consumer(arm, i), arm, NOW - timedelta(days=days_ago)))


def pay(admin, c, *, days_after_allocation=3, allocated_days_ago=20, status='paid', world='real'):
    ref = 'cust-' + c[:12]
    replica(admin, """insert into control.nexloop_commercial_connectors(connector_id,tenant_id,world,data_mode,owner_confirmation,accepted_types,currencies,key_id,
        key_material) values(%s,%s,%s,%s,'synthetic',array['payment'],array['CNY'],'x6-key',decode(repeat('ab',32),'hex')) on conflict do nothing""",
            ('x6-' + world.replace(':', '')[:20].lower().replace('_', '-'), TENANT_A, world, 'real' if world == 'real' else 'test'))
    connector = 'x6-' + world.replace(':', '')[:20].lower().replace('_', '-')
    replica(admin, "insert into control.nexloop_commercial_customer_links(connector_id,customer_ref,tenant_id,world,consumer_id) values(%s,%s,%s,%s,%s) on conflict do nothing",
            (connector, ref, TENANT_A, world, c))
    replica(admin, """insert into runtime.nexloop_commercial_events(tenant_id,world,data_mode,connector_id,source_event_id,event_type,record_kind,record_key,status,occurred_at,
        amount_minor,currency,customer_ref,payload_digest,signature_key_id,disposition,recorded_at) values(%s,%s,%s,%s,%s,'payment','payment',%s,%s,%s,100,'CNY',%s,%s,'k','applied',now())""",
            (TENANT_A, world, 'real' if world == 'real' else 'test', connector, uuid.uuid4().hex, sha('rec' + c + status), status,
             NOW - timedelta(days=allocated_days_ago) + timedelta(days=days_after_allocation), ref, sha('p' + c)))


def unknown(admin, c, *, allocated_days_ago=20):
    replica(admin, """insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,plan_step_identity,slot_identity,
        action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,capability,executor_principal,control_revision,created_at,state)
        values(%s,%s,%s,%s,'real',%s,'g','p','s','nexloop.outreach.send',1,%s,%s,'{}','{}','{}','x',1,%s,'unknown')""",
            (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), TENANT_A, c, sha('b' + c), sha('pp' + c), NOW - timedelta(days=allocated_days_ago - 1)))


def freeze(admin, as_of=None):
    return admin.execute('select runtime.nexloop_experiment_report_freeze(%s,%s,%s,%s)', (TENANT_A, EXPERIMENT, as_of or TODAY, 'owner-principal')).fetchone()[0]


@pytest.fixture
def online(ready, db):  # noqa: F811
    register(db, registration('real', None, experiment_id=EXPERIMENT, stage='online', primary_metric={'key': 'payment', 'direction': 'increase'}))
    return db


def test_under_30_matured_in_an_arm_reports_insufficient_sample_and_nothing_narrower(online):
    admin = online
    allocate(admin, 'candidate', 20)  # the D2 cap
    allocate(admin, 'control', 40)
    allocate(admin, 'candidate', 15, days_ago=5, start=100)  # not matured (14-day window): not counted
    for i in range(10):
        pay(admin, consumer('candidate', i))
    r = freeze(admin)
    assert (r['conclusion'], r['reason']) == ('not_reported', 'insufficient_sample')
    assert r['scores'] == {'candidate': {'matured': 'some'}, 'control': {'matured': 40}} and r['difference'] is None  # under 30: none / some
    assert r['maturity'] == {'window_days': 14, 'min_samples_per_group': 30}
    assert r['hard_gates'] == [{'gate': 'minimum_sample', 'result': 'fail'}]
    text = json.dumps(r)
    assert 'successes' not in text and 'rate' not in text and 'interval' not in text


def test_the_rates_and_the_difference_come_with_intervals_and_unknown_is_never_a_success(online):
    admin = online
    allocate(admin, 'candidate', 40)
    allocate(admin, 'control', 40)
    for i in range(20):
        pay(admin, consumer('candidate', i))
    for i in range(2):
        unknown(admin, consumer('candidate', i))  # paid but an effect left unknown: not a success
    for i in range(10):
        pay(admin, consumer('control', i))
    pay(admin, consumer('control', 30), days_after_allocation=20)  # after the 14-day window: not counted
    pay(admin, consumer('control', 31), status='pending')          # not a completed payment
    r = freeze(admin)
    assert (r['conclusion'], r['reason']) == ('reported', None)
    cand, ctrl = r['scores']['candidate'], r['scores']['control']
    assert (cand['matured'], cand['successes'], cand['unknown'], cand['rate']) == (40, 18, 'some', 0.45)  # 2 unknown: under 30, only 'some'
    assert (ctrl['matured'], ctrl['successes'], ctrl['unknown'], ctrl['rate']) == (40, 10, 'none', 0.25)
    for arm in (cand, ctrl):
        low, high = arm['interval']
        assert 0 <= low < arm['rate'] < high <= 1
    assert cand['interval'] == pytest.approx([0.3071, 0.6017], abs=0.0005)  # Wilson 95% of 18/40 (by hand: centre 0.4544, half width 0.1473)
    d = r['difference']
    assert d['estimate'] == 0.2 and d['method'] == 'newcombe-wilson-95' and d['interval'][0] < 0.2 < d['interval'][1]
    assert d['interval'][0] > -0.1 and d['interval'][1] < 0.45
    assert 'not a general causal effect' in r['statement'] and r['unknown_policy'] == 'an unknown effect is never a success'
    assert r['frozen_inputs_digest'] == admin.execute('select registration_digest from control.nexloop_experiments where experiment_id=%s', (EXPERIMENT,)).fetchone()[0]


def test_the_report_is_frozen_and_names_no_person(online):
    admin = online
    allocate(admin, 'candidate', 35)
    allocate(admin, 'control', 35)
    for i in range(7):
        pay(admin, consumer('control', i))
    first = freeze(admin, TODAY)
    again = freeze(admin, TODAY)
    assert again['report_digest'] == first['report_digest'] and again['replayed'] is True and first['replayed'] is False
    for sql in ("update control.nexloop_experiment_reports set report='{}'", 'delete from control.nexloop_experiment_reports',
                'truncate control.nexloop_experiment_reports'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
            admin.execute(sql)
    text = json.dumps(first)
    for arm in ('candidate', 'control'):
        for i in range(35):
            assert consumer(arm, i) not in text and ('cust-' + consumer(arm, i)[:12]) not in text
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='invalid'):
        freeze(admin, TODAY + timedelta(days=1))  # no report of the future
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='window_not_on_grid'):
        freeze(admin, TODAY - timedelta(hours=1))  # whole UTC days only (no differencing by the hour)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_found'):
        admin.execute('select runtime.nexloop_experiment_report_freeze(%s,%s,%s,%s)', (TENANT_A, 'no.such', TODAY, 'owner-principal'))
    for role in ('nexloop_api', 'nexloop_metrics', 'nexloop_domain_worker', 'nexloop_sim_api'):
        assert not admin.execute("select has_function_privilege(%s,'runtime.nexloop_experiment_report_freeze(text,text,timestamptz,text)','execute')", (role,)).fetchone()[0]


def test_another_world_and_an_unsupported_metric_count_nothing(online, ready):  # noqa: F811
    admin = online
    allocate(admin, 'candidate', 30)
    allocate(admin, 'control', 30)
    for i in range(30):
        pay(admin, consumer('candidate', i), world='test')  # a test-world payment is no real outcome
    r = freeze(admin)
    assert r['scores']['candidate']['successes'] == 0 and r['data_mode'] == 'real'
    register(admin, registration('real', None, experiment_id='x6.reply', stage='online', primary_metric={'key': 'reply_within_24h', 'direction': 'increase'}))
    out = admin.execute('select runtime.nexloop_experiment_report_freeze(%s,%s,%s,%s)', (TENANT_A, 'x6.reply', TODAY, 'owner-principal')).fetchone()[0]
    assert (out['conclusion'], out['reason']) == ('not_reported', 'metric_not_supported') and out['difference'] is None


def test_a_new_report_comes_later_and_after_at_least_ten_new_matured_per_arm(online):
    """L6 X6 review 2.2: two reports of one experiment never differ by one person - a later as_of only, and each arm at least 10 more
    matured Consumers than the last report; the same as_of answers the frozen report."""
    admin = online
    day = lambda n: TODAY - timedelta(days=n)  # noqa: E731
    for arm in ('candidate', 'control'):
        for n, (count, start) in {16: (35, 0), 15: (5, 100), 14: (10, 200)}.items():  # allocated n days before today 00:00 UTC
            for i in range(start, start + count):
                replica(admin, """insert into control.nexloop_experiment_allocations(tenant_id,experiment_id,consumer_id,cluster_key,arm,allocated_at)
                    values(%s,%s,%s,%s,%s,%s)""", (TENANT_A, EXPERIMENT, consumer(arm, i), consumer(arm, i), arm, day(n)))
    first = freeze(admin, day(2))                               # 35 matured per arm
    assert first['scores']['candidate']['matured'] == 35 and first['scores']['control']['matured'] == 35
    assert freeze(admin, day(2))['replayed'] is True             # the same as_of: the frozen report
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='as_of_not_after_last'):
        freeze(admin, day(3))
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='too_few_new_matured'):
        freeze(admin, day(1))                                    # only 5 more per arm
    later = freeze(admin, TODAY)                                 # 15 more per arm
    assert later['scores']['candidate']['matured'] == 50 and later['replayed'] is False
