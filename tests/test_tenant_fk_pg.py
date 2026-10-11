"""NX-052 batch 2: tenant foreign keys on the cold control-plane configuration tables, and the write-cost method check."""
import sys
from pathlib import Path

import psycopg
import pytest

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_OTHER

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/load'))
import tenant_fk_cost  # noqa: E402

BATCH2 = ('browser_applications', 'browser_business_applications', 'browser_accounts', 'browser_memberships', 'browser_rate_policies',
          'alert_rules', 'alert_silences', 'workbench_configurations', 'budget_limits', 'context_strategies', 'metric_definitions',
          'metric_sources', 'goals', 'goal_versions', 'key_results', 'restore_channels', 'restore_scopes', 'retention_holds',
          'relation_action_bindings', 'action_effect_categories', 'erasure_salts', 'consumer_owners', 'relay_enrollments',
          'control_heads', 'control_scopes', 'commercial_connectors')
# authz.nexloop_authority_facts has carried a tenant foreign key since 0004; the other two hot tables are batch 5.
HOT = ('ontology.objects', 'runtime.jobs')


@pytest.fixture
def db(admin):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    return admin


def test_batch2_foreign_keys_are_validated_and_hot_tables_untouched(db):
    rows = dict(db.execute("""select conrelid::regclass::text, convalidated from pg_constraint
        where contype='f' and confrelid='control.nexloop_tenants'::regclass""").fetchall())
    assert all(rows.get(f'control.nexloop_{t}') is True for t in BATCH2), [t for t in BATCH2 if rows.get(f'control.nexloop_{t}') is not True]
    assert not set(HOT) & set(rows) and rows['authz.nexloop_authority_facts'] is True


def test_unknown_tenant_is_refused_and_known_tenant_written(db):
    with db.transaction(), pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',3,60,true)", (TENANT_OTHER,))
    with db.transaction(force_rollback=True):
        db.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',3,60,true)", (TENANT_A,))


def test_cost_method_reports_quantiles_trigger_and_lock_conflict(db, pg):
    report = tenant_fk_cost.measure(pg, 'control.nexloop_commercial_connectors', TENANT_A, rows=40, rounds=1, hold_seconds=0.3)
    assert set(report['with_fk']) == {'median_us', 'p95_us'} and set(report['ratio']) == {'median_us', 'p95_us'}
    assert report['fk_trigger_sample'] and report['fk_trigger_sample'][0]['calls'] == 1
    # 0050's explicit FOR UPDATE on the tenant row blocks a foreign key check (FOR KEY SHARE); without the key it does not.
    assert report['lock_probe']['blocked_by_for_update'] and report['lock_probe']['insert_wait_without_fk_s'] < 0.2
    assert db.execute("select convalidated from pg_constraint where conname='nexloop_commercial_connectors_tenant_fk'").fetchone() == (True,)
    assert db.execute("select count(*) from control.nexloop_commercial_connectors where connector_id like 'fk-cost-%'").fetchone() == (0,)
