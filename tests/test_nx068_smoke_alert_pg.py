"""NX-068 Q9: rules v38 add schema_publish_smoke_failed on schema_publish_smoke.problems (migration nx068_publish_gate_smoke). End to
end through trusted configuration and the real alert evaluator: no smoke, no alert; a failed round in the last 24 hours fires it as
critical and it resolves once the round is older; a round 30 minutes overdue (its runner stopped) fires it too, so a smoke that never
ran is not a pass. The evaluator's maintenance runs due rounds itself. The rule's metric must resolve; coverage 0 fails."""
import json
import uuid
from pathlib import Path

import pytest

from nexloop_eios.observability import apply_rules, load_rules
from nexloop_eios.observability import validate_rules
from test_observability_pg import TENANT, observed  # noqa: F401
from test_relay_reply_alert_pg import covered, resolved
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'
V38 = RULES / 'alert-rules.v38.json'
RULE = 'schema_publish_smoke_failed'


def state(admin):
    return admin.execute('select firing,last_value from control.nexloop_alert_state where rule_id=%s', (RULE,)).fetchall()


def smoke(admin, *, outcome, due, ran=None, type_name='Consumer'):
    admin.execute("""insert into runtime.nexloop_schema_publish_smokes(tenant_id,world,publication_id,round,type_name,due_at,outcome,ran_at,checks)
        values(%s,'real',%s,1,%s,now()-%s::interval,%s,case when %s::text is null then null else now()-%s::interval end,%s::jsonb)""",
        (TENANT, str(uuid.uuid4()), type_name, due, outcome, ran, ran, json.dumps([] if outcome != 'failed' else ['uncovered:Consumer.create'])))


def test_rules_v38_are_v37_plus_the_smoke_rule():
    v37, v38 = json.loads((RULES / 'alert-rules.v37.json').read_text()), load_rules(V38)
    validate_rules(v38)
    assert v38['version'] == 38 and v38['rules'][:-1] == v37['rules'] and v38['decision'].startswith(v37['decision'])
    assert {k: v38['rules'][-1][k] for k in ('rule_id', 'metric', 'condition', 'threshold', 'for_seconds', 'severity')} == {
        'rule_id': RULE, 'metric': 'schema_publish_smoke.problems', 'condition': 'gt', 'threshold': 0, 'for_seconds': 0, 'severity': 'critical'}


def test_a_failed_or_overdue_smoke_fires_and_resolves(observed):  # noqa: F811
    o = observed
    admin = o['admin']
    assert apply_rules(load_rules(V38), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    assert o['evaluate']()['rules_version'] == 38
    assert [f for f, _ in state(admin)] in ([], [False])
    smoke(admin, outcome='failed', due='2 minutes', ran='1 minute')
    o['evaluate']()
    assert state(admin) == [(True, 1)]
    assert admin.execute("select r->>'severity' from control.nexloop_alert_rules a cross join jsonb_array_elements(a.rules) r where a.version=38 and r->>'rule_id'=%s",
                         (RULE,)).fetchone()[0] == 'critical'
    admin.execute("update runtime.nexloop_schema_publish_smokes set ran_at=now()-interval '25 hours',due_at=now()-interval '26 hours' where tenant_id=%s", (TENANT,))
    o['evaluate']()
    assert state(admin) == [(False, 0)]
    # A round that never ran: the maintenance would run it, unless the runner fails; a type nothing is bound to still runs and passes.
    smoke(admin, outcome='pending', due='31 minutes', type_name='NoSuchType')
    o['evaluate']()
    assert admin.execute("select outcome from runtime.nexloop_schema_publish_smokes where type_name='NoSuchType'").fetchone()[0] == 'passed'
    assert state(admin) == [(False, 0)]
    admin.execute('alter function runtime.nexloop_run_schema_publish_smokes() rename to nexloop_run_schema_publish_smokes_off')
    try:
        smoke(admin, outcome='pending', due='31 minutes')
        o['evaluate']()  # the maintenance's own failure is caught: the evaluation goes on and the round stays pending
        assert state(admin) == [(True, 1)]
    finally:
        admin.execute('alter function runtime.nexloop_run_schema_publish_smokes_off() rename to nexloop_run_schema_publish_smokes')


def test_the_rule_metric_resolves_and_an_empty_rule_set_fails(observed):  # noqa: F811
    admin = observed['admin']
    snapshot = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,)).fetchone()[0]
    assert snapshot['schema_publish_smoke'] == {'failed_24h': 0, 'overdue': 0, 'problems': 0}
    rules = load_rules(V38)
    assert resolved(snapshot, next(r['metric'] for r in rules['rules'] if r['rule_id'] == RULE))
    hit, total = covered(rules, snapshot)
    assert total == len(rules['rules']) and hit > 0
    with pytest.raises(AssertionError, match='coverage 0'):
        covered({'rules': []}, snapshot)


def test_round_two_fails_when_relay_replies_start_failing_after_the_publication(observed):  # noqa: F811
    """S4: round 1 records the relay baseline; round 2 fails when replies fail without success now while the baseline was 0, and does
    not blame the publication when they were failing before it."""
    admin = observed['admin']
    for baseline, expected in ((0, 'failed'), (1, 'passed')):
        publication = str(uuid.uuid4())
        admin.execute("""insert into runtime.nexloop_schema_publish_smokes(tenant_id,world,publication_id,round,type_name,due_at,outcome,ran_at,checks,baseline)
            values(%s,'real',%s,1,'NoSuchType',now()-interval '20 minutes','passed',now()-interval '19 minutes','[]',%s),
                  (%s,'real',%s,2,'NoSuchType',now()-interval '1 second','pending',null,'[]',null)""", (TENANT, publication, baseline, TENANT, publication))
        admin.execute("insert into runtime.nexloop_relay_attempts(tenant_id,world,message_id,attempts) values(%s,'real',%s,1) on conflict do nothing", (TENANT, 'e' * 64))
        assert admin.execute('select runtime.nexloop_run_schema_publish_smokes()').fetchone()[0] == 1
        assert admin.execute('select outcome,checks from runtime.nexloop_schema_publish_smokes where publication_id=%s and round=2', (publication,)).fetchone() == (
            expected, ['relay_replies_failing_since_publication'] if expected == 'failed' else [])
