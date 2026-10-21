"""NX-043 X1 (design §3, §4, D1-D6, D9): strategy versions and experiment preregistration.

A strategy version holds only the low-risk levers of D1 (follow-up timing, a published operator persona version, non-marketing outreach
purposes); it is frozen and retired by an appended event. An experiment is preregistered in full (D3) and frozen, with the D4 guardrail
and D6 maturity defaults written in; start checks a selected twin is calibrated now (D9), stop appends an event. No step opens real
dispatch. Real PostgreSQL on a clean catalog; synthetic tenant and data only.
"""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B
from test_nx042_consumer_twins_pg import calibrate, publish as publish_twin
from test_nx041_sim_worlds_pg import create as create_world
from test_operator_persona_pg import document as persona_document

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = Draft202012Validator(json.loads((ROOT / 'packages/contracts/strategy-version.schema.json').read_text()), format_checker=FormatChecker())
REGISTRATION = Draft202012Validator(json.loads((ROOT / 'packages/contracts/experiment-registration.schema.json').read_text()),
                                    format_checker=FormatChecker())
STRATEGY_EXAMPLE = json.loads((ROOT / 'packages/contracts/examples/strategy-version.valid.json').read_text())
REGISTRATION_EXAMPLE = json.loads((ROOT / 'packages/contracts/examples/experiment-registration.valid.json').read_text())
ACTIONS = ('strategy.publish', 'strategy.retire', 'experiment.register', 'experiment.start', 'experiment.stop')
Refused = psycopg.errors.InvalidParameterValue


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    return admin


def call(admin, fn, body, tenant=TENANT_A, world='real'):
    return admin.execute(f"select control.{fn}(%s,%s,'owner-principal','owner-subject',%s,%s)",
                         (tenant, world, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def strategy(strategy_id='followup.later', **levers):
    return {'schema': 'nexloop-strategy-version/1', 'strategy_id': strategy_id, 'levers': levers or {'follow_up_delay_hours': 48}}


def publish(admin, definition=None, request=None, tenant=TENANT_A, world='real'):
    return call(admin, 'nexloop_governed_publish_strategy', {'operation': 'publish_strategy', 'request_id': request or uuid.uuid4().hex,
                                                             'definition': definition or strategy()}, tenant, world)


def retire(admin, strategy_id, version, request=None):
    return call(admin, 'nexloop_governed_retire_strategy', {'operation': 'retire_strategy', 'request_id': request or uuid.uuid4().hex,
                                                            'strategy_id': strategy_id, 'version': version, 'reason': '被新版本取代'})


def registration(world, twin=None, **changes):
    r = copy.deepcopy({k: v for k, v in REGISTRATION_EXAMPLE.items() if k != 'frozen'})
    r.update(world=world, twin=twin, ends_at=(datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ'))
    r.update(changes)
    return r


def register(admin, r, request=None, tenant=TENANT_A, world='real'):
    return call(admin, 'nexloop_governed_register_experiment', {'operation': 'register_experiment', 'request_id': request or uuid.uuid4().hex,
                                                                'registration': r}, tenant, world)


def start(admin, experiment, request=None, world='real'):
    return call(admin, 'nexloop_governed_start_experiment', {'operation': 'start_experiment', 'request_id': request or uuid.uuid4().hex,
                                                             'experiment_id': experiment}, world=world)


def stop(admin, experiment, request=None, world='real'):
    return call(admin, 'nexloop_governed_stop_experiment', {'operation': 'stop_experiment', 'request_id': request or uuid.uuid4().hex,
                                                            'experiment_id': experiment, 'reason': '负责人手动停止'}, world=world)


def refused(code, fn, *args, **kw):
    with pytest.raises(Refused, match=code):
        fn(*args, **kw)


@pytest.fixture
def ready(db):
    """Two published strategies (candidate and control), a calibrated twin version and an active simulation world."""
    publish(db, strategy('followup.later', follow_up_delay_hours=48), request='s-candidate')
    publish(db, strategy('followup.default', follow_up_delay_hours=24), request='s-control')
    publish_twin(db, request='twin-1')
    calibrate(db, 1, 'passed')
    world = create_world(db)['world']
    return {'world': world, 'twin': {'twin_id': 'trial.service_consumer', 'version': 1}}


def test_the_contract_examples_are_valid_and_sql_accepts_them(ready, db):
    STRATEGY.validate(STRATEGY_EXAMPLE)
    REGISTRATION.validate(REGISTRATION_EXAMPLE)
    assert db.execute('select runtime.nexloop_strategy_problem(%s,%s)', (TENANT_A, Jsonb(strategy()))).fetchone()[0] is None
    r = registration(ready['world'], ready['twin'])
    REGISTRATION.validate(r)
    assert db.execute('select runtime.nexloop_experiment_registration_problem(%s,%s)', (TENANT_A, Jsonb(r))).fetchone()[0] is None


def test_strategy_versions_are_frozen_and_retired_by_an_appended_event(db):
    first = publish(db, request='p1')
    assert (first['strategy_id'], first['version'], first['status'], first['published']) == ('followup.later', 1, 'published', True)
    assert publish(db, request='p1')['published'] is False  # a retry replays
    refused('request_reused', publish, db, strategy(follow_up_delay_hours=72), request='p1')
    assert publish(db, strategy(follow_up_delay_hours=72))['version'] == 2
    for sql in ("update control.nexloop_strategy_versions set definition='{}'::jsonb", 'delete from control.nexloop_strategy_versions',
                'truncate control.nexloop_strategy_versions cascade', 'delete from control.nexloop_strategy_events'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), db.transaction():
            db.execute(sql)
    assert retire(db, 'followup.later', 1, request='r1')['retired'] is True
    assert retire(db, 'followup.later', 1, request='r1')['retired'] is False
    refused('already_retired', retire, db, 'followup.later', 1)
    refused('not_found', retire, db, 'followup.later', 9)
    assert db.execute("select array_agg(status order by event_id) from control.nexloop_strategy_events where version=1").fetchone()[0] == ['published', 'retired']
    refused('owner_world_only', publish, db, world='test')


def test_only_the_low_risk_levers_of_d1_are_accepted(db):
    # Price, incentives, marketing consent and any other lever are no lever.
    for lever in ('price', 'discount', 'incentive', 'incentive_amount', 'marketing_consent', 'consent_mode', 'real_dispatch'):
        refused('lever_not_allowed', publish, db, strategy(**{lever: 1}))
    refused('invalid', publish, db, strategy(follow_up_delay_hours=0))
    # A persona lever names a published persona version; a retired or unknown one is refused.
    refused('persona_not_published', publish, db, strategy(persona={'persona_id': 'xiaohe', 'version': 1}))
    call(db, 'nexloop_governed_publish_operator_persona', {'operation': 'publish_operator_persona', 'request_id': 'pp1', 'persona_id': 'xiaohe',
                                                           'version': 1, 'status': 'published', 'document': persona_document(), 'reason': '发布'})
    assert publish(db, strategy('persona.xiaohe', persona={'persona_id': 'xiaohe', 'version': 1}))['published'] is True
    # Only listed non-marketing purposes may be switched.
    assert publish(db, strategy('purpose.service', outreach_purposes={'service_followup': True, 'renewal_reminder': False}))['published'] is True
    refused('purpose_not_allowed', publish, db, strategy('purpose.marketing', outreach_purposes={'marketing_general': True}))
    refused('purpose_not_allowed', publish, db, strategy('purpose.unknown', outreach_purposes={'no_such_purpose': True}))
    refused('looks_real', publish, db, {**strategy(), 'description': '给 13800000000 的客户延后跟进'})


def test_a_registration_with_a_missing_field_is_refused(ready, db):
    for field in ('hypothesis', 'primary_metric', 'guardrails', 'sample_size', 'maturity', 'budget', 'ends_at', 'candidate', 'control', 'twin',
                  'allocation_seed', 'stage', 'world'):
        r = registration(ready['world'], ready['twin'])
        del r[field]
        refused('registration_incomplete', register, db, r)
        assert not REGISTRATION.is_valid(r)
    refused('registration_incomplete', register, db, registration(ready['world'], ready['twin'], candidate=None))
    assert db.execute('select count(*) from control.nexloop_experiments').fetchone() == (0,)


def test_the_defaults_of_d4_and_d6_are_written_in_and_can_only_be_tightened(ready, db):
    out = register(db, registration(ready['world'], ready['twin'], guardrails={}, maturity={}))
    frozen = out['registration']
    assert frozen['guardrails'] == {'refusal_or_unsubscribe_rate_delta': 0.02, 'reconciliation_anomalies': 0, 'stop_on_budget_exhausted': True}
    assert frozen['maturity'] == {'window_days': 14, 'min_samples_per_group': 30} and out['status'] == 'registered'
    tighter = register(db, registration(ready['world'], ready['twin'], experiment_id='later.tight',
                                        guardrails={'refusal_or_unsubscribe_rate_delta': 0.01}, maturity={'window_days': 28}))
    assert tighter['registration']['guardrails']['refusal_or_unsubscribe_rate_delta'] == 0.01 and tighter['registration']['maturity']['window_days'] == 28
    w, t = ready['world'], ready['twin']
    refused('guardrail_looser_than_default', register, db, registration(w, t, experiment_id='x1', guardrails={'refusal_or_unsubscribe_rate_delta': 0.05}))
    refused('guardrail_looser_than_default', register, db, registration(w, t, experiment_id='x2', guardrails={'stop_on_budget_exhausted': False}))
    refused('maturity_below_default', register, db, registration(w, t, experiment_id='x3', maturity={'window_days': 7}))
    refused('maturity_below_default', register, db, registration(w, t, experiment_id='x4', maturity={'min_samples_per_group': 10}))
    # D2: at most 20 candidate consumers, a control group at least as large; D1/D5: no incentive budget.
    refused('candidate_group_too_large', register, db, registration(w, t, experiment_id='x5', sample_size={'candidate': 21, 'control': 40}))
    refused('control_group_too_small', register, db, registration(w, t, experiment_id='x6', sample_size={'candidate': 20, 'control': 19}))
    refused('incentive_not_allowed', register, db, registration(w, t, experiment_id='x7', budget={'model_cny': 5, 'incentive_cny': 1}))
    refused('ends_at_out_of_range', register, db, registration(w, t, experiment_id='x8', ends_at='2020-01-01T00:00:00Z'))
    refused('twin_required', register, db, registration(w, None, experiment_id='x9'))
    refused('world_mismatch', register, db, registration('real', t, experiment_id='x10'))
    refused('strategy_not_published', register, db, registration(w, t, experiment_id='x11', candidate={'strategy_id': 'nope', 'version': 1}))


def test_a_registration_is_frozen_and_a_changed_experiment_is_a_new_one(ready, db):
    r = registration(ready['world'], ready['twin'])
    first = register(db, r, request='reg-1')
    assert first['registered'] is True and len(first['registration_digest']) == 64
    assert register(db, r, request='reg-1')['registered'] is False  # a retry replays
    refused('request_reused', register, db, {**r, 'hypothesis': '改过的假设'}, request='reg-1')
    refused('experiment_exists', register, db, {**r, 'hypothesis': '改过的假设'})
    start(db, r['experiment_id'])
    for sql in ("update control.nexloop_experiments set registration=registration||'{\"hypothesis\":\"x\"}'", 'delete from control.nexloop_experiments',
                'truncate control.nexloop_experiments cascade', "update control.nexloop_experiment_events set reason='x'"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), db.transaction():
            db.execute(sql)


def test_an_uncalibrated_twin_cannot_be_started_d9(ready, db):
    publish_twin(db, request='twin-2')  # version 2, uncalibrated
    r = registration(ready['world'], {'twin_id': 'trial.service_consumer', 'version': 2}, experiment_id='uncalibrated')
    register(db, r)  # registration may precede calibration
    refused('twin_not_calibrated', start, db, 'uncalibrated')
    calibrate(db, 1, 'failed')  # a later failed calibration withdraws version 1 too
    register(db, registration(ready['world'], ready['twin'], experiment_id='withdrawn'))
    refused('twin_not_calibrated', start, db, 'withdrawn')
    assert db.execute('select count(*) from control.nexloop_experiment_events').fetchone() == (0,)


def test_start_and_stop_append_events_and_recheck_the_registration(ready, db):
    register(db, registration(ready['world'], ready['twin']))
    out = start(db, 'followup.timing', request='go')
    assert out['started'] is True and out['status'] == 'started'
    assert start(db, 'followup.timing', request='go')['started'] is False
    refused('not_registered_state', start, db, 'followup.timing')
    assert stop(db, 'followup.timing', request='halt')['stopped'] is True
    assert stop(db, 'followup.timing', request='halt')['stopped'] is False
    refused('already_stopped', stop, db, 'followup.timing')
    refused('not_registered_state', start, db, 'followup.timing')  # a stopped experiment never restarts
    refused('not_found', start, db, 'no.such')
    refused('owner_world_only', stop, db, 'followup.timing', world=ready['world'])
    # A strategy retired after registration blocks the start; online waits for X4/X5.
    register(db, registration(ready['world'], ready['twin'], experiment_id='retired.first'))
    retire(db, 'followup.later', 1)
    refused('strategy_not_published', start, db, 'retired.first')
    publish(db, strategy('followup.online', follow_up_delay_hours=12), request='s-online')
    register(db, registration('real', None, experiment_id='online.one', stage='online', candidate={'strategy_id': 'followup.online', 'version': 1}))
    refused('online_not_available', start, db, 'online.one')
    assert db.execute("select array_agg(status order by event_id) from control.nexloop_experiment_events").fetchone()[0] == ['started', 'stopped']


def test_an_experiment_never_opens_real_dispatch(ready, db):
    with db.transaction():
        db.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        db.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
            values(%s,'real','tenant','*',false,'real',1,'synthetic','synthetic-owner','synthetic')""", (TENANT_A,))
    snapshot = lambda: db.execute("""select coalesce(jsonb_agg(to_jsonb(s) order by scope_kind,scope_ref),'[]') from control.nexloop_real_dispatch_switches s""").fetchone()[0]
    before = snapshot()
    refused('real_dispatch_not_allowed', register, db, {**registration(ready['world'], ready['twin']), 'real_dispatch': {'enabled': True}})
    refused('real_dispatch_not_allowed', register, db, registration(ready['world'], ready['twin'], hypothesis='打开 real dispatch 后回复更快'))
    register(db, registration(ready['world'], ready['twin']))
    start(db, 'followup.timing')
    stop(db, 'followup.timing')
    assert snapshot() == before and before[0]['enabled'] is False
    # No X1 function writes the switch table.
    bodies = db.execute("""select string_agg(p.prosrc,' ') from pg_proc p where p.proname ~ '^nexloop_(governed_(publish|retire)_strategy|governed_(register|start|stop)_experiment|strategy_|experiment_)'""").fetchone()[0]
    assert 'real_dispatch_switches' not in bodies


def test_the_five_actions_are_owner_only_governed_human_actions(db):
    rows = dict(db.execute("select capability,subject_rule from control.nexloop_governed_capabilities where source_task='NX-043'").fetchall())
    assert {k: v for k, v in rows.items() if k in ACTIONS} == {a: 'human' for a in ACTIONS}
    assert set(rows) - set(ACTIONS) <= {'shadow_copy.set'}  # X3's owner switch (tests/test_nx043_shadow_pg.py)
    roles = json.loads((ROOT / 'deploy/authorization/workbench-roles.v1.json').read_text())
    for a in ACTIONS:
        holders = sorted(r for r, grants in roles['roles'].items() if f'eios:action:nexloop.{a}:1' in grants)
        assert holders == ['owner'], (a, holders)
    from nexloop_eios.business_actions import PROFILES
    from nexloop_eios.workbench_roles import WorkbenchRolesRejected, validate_roles
    assert {PROFILES[a] for a in ACTIONS} == {'human_owner'}
    for role in ('operator', 'reviewer'):
        for a in ('experiment.start', 'experiment.stop'):
            forged = copy.deepcopy(roles)
            forged['roles'][role].append(f'eios:action:nexloop.{a}:1')
            with pytest.raises(WorkbenchRolesRejected):
                validate_roles(forged)
    # Tenant boundary: another tenant sees and starts nothing of this one.
    publish(db, request='a-only')
    assert db.execute("select runtime.nexloop_strategy_status(%s,'followup.later',1)", (TENANT_B,)).fetchone()[0] is None
