"""NX-043 X5 (design §4, §5, D4, D5): stopping rules and the experiment budget.

Each guardrail stops a started experiment (one appended stopped event by the guard), after which every consumer is back on the control
strategy; a spend beyond the experiment's cap is refused and stops it; no step touches real dispatch. Real PostgreSQL on a clean
catalog; synthetic tenant and synthetic consumers only.
"""
import json
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_nx042_consumer_twins_pg import calibrate
from test_nx043_allocation_pg import EXPERIMENT, assign, consumer, strategy_for
from test_nx043_experiments_pg import db, publish, ready, refused, register, registration, start, stop, strategy  # noqa: F401


def guard(admin, experiment=EXPERIMENT):
    return admin.execute('select runtime.nexloop_experiment_guard(%s,%s)', (TENANT_A, experiment)).fetchone()[0]


def spend(admin, category, amount, ref=None, experiment=EXPERIMENT):
    return admin.execute('select runtime.nexloop_experiment_spend(%s,%s,%s,%s::numeric,%s)',
                         (TENANT_A, experiment, category, amount, ref or 'spend-' + uuid.uuid4().hex[:12])).fetchone()[0]


def stop_event(admin, experiment=EXPERIMENT):
    return admin.execute("""select reason,recorded_by from control.nexloop_experiment_events where experiment_id=%s and status='stopped'""",
                         (experiment,)).fetchall()


def arms(admin, world, n=15):
    decided = {i: strategy_for(admin, world, consumer(i)) for i in range(n)}
    return [i for i, d in decided.items() if d and d['arm'] == 'candidate'], [i for i, d in decided.items() if d and d['arm'] == 'control']


@pytest.fixture
def running(ready, db):
    register(db, registration(ready['world'], ready['twin'], sample_size={'candidate': 5, 'control': 10}))
    start(db, EXPERIMENT)
    return ready


def assert_back_on_control(admin, world, members):
    for i in members:
        d = strategy_for(admin, world, consumer(i))
        assert d['applied'] == 'control' and d['strategy']['strategy_id'] == 'followup.default' and d['status'] == 'stopped'


def test_a_quiet_experiment_keeps_running(running, db):
    arms(db, running['world'])
    out = guard(db)
    assert out['stopped'] is False and out['guardrail'] is None and out['metrics']['candidate'] >= 1


def test_the_refusal_rate_guardrail_stops_and_returns_everyone_to_control(running, db):
    world = running['world']
    candidates, controls = arms(db, world)
    # One candidate refuses contact after allocation: 1/5 above 0/10 is 20 points over the 2-point default.
    with db.transaction():
        db.execute("""insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,
            rule_version,rule_id,certainty,matched_text,restricted_at) values(%s,%s,%s,true,1,'m-synthetic','c-synthetic',1,'r1','refuse','别再联系',clock_timestamp())""",
                   (TENANT_A, world, consumer(candidates[0])))
    out = guard(db)
    assert out['stopped'] is True and out['guardrail'] == 'refusal_rate' and out['metrics']['candidate_refused'] == 1
    assert stop_event(db) == [('guardrail: refusal_rate', 'nexloop:experiment-guard')]
    assert_back_on_control(db, world, candidates + controls)
    assert guard(db)['stopped'] is False  # already stopped: nothing more is appended
    assert len(stop_event(db)) == 1


def test_a_marketing_withdrawal_counts_as_an_unsubscribe(running, db):
    world = running['world']
    candidates, _ = arms(db, world)
    db.execute("""insert into runtime.nexloop_marketing_consents(tenant_id,world,consumer_id,kind,source,evidence_ref,recorded_by,request_intent,reason)
        values(%s,%s,%s,'withdrawn','offline','synthetic-evidence','owner-principal','intent-x','退订')""", (TENANT_A, world, consumer(candidates[0])))
    assert guard(db)['guardrail'] == 'refusal_rate'


def test_a_reconciliation_anomaly_stops_an_online_experiment(ready, db):
    publish(db, strategy('followup.online', follow_up_delay_hours=12), request='s-online')
    register(db, registration('real', None, experiment_id='online.one', stage='online', candidate={'strategy_id': 'followup.online', 'version': 1}))
    # X1 refuses an online start until the guard is scheduled; the started event is seeded as start will write it then.
    db.execute("""insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,recorded_by,request_id)
        values(%s,'online.one','started','owner-principal','seeded-start')""", (TENANT_A,))
    member = consumer('online-member')
    assert assign(db, member, experiment='online.one')['arm'] in ('candidate', 'control')
    assert guard(db, 'online.one')['stopped'] is False
    # An effect of that consumer ends unknown (seeded without its context chain: foreign-key triggers off for this one insert).
    with db.transaction():
        db.execute("set local session_replication_role=replica")
        db.execute("""insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,plan_step_identity,
            slot_identity,action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,capability,executor_principal,
            control_revision,created_at,state) values(%s,%s,%s,%s,'real',%s,'g','p','s','nexloop.outreach.send',1,%s,%s,'{}','{}','{}','synthetic-executor',1,clock_timestamp(),'unknown')""",
                   (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), TENANT_A, member, 'a' * 64, 'b' * 64))
    out = guard(db, 'online.one')
    assert out['guardrail'] == 'reconciliation_anomaly' and out['metrics']['reconciliation_anomalies'] == 1
    # The effect is only reconciled: the guard never touches it.
    assert db.execute("select state from runtime.nexloop_effect_intents where consumer_id=%s", (member,)).fetchone() == ('unknown',)


def test_the_budget_blocks_and_stops(running, db):
    # D5: model within the cap (10 in the example registration) is admitted; beyond it refused and stopped.
    assert spend(db, 'model', 4, ref='m1')['allowed'] is True
    assert spend(db, 'model', 4, ref='m1')['replayed'] is True
    refused('request_reused', spend, db, 'model', 5, ref='m1')
    out = spend(db, 'model', 7)
    assert (out['allowed'], out['reason'], out['stopped']) == (False, 'budget_exhausted', True)
    assert stop_event(db) == [('guardrail: budget_exhausted', 'nexloop:experiment-guard')]
    assert spend(db, 'model', 1)['allowed'] is False  # a stopped experiment spends nothing
    assert db.execute('select coalesce(sum(amount_cny),0) from control.nexloop_experiment_spend').fetchone()[0] == 4
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), db.transaction():
        db.execute('delete from control.nexloop_experiment_spend')


def test_the_incentive_budget_is_zero_and_reaching_the_model_cap_stops(ready, db):
    register(db, registration(ready['world'], ready['twin']))
    start(db, EXPERIMENT)
    out = spend(db, 'incentive', 0.01)
    assert (out['allowed'], out['stopped']) == (False, True)
    register(db, registration(ready['world'], ready['twin'], experiment_id='exact'))
    start(db, 'exact')
    assert spend(db, 'model', 10, experiment='exact') == {'allowed': True, 'replayed': False, 'stopped': True}
    assert stop_event(db, 'exact') == [('guardrail: budget_exhausted', 'nexloop:experiment-guard')]


def test_a_withdrawn_twin_and_the_end_time_stop(running, db):
    calibrate(db, 1, 'failed')
    assert guard(db)['guardrail'] == 'twin_withdrawn'
    # The end time: an experiment past ends_at is stopped by the guard (the registration is frozen, so time is moved by the row's view).
    calibrate(db, 1, 'passed'), calibrate(db, 1, 'passed'), calibrate(db, 1, 'passed')
    register(db, registration(running['world'], running['twin'], experiment_id='ending'))
    start(db, 'ending')
    with db.transaction():
        db.execute("set local session_replication_role=replica")  # frozen-row trigger off to age the end time for this test only
        db.execute("""update control.nexloop_experiments set registration=jsonb_set(registration,'{ends_at}','"2020-01-01T00:00:00Z"') where experiment_id='ending'""")
    assert guard(db, 'ending')['guardrail'] == 'ended'


def test_a_reclassified_purpose_stops_a_running_experiment(ready, db):
    publish(db, strategy('purpose.service', outreach_purposes={'service_followup': True}), request='s-purpose')
    register(db, registration(ready['world'], ready['twin'], experiment_id='purpose.run', candidate={'strategy_id': 'purpose.service', 'version': 1}))
    start(db, 'purpose.run')
    v1 = db.execute('select definition from control.nexloop_outreach_purpose_lists where version=1').fetchone()[0]
    v2 = {**v1, 'version': 2, 'purposes': [{**p, 'kind': 'marketing', 'frequency_class': 'marketing'} if p['purpose_id'] == 'service_followup' else p
                                           for p in v1['purposes']]}
    db.execute("insert into control.nexloop_outreach_purpose_lists(version,definition,definition_digest,published_by) values(2,%s,%s,'synthetic-config')",
               (Jsonb(v2), 'e' * 64))
    assert guard(db, 'purpose.run')['guardrail'] == 'purpose_not_allowed'


def test_guardrails_and_the_budget_never_touch_real_dispatch(running, db):
    with db.transaction():
        db.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        db.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
            values(%s,'real','tenant','*',false,'real',1,'synthetic','synthetic-owner','synthetic')""", (TENANT_A,))
    snapshot = lambda: db.execute("select coalesce(jsonb_agg(to_jsonb(s) order by scope_kind,scope_ref),'[]') from control.nexloop_real_dispatch_switches s").fetchone()[0]
    before = snapshot()
    arms(db, running['world'])
    guard(db)
    spend(db, 'model', 50)
    assert snapshot() == before and before[0]['enabled'] is False
    bodies = db.execute("""select string_agg(p.prosrc,' ') from pg_proc p where p.proname in ('nexloop_experiment_autostop','nexloop_experiment_metrics',
        'nexloop_experiment_guard','nexloop_experiment_spend')""").fetchone()[0]
    assert 'real_dispatch' not in bodies
    for role in ('nexloop_api', 'nexloop_runtime', 'nexloop_domain_worker', 'nexloop_action_worker'):
        assert db.execute("select has_function_privilege(%s,'runtime.nexloop_experiment_spend(text,text,text,numeric,text)','execute')", (role,)).fetchone()[0] is False
    assert json.dumps(stop_event(db))  # the budget hit stopped it
