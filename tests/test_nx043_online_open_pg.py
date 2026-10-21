"""NX-043 online opened (owner 2026-10-21: "this is still a test stage, so it can be opened"; D9 calibration is not a precondition online).

An online experiment (world real) starts through the governed start Action; a twin it selects need not be calibrated (the start then
says calibration_waived, in its result and its audit), while screening keeps D9. The candidate group gets the candidate strategy and an
outreach send records its version; the guard stops the experiment and everyone returns to control; a marketing purpose still blocks the
start; a closed real dispatch switch still refuses (online never bypasses the switches). Real PostgreSQL on a clean catalog; synthetic
tenant and synthetic consumer ids only.
"""
import uuid

import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_nx042_consumer_twins_pg import calibrate
from test_nx043_allocation_pg import consumer, strategy_for
from test_nx043_experiments_pg import db, publish, ready, refused, register, registration, start, strategy  # noqa: F401
from test_nx043_guardrails_pg import guard
from test_nx043_x4_wiring_pg import gate, gates, levers, send

EXPERIMENT = 'online.open'
LEVERS = {'follow_up_delay_hours': 48, 'outreach_purposes': {'service_followup': False}}


def online(admin, experiment=EXPERIMENT, twin=None, strategy_id='online.later'):
    register(admin, registration('real', twin, experiment_id=experiment, stage='online', candidate={'strategy_id': strategy_id, 'version': 1}))
    return start(admin, experiment)


@pytest.fixture
def opened(ready, db):
    publish(db, strategy('online.later', **LEVERS), request='s-online')
    out = online(db)
    assert out['started'] is True and out['status'] == 'started' and 'notice' not in out
    decided = {n: strategy_for(db, 'real', consumer(f'o{n}')) for n in range(15)}
    arms = {arm: [consumer(f'o{n}') for n, d in decided.items() if d and d['arm'] == arm] for arm in ('candidate', 'control')}
    assert arms['candidate'] and arms['control']
    return arms


def test_an_online_experiment_starts_and_screening_keeps_d9(ready, db):
    publish(db, strategy('online.later', **LEVERS), request='s-online')
    assert online(db)['started'] is True
    # Owner 2026-10-21: an uncalibrated twin does not block online; the start says so in its result and its audit.
    calibrate(db, 1, 'failed')  # the twin is no longer selectable
    out = online(db, 'online.twin', twin=ready['twin'])
    assert out['started'] is True and out['notice'] == 'calibration_waived'
    payload = db.execute("""select payload from runtime.audit_events where action='nexloop.experiment.started' and payload->>'experiment_id'='online.twin'""").fetchone()[0]
    assert payload['notice'] == 'calibration_waived'
    member = consumer('twin-member')
    assert strategy_for(db, 'real', member)['experiment_id'] in (EXPERIMENT, 'online.twin')  # allocation is not blocked by the waived twin
    # Screening still needs a calibrated twin (D9).
    register(db, registration(ready['world'], ready['twin'], experiment_id='screening.one'))
    refused('twin_not_calibrated', start, db, 'screening.one')


def test_the_candidate_group_takes_the_candidate_strategy_and_its_send_is_audited(opened, db):
    for c in opened['candidate']:
        assert levers(db, c)['strategy_id'] == 'online.later'
        assert gate(gates(db, c, 'service_followup'), 'purpose')['detail'] == 'purpose_disabled'
    assert all(levers(db, c) is None for c in opened['control'])
    c = opened['candidate'][0]
    send(db, c, {k: v for k, v in levers(db, c).items() if k != 'levers'})
    payload = db.execute("""select payload from runtime.audit_events where action='nexloop.experiment.strategy_applied'""").fetchone()[0]
    assert (payload['experiment_id'], payload['strategy_id'], payload['version']) == (EXPERIMENT, 'online.later', 1)


def test_the_guard_stops_it_and_everyone_returns_to_control(opened, db):
    member = opened['candidate'][0]
    with db.transaction():  # an effect of a member ends unknown (seeded without its context chain, as the guardrail tests do)
        db.execute('set local session_replication_role=replica')
        db.execute("""insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,plan_step_identity,
            slot_identity,action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,capability,executor_principal,
            control_revision,created_at,state) values(%s,%s,%s,%s,'real',%s,'g','p','s','nexloop.outreach.send',1,%s,%s,'{}','{}','{}','synthetic-executor',1,clock_timestamp(),'unknown')""",
                   (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), TENANT_A, member, 'a' * 64, 'b' * 64))
    out = guard(db, EXPERIMENT)
    assert out['stopped'] is True and out['guardrail'] == 'reconciliation_anomaly'
    for c in opened['candidate'] + opened['control']:
        assert levers(db, c) is None and strategy_for(db, 'real', c)['applied'] == 'control'
        assert gate(gates(db, c, 'service_followup'), 'experiment_strategy') is None


def test_a_marketing_purpose_still_blocks_the_online_start(ready, db):
    refused('purpose_not_allowed', publish, db, strategy('online.renewal', outreach_purposes={'renewal_reminder': False}))
    publish(db, strategy('online.service', outreach_purposes={'service_followup': False}), request='s-service')
    register(db, registration('real', None, experiment_id='online.reclassified', stage='online', candidate={'strategy_id': 'online.service', 'version': 1}))
    v1 = db.execute('select definition from control.nexloop_outreach_purpose_lists order by version desc limit 1').fetchone()[0]
    v2 = {**v1, 'version': v1['version'] + 1, 'purposes': [{**p, 'kind': 'marketing', 'frequency_class': 'marketing'} if p['purpose_id'] == 'service_followup' else p
                                           for p in v1['purposes']]}
    db.execute("insert into control.nexloop_outreach_purpose_lists(version,definition,definition_digest,published_by) values(%s,%s,%s,'synthetic-config')",
               (v2['version'], Jsonb(v2), 'e' * 64))
    refused('purpose_not_allowed', start, db, 'online.reclassified')


def test_a_closed_real_dispatch_switch_still_refuses_the_candidate_group(opened, db):
    switches = lambda: db.execute("select coalesce(jsonb_agg(to_jsonb(s) order by scope_kind,scope_ref),'[]') from control.nexloop_real_dispatch_switches s").fetchone()[0]  # noqa: E731
    with db.transaction():
        db.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        db.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
            values(%s,'real','tenant','*',false,'real',1,'synthetic','synthetic-owner','synthetic')""", (TENANT_A,))
    before = switches()
    for c in opened['candidate']:
        g = gate(gates(db, c, 'commitment_followup'), 'real_dispatch')
        assert g['result'] == 'refuse' and g['detail'] != 'open'
    assert switches() == before  # the experiment never writes a switch
