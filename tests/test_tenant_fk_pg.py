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
    assert not report['lock_probe']['blocked_by_for_no_key_update']  # the proposed 0050 lock no longer blocks the check
    assert db.execute("select convalidated from pg_constraint where conname='nexloop_commercial_connectors_tenant_fk'").fetchone() == (True,)
    assert db.execute("select count(*) from control.nexloop_commercial_connectors where connector_id like 'fk-cost-%'").fetchone() == (0,)


BATCH3 = ('ontology.nexloop_merge_configurations', 'ontology.nexloop_recall_settings', 'ontology.nexloop_embedding_profiles',
          'ontology.nexloop_definition_aliases', 'ontology.nexloop_candidate_definitions', 'ontology.nexloop_candidate_events',
          'ontology.nexloop_candidate_rejections', 'ontology.nexloop_review_decisions', 'ontology.nexloop_instance_approvals',
          'ontology.nexloop_assessment_revisions',
          'ontology.nexloop_claims', 'ontology.nexloop_claim_matches', 'ontology.nexloop_claim_rematch', 'ontology.nexloop_conversation_topics',
          'ontology.nexloop_extraction_runs', 'ontology.nexloop_extraction_run_claims', 'ontology.nexloop_mutation_proposals',
          'runtime.nexloop_claim_extraction_feed', 'ontology.nexloop_recall_entries', 'ontology.nexloop_recall_embeddings')
# Vendored EIOS core (0001): no NexLoop foreign key, the core does not depend on the NexLoop control plane (dispatcher ruling).
CORE = ('ontology.events', 'ontology.relations', 'ontology.object_type_versions', 'ontology.event_type_versions', 'ontology.relation_type_versions')


def test_batch3_foreign_keys_are_validated_and_the_vendored_core_is_untouched(db):
    rows = dict(db.execute("""select conrelid::regclass::text, convalidated from pg_constraint
        where contype='f' and confrelid='control.nexloop_tenants'::regclass""").fetchall())
    assert [t for t in BATCH3 if rows.get(t) is not True] == []
    assert not set(CORE) & set(rows)


@pytest.mark.parametrize('table', ['ontology.nexloop_merge_configurations', 'ontology.nexloop_extraction_runs', 'runtime.nexloop_conversations', 'runtime.nexloop_message_inbox',
                                   'runtime.nexloop_run_tool_timings', 'runtime.nexloop_work_feed'])
def test_cost_method_covers_the_batch3_samples(db, pg, table):
    report = tenant_fk_cost.measure(pg, table, TENANT_A, rows=20, rounds=1, hold_seconds=0.3)
    assert report['fk_trigger_sample'] and report['lock_probe']['blocked_by_for_update'] and not report['lock_probe']['blocked_by_for_no_key_update']
    assert db.execute("select count(*) from runtime.nexloop_conversations where principal_id='fk-cost-principal'").fetchone() == (0,)


BATCH3C_ROOTS = tuple('runtime.nexloop_' + t for t in ('conversations', 'native_web_events', 'message_inbox', 'message_outbox', 'message_routes',
                      'message_provider_facts', 'message_reply_links', 'message_relay_leases', 'relay_claims', 'relay_consumer_cursors', 'staff_replies'))
BATCH3C_CHAIN = ('runtime.nexloop_conversation_messages', 'runtime.nexloop_outbound_messages', 'runtime.nexloop_native_web_transports')
TENANT_PATH_SQL = """with recursive fk as (
  select c.conrelid::regclass::text child, c.confrelid::regclass::text parent, c.convalidated validated,
   exists(select 1 from unnest(c.conkey,c.confkey) k(ck,pk) join pg_attribute a on a.attrelid=c.conrelid and a.attnum=k.ck
          join pg_attribute b on b.attrelid=c.confrelid and b.attnum=k.pk where a.attname='tenant_id' and b.attname='tenant_id') carries_tenant
  from pg_constraint c where c.contype='f'),
 path(tbl) as (select child from fk where parent='control.nexloop_tenants' and validated
  union select fk.child from fk join path on fk.parent=path.tbl where fk.carries_tenant and fk.validated)
 select tbl from path"""


def test_batch3c_roots_are_validated_and_children_are_covered_by_the_tenant_chain(db):
    rows = dict(db.execute("""select conrelid::regclass::text, convalidated from pg_constraint
        where contype='f' and confrelid='control.nexloop_tenants'::regclass""").fetchall())
    assert [t for t in BATCH3C_ROOTS if rows.get(t) is not True] == []
    # Option A: chain children carry no foreign key of their own, the composite one (tenant_id -> tenant_id) covers them.
    assert not set(BATCH3C_CHAIN) & set(rows)
    covered = {t for (t,) in db.execute(TENANT_PATH_SQL).fetchall()}
    assert set(BATCH3C_ROOTS) | set(BATCH3C_CHAIN) <= covered, (set(BATCH3C_ROOTS) | set(BATCH3C_CHAIN)) - covered


def test_every_child_of_a_3c_root_satisfies_the_chain_rule(db):
    """A future table referencing a 3c root must map tenant_id onto the root's tenant_id (or carry its own tenant foreign key)."""
    children = {t for (t,) in db.execute("""select distinct conrelid::regclass::text from pg_constraint where contype='f'
        and confrelid::regclass::text = any(%s)""", (list(BATCH3C_ROOTS) + list(BATCH3C_CHAIN),)).fetchall()}
    covered = {t for (t,) in db.execute(TENANT_PATH_SQL).fetchall()}
    assert children and children <= covered, children - covered


def test_chain_rule_rejects_a_child_that_does_not_carry_the_tenant(db):
    with db.transaction(force_rollback=True):
        db.execute("""create table runtime.nx052_bad_child(tenant_id text not null, world text not null, conversation_id text not null, other_tenant text not null,
            foreign key(other_tenant, world, conversation_id) references runtime.nexloop_conversations)""")
        db.execute("""create table runtime.nx052_good_child(tenant_id text not null, world text not null, conversation_id text not null,
            foreign key(tenant_id, world, conversation_id) references runtime.nexloop_conversations)""")
        covered = {t for (t,) in db.execute(TENANT_PATH_SQL).fetchall()}
        assert 'runtime.nx052_good_child' in covered and 'runtime.nx052_bad_child' not in covered


BATCH4_DIRECT = ('authz.nexloop_action_decisions', 'authz.nexloop_artifact_permits', 'authz.nexloop_message_fallback_issuances',
                 'authz.nexloop_message_run_issuances', 'authz.nexloop_role_policy_bindings', 'authz.nexloop_role_run_bindings',
                 'authz.nexloop_runtime_activations', 'authz.nexloop_runtime_execution_markers', 'authz.nexloop_runtime_run_bindings') + tuple(
    'runtime.nexloop_' + t for t in ('local_artifacts', 'context_packs', 'effect_intents', 'effect_control_reservations', 'effect_dispatch_controls',
                                     'effect_outbox', 'effect_recovery_audits', 'plans', 'role_trigger_events', 'role_context_artifacts', 'run_tool_timings',
                                     'run_purge_items', 'task_dispatch_controls', 'scope_denials', 'cost_entries', 'budget_settlements', 'dispatch_refusals',
                                     'strategies', 'human_requests', 'relay_attempts', 'relay_handoffs', 'work_feed', 'action_claims'))
BATCH4_CHAIN = tuple('runtime.nexloop_' + t for t in ('context_sources', 'model_requests', 'model_request_prompts', 'context_artifact_bindings',
                                                      'fallback_context_bindings', 'effect_attempts', 'effect_catalog_bindings', 'effect_provider_bindings',
                                                      'effect_events', 'effect_observations', 'effect_query_admissions', 'local_delivery_recoveries',
                                                      'plan_events', 'plan_outcomes', 'plan_runs', 'plan_steps'))
VENDORED_RUNTIME = ('runtime.jobs', 'runtime.invocations', 'runtime.job_events', 'runtime.audit_events', 'runtime.idempotency_records', 'runtime.artifact_references')


def test_batch4_direct_keys_are_validated_chain_tables_are_covered_and_the_vendored_core_is_untouched(db):
    rows = dict(db.execute("""select conrelid::regclass::text, convalidated from pg_constraint
        where contype='f' and confrelid='control.nexloop_tenants'::regclass""").fetchall())
    assert [t for t in BATCH4_DIRECT if rows.get(t) is not True] == []
    assert not (set(BATCH4_CHAIN) | set(VENDORED_RUNTIME)) & set(rows)
    covered = {t for (t,) in db.execute(TENANT_PATH_SQL).fetchall()}
    assert set(BATCH4_DIRECT) | set(BATCH4_CHAIN) <= covered, (set(BATCH4_DIRECT) | set(BATCH4_CHAIN)) - covered
