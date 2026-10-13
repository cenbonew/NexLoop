"""NX-037 provider routing: alert rules v4 add effect_route_unconfigured, a warning on the effect refusal NXE01 (an effect intent
whose Action has no provider route). Evaluated by the actual NX-030 evaluator on clean catalog PostgreSQL."""
import uuid
from pathlib import Path

from nexloop_eios.observability import apply_rules, load_rules
from test_observability_pg import observed  # noqa: F401
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, private, provision, published_action, uow, workbench,  # noqa: F401
                                    workbench_login)

V4 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v4.json'


def test_an_unrouted_effect_intent_fires_a_warning(observed):  # noqa: F811
    o = observed
    rules = load_rules(V4)
    rule = next(r for r in rules['rules'] if r['rule_id'] == 'effect_route_unconfigured')
    assert rule == {**rule, 'metric': 'refusals.last_5m', 'selector': 'NXE01', 'condition': 'gt', 'threshold': 0, 'severity': 'warning'}
    assert apply_rules(rules, TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    o['admin'].execute("insert into runtime.nexloop_dispatch_refusals(tenant_id,world,path,code,intent_id) values(%s,'real','effect','NXE01',%s)", (TENANT, uuid.uuid4()))
    assert o['evaluate']()['rules_version'] == 4
    assert o['admin'].execute("select severity,kind from control.nexloop_alert_events where rule_id='effect_route_unconfigured' and selector='NXE01'").fetchall() == [('warning', 'firing')]
