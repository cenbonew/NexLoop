"""S6 stage preflight (scripts/acceptance/s6_preflight.py) on the S6 stage fixture: one read-only report of P1-P5, ok only with data,
an expired or missing evolution budget named as such, too few synthetic Consumers missing, nothing written, no private value printed."""
from __future__ import annotations

import io
import json
import sys
from datetime import timedelta
from pathlib import Path

from support.tenants import TENANT_A
from test_s6_stage_evolution_pg import everything, stage  # noqa: F401 - the stage fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/acceptance'))
import s6_preflight as P  # noqa: E402


def replica(admin, sql, params):
    """The budget limits are append-only (nx022): the fixture's synthetic history is rewritten with the triggers off."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(sql, params)


def call(stage, *more):  # noqa: F811 - the fixture value, passed on
    out = io.StringIO()
    code = P.main(['--config', stage['config'], *more], out=out, now=stage['now'] + timedelta(minutes=5))
    return code, out.getvalue(), json.loads(out.getvalue())


def test_the_fixture_reports_every_item_read_only_and_no_data_is_not_a_pass(stage):  # noqa: F811 - the imported fixture
    before = everything(stage['admin'])
    code, text, doc = call(stage)
    items = doc['items']
    assert (items['P1']['status'], items['P2']['status'], items['P3']['status'], items['P5']['status']) == ('ok', 'ok', 'ok', 'ok')
    assert items['P5']['synthetic_consumers_with_miss'] == 5 and items['P5']['near_duplicate_pairs'] >= 1
    assert items['P3']['unit'] == 'CNY' and items['P3']['days_left'] >= 28
    # The fixture grants the synthetic owner nothing: P4 is not ok, so the whole preflight is not a pass (exit 1).
    assert items['P4']['status'] in ('no_data', 'missing') and doc['passed'] is False and doc['not_ok'] == ['P4'] and code == 1
    assert doc['rule'] == 'no data is not a pass'
    assert everything(stage['admin']) == before
    for secret in stage['tokens']:
        assert secret not in text
    assert stage['pg'] not in text and TENANT_A not in text


def test_an_expired_evolution_budget_is_expired_with_its_renewal(stage):  # noqa: F811
    replica(stage['admin'], """update control.nexloop_budget_limits set period_start=%s,period_end=%s
        where tenant_id=%s and world='real' and budget_kind='evolution'""", (stage['now'] - timedelta(days=37), stage['now'] - timedelta(days=6), TENANT_A))
    code, _, doc = call(stage)
    p3 = doc['items']['P3']
    assert p3['status'] == 'expired' and 'set_budget' in p3['remedy'] and p3['limit_amount'].startswith('100')
    assert 'P3' in doc['not_ok'] and code == 1


def test_a_missing_or_not_started_budget_is_missing(stage):  # noqa: F811
    admin = stage['admin']
    replica(admin, """update control.nexloop_budget_limits set period_start=%s,period_end=%s
        where tenant_id=%s and world='real' and budget_kind='evolution'""", (stage['now'] + timedelta(days=1), stage['now'] + timedelta(days=31), TENANT_A))
    assert call(stage)[2]['items']['P3']['status'] == 'missing'
    replica(admin, "delete from control.nexloop_budget_limits where tenant_id=%s and budget_kind='evolution'", (TENANT_A,))
    p3 = call(stage)[2]['items']['P3']
    assert p3 == {'status': 'missing', 'rows': 0, 'remedy': p3['remedy']} and 'loosen_budget' in p3['remedy']


def test_three_synthetic_consumers_are_missing_and_none_is_no_data(stage):  # noqa: F811
    admin = stage['admin']
    admin.execute('delete from control.nexloop_consumer_personas where tenant_id=%s and consumer_id = any(%s)', (TENANT_A, stage['consumers'][3:]))
    p5 = call(stage)[2]['items']['P5']
    assert p5['status'] == 'missing' and p5['synthetic_consumers_with_miss'] == 3 and 'customer-5' in p5['remedy']
    admin.execute('delete from control.nexloop_consumer_personas where tenant_id=%s', (TENANT_A,))
    assert call(stage)[2]['items']['P5']['status'] == 'no_data'


def test_a_missing_migration_and_an_unreadable_token_are_named(stage, tmp_path):  # noqa: F811
    admin = stage['admin']
    admin.execute("delete from control.schema_migrations where version='0252'")
    p1 = call(stage)[2]['items']['P1']
    assert p1['status'] == 'missing' and p1['missing'] == ['0252']
    config = json.loads(Path(stage['config']).read_text())
    Path(config['roles']['evaluation']['token_file']).chmod(0o644)  # not a private file: not read
    p2 = call(stage)[2]['items']['P2']
    assert p2['status'] == 'no_data' and p2['tokens_read'] == 3
