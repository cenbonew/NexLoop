"""NX-043 X4 (design §3, §4, D2, D9): online allocation.

Consumers of a started experiment are allocated on first sight, by session cluster, into a frozen arm; the candidate group never
exceeds its cap or the control group's size; a decision takes the candidate strategy only while the experiment runs, and the control
strategy once it stops. Eligibility is frozen in the registration. Nothing opens real dispatch. Real PostgreSQL on a clean catalog;
synthetic tenant and synthetic consumer ids only.
"""
import hashlib
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_nx042_consumer_twins_pg import calibrate
from test_nx043_experiments_pg import (Refused, db, publish, ready, refused, register, registration, start, stop,  # noqa: F401
                                       strategy)

EXPERIMENT = 'followup.timing'


def consumer(n):
    return hashlib.sha256(f'nx043-x4-synthetic-{n}'.encode()).hexdigest()


def assign(admin, c, cluster=None, experiment=EXPERIMENT):
    return admin.execute('select runtime.nexloop_experiment_assign(%s,%s,%s,%s)', (TENANT_A, experiment, c, cluster)).fetchone()[0]


def strategy_for(admin, world, c, cluster=None):
    return admin.execute('select runtime.nexloop_experiment_strategy_for(%s,%s,%s,%s)', (TENANT_A, world, c, cluster)).fetchone()[0]


def counts(admin, experiment=EXPERIMENT):
    return dict(admin.execute("""select coalesce(arm,excluded_reason),count(*) from control.nexloop_experiment_allocations
        where experiment_id=%s group by 1""", (experiment,)).fetchall())


@pytest.fixture
def running(ready, db):
    register(db, registration(ready['world'], ready['twin'], sample_size={'candidate': 5, 'control': 10}))
    start(db, EXPERIMENT)
    return ready


def test_the_eligibility_rule_is_frozen_in_the_registration(ready, db):
    out = register(db, registration(ready['world'], ready['twin']))
    assert out['registration']['eligibility'] == {'persona': 'real', 'exclude_contact_restricted': True, 'exclude_in_takeover': True}
    refused('eligibility_looser_than_default', register, db,
            registration(ready['world'], ready['twin'], experiment_id='loose', eligibility={'exclude_in_takeover': False}))
    refused('eligibility_looser_than_default', register, db,
            registration(ready['world'], ready['twin'], experiment_id='loose2', eligibility={'persona': 'synthetic'}))


def test_an_allocation_is_stable_and_frozen(running, db):
    first = {n: assign(db, consumer(n)) for n in range(8)}
    assert all(v['allocated'] is True and v['arm'] in ('candidate', 'control') for v in first.values())
    again = {n: assign(db, consumer(n), cluster='another-cluster') for n in range(8)}
    assert all(again[n]['allocated'] is False and again[n]['arm'] == first[n]['arm'] for n in range(8))
    for sql in ("update control.nexloop_experiment_allocations set arm='candidate'", 'delete from control.nexloop_experiment_allocations',
                'truncate control.nexloop_experiment_allocations'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), db.transaction():
            db.execute(sql)


def test_a_session_cluster_is_in_one_group(running, db):
    arms = set()
    for cluster in ('session-a', 'session-b', 'session-c'):
        members = [assign(db, consumer(f'{cluster}-{i}'), cluster=cluster) for i in range(3)]
        assert len({m['arm'] for m in members if m['arm']} | {m['excluded_reason'] for m in members if m['excluded_reason']}) >= 1
        assert len({m['arm'] for m in members if m['arm']}) == 1, members
        arms |= {m['arm'] for m in members if m['arm']}
    assert arms <= {'candidate', 'control'}


def test_the_share_and_the_caps_hold_at_every_step(running, db):
    for n in range(40):
        assign(db, consumer(n))
        c = counts(db)
        assert c.get('candidate', 0) <= 5 and c.get('control', 0) <= 10
        assert c.get('candidate', 0) <= c.get('control', 0)  # control is at least half at every moment
    c = counts(db)
    assert c.get('candidate', 0) + c.get('control', 0) == 15 and c['sample_full'] == 25 and c.get('candidate', 0) >= 1


def test_ineligible_consumers_are_excluded_and_counted(running, db):
    world = running['world']
    restricted, taken = consumer('restricted'), consumer('taken')
    with db.transaction():
        db.execute("""insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,
            rule_version,rule_id,certainty,matched_text,restricted_at) values(%s,%s,%s,true,1,'m-synthetic','c-synthetic',1,'r1','refuse','别再联系',now())""",
                   (TENANT_A, world, restricted))
        db.execute("""insert into control.nexloop_takeovers(tenant_id,world,takeover_id,scope_kind,scope_ref,consumer_id,taken_by,start_intent,reason,
            started_at,expires_at,start_revision) values(%s,%s,%s,'consumer',%s,%s,'owner-principal','intent-takeover','人工接管',now(),now()+interval '1 hour',1)""",
                   (TENANT_A, world, uuid.uuid4(), taken, taken))
    assert assign(db, restricted)['excluded_reason'] == 'contact_restricted'
    assert assign(db, taken)['excluded_reason'] == 'in_takeover'
    # A consumer in another running experiment of the same world is not allocated twice.
    publish(db, strategy('followup.other', follow_up_delay_hours=36), request='s-other')
    register(db, registration(world, running['twin'], experiment_id='other', candidate={'strategy_id': 'followup.other', 'version': 1}))
    start(db, 'other')
    busy = consumer('busy')
    assert assign(db, busy)['arm'] is not None
    assert assign(db, busy, experiment='other')['excluded_reason'] == 'in_other_experiment'
    assert counts(db) == {'contact_restricted': 1, 'in_takeover': 1, **{assign(db, busy)['arm']: 1}}


def test_online_eligibility_requires_a_real_persona(ready, db):
    publish(db, strategy('followup.online', follow_up_delay_hours=12), request='s-online')
    register(db, registration('real', None, experiment_id='online.one', stage='online', candidate={'strategy_id': 'followup.online', 'version': 1}))
    # X1 refuses an online start until X5; the started event is seeded here as start will write it then.
    db.execute("""insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,recorded_by,request_id)
        values(%s,'online.one','started','owner-principal','seeded-start')""", (TENANT_A,))
    synthetic = consumer('synthetic-persona')
    db.execute("""insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by)
        values(%s,'real',%s,'synthetic',1,'owner-principal')""", (TENANT_A, synthetic))
    assert assign(db, synthetic, experiment='online.one')['excluded_reason'] == 'persona_not_real'
    assert assign(db, consumer('real-persona'), experiment='online.one')['arm'] in ('candidate', 'control')


def test_a_decision_takes_the_group_strategy_and_returns_to_control_after_stop(running, db):
    world = running['world']
    decided = {n: strategy_for(db, world, consumer(n)) for n in range(15)}
    candidates = [n for n, d in decided.items() if d['arm'] == 'candidate']
    controls = [n for n, d in decided.items() if d['arm'] == 'control']
    assert candidates and controls
    assert all(decided[n]['strategy']['strategy_id'] == 'followup.later' and decided[n]['applied'] == 'candidate' for n in candidates)
    assert all(decided[n]['strategy']['strategy_id'] == 'followup.default' for n in controls)
    assert decided[candidates[0]]['strategy']['levers'] == {'follow_up_delay_hours': 48}
    assert strategy_for(db, world, consumer(99)) is None  # sample full: no experiment, the current policy
    stop(db, EXPERIMENT)
    after = {n: strategy_for(db, world, consumer(n)) for n in candidates + controls}
    assert all(a['applied'] == 'control' and a['strategy']['strategy_id'] == 'followup.default' and a['status'] == 'stopped' for a in after.values())
    assert strategy_for(db, world, consumer('newcomer')) is None
    assert assign(db, consumer('newcomer2'))['arm'] is None  # a stopped experiment allocates nobody


def test_a_withdrawn_calibration_stops_new_allocations_d9(running, db):
    assert assign(db, consumer(1))['allocated'] is True
    calibrate(db, 1, 'failed')
    refused('twin_not_calibrated', assign, db, consumer(2))
    assert assign(db, consumer(1))['allocated'] is False  # an existing allocation is kept


def test_allocation_never_touches_real_dispatch(running, db):
    with db.transaction():
        db.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        db.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
            values(%s,'real','tenant','*',false,'real',1,'synthetic','synthetic-owner','synthetic')""", (TENANT_A,))
    snapshot = lambda: db.execute("select coalesce(jsonb_agg(to_jsonb(s) order by scope_kind,scope_ref),'[]') from control.nexloop_real_dispatch_switches s").fetchone()[0]
    before = snapshot()
    for n in range(20):
        strategy_for(db, running['world'], consumer(n))
    stop(db, EXPERIMENT)
    strategy_for(db, running['world'], consumer(1))
    assert snapshot() == before and before[0]['enabled'] is False
    bodies = db.execute("""select string_agg(p.prosrc,' ') from pg_proc p where p.proname in
        ('nexloop_experiment_assign','nexloop_experiment_strategy_for','nexloop_experiment_eligibility_default')""").fetchone()[0]
    assert 'real_dispatch' not in bodies


def test_no_application_role_runs_allocation_and_start_stop_stay_owner_only(db):
    for role in ('nexloop_api', 'nexloop_runtime', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_sim_api', 'nexloop_twin_port'):
        for fn in ('runtime.nexloop_experiment_assign(text,text,text,text)', 'runtime.nexloop_experiment_strategy_for(text,text,text,text)'):
            assert db.execute('select has_function_privilege(%s,%s,%s)', (role, fn, 'execute')).fetchone()[0] is False, (role, fn)
    rows = dict(db.execute("select capability,subject_rule from control.nexloop_governed_capabilities where capability in ('experiment.start','experiment.stop')").fetchall())
    assert rows == {'experiment.start': 'human', 'experiment.stop': 'human'}


def test_a_growing_candidate_cluster_never_breaks_the_share_or_the_cap(ready, db):
    """X4 review 3.1: members joining a cluster already in the candidate group are checked like a new cluster; one that would put
    the candidate group above the control group or its cap is excluded (never moved to control: a cluster is never split)."""
    register(db, registration(ready['world'], ready['twin'], sample_size={'candidate': 5, 'control': 10}))
    start(db, EXPERIMENT)

    def invariant():
        c = counts(db)
        assert c.get('candidate', 0) <= 5 and c.get('candidate', 0) <= c.get('control', 0), c

    grown = None
    for k in range(30):  # single-member clusters until one draws the candidate group
        out = assign(db, consumer(f'single-{k}'), cluster=f'single-{k}')
        invariant()
        if out['arm'] == 'candidate':
            grown = f'single-{k}'
            break
    assert grown is not None
    joined = [assign(db, consumer(f'{grown}-member-{i}'), cluster=grown) for i in range(12)]
    invariant()
    assert {j['arm'] for j in joined if j['arm']} <= {'candidate'}  # never split into control
    assert any(j['excluded_reason'] == 'candidate_cluster_full' for j in joined)
    for k in range(20):  # more single clusters: the invariant keeps holding as the groups fill
        assign(db, consumer(f'late-{k}'), cluster=f'late-{k}')
        invariant()


def test_a_consumer_being_erased_is_excluded(running, db):
    erased = consumer('being-erased')
    db.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,%s,'erase-synthetic',%s,'owner','owner-principal','顾客要求删除','purging',1)""", (TENANT_A, running['world'], erased))
    assert assign(db, erased)['excluded_reason'] == 'erasing'


def test_a_retired_strategy_or_a_withdrawn_twin_falls_back_at_decision(running, db):
    world = running['world']
    decided = {n: strategy_for(db, world, consumer(n)) for n in range(15)}
    candidate = next(n for n, d in decided.items() if d['arm'] == 'candidate')
    control = next(n for n, d in decided.items() if d['arm'] == 'control')
    calibrate(db, 1, 'failed')  # the twin is withdrawn: candidates take the control strategy
    assert strategy_for(db, world, consumer(candidate))['strategy']['strategy_id'] == 'followup.default'
    calibrate(db, 1, 'passed'), calibrate(db, 1, 'passed'), calibrate(db, 1, 'passed')
    assert strategy_for(db, world, consumer(candidate))['strategy']['strategy_id'] == 'followup.later'
    db.execute("select control.nexloop_governed_retire_strategy(%s,'real','owner-principal','owner-subject','intent-r',%s)",
               (TENANT_A, Jsonb({'operation': 'retire_strategy', 'request_id': 'ret-cand', 'strategy_id': 'followup.later', 'version': 1, 'reason': '撤下'})))
    d = strategy_for(db, world, consumer(candidate))
    assert d['applied'] == 'control' and d['strategy']['strategy_id'] == 'followup.default'
    db.execute("select control.nexloop_governed_retire_strategy(%s,'real','owner-principal','owner-subject','intent-r2',%s)",
               (TENANT_A, Jsonb({'operation': 'retire_strategy', 'request_id': 'ret-ctrl', 'strategy_id': 'followup.default', 'version': 1, 'reason': '撤下'})))
    assert strategy_for(db, world, consumer(control))['strategy'] is None  # the current policy
