"""NX-043 X5 wiring (0296): the guard on the alert evaluator's tick, model and incentive spend at reservation, the stop alert.

The real alert evaluator (service credential on nexloop_domain_worker with the nexloop.alert.evaluate grant) runs the experiment guard
before evaluating alerts; a repeated tick appends nothing; alert rules v11 fire experiment_auto_stopped. A model reservation of an
experiment's Run is written to the experiment ledger, stops it at the cap and is not admitted beyond it; an incentive reservation for
an allocated Consumer is refused. Real PostgreSQL through the NX-030 fixture; synthetic tenant and consumers only.
"""
import json
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
    reserve(db, world, 'model', f'run:{uuid.uuid4()}', 1)  # stopped: nothing more is attributed
    assert db.execute('select sum(amount_cny) from control.nexloop_experiment_spend').fetchone()[0] == 4


def test_reaching_the_model_cap_exactly_stops_the_experiment(running, db):
    reserve(db, running['world'], 'model', f'run:{uuid.uuid4()}', 10)
    assert stops(db) == [('guardrail: budget_exhausted', 'nexloop:experiment-guard')]


def test_a_real_run_is_attributed_only_for_a_candidate_consumer(observed, ready, db):  # noqa: F811
    online(db)
    limits(db, 'model')
    decided = {i: strategy_for(db, 'real', consumer(f'r{i}')) for i in range(6)}
    arms = {d['arm']: f'r{i}' for i, d in decided.items() if d}
    runs = {}
    for arm, member in arms.items():
        runs[arm] = uuid.uuid4()
        with db.transaction():
            db.execute('set local session_replication_role=replica')  # the binding without its credential chain, for this seed only
            db.execute("""insert into authz.nexloop_role_run_bindings(run_id,source_digest,tenant_id,world,consumer_id,link_id,role_id,step_id,link_revision,
                role_revision,step_revision,role_ref,scope,expires_at) values(%s,%s,%s,'real',%s,'l','r','s',1,1,1,'role','scope',now()+interval '1 hour')""",
                       (runs[arm], 'a' * 64, TENANT, consumer(member)))
        reserve(db, 'real', 'model', f'run:{runs[arm]}', 2)
    refs = {r for (r,) in db.execute("select spend_ref from control.nexloop_experiment_spend where experiment_id='online.one'").fetchall()}
    assert refs == {f"run:{runs['candidate']}"}


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
                     'control.nexloop_experiment_budget_gate()'):
        assert not db.execute("""select bool_or(has_function_privilege(rolname,%s,'execute')) from pg_roles where rolname in
            ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler','nexloop_runtime')""", (internal,)).fetchone()[0], internal
    # Unsigned or foreign-protocol calls are refused even on the granted role.
    with psycopg.connect(make_conninfo(observed['pg'], user='nexloop_domain_worker')) as worker, pytest.raises(psycopg.errors.InsufficientPrivilege):
        worker.execute("select authz.nexloop_experiment_guard_tick('x','real','{}','x','{}')")
