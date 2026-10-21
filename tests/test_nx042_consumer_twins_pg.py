"""NX-042 T1 (design §2, §3.3, D1, D4, D5, D7): consumer twin definitions and versions.

A twin version (contract nexloop-consumer-twin/1) is published by the owner through a governed Action; the server assigns the version,
freezes it and records its calibration (a report reference and a status: uncalibrated until NX-042 T5 can attest a passed report);
its status is kept as appended events (published, retired). Nothing selectable for NX-043 unless published, not retired and calibrated.
No real content and no world (D5): the version outlives every simulation world. Real PostgreSQL on a clean catalog; synthetic tenant.
"""
import copy
import json
import uuid
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Draft202012Validator(json.loads((ROOT / 'packages/contracts/consumer-twin.schema.json').read_text()), format_checker=FormatChecker())
EXAMPLE = json.loads((ROOT / 'packages/contracts/examples/consumer-twin.valid.json').read_text())
DEFINITION = {k: v for k, v in EXAMPLE.items() if k != 'frozen'}


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    return admin


def call(admin, fn, body, tenant=TENANT_A, world='real'):
    return admin.execute(f"select control.{fn}(%s,%s,'owner-principal','owner-subject',%s,%s)",
                         (tenant, world, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def publish(admin, definition=None, request=None, report=None, tenant=TENANT_A, world='real'):
    body = {'operation': 'publish_consumer_twin', 'request_id': request or uuid.uuid4().hex, 'definition': definition or DEFINITION}
    if report is not None:
        body['calibration_report_ref'] = report
    return call(admin, 'nexloop_governed_publish_consumer_twin', body, tenant, world)


def retire(admin, twin, version, request=None, reason='参数假设被新版本取代'):
    return call(admin, 'nexloop_governed_retire_consumer_twin', {'operation': 'retire_consumer_twin', 'request_id': request or uuid.uuid4().hex,
                                                                  'twin_id': twin, 'version': version, 'reason': reason})


def selectable(admin, twin, version, tenant=TENANT_A):
    return admin.execute('select runtime.nexloop_consumer_twin_selectable(%s,%s,%s)', (tenant, twin, version)).fetchone()[0]


def test_the_contract_example_is_valid_and_its_definition_is_what_sql_accepts(db):
    CONTRACT.validate(EXAMPLE)
    assert db.execute('select runtime.nexloop_consumer_twin_valid(%s)', (Jsonb(DEFINITION),)).fetchone()[0] is True
    # D4 in the contract too: a single probability is no behaviour.
    single = copy.deepcopy(EXAMPLE)
    single['parameters']['behaviors']['payment_intent'] = {'probability': 0.1, 'coverage': 0.8, 'hypotheses': ['discount_intent']}
    assert not CONTRACT.is_valid(single)


def test_the_owner_publishes_frozen_versions_uncalibrated_and_never_selectable_yet(db):
    first = publish(db, request='r1')
    assert (first['twin_id'], first['version'], first['status'], first['published']) == ('trial.service_consumer', 1, 'published', True)
    assert first['calibration'] == {'report_ref': None, 'status': 'uncalibrated'}
    # The frozen part validates as the contract's.
    CONTRACT.validate({**DEFINITION, 'frozen': {k: first[k] for k in ('version', 'definition_digest', 'calibration', 'status')}})
    # A retried request replays; the same request with another definition is refused; another request is the next version.
    assert publish(db, request='r1')['published'] is False
    changed = copy.deepcopy(DEFINITION)
    changed['parameters']['behaviors']['refuse_contact']['high'] = 0.1
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='request_reused'), db.transaction():
        publish(db, changed, request='r1')
    second = publish(db, changed, request='r2', report='twin-calibration:' + 'a' * 64)
    assert second['version'] == 2 and second['definition_digest'] != first['definition_digest']
    # D7: a report reference alone calibrates nothing until T5 attests it passed; NX-043 can select neither version.
    assert second['calibration'] == {'report_ref': 'twin-calibration:' + 'a' * 64, 'status': 'uncalibrated'}
    assert selectable(db, 'trial.service_consumer', 1) is False and selectable(db, 'trial.service_consumer', 2) is False
    # Frozen: no update, delete or truncate of a version, its events or its calibration events.
    for sql in ("update control.nexloop_consumer_twins set calibration_report_ref=null where version=2",
                "update control.nexloop_consumer_twins set definition='{}'", 'delete from control.nexloop_consumer_twins',
                "update control.nexloop_consumer_twin_events set status='retired',reason='x'", 'delete from control.nexloop_consumer_twin_events',
                'truncate control.nexloop_consumer_twins cascade', 'truncate control.nexloop_consumer_twin_calibrations'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), db.transaction():
            db.execute(sql)
    # Tenants are apart: another tenant has its own version 1 of the same twin id.
    assert publish(db, tenant=TENANT_B)['version'] == 1


@pytest.mark.parametrize('label,change', [
    ('a single probability', lambda d: d['parameters']['behaviors'].update(payment_intent={'probability': 0.1, 'coverage': 0.8, 'hypotheses': ['discount_intent']})),
    ('low equal to high (a single probability)', lambda d: d['parameters']['behaviors']['payment_intent'].update(low=0.3, high=0.3)),
    ('an interval narrower than 0.01', lambda d: d['parameters']['behaviors']['payment_intent'].update(low=0.3, high=0.305)),
    ('an email in a statement', lambda d: d['hypotheses'][0].update(statement='客户 someone@example.org 说过晚上回复慢')),
    ('a phone-like number in a statement', lambda d: d['hypotheses'][0].update(statement='尾号 13800001111 的客户晚上回复慢')),
    ('a member number in the description', lambda d: d.update(description='会员 M-1001 的习惯')),
    ('an identifier digest in a statement', lambda d: d['hypotheses'][0].update(statement='consumer ' + 'ab12' * 8)),
    ('low above high', lambda d: d['parameters']['behaviors']['payment_intent'].update(low=0.5, high=0.2)),
    ('coverage 1', lambda d: d['parameters']['behaviors']['payment_intent'].update(coverage=1)),
    ('an unknown hypothesis', lambda d: d['parameters']['behaviors']['payment_intent'].update(hypotheses=['no_such'])),
    ('a fact, not a hypothesis', lambda d: d['hypotheses'][0].update(kind='fact')),
    ('no hypotheses', lambda d: d.update(hypotheses=[])),
    ('real content field', lambda d: d.update(messages=['真实客户的原话'])),
    ('a consumer id', lambda d: d.update(consumer_id='a' * 64)),
    ('duplicate hypothesis keys', lambda d: d['hypotheses'].append(dict(d['hypotheses'][0]))),
    ('another schema', lambda d: d.update(schema='nexloop-consumer-twin/2')),
])
def test_a_definition_outside_the_contract_is_refused(db, label, change):
    from nexloop_eios.consumer_twins import definition_problems
    d = copy.deepcopy(DEFINITION)
    change(d)
    # The contract or the Python mirror refuses it too (JSON Schema cannot compare low and high, nor read the text).
    if label.startswith(('low equal', 'an interval narrower', 'an email', 'a phone', 'a member', 'an identifier')):
        assert not CONTRACT.is_valid(d) or definition_problems(d), label
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer twin: invalid'):
        publish(db, d)
    assert db.execute('select count(*) from control.nexloop_consumer_twins').fetchone()[0] == 0, label


def test_retiring_appends_a_status_and_keeps_the_version(db):
    v = publish(db)
    row = db.execute('select definition,definition_digest from control.nexloop_consumer_twins where version=1').fetchone()
    assert retire(db, 'trial.service_consumer', 1, request='ret-1')['retired'] is True
    assert retire(db, 'trial.service_consumer', 1, request='ret-1')['retired'] is False  # a retried request replays
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='already_retired'), db.transaction():
        retire(db, 'trial.service_consumer', 1)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_found'), db.transaction():
        retire(db, 'trial.service_consumer', 9)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer twin: invalid'), db.transaction():
        retire(db, 'trial.service_consumer', 1, reason=' ')
    events = db.execute('select status,reason from control.nexloop_consumer_twin_events order by event_id').fetchall()
    assert events == [('published', None), ('retired', '参数假设被新版本取代')]
    assert db.execute('select definition,definition_digest from control.nexloop_consumer_twins where version=1').fetchone() == row
    assert selectable(db, 'trial.service_consumer', v['version']) is False


def test_only_the_owner_publishes_from_real(db):
    rows = dict(db.execute("select capability,subject_rule from control.nexloop_governed_capabilities where source_task='NX-042'").fetchall())
    assert rows == {'consumer_twin.publish': 'human', 'consumer_twin.retire': 'human'}
    roles = json.loads((ROOT / 'deploy/authorization/workbench-roles.v1.json').read_text())['roles']
    for name in ('nexloop.consumer_twin.publish', 'nexloop.consumer_twin.retire'):
        assert sorted(r for r, grants in roles.items() if f'eios:action:{name}:1' in grants) == ['owner'], name
    from nexloop_eios.business_actions import PROFILES
    assert PROFILES['consumer_twin.publish'] == PROFILES['consumer_twin.retire'] == 'human_owner'
    for world in ('test', 'sim:' + str(uuid.uuid4())):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='owner_world_only'), db.transaction():
            publish(db, world=world)
    # No application role reaches the tables or the handlers directly.
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_sim_api', 'nexloop_twin_port'):
        assert not db.execute("select has_table_privilege(%s,'control.nexloop_consumer_twins','select,insert,update,delete')", (role,)).fetchone()[0]
        assert not db.execute("select has_function_privilege(%s,'control.nexloop_governed_publish_consumer_twin(text,text,text,text,text,jsonb)','execute')",
                              (role,)).fetchone()[0]


def test_a_twin_has_no_world_and_outlives_a_simulation_world(db):
    """D5: the twin tables have no world column, so the S2 whole deletion never targets them; deleting a world leaves its twins."""
    for table in ('control.nexloop_consumer_twins', 'control.nexloop_consumer_twin_events'):
        assert db.execute("select count(*) from pg_attribute where attrelid=%s::regclass and attname='world' and not attisdropped", (table,)).fetchone()[0] == 0
        assert db.execute('select count(*) from runtime.nexloop_sim_world_targets() t where t.target=%s::regclass', (table,)).fetchone()[0] == 0
    publish(db)
    world = call(db, 'nexloop_governed_create_sim_world', {'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': 'simulation',
                                                            'purpose': 'twin outlives', 'expires_in_days': 7})['world']
    result = call(db, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': world, 'reason': '实验结束'})
    assert result['status'] == 'deleted'
    assert db.execute('select count(*) from control.nexloop_consumer_twins').fetchone()[0] == 1


def test_python_validators_mirror_the_sql_bodies():
    from nexloop_eios.workbench_actions_http import OPERATIONS
    ops = OPERATIONS['publish_consumer_twin']
    assert ops['definition'](DEFINITION) == DEFINITION and ops['calibration_report_ref'](None) is None
    point = copy.deepcopy(DEFINITION)
    point['parameters']['behaviors']['payment_intent'].update(low=0.3, high=0.3)
    with pytest.raises(ValueError):
        ops['definition'](point)
    for bad in (lambda: ops['definition']({'schema': 'other'}), lambda: ops['calibration_report_ref']('report:1'),
                lambda: OPERATIONS['retire_consumer_twin']['version'](0), lambda: OPERATIONS['retire_consumer_twin']['twin_id']('Bad Id')):
        with pytest.raises(ValueError):
            bad()


def calibrate(admin, version, verdict, twin='trial.service_consumer', tenant=TENANT_A):
    """What NX-042 T5 will write after checking the report (no writer exists yet: the owner role writes directly here)."""
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute("""insert into control.nexloop_consumer_twin_calibrations(tenant_id,twin_id,version,report_ref,verdict,recorded_by,request_id)
            values(%s,%s,%s,%s,%s,'t5-test',%s)""", (tenant, twin, version, 'twin-calibration:' + 'c' * 64, verdict, uuid.uuid4().hex))


def test_calibration_is_an_appended_event_and_the_latest_one_decides(db):
    """Review fix: the version row is frozen, so calibration is an event table; status and selectability follow the latest event."""
    publish(db)
    status = lambda: db.execute("select runtime.nexloop_consumer_twin_calibration(%s,'trial.service_consumer',1)", (TENANT_A,)).fetchone()[0]
    assert status() == 'uncalibrated' and selectable(db, 'trial.service_consumer', 1) is False
    calibrate(db, 1, 'passed')
    assert status() == 'calibrated' and selectable(db, 'trial.service_consumer', 1) is True
    assert publish(db, request=None)['calibration']['status'] == 'uncalibrated'  # a new version starts uncalibrated
    calibrate(db, 1, 'failed')  # a later failed calibration withdraws it
    assert status() == 'uncalibrated' and selectable(db, 'trial.service_consumer', 1) is False
    calibrate(db, 1, 'passed')
    retire(db, 'trial.service_consumer', 1)
    assert status() == 'calibrated' and selectable(db, 'trial.service_consumer', 1) is False  # retired is never selectable
    assert selectable(db, 'trial.service_consumer', 1, tenant=TENANT_B) is False  # tenants are apart
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        calibrate(db, 9, 'passed')
    for sql in ("update control.nexloop_consumer_twin_calibrations set verdict='passed'", 'delete from control.nexloop_consumer_twin_calibrations'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), db.transaction():
            db.execute(sql)
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_sim_api', 'nexloop_twin_port'):
        assert not db.execute("select has_table_privilege(%s,'control.nexloop_consumer_twin_calibrations','select,insert,update,delete')", (role,)).fetchone()[0]


def test_the_python_digest_mirrors_the_sql_digest(db):
    from nexloop_eios.consumer_twins import definition_digest
    d = copy.deepcopy(DEFINITION)
    d['description'] = '晚上回复慢的服务型顾客'
    assert publish(db, d)['definition_digest'] == definition_digest(d)
