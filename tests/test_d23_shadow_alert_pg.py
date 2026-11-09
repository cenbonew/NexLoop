"""D23 option B (L6 J1 / P1 on a6692f1c): the alert chain end to end - rules v36 applied through trusted configuration, the real
alert evaluator (its service identity, authz.nexloop_alert_evaluate) reads the metrics snapshot through the 0437 and 0438 wrappers:
no shadow failure, no alert; one shadow failure recorded by an application role, action_input_shadow_failures fires as a warning."""
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from nexloop_eios.observability import apply_rules, load_rules
from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

V36 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v36.json'


def state(admin):
    return admin.execute("select firing,last_value,world from control.nexloop_alert_state where rule_id='action_input_shadow_failures'").fetchall()


def test_a_shadow_failure_fires_the_warning_through_the_real_evaluator(observed):  # noqa: F811
    o = observed
    admin = o['admin']
    assert apply_rules(load_rules(V36), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    assert o['evaluate']()['rules_version'] == 36
    assert [f for f, _, _ in state(admin)] in ([], [False])  # negative case: no failure, no alert
    with psycopg.connect(make_conninfo(o['pg'], user='nexloop_api'), autocommit=True) as api:
        assert api.execute("select authz.nexloop_action_input_shadow_failure('real','OperationalError')").fetchone()[0] is True
    o['evaluate']()
    rows = state(admin)
    assert len(rows) == 1 and rows[0][0] is True and rows[0][1] == 1 and rows[0][2] == 'real', rows
    assert admin.execute("select r->>'severity' from control.nexloop_alert_rules a cross join jsonb_array_elements(a.rules) r where a.version=36 and r->>'rule_id'='action_input_shadow_failures'").fetchone()[0] == 'warning'
