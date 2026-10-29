"""NX-043 X7 item 10 (owner 2026-10-28, option A; temporary migration 0364): the budget drill.

A screening registration with "budget_drill": true starts with a published, uncalibrated twin only when every drill condition holds
(simulation world, model cap in (0, 1] CNY, end within an hour); a screening without the flag is still refused twin_not_calibrated
(item 4). Once the drill's experiment is stopped for budget_exhausted, a new Run model reservation in its world is refused with NXB01 at
dispatch, the earlier reservations and the experiment ledger stay; real is never touched. The flag shows in the frozen registration, the
audit, the notice and the report. Real PostgreSQL on a clean catalog; synthetic tenant and data only.
"""
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from support.tenants import TENANT_A
from test_nx042_consumer_twins_pg import calibrate, retire as retire_twin
from test_nx043_experiments_pg import db, publish, publish_twin, refused, register, registration, start, stop, strategy  # noqa: F401
from test_nx041_sim_worlds_pg import create as create_world

DRILL = 'budget.drill'
TWIN = {'twin_id': 'trial.service_consumer', 'version': 1}
NOTICE = "budget drill: the twin is not calibrated; the result does not represent the experiment's effect"


def soon(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')


def drill(world, **changes):
    r = registration(world, TWIN, experiment_id=DRILL, budget={'model_cny': 1, 'incentive_cny': 0}, ends_at=soon(), budget_drill=True)
    r.update(changes)
    return r


def reserve(admin, world, amount, consumption=None):
    consumption = consumption or f'run:{uuid.uuid4()}'
    admin.execute("""insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
        values(%s,%s,'model',%s,%s,'CNY',%s,'synthetic-service')""", (TENANT_A, world, consumption, amount, consumption))
    return consumption


def stops(admin, experiment=DRILL):
    return [r[0] for r in admin.execute("select reason from control.nexloop_experiment_events where experiment_id=%s and status='stopped'", (experiment,))]


@pytest.fixture
def uncalibrated(db):  # noqa: F811
    """Two published strategies, a published twin version that is NOT calibrated, and an active simulation world."""
    publish(db, strategy('followup.later', follow_up_delay_hours=48), request='s-candidate')
    publish(db, strategy('followup.default', follow_up_delay_hours=24), request='s-control')
    publish_twin(db, request='twin-1')
    return create_world(db)['world']


def test_a_drill_registration_is_frozen_with_its_flag_audited_and_noticed(uncalibrated, db):
    out = register(db, drill(uncalibrated))
    assert out['registered'] is True and out['registration']['budget_drill'] is True and out['notice'] == NOTICE
    stored = db.execute('select registration from control.nexloop_experiments where experiment_id=%s', (DRILL,)).fetchone()[0]
    assert stored['budget_drill'] is True
    audit = db.execute("select payload from runtime.audit_events where action='nexloop.experiment.budget_drill'").fetchall()
    assert [a[0]['experiment_id'] for a in audit] == [DRILL] and audit[0][0]['calibration_waived'] == 'budget_drill' and audit[0][0]['notice'] == NOTICE
    assert start(db, DRILL)['started'] is True


@pytest.mark.parametrize('change', [
    {'budget': {'model_cny': 2, 'incentive_cny': 0}},   # above 1 CNY
    {'budget': {'model_cny': 0, 'incentive_cny': 0}},   # no cap: nothing to exhaust
    {'ends_at': 120},                                    # longer than an hour (minutes from when the test runs)
])
def test_a_drill_outside_its_conditions_is_refused(uncalibrated, db, change):
    # An end is given in minutes and turned into a time here, not at collection: a full CI round runs this long after import.
    change = {k: soon(v) if k == 'ends_at' else v for k, v in change.items()}
    refused('budget_drill_not_allowed', register, db, drill(uncalibrated, **change))


def test_a_drill_needs_the_flag_true_a_simulation_world_and_a_published_uncalibrated_twin(uncalibrated, db):
    refused('invalid', register, db, drill(uncalibrated, budget_drill=False))
    refused('world_mismatch', register, db, drill(create_world(db, kind='shadow')['world'], stage='screening'))
    online = drill(uncalibrated, stage='online', twin=None)
    online['world'] = 'real'
    refused('budget_drill_not_allowed', register, db, online)
    calibrate(db, 1, 'passed')
    refused('budget_drill_not_allowed', register, db, drill(uncalibrated))  # a calibrated twin needs no drill
    publish_twin(db, request='twin-2')
    retire_twin(db, 'trial.service_consumer', 2)
    refused('budget_drill_not_allowed', register, db, drill(uncalibrated, twin={'twin_id': 'trial.service_consumer', 'version': 2}))


def test_item_4_still_refuses_a_screening_without_the_flag(uncalibrated, db):
    r = registration(uncalibrated, TWIN, experiment_id='plain.screening', budget={'model_cny': 1, 'incentive_cny': 0}, ends_at=soon())
    register(db, r)
    refused('twin_not_calibrated', start, db, 'plain.screening')
    register(db, drill(uncalibrated))
    assert start(db, DRILL)['started'] is True  # the same world, twin and cap: only the flag differs


def test_spend_is_admitted_until_the_cap_and_new_runs_are_refused_after_it(uncalibrated, db):
    register(db, drill(uncalibrated))
    start(db, DRILL)
    first = reserve(db, uncalibrated, '0.6')  # positive control: below the cap, admitted and attributed
    assert stops(db) == []
    second = reserve(db, uncalibrated, '0.4')  # reaches the cap exactly: admitted, the experiment stops (its stop commits with it)
    assert stops(db) == ['guardrail: budget_exhausted']
    with pytest.raises(psycopg.Error) as refusal, db.transaction():
        reserve(db, uncalibrated, '0.1')
    assert refusal.value.sqlstate == 'NXB01' and 'experiment budget exhausted' in refusal.value.diag.message_primary
    # What was spent stays: both reservations and the experiment ledger.
    assert db.execute("select count(*) from control.nexloop_budget_consumption where world=%s and consumption_id in (%s,%s)",
                      (uncalibrated, first, second)).fetchone() == (2,)
    assert db.execute('select sum(amount_cny) from control.nexloop_experiment_spend where experiment_id=%s', (DRILL,)).fetchone()[0] == 1
    # Only Run reservations of that world: an incentive-free other world, and real, go on.
    other = create_world(db)['world']
    reserve(db, other, '0.1')
    db.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real','model','CNY',1000,now()-interval '1 day',now()+interval '30 days',1) on conflict do nothing""", (TENANT_A,))
    reserve(db, 'real', '0.1')


def test_crossing_the_cap_is_admitted_once_then_refused(uncalibrated, db):
    register(db, drill(uncalibrated))
    start(db, DRILL)
    reserve(db, uncalibrated, '1.5')  # crosses the cap: not attributed, the experiment stops, the Run keeps the world's own budget
    assert stops(db) == ['guardrail: budget_exhausted']
    with pytest.raises(psycopg.Error, match='experiment budget exhausted'), db.transaction():
        reserve(db, uncalibrated, '0.1')


def test_an_owner_stop_or_a_new_started_experiment_does_not_refuse(uncalibrated, db):
    register(db, drill(uncalibrated))
    start(db, DRILL)
    stop(db, DRILL)  # stopped by the owner, not by the budget
    reserve(db, uncalibrated, '0.5')
    other = create_world(db)['world']
    register(db, drill(other, experiment_id='budget.drill2'))
    start(db, 'budget.drill2')
    reserve(db, other, '1')  # exhausts it
    register(db, drill(other, experiment_id='budget.drill3'))
    start(db, 'budget.drill3')  # a started experiment in the world: its Runs are its own again
    reserve(db, other, '0.5')


def test_a_drill_report_carries_the_flag_and_the_notice_in_its_digest(uncalibrated, db):
    register(db, drill(uncalibrated))
    start(db, DRILL)
    report = db.execute('select runtime.nexloop_experiment_report_freeze(%s,%s,%s,%s)',
                        (TENANT_A, DRILL, datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0), 'owner-principal')).fetchone()[0]
    assert report['budget_drill'] is True and report['notice'] == NOTICE
    stored, digest = db.execute('select report,report_digest from control.nexloop_experiment_reports where experiment_id=%s', (DRILL,)).fetchone()
    assert stored['budget_drill'] is True and digest == report['report_digest']
    assert db.execute("select encode(sha256(convert_to(report::text,'UTF8')),'hex')=report_digest from control.nexloop_experiment_reports").fetchone() == (True,)
    assert not db.execute("select count(*) from control.nexloop_experiment_reports where not (report ? 'budget_drill')").fetchone()[0]


def test_no_function_here_touches_a_real_dispatch_switch_or_a_credential(db):  # noqa: F811
    bodies = db.execute("""select string_agg(prosrc,' ') from pg_proc where proname in ('nexloop_experiment_budget_refusal','nexloop_experiment_budget_drill_problem',
        'nexloop_experiment_budget_drill_audit','nexloop_experiment_budget_drill_report','nexloop_experiment_budget_drill_ok')""").fetchone()[0]
    assert 'real_dispatch' not in bodies and 'credential' not in bodies


def test_a_later_unrelated_event_keeps_the_refusal_and_only_a_new_start_lifts_it(uncalibrated, db):
    """L6 S1 on f94107af: the refusal reads the budget_exhausted stop itself, not the latest event's reason."""
    register(db, drill(uncalibrated))
    start(db, DRILL)
    reserve(db, uncalibrated, '1')  # exhausts it
    assert stops(db) == ['guardrail: budget_exhausted']
    # An unrelated event after the stop (another stop entry, as a later writer could append): still refused.
    db.execute("""insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,reason,recorded_by,request_id)
        values(%s,%s,'stopped','an unrelated later stop','synthetic-writer','later-event')""", (TENANT_A, DRILL))
    with pytest.raises(psycopg.Error, match='experiment budget exhausted'), db.transaction():
        reserve(db, uncalibrated, '0.1')
    # A newer experiment of the same world registered but not started: still refused.
    register(db, drill(uncalibrated, experiment_id='budget.next'))
    with pytest.raises(psycopg.Error, match='experiment budget exhausted'), db.transaction():
        reserve(db, uncalibrated, '0.1')
    start(db, 'budget.next')  # started after the stop: lifted
    reserve(db, uncalibrated, '0.1')
