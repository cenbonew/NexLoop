"""P0 2026-11-10 (L6 Q10; stage 11-09/10: every relay reply to an existing customer failed for 20 hours and nothing alerted): rules
v37 add relay_reply_failing on relay_replies.failing_without_success_15m (migration effect_scope_successor_proof). End to end through
trusted configuration and the real alert evaluator: no failure, no alert; a failed relay attempt in the last 15 minutes with no reply
intent fires it as critical; once the failure is older than 15 minutes it resolves. The rule's metric must resolve in the snapshot:
coverage is counted, and an empty rule set (coverage 0) fails rather than passing silently."""
from pathlib import Path

import pytest

from nexloop_eios.observability import apply_rules, load_rules
from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

V37 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v37.json'
RULE = 'relay_reply_failing'


def state(admin):
    return admin.execute('select firing,last_value from control.nexloop_alert_state where rule_id=%s', (RULE,)).fetchall()


def resolved(snapshot, path):
    value = snapshot
    for part in path.split('.'):
        if part == '*':
            return isinstance(value, dict) and bool(value)
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return value is not None


def covered(rules, snapshot):
    """(resolved, total) over the rules' metrics; no rule at all is a failure, never a pass."""
    total = len(rules['rules'])
    if total == 0:
        raise AssertionError('alert rule coverage 0: no rules configured')
    return sum(resolved(snapshot, r['metric']) for r in rules['rules']), total


def test_a_failing_relay_without_a_reply_fires_and_resolves(observed):  # noqa: F811
    o = observed
    admin = o['admin']
    assert apply_rules(load_rules(V37), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    assert o['evaluate']()['rules_version'] == 37
    assert [f for f, _ in state(admin)] in ([], [False])
    # The relay port's own ledger row of a failed attempt (0150 yield), as the relay writes it.
    admin.execute("insert into runtime.nexloop_relay_attempts(tenant_id,world,message_id,attempts) values(%s,'real',%s,1)", (TENANT, 'e' * 64))
    o['evaluate']()
    assert state(admin) == [(True, 1)]
    assert admin.execute("select r->>'severity' from control.nexloop_alert_rules a cross join jsonb_array_elements(a.rules) r where a.version=37 and r->>'rule_id'=%s",
                         (RULE,)).fetchone()[0] == 'critical'
    admin.execute("update runtime.nexloop_relay_attempts set last_failure_at=clock_timestamp()-interval '16 minutes' where tenant_id=%s", (TENANT,))
    o['evaluate']()
    assert state(admin) == [(False, 0)]


def test_the_rule_metric_resolves_and_an_empty_rule_set_fails(observed):  # noqa: F811
    admin = observed['admin']
    snapshot = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,)).fetchone()[0]
    assert snapshot['relay_replies'] == {'failures_15m': 0, 'exhausted_15m': 0, 'intents_15m': 0, 'failing_without_success_15m': 0}
    rules = load_rules(V37)
    assert resolved(snapshot, next(r['metric'] for r in rules['rules'] if r['rule_id'] == RULE))
    hit, total = covered(rules, snapshot)
    assert total == len(rules['rules']) and hit > 0
    with pytest.raises(AssertionError, match='coverage 0'):
        covered({'rules': []}, snapshot)
