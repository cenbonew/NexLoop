"""NX-043 X5 wiring (0296): the guard on the alert evaluator's tick, model and incentive spend at reservation, the stop alert.

The real alert evaluator (service credential on nexloop_domain_worker with the nexloop.alert.evaluate grant) runs the experiment guard
before evaluating alerts; a repeated tick appends nothing; alert rules v11 fire experiment_auto_stopped. A model reservation of an
experiment's Run is written to the experiment ledger, stops it at the cap and is not admitted beyond it; an incentive reservation for
an allocated Consumer is refused. Real PostgreSQL through the NX-030 fixture; synthetic tenant and consumers only.
"""
import json
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.observability import apply_rules, load_rules, validate_rules
from test_nx043_allocation_pg import EXPERIMENT, consumer, strategy_for
from test_nx043_experiments_pg import db, ready, register, registration, start  # noqa: F401
from test_observability_pg import RULES, TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

V10, V11 = RULES.parent / 'alert-rules.v10.json', RULES.parent / 'alert-rules.v11.json'


def stops(admin, experiment=EXPERIMENT):
    return admin.execute("select reason,recorded_by from control.nexloop_experiment_events where experiment_id=%s and status='stopped'",
                         (experiment,)).fetchall()


def reserve(admin, world, kind, consumption, amount, unit='CNY'):
    admin.execute("""insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
        values(%s,%s,%s,%s,%s,%s,%s,'synthetic-service')""", (TENANT, world, kind, consumption, amount, unit, consumption))


def limits(admin, *kinds):
    """The tenant's real budgets (the trusted configuration's rows, seeded): a reservation in real needs its limit."""
    for kind in kinds:
        admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
            values(%s,'real',%s,'CNY',1000,now()-interval '1 day',now()+interval '30 days',1) on conflict do nothing""", (TENANT, kind))


def online(admin):
    """A started online experiment (X1 still refuses an online start; the started event is seeded as start will write it)."""
    from test_nx043_experiments_pg import publish, strategy
    publish(admin, strategy('followup.online', follow_up_delay_hours=12), request='s-online')
    register(admin, registration('real', None, experiment_id='online.one', stage='online', candidate={'strategy_id': 'followup.online', 'version': 1},
                                 sample_size={'candidate': 5, 'control': 10}))
    admin.execute("""insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,recorded_by,request_id)
        values(%s,'online.one','started','owner-principal','seeded-start')""", (TENANT,))


@pytest.fixture
def running(observed, ready, db):  # noqa: F811
    register(db, registration(ready['world'], ready['twin'], sample_size={'candidate': 5, 'control': 10}))
    start(db, EXPERIMENT)
    return {**ready, 'o': observed}


def test_rules_v11_are_v10_plus_the_experiment_stop_rule():
    v10, v11 = json.loads(V10.read_text()), load_rules(V11)
    assert v11['version'] == 11 and v11['rules'][:-1] == v10['rules']
    assert {k: v11['rules'][-1][k] for k in ('rule_id', 'metric', 'condition', 'threshold', 'severity')} == {
        'rule_id': 'experiment_auto_stopped', 'metric': 'experiments.auto_stopped_24h', 'condition': 'gt', 'threshold': 0, 'severity': 'warning'}
    validate_rules(v11)


def test_the_evaluator_tick_stops_on_a_guardrail_once_and_the_alert_fires(running, db):
    o, world = running['o'], running['world']
    assert apply_rules(load_rules(V11), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    decided = {i: strategy_for(db, world, consumer(i)) for i in range(15)}
    candidate = next(i for i, d in decided.items() if d and d['arm'] == 'candidate')
    quiet = o['evaluate']()
    assert (quiet['experiments_checked'], quiet['experiments_stopped']) == (1, 0)
    with db.transaction():
        db.execute("""insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,
            rule_version,rule_id,certainty,matched_text,restricted_at) values(%s,%s,%s,true,1,'m-synthetic','c-synthetic',1,'r1','refuse','别再联系',clock_timestamp())""",
                   (TENANT, world, consumer(candidate)))
    hit = o['evaluate']()
    assert hit['experiments_stopped'] == 1 and stops(db) == [('guardrail: refusal_rate', 'nexloop:experiment-guard')]
    assert db.execute("select firing from control.nexloop_alert_state where rule_id='experiment_auto_stopped'").fetchone() == (True,)
    # A repeated tick is idempotent: nothing checked, nothing appended.
    again = o['evaluate']()
    assert (again['experiments_checked'], again['experiments_stopped']) == (0, 0) and len(stops(db)) == 1
    assert strategy_for(db, world, consumer(candidate))['applied'] == 'control'


def test_model_reservations_are_admitted_to_the_cap_and_stop_beyond_it(running, db):
    world = running['world']
    reserve(db, world, 'model', f'run:{uuid.uuid4()}', 4)
    assert db.execute('select sum(amount_cny) from control.nexloop_experiment_spend').fetchone()[0] == 4 and stops(db) == []
    over = f'run:{uuid.uuid4()}'
    reserve(db, world, 'model', over, 7)  # 4 + 7 > 10: not admitted to the experiment, which stops at once
    assert stops(db) == [('guardrail: budget_exhausted', 'nexloop:experiment-guard')]
    assert db.execute('select count(*) from control.nexloop_experiment_spend where spend_ref=%s', (over,)).fetchone() == (0,)
    # Stopped for its budget: a new Run reservation in its world is refused at dispatch (NXB01, 0364 budget drill / D5), nothing more attributed.
    with pytest.raises(psycopg.Error, match='experiment budget exhausted'), db.transaction():
        reserve(db, world, 'model', f'run:{uuid.uuid4()}', 1)
    assert db.execute('select sum(amount_cny) from control.nexloop_experiment_spend').fetchone()[0] == 4


def test_reaching_the_model_cap_exactly_stops_the_experiment(running, db):
    reserve(db, running['world'], 'model', f'run:{uuid.uuid4()}', 10)
    assert stops(db) == [('guardrail: budget_exhausted', 'nexloop:experiment-guard')]


def bind_run(admin, path, member):
    """A Run of `member` on one of the three paths runtime.nexloop_evo_run_consumer reads (message route, fallback binding, role
    binding), seeded without its credential and context chain (replica, this seed only)."""
    run, message, conversation = uuid.uuid4(), uuid.uuid4().hex, uuid.uuid4().hex
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        if path in ('message', 'fallback'):
            admin.execute("""insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key)
                values(%s,'real',%s,%s,'synthetic-principal',%s)""", (TENANT, conversation, member, conversation))
            admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                values(%s,'real',%s,1,%s,%s,%s,'{}')""", (TENANT, conversation, message, message, 'a' * 64))
        if path == 'message':
            admin.execute("""insert into runtime.nexloop_message_routes(tenant_id,world,message_id,run_id,run_digest,queue,event_id,event_envelope,command,
                command_digest,input_digest,bound_by,context_id) values(%s,'real',%s,%s,'d','q',%s,'{}','{}',%s,%s,'synthetic',%s)""",
                          (TENANT, message, run, uuid.uuid4(), 'b' * 64, 'c' * 64, uuid.uuid4()))
        elif path == 'fallback':
            admin.execute("""insert into runtime.nexloop_fallback_context_bindings(tenant_id,world,message_id,run_id,source_principal,source_digest,context_id,
                artifact_id,namespace,pack_text,pack_digest,command_binding,created_at) values(%s,'real',%s,%s,'synthetic','d',%s,'a1','ns','{}',%s,'{}',now())""",
                          (TENANT, message, run, uuid.uuid4(), 'e' * 64))
        else:
            admin.execute("""insert into authz.nexloop_role_run_bindings(run_id,source_digest,tenant_id,world,consumer_id,link_id,role_id,step_id,link_revision,
                role_revision,step_revision,role_ref,scope,expires_at) values(%s,%s,%s,'real',%s,'l','r','s',1,1,1,'role','scope',now()+interval '1 hour')""",
                          (run, 'a' * 64, TENANT, member))
    return run


def online_members(admin):
    decided = {i: strategy_for(admin, 'real', consumer(f'r{i}')) for i in range(6)}
    candidate = next(f'r{i}' for i, d in decided.items() if d and d['arm'] == 'candidate')
    control = next(f'r{i}' for i, d in decided.items() if d and d['arm'] == 'control')
    return consumer(candidate), consumer(control)


def ledger(admin):
    return sorted(admin.execute("select spend_ref,arm,amount_cny from control.nexloop_experiment_spend where experiment_id='online.one'").fetchall())


def test_a_real_run_is_attributed_on_every_path_and_the_cap_counts_the_candidate_only(observed, ready, db):  # noqa: F811
    """Review 2.1: message, fallback and role Runs of a candidate count toward the cap; a control Run is written with arm control
    (for the report) and never counts (D5, dispatcher 2026-10-21: the cap is the candidate strategy's increment)."""
    online(db)
    limits(db, 'model')
    candidate, control = online_members(db)
    runs = {path: bind_run(db, path, candidate) for path in ('message', 'fallback', 'role')}
    for run in runs.values():
        reserve(db, 'real', 'model', f'run:{run}', 2)
    assert {(r, a) for r, a, _ in ledger(db)} == {(f'run:{run}', 'candidate') for run in runs.values()}
    control_run = bind_run(db, 'message', control)
    reserve(db, 'real', 'model', f'run:{control_run}', 9)  # far beyond the 10 CNY cap if it counted
    assert (f'run:{control_run}', 'control', 9) in ledger(db) and stops(db, 'online.one') == []
    metrics = db.execute("select runtime.nexloop_experiment_metrics(%s,'online.one')", (TENANT,)).fetchone()[0]
    assert (float(metrics['model_cny']), float(metrics['control_model_cny'])) == (6.0, 9.0)
    reserve(db, 'real', 'model', f"run:{bind_run(db, 'message', candidate)}", 4)  # 6 + 4 = 10: the candidate's cap, reached
    assert stops(db, 'online.one') == [('guardrail: budget_exhausted', 'nexloop:experiment-guard')]


def test_an_accounting_error_never_fails_the_real_reservation(observed, ready, db, pg):  # noqa: F811
    """Review 2.2: the experiment accounting waits at most 200 ms for its lock; an error there is counted and the reservation stands."""
    online(db)
    limits(db, 'model')
    candidate, _ = online_members(db)
    run = bind_run(db, 'message', candidate)
    with psycopg.connect(pg, autocommit=False) as holder:
        holder.execute("select pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||%s||':online.one',0))", (TENANT,))
        reserve(db, 'real', 'model', f'run:{run}', 2)  # the experiment lock is held elsewhere: lock_timeout inside the safe block
        holder.rollback()
    assert db.execute("select count(*) from control.nexloop_budget_consumption where consumption_id=%s", (f'run:{run}',)).fetchone() == (1,)
    assert db.execute("select sqlstate from runtime.nexloop_experiment_accounting_failures").fetchall() == [('55P03',)]
    assert ledger(db) == []
    snapshot = db.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'experiments'", (TENANT,)).fetchone()[0]
    assert snapshot['accounting_failures_24h'] == 1


def test_a_failing_guard_never_costs_the_tenant_its_alerts(running, db, monkeypatch):
    """Review 2.3: in SQL each experiment is guarded apart (a failing one is counted, the others are checked); in the evaluator a
    failing guard pass is counted and the alerts are evaluated all the same."""
    o = running['o']
    assert apply_rules(load_rules(V11), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    register(db, registration(running['world'], running['twin'], experiment_id='broken'))
    start(db, 'broken')
    with db.transaction():
        db.execute('set local session_replication_role=replica')  # a registration the guard cannot read, for this test only
        db.execute("""update control.nexloop_experiments set registration=jsonb_set(registration,'{ends_at}','"not a time"') where experiment_id='broken'""")
    out = o['evaluate']()
    assert (out['experiments_checked'], out['experiments_guard_failed'], out['rules_version']) == (1, 1, 11)
    assert out['experiments_guard_sqlstates'] == '22007'  # review 3.2: the failing experiment's SQLSTATE (an invalid timestamp)
    from nexloop_eios import observability
    monkeypatch.setattr(observability.AlertEvaluator, 'guard_experiments', lambda self: (_ for _ in ()).throw(RuntimeError('guard down')))
    out = o['evaluate']()
    assert out['experiments_guard_failed'] == 1 and out['rules_version'] == 11  # the alerts still ran


def test_an_incentive_for_an_allocated_consumer_is_refused(observed, ready, db):  # noqa: F811
    """Incentives are real effects (simulation worlds refuse them), so the path matters for an online experiment."""
    online(db)
    limits(db, 'incentive')
    strategy_for(db, 'real', consumer(1))
    with db.transaction():
        db.execute('set local session_replication_role=replica')  # the issue row without its offering chain, for this seed only
        for intent, member in (('intent-x', consumer(1)), ('intent-y', consumer('outsider'))):
            db.execute("""insert into runtime.nexloop_incentive_issues(tenant_id,world,intent_id,consumer_id,offering_id,offering_revision,binding_id,amount_minor,
                currency,notification_text,initiator,principal_id,origin,run_id,control_snapshot) values(%s,'real',%s,%s,%s,1,%s,100,'CNY','synthetic',
                'agent','synthetic-agent','agent',%s,'{}')""", (TENANT, intent, member, 'b' * 64, 'c' * 64, uuid.uuid4()))
    with pytest.raises(psycopg.Error, match='experiment incentive budget is 0') as refused, db.transaction():
        reserve(db, 'real', 'incentive', 'incentive:intent-x', 100)
    assert refused.value.sqlstate == 'NXB01'  # as a budget exhaustion: the incentive port reports budget_exhausted
    reserve(db, 'real', 'incentive', 'incentive:intent-y', 100)  # a Consumer outside every experiment keeps the tenant budget only


def test_only_the_alert_evaluator_role_runs_the_guard_tick(observed, db):  # noqa: F811
    fn = 'authz.nexloop_experiment_guard_tick(text,text,text,text,text)'
    holders = {r for (r,) in db.execute("""select rolname from pg_roles where rolname like 'nexloop_%%' and has_function_privilege(rolname,%s,'execute')
        and not rolsuper and rolname<>'nexloop_owner'""", (fn,)).fetchall()}
    assert holders == {'nexloop_domain_worker'}
    for internal in ('runtime.nexloop_experiment_guard(text,text)', 'runtime.nexloop_experiment_spend(text,text,text,numeric,text)',
                     'control.nexloop_experiment_budget_attribution()'):
        assert not db.execute("""select bool_or(has_function_privilege(rolname,%s,'execute')) from pg_roles where rolname in
            ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler','nexloop_runtime')""", (internal,)).fetchone()[0], internal
    # Unsigned or foreign-protocol calls are refused even on the granted role.
    with psycopg.connect(make_conninfo(observed['pg'], user='nexloop_domain_worker')) as worker, pytest.raises(psycopg.errors.InsufficientPrivilege):
        worker.execute("select authz.nexloop_experiment_guard_tick('x','real','{}','x','{}')")


def test_the_accounting_block_restores_the_callers_lock_timeout(observed, ready, db, pg):  # noqa: F811
    """Review 2.1: after the experiment accounting succeeds, the rest of the caller's transaction runs on its own lock_timeout - a
    later lock may wait well beyond the block's 200 ms."""
    online(db)
    limits(db, 'model')
    candidate, _ = online_members(db)
    run = bind_run(db, 'message', candidate)
    key = 'nx043-lock-timeout-probe'
    held = threading.Event()

    def hold():
        with psycopg.connect(pg) as other, other.transaction():
            other.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))
            held.set()
            time.sleep(0.6)
    t = threading.Thread(target=hold)
    with db.transaction():
        db.execute("set local lock_timeout='5s'")
        reserve(db, 'real', 'model', f'run:{run}', 2)
        assert db.execute("select current_setting('lock_timeout')").fetchone() == ('5s',)
        t.start()
        held.wait(5)
        started = time.monotonic()
        db.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))  # waits ~0.6 s: no 200 ms timeout left behind
        assert time.monotonic() - started > 0.3
    t.join()
    assert [r for r, *_ in ledger(db)] == [f'run:{run}']
