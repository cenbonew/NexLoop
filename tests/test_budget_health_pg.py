"""Budget health (dispatcher 2026-11-07; migration budget_health, alert rules v32): after the stage model budget's period ended on
11-05 16:00Z nothing said so for two days. The snapshot block budget_health and four rules: budget_expiring (7 days, warning),
budget_expired, budget_unconfigured (a required kind without a row is never a silent pass) and budget_exhausted_recent (an NXB01
refusal in the last hour), critical; a healthy configuration fires none. Real PostgreSQL through the NX-030 fixture and the real
alert evaluator; admin seeds synthetic budget rows and refusals."""
import json
from pathlib import Path

import pytest

from nexloop_eios.observability import apply_rules, load_rules
from test_observability_pg import TENANT, observed  # noqa: F401
from test_outcome_classes_pg import refusal
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'
NEW = ('budget_expiring', 'budget_expired', 'budget_unconfigured', 'budget_exhausted_recent')


def v32():
    return load_rules(RULES / 'alert-rules.v32.json')


def budget(admin, kind, start, end, limit='100'):
    """A budget row of the tenant (start/end: SQL interval offsets from now)."""
    admin.execute(f"""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real',%s,'CNY',%s,clock_timestamp()+interval '{start}',clock_timestamp()+interval '{end}',1)
        on conflict(tenant_id,world,budget_kind) do update set limit_amount=excluded.limit_amount,period_start=excluded.period_start,
        period_end=excluded.period_end,revision=control.nexloop_budget_limits.revision+1""", (TENANT, kind, limit))


def healthy(admin):
    budget(admin, 'model', '-1 day', '30 days')
    budget(admin, 'evolution', '-1 day', '30 days')


def health(admin):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'budget_health'", (TENANT,)).fetchone()[0]


def firing(o):
    o['evaluate']()
    rows = o['admin'].execute('select rule_id from control.nexloop_alert_state where tenant_id=%s and firing and rule_id=any(%s)', (TENANT, list(NEW))).fetchall()
    return {r for (r,) in rows}


def test_v32_adds_exactly_the_four_budget_rules_on_v31():
    v31, new = json.loads((RULES / 'alert-rules.v31.json').read_text()), v32()
    assert new['version'] == 32 and new['rules'][:len(v31['rules'])] == v31['rules'] and len(new['rules']) == len(v31['rules']) + 4
    assert new['decision'].startswith(v31['decision'] + ' v32 ') and '2026-11-07' in new['decision']
    added = {r['rule_id']: (r['metric'], r['condition'], r['threshold'], r['severity']) for r in new['rules'][len(v31['rules']):]}
    assert added == {'budget_expiring': ('budget_health.expiring_7d', 'gt', 0, 'warning'),
                     'budget_expired': ('budget_health.expired', 'gt', 0, 'critical'),
                     'budget_unconfigured': ('budget_health.unconfigured', 'gt', 0, 'critical'),
                     'budget_exhausted_recent': ('budget_health.exhausted_refusals_1h', 'gt', 0, 'critical')}


@pytest.fixture
def ruled(observed):  # noqa: F811
    assert apply_rules(v32(), TENANT, database_url_file=observed['w']['configurator'])['configured'] is True
    return observed


def test_a_healthy_configuration_fires_none(ruled):
    healthy(ruled['admin'])
    h = health(ruled['admin'])
    assert (h['expiring_7d'], h['expired'], h['unconfigured'], h['exhausted'], h['exhausted_refusals_1h']) == (0, 0, 0, 0, 0)
    assert h['required'] == ['model', 'evolution'] and {k: h['kinds'][k]['configured'] for k in ('model', 'evolution')} == {'model': 1, 'evolution': 1}
    assert h['kinds']['model']['expires_in_seconds'] > 29 * 86400 and h['kinds']['model']['ratio'] == 0
    assert firing(ruled) == set()


def test_a_required_kind_without_a_row_is_unconfigured_never_a_pass(ruled):
    budget(ruled['admin'], 'model', '-1 day', '30 days')  # evolution has no row
    h = health(ruled['admin'])
    assert h['unconfigured'] == 1 and h['kinds']['evolution'] == {'configured': 0, 'period_end': None, 'expires_in_seconds': None, 'expired': 0, 'ratio': None}
    assert firing(ruled) == {'budget_unconfigured'}


def test_a_period_ending_within_7_days_warns(ruled):
    healthy(ruled['admin'])
    budget(ruled['admin'], 'model', '-6 days', '1 day')
    h = health(ruled['admin'])
    assert (h['expiring_7d'], h['expired']) == (1, 0) and 0 < h['kinds']['model']['expires_in_seconds'] <= 86400
    assert firing(ruled) == {'budget_expiring'}


def test_an_ended_period_is_expired_as_on_stage_11_05(ruled):
    healthy(ruled['admin'])
    budget(ruled['admin'], 'model', '-9 days', '-2 days')  # the stage case: ended two days ago, no conversation since
    h = health(ruled['admin'])
    assert (h['expired'], h['expiring_7d'], h['exhausted_refusals_1h']) == (1, 0, 0)
    assert h['kinds']['model']['expired'] == 1 and h['kinds']['model']['expires_in_seconds'] < -86400
    assert firing(ruled) == {'budget_expired'}
    budget(ruled['admin'], 'model', '1 day', '8 days')  # not yet started refuses as an ended one (NXB01 budget period closed)
    assert health(ruled['admin'])['expired'] == 1


def test_a_budget_refusal_in_the_last_hour_is_critical(ruled):
    healthy(ruled['admin'])
    refusal(ruled['admin'], 'NXB01')
    refusal(ruled['admin'], 'NXC05')  # another code is not a budget refusal
    assert health(ruled['admin'])['exhausted_refusals_1h'] == 1
    assert firing(ruled) == {'budget_exhausted_recent'}
