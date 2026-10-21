"""NX-042 T5 (design §3.2, §3.3, D3, D4, D7): calibration runs and frozen reports of a consumer twin version.

Real PostgreSQL on a clean catalog; the owner's governed handlers are called as the governed entry calls them (as the S2 tests);
real aggregates are seeded with the T4 helpers (synthetic Consumers, triggers off). The calibration window is July 2026 (ended); the
twin version's publication is moved before it as trusted seeding (the version row is frozen otherwise).
- Coverage inside 70%-90% passes (a `passed` event; the version becomes selectable), outside fails (a `failed` event).
- Fewer than 30 Consumers, or dayparts suppressed (fewer than 8 cells), gives no verdict and no event; so does a behaviour without
  real aggregate data (not_calibratable, D4).
- The report is frozen; a calibration event only comes from a report of that version with that conclusion.
- No per-Consumer value, id or text reaches the report.
"""
from datetime import timedelta
import json
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jsonschema import Draft202012Validator
from support.tenants import TENANT_A
from test_nx042_twin_calibration_read_pg import MARK, SPLIT, db, seed  # noqa: F401

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
TWIN = 'trial.calibrated'


def definition(behaviors=None):
    return {'schema': 'nexloop-consumer-twin/1', 'twin_id': TWIN, 'description': '校准测试用孪生（合成参数）',
            'hypotheses': [{'key': 'evening_slower', 'kind': 'hypothesis', 'statement': '假设：晚间回复较慢。'}],
            'parameters': {'behaviors': behaviors or {
                'reply_within_24h': {'low': 0.35, 'high': 0.6, 'coverage': 0.8, 'hypotheses': ['evening_slower']},
                'refuse_contact': {'low': 0.02, 'high': 0.08, 'coverage': 0.8, 'hypotheses': ['evening_slower']}}},
            'generator_version': 'twin-template/1'}


def call(admin, fn, body, world='real'):
    return admin.execute(f"select control.{fn}(%s,%s,'owner-principal','owner-subject',%s,%s)",
                         (TENANT_A, world, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def publish(admin, behaviors=None, published_at=SPLIT - timedelta(days=2)):
    out = call(admin, 'nexloop_governed_publish_consumer_twin', {'operation': 'publish_consumer_twin', 'request_id': uuid.uuid4().hex,
                                                                 'definition': definition(behaviors)})
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('update control.nexloop_consumer_twins set published_at=%s where tenant_id=%s and twin_id=%s and version=%s',
                      (published_at, TENANT_A, TWIN, out['version']))
    return out['version']


def calibrate(admin, version, split=SPLIT, span=28, request=None, world='real'):
    return call(admin, 'nexloop_governed_calibrate_consumer_twin', {'operation': 'calibrate_consumer_twin', 'request_id': request or uuid.uuid4().hex,
                                                                    'twin_id': TWIN, 'version': version, 'split_at': split.date().isoformat(), 'span_days': span}, world)


def refused(admin, reason, *args, **kw):
    with pytest.raises(psycopg.errors.InvalidParameterValue, match=f'consumer twin: {reason}'), admin.transaction():
        calibrate(admin, *args, **kw)
    return True


def report(admin, ref):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    return admin.execute("select report,conclusion,coverage from control.nexloop_twin_calibration_reports where 'twin-calibration:'||report_digest=%s", (ref,)).fetchone()


def events(admin, version):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    return admin.execute('select verdict,report_ref from control.nexloop_consumer_twin_calibrations where tenant_id=%s and twin_id=%s and version=%s order by event_id',
                         (TENANT_A, TWIN, version)).fetchall()


def selectable(admin, version):
    return admin.execute('select runtime.nexloop_consumer_twin_selectable(%s,%s,%s)', (TENANT_A, TWIN, version)).fetchone()[0]


def dayparts(admin, plan, tag='c'):
    """plan: per UTC daypart (replied, refused) of 30 Consumers contacted in the calibration window."""
    for d, (replied, refused_n) in enumerate(plan):
        seed(admin, f'{tag}{d}', 30, SPLIT + timedelta(days=3, hours=6 * d + 1), replied=replied, refused=refused_n)


# 6/8 cells covered (0.75): reply 0.4, 0.5, 0.2 (miss), 0.467; refusal 0.033, 0.067, 0.033, 0.0 (miss).
INSIDE = [(12, 1), (15, 2), (6, 1), (14, 0)]
# 8/8 covered (1.0): intervals too wide for the nominal 80%.
OVER = [(12, 1), (15, 2), (13, 1), (14, 1)]
# 2/8 covered (0.25).
UNDER = [(3, 0), (25, 9), (12, 1), (27, 0)]


def test_coverage_inside_the_band_passes_and_makes_the_version_selectable(db):
    admin, _ = db
    version = publish(admin)
    dayparts(admin, INSIDE)
    assert selectable(admin, version) is False  # uncalibrated before
    out = calibrate(admin, version)
    assert out['conclusion'] == 'passed' and float(out['coverage']) == 0.75 and out['calibration'] == 'calibrated' and out['created'] is True
    body, conclusion, coverage = report(admin, out['report_ref'])
    m = body['metrics']
    assert (m['cell_count'], m['covered_cells'], m['nominal_coverage'], m['coverage_band'], m['min_cells']) == (8, 6, 0.8, [0.7, 0.9], 8)
    assert 0 < m['brier'] < 1 and all(0 <= c['brier'] <= 1 for c in m['cells']) and m['uncalibrated_behaviors'] == []
    assert body['split']['grouping'] == 'consumer' and body['split']['span_days'] == 28 and body['input']['min_group_consumers'] == 30
    Draft202012Validator(json.loads((ROOT / 'packages/contracts/twin-calibration-report.schema.json').read_text())).validate(body)
    assert events(admin, version) == [('passed', out['report_ref'])] and selectable(admin, version) is True
    # The same request replays; nothing new is written.
    request = uuid.uuid4().hex
    first = calibrate(admin, version, request=request)
    again = calibrate(admin, version, request=request)
    assert again == {**first, 'created': False} and len(events(admin, version)) == 2


@pytest.mark.parametrize('plan,why', [(OVER, 'over-coverage'), (UNDER, 'under-coverage')])
def test_coverage_outside_the_band_fails_and_appends_a_failed_event(db, plan, why):
    admin, _ = db
    version = publish(admin)
    dayparts(admin, plan)
    out = calibrate(admin, version)
    body = report(admin, out['report_ref'])[0]
    assert out['conclusion'] == 'failed' and why in body['reason'] and out['calibration'] == 'uncalibrated'
    assert events(admin, version) == [('failed', out['report_ref'])] and selectable(admin, version) is False


def test_a_passed_then_failed_calibration_leaves_the_version_uncalibrated(db):
    admin, _ = db
    version = publish(admin)
    dayparts(admin, INSIDE)
    assert calibrate(admin, version)['calibration'] == 'calibrated'
    dayparts(admin, UNDER, 'later')  # more Consumers in the window: 1/8 covered now; the latest event decides
    out = calibrate(admin, version)
    assert out['conclusion'] == 'failed' and selectable(admin, version) is False


def test_an_insufficient_sample_gives_no_verdict_and_no_event(db):
    admin, _ = db
    version = publish(admin)
    seed(admin, 'few', 29, SPLIT + timedelta(days=2), replied=12, refused=1)
    out = calibrate(admin, version)
    body = report(admin, out['report_ref'])[0]
    assert out['conclusion'] == 'insufficient_sample' and body['input']['groups'] == [] and 'fewer than 30' in body['reason']
    assert body['metrics']['coverage'] is None and events(admin, version) == []
    # 30 in one daypart: `all` alone, two cells - fewer than 8, still no verdict.
    seed(admin, 'one', 1, SPLIT + timedelta(days=2))
    out = calibrate(admin, version)
    body = report(admin, out['report_ref'])[0]
    assert out['conclusion'] == 'insufficient_sample' and body['metrics']['cell_count'] == 2 and 'fewer than 8' in body['reason']
    assert events(admin, version) == [] and selectable(admin, version) is False


def test_a_behaviour_without_real_aggregate_data_is_not_calibratable(db):
    admin, _ = db
    version = publish(admin, {'reply_within_24h': {'low': 0.35, 'high': 0.6, 'coverage': 0.8, 'hypotheses': ['evening_slower']},
                              'payment_intent': {'low': 0.05, 'high': 0.15, 'coverage': 0.8, 'hypotheses': ['evening_slower']}})
    dayparts(admin, INSIDE)
    out = calibrate(admin, version)
    body = report(admin, out['report_ref'])[0]
    assert out['conclusion'] == 'not_calibratable' and body['metrics']['uncalibrated_behaviors'] == ['payment_intent'] and 'payment_intent' in body['reason']
    assert events(admin, version) == [] and selectable(admin, version) is False


def test_the_report_is_frozen_and_events_only_come_from_a_matching_report(db):
    admin, _ = db
    version = publish(admin)
    dayparts(admin, OVER)
    failed = calibrate(admin, version)['report_ref']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    for sql in ("update control.nexloop_twin_calibration_reports set conclusion='passed'", 'delete from control.nexloop_twin_calibration_reports',
                'truncate control.nexloop_twin_calibration_reports'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='frozen'), admin.transaction():
            admin.execute(sql)

    def event(ref, verdict, v=version):
        admin.execute("""insert into control.nexloop_consumer_twin_calibrations(tenant_id,twin_id,version,report_ref,verdict,recorded_by,request_id)
            values(%s,%s,%s,%s,%s,'x',%s)""", (TENANT_A, TWIN, v, ref, verdict, uuid.uuid4().hex))
    # A passed event from a failed report, from no report, or for another version of the same twin: refused.
    other = publish(admin)
    for ref, verdict, v in ((failed, 'passed', version), ('twin-calibration:' + 'e' * 64, 'passed', version), (failed, 'failed', other)):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='matching report'), admin.transaction():
            event(ref, verdict, v)
    assert selectable(admin, version) is False


def test_independence_and_window_rules(db):
    admin, _ = db
    version = publish(admin, published_at=SPLIT + timedelta(days=1))
    assert refused(admin, 'window_before_publication', version)  # its parameters could have seen the window
    today = __import__('datetime').datetime.now(__import__('datetime').UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    early = publish(admin, published_at=today - timedelta(days=60))
    assert refused(admin, 'window_open', early, split=today - timedelta(days=10))
    assert refused(admin, 'owner_world_only', early, world='test')
    assert refused(admin, 'not_found', 99)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer twin: invalid'), admin.transaction():
        calibrate(admin, early, span=30)
    call(admin, 'nexloop_governed_retire_consumer_twin', {'operation': 'retire_consumer_twin', 'request_id': uuid.uuid4().hex,
                                                          'twin_id': TWIN, 'version': early, 'reason': '停用'})
    assert refused(admin, 'not_published', early)


def test_no_per_consumer_value_id_or_text_reaches_the_report(db):
    admin, _ = db
    version = publish(admin)
    dayparts(admin, INSIDE)
    out = calibrate(admin, version)
    text = json.dumps(report(admin, out['report_ref'])[0])
    consumers = [r[0] for r in admin.execute('select consumer_id from runtime.nexloop_conversations where tenant_id=%s', (TENANT_A,)).fetchall()]
    assert len(consumers) == 120 and MARK not in text and not any(c[:16] in text for c in consumers)
    cells = report(admin, out['report_ref'])[0]['metrics']['cells']
    assert all(c['consumers'] >= 30 for c in cells) and set(cells[0]) == {'behavior', 'group', 'consumers', 'observed_rate', 'low', 'high',
                                                                          'nominal_coverage', 'covered', 'brier'}
    # Only the owner function reads the reports; no application role holds the table.
    assert admin.execute("""select count(*) from pg_roles r where not r.rolsuper and r.rolname<>'nexloop_owner' and r.rolname !~ '^pg_'
        and has_table_privilege(r.oid,'control.nexloop_twin_calibration_reports','select')""").fetchone()[0] == 0
