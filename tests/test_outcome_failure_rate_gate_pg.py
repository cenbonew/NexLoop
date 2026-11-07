"""NX-061 B5 threshold (owner 2026-11-07, as recommended; migration outcome_failure_rate_gate, alert rules v31): a warning when at
least 10 outcomes were decided in the hour and the failure rate is above 0.2. Fewer than 10 decided is insufficient data and no
snapshot (or no outcomes block) is no data: neither fires, neither is a pass. Real PostgreSQL through the NX-030 fixture and the real
alert evaluator; admin seeds synthetic refusals as test_outcome_classes_pg does."""
import json
from pathlib import Path

from nexloop_eios.observability import apply_rules, load_rules
from nexloop_eios.run_outcome_taxonomy import FAILURE_RATE_WARNING, MIN_DECIDED, failure_rate_gate
from test_observability_pg import TENANT, observed  # noqa: F401
from test_outcome_classes_pg import outcomes, refusal
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'
RULE = 'outcomes_failure_rate_high'


def v31():
    return load_rules(RULES / 'alert-rules.v31.json')


def seed(admin, failures, protected):
    for _ in range(failures):
        refusal(admin, 'NXB02')  # concurrent budget consumption: transient, a failure
    for _ in range(protected):
        refusal(admin, 'NXC05')  # contact restricted: protected by rule, decided but not a failure


def firing(o):
    """The real evaluator on v31: whether the B5 rule is firing for the tenant."""
    o['evaluate']()
    row = o['admin'].execute('select firing from control.nexloop_alert_state where tenant_id=%s and rule_id=%s', (TENANT, RULE)).fetchone()
    return bool(row and row[0])


def test_v31_adds_exactly_the_b5_rule_on_v30():
    v30, new = json.loads((RULES / 'alert-rules.v30.json').read_text()), v31()
    assert new['version'] == 31 and len(new['rules']) == len(v30['rules']) + 1 and new['rules'][:-1] == v30['rules']
    assert new['decision'].startswith(v30['decision'] + ' v31 ') and '2026-11-07' in new['decision']
    r = new['rules'][-1]
    assert (r['rule_id'], r['metric'], r['condition'], r['threshold'], r['severity']) == (RULE, 'outcomes.failure_rate_gated', 'gt', FAILURE_RATE_WARNING, 'warning')
    assert (MIN_DECIDED, FAILURE_RATE_WARNING) == (10, 0.2)


def test_too_few_decided_is_insufficient_data_never_a_warning_or_a_pass(observed):  # noqa: F811
    o = observed
    assert apply_rules(v31(), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    x = outcomes(o['admin'])
    assert (x['decided'], x['failure_rate_gated'], x['gate'], x['insufficient_data']) == (0, None, 'insufficient_data', 1)  # a quiet hour
    seed(o['admin'], 3, 0)  # every decided outcome failed, but only 3
    x = outcomes(o['admin'])
    assert (x['decided'], x['failure_rate'], x['failure_rate_gated'], x['gate'], x['min_decided']) == (3, 1, None, 'insufficient_data', 10)
    assert failure_rate_gate({'outcomes': x}) == 'insufficient_data' and not firing(o)


def test_enough_decided_below_or_at_the_threshold_is_within(observed):  # noqa: F811
    o = observed
    apply_rules(v31(), TENANT, database_url_file=o['w']['configurator'])
    seed(o['admin'], 2, 8)  # 10 decided, rate 0.2: not above
    x = outcomes(o['admin'])
    assert (x['decided'], x['failure_rate_gated'], x['gate'], x['insufficient_data']) == (10, 0.2, 'measured', 0)
    assert failure_rate_gate({'outcomes': x}) == 'within' and not firing(o)


def test_enough_decided_above_the_threshold_warns(observed):  # noqa: F811
    o = observed
    apply_rules(v31(), TENANT, database_url_file=o['w']['configurator'])
    seed(o['admin'], 3, 7)  # 10 decided, rate 0.3
    x = outcomes(o['admin'])
    assert (x['decided'], x['failure_rate_gated'], x['gate']) == (10, 0.3, 'measured')
    assert failure_rate_gate({'outcomes': x}) == 'warning' and firing(o)
    event = o['admin'].execute("select severity,kind from control.nexloop_alert_events where tenant_id=%s and rule_id=%s", (TENANT, RULE)).fetchall()
    assert event == [('warning', 'firing')]


def test_no_snapshot_is_no_data_not_a_pass():
    for snapshot in (None, {}, {'outcomes': None}, {'outcomes': {'window_seconds': 3600}}):
        assert failure_rate_gate(snapshot) == 'no_data'
    # A snapshot of before 0418 (no gated field) cannot pass either: enough decided but nothing gated is not judged.
    assert failure_rate_gate({'outcomes': {'decided': 12, 'failure_rate': 0.5}}) == 'insufficient_data'
