"""NX-039 V5 (D39-10): alert rules v7 warn when a semantic release in its 7-day watch regresses (positive control) - and only then;
the alert suggests a rollback and nothing rolls back. Reuses the observability fixture (configurator, evaluator); admin seeds E3
records and the watch row the release path would start."""
import uuid
from pathlib import Path

from nexloop_eios.observability import apply_rules, load_rules
from test_observability_pg import observed  # noqa: F401
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, published_action, uow, workbench,  # noqa: F401
    workbench_login)

V7 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v7.json'


def seed(admin, n, missed, at_sql):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for i in range(n):
            admin.execute(f"""insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,operation,query_kind,term_hashes,coverage,ambiguity,
                result_refs,result_count,unresolved_count,recorded_at) values(%s,'real',%s,'access','browse','any',%s,%s,false,'[]',%s,0,{at_sql})""",
                (TENANT, uuid.uuid4(), ['f' * 64], 0 if i < missed else 1, 0 if i < missed else 1))


def test_alert_rules_v7_warn_on_a_regressing_release_and_keep_v6(observed):  # noqa: F811
    o = observed
    admin = o['admin']
    rules = load_rules(V7)
    assert {r['rule_id'] for r in load_rules(V7.parent / 'alert-rules.v6.json')['rules']} < {r['rule_id'] for r in rules['rules']}
    assert apply_rules(rules, TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    firing = lambda: {r[0] for r in admin.execute("select rule_id from control.nexloop_alert_state where tenant_id=%s and firing", (TENANT,)).fetchall()}  # noqa: E731
    assert o['evaluate']()['rules_version'] == 7 and 'evolution_release_regressing' not in firing()
    admin.execute("select runtime.nexloop_evo_watch_start(%s,'real','semantic-release:2.0.0',now()-interval '2 days')", (TENANT,))
    seed(admin, 20, 2, "now()-interval '4 days'")
    seed(admin, 20, 2, "now()-interval '1 day'")
    o['evaluate']()
    assert 'evolution_release_regressing' not in firing()  # equal rates: no alert
    seed(admin, 20, 6, "now()-interval '6 hours'")
    o['evaluate']()
    assert 'evolution_release_regressing' in firing()
    assert admin.execute('select count(*) from runtime.nexloop_evo_release_watches').fetchone()[0] == 1  # read only
