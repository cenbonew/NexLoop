"""NX-029 / AT-026 erasure coverage, batch 3 (0306; docs/implementation/erasure-coverage.md): the remaining real tables with a consumer_id.
End to end on real PostgreSQL: a Consumer with a row in each table (seeded as their writers leave them), the owner's governed erasure
Action and the retention keeper until completed; then no row of those tables holds the Consumer's id, principal, identity or text, the
deleted links are gone, the facts stay, another Consumer's rows are untouched, and a retention hold keeps everything.
"""
import secrets
import uuid

from psycopg.types.json import Jsonb

from commercial_fixture import TENANT
from test_erasure_pg import act, erasing, keeper, request  # noqa: F401
from test_erasure_pg import pytestmark  # noqa: F401  (execution_plan parametrization)
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_erasure_coverage_pg import run_erasure

DELETED = ('control.nexloop_consumer_owners', 'control.nexloop_relationship_context_recipes', 'runtime.nexloop_evo_patch_sources',
           'runtime.nexloop_message_relay_leases', 'runtime.nexloop_commitments')
PSEUDONYMISED = ('authz.nexloop_role_run_bindings', 'control.nexloop_consumer_identity_link_receipts', 'control.nexloop_data_exports',
                 'control.nexloop_effect_contexts', 'control.nexloop_effect_control_ledger', 'ontology.nexloop_extraction_runs',
                 'runtime.nexloop_commitment_exceptions', 'runtime.nexloop_native_web_events', 'runtime.nexloop_relay_handoffs',
                 'runtime.nexloop_scope_denials')
TABLES = DELETED + PSEUDONYMISED + ('runtime.nexloop_native_web_transports',)
TEXT = ('导出原因：顾客投诉复核', '被拒抽取：我的手机号是', '承诺异常备注：顾客原话', '计划配方：给王先生回电', 'scope 参数原文：退款单号')


def seed(admin, consumer, tag):
    """One row per table naming the Consumer (and its principal / identity markers), guards bypassed for the seed only."""
    principal, identity_ref = f'synthetic-principal-{tag}', f'wechat:openid-{tag}'
    conversation, message, event = secrets.token_hex(32), secrets.token_hex(32), uuid.uuid4()
    run, context, patch, plan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ex = lambda sql, args: admin.execute(sql, args)  # noqa: E731
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        ex("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,%s,%s)",
           (TENANT, conversation, consumer, principal, 'conv-' + tag))
        ex("insert into control.nexloop_consumer_owners(tenant_id,world,principal_id,consumer_id,ownership_id,idempotency_key) values(%s,'real',%s,%s,%s,%s)",
           (TENANT, principal, consumer, 'own-' + tag, 'own-key-' + tag))
        ex("insert into control.nexloop_relationship_context_recipes(tenant_id,world,source_principal,consumer_id,assessment_ids) values(%s,'real',%s,%s,%s)",
           (TENANT, 'synthetic-source', consumer, Jsonb(['assessment-1'])))
        ex("insert into runtime.nexloop_evo_patch_sources(tenant_id,world,patch_id,consumer_id) values(%s,'real',%s,%s)", (TENANT, patch, consumer))
        ex("""insert into runtime.nexloop_message_relay_leases(tenant_id,world,message_id,consumer_id,principal_id,credential_digest,fence,lease_until)
            values(%s,'real',%s,%s,%s,%s,1,clock_timestamp())""", (TENANT, message, consumer, principal, 'a' * 64))
        ex("""insert into runtime.nexloop_commitments(tenant_id,world,commitment_id,dedupe_key,origin,consumer_id,supersedes,intent_id,properties,prepared_at)
            values(%s,'real',%s,%s,'extend',%s,%s,%s,%s,clock_timestamp())""", (TENANT, secrets.token_hex(32), 'dedupe-' + tag, consumer, 'f' * 64, 'intent-' + tag, Jsonb({'note': TEXT[2] + tag})))
        ex("""insert into authz.nexloop_role_run_bindings(run_id,source_digest,tenant_id,world,consumer_id,link_id,role_id,step_id,link_revision,role_revision,
            step_revision,role_ref,scope,expires_at) values(%s,%s,%s,'real',%s,'l','r','s',1,1,1,'role','scope',clock_timestamp())""", (run, 'b' * 64, TENANT, consumer))
        ex("""insert into control.nexloop_consumer_identity_link_receipts(tenant_id,world,identity_ref,consumer_id,subject_id,account_id,request_payload,recorded_txid)
            values(%s,'real',%s,%s,%s,%s,%s,1)""", (TENANT, identity_ref, consumer, 'subject-' + tag, 'account-' + tag, Jsonb({'identity_ref': identity_ref})))
        ex("""insert into control.nexloop_data_exports(tenant_id,world,export_id,consumer_id,requested_by,reason,created_at,expires_at,downloads)
            values(%s,'real',%s,%s,'synthetic-owner',%s,clock_timestamp(),clock_timestamp()+interval '1 day',0)""", (TENANT, 'export-' + tag, consumer, TEXT[0] + tag))
        ex("""insert into control.nexloop_effect_contexts(context_id,tenant_id,world,consumer_id,goal_identity,goal_version_ref,plan_step_identity,slot_identity,consumer_revision,
            control_revision,allow_effect,valid_until,budget_units,reserved_units,expected_versions,executor_principal)
            values(%s,%s,'real',%s,'g','goal:g@1','p','s',1,1,true,clock_timestamp(),1,0,'[1]','synthetic-executor')""", (context, TENANT, consumer))
        ex("""insert into control.nexloop_effect_control_ledger(control_id,tenant_id,world,consumer_id,owner_principal,owner_digest,executor_principal,executor_digest,
            control_revision,budget_units,reserved_units,valid_until) values(%s,%s,'real',%s,'o','d','e','d',1,1,0,clock_timestamp())""", ('control-' + tag, TENANT, consumer))
        ex("""insert into ontology.nexloop_extraction_runs(tenant_id,world,input_digest,conversation_id,consumer_id,principal_id,extractor_version,prompt_version,provider,model_id,
            timezone,message_refs,rejected,recorded_at) values(%s,'real',%s,%s,%s,%s,'x1','p1','prov','m','Asia/Shanghai',%s,%s,clock_timestamp())""",
           (TENANT, secrets.token_hex(32), conversation, consumer, principal, Jsonb([message]), Jsonb([{'quote': TEXT[1] + tag}])))
        ex("""insert into runtime.nexloop_commitment_exceptions(tenant_id,world,subject_ref,consumer_id,reason,detail,raised_at)
            values(%s,'real',%s,%s,'breached',%s,clock_timestamp())""", (TENANT, 'commitment:' + secrets.token_hex(32), consumer, Jsonb({'note': TEXT[2] + tag})))
        ex("""insert into runtime.nexloop_native_web_events(tenant_id,world,provider_namespace,principal_id,provider_event_id,conversation_id,consumer_id,message_id,
            payload_digest,first_transport_key,accepted_at) values(%s,'real','native.webchat',%s,%s,%s,%s,%s,%s,'t1',clock_timestamp())""",
           (TENANT, principal, event, conversation, consumer, message, 'c' * 64))
        ex("""insert into runtime.nexloop_native_web_transports(tenant_id,world,provider_namespace,principal_id,transport_key,provider_event_id,conversation_id,payload_digest)
            values(%s,'real','native.webchat',%s,'t1',%s,%s,%s)""", (TENANT, principal, event, conversation, 'c' * 64))
        ex("""insert into runtime.nexloop_relay_handoffs(tenant_id,world,message_id,consumer_id,reason,relay_principal,attempts,handed_at)
            values(%s,'real',%s,%s,'attempts_exhausted','synthetic-relay',3,clock_timestamp())""", (TENANT, message, consumer))
        ex("""insert into runtime.nexloop_scope_denials(tenant_id,world,run_id,message_id,consumer_id,scope_digest,parameters_digest,record_id,scope,request_scope,recorded_at,
            source_principal,claim_id,request_digest) values(%s,'real',%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp(),'synthetic','claim-1',%s)""",
           (TENANT, run, message, consumer, 'd' * 64, 'e' * 64, 'record-' + tag, Jsonb({'refund': TEXT[4] + tag}), Jsonb({'refund': TEXT[4] + tag}), 'f' * 64))
        ex("""insert into runtime.nexloop_plans(tenant_id,world,plan_id,version,consumer_id,goal_version_ref,context_strategy_ref,recipe,settings,control_snapshot,created_by)
            values(%s,'real',%s,1,%s,'goal:g@1','context-strategy:default@1',%s,'{}',%s,'synthetic')""",
           (TENANT, plan, consumer, Jsonb({'step': TEXT[3] + tag}), Jsonb({'consumer': consumer})))
    return {'principal': principal, 'identity_ref': identity_ref, 'subject': 'subject-' + tag, 'account': 'account-' + tag}


def holding(admin, needles):
    out = {}
    for table in TABLES:
        n = admin.execute(f"select count(*) from {table} x where to_jsonb(x)::text like any(%s)", (['%' + w + '%' for w in needles],)).fetchone()[0]
        if n:
            out[table] = n
    return out


def test_the_erasure_reaches_every_remaining_table_with_the_consumer(erasing, admin):
    c = erasing
    consumer = c['consumer']
    marks = seed(admin, consumer, 'erased')
    needles = [consumer, *marks.values(), *(t + 'erased' for t in TEXT)]
    # A bystander's rows (another Consumer) stay as they are.
    bystander = secrets.token_hex(32)
    other = seed(admin, bystander, 'bystander')
    before_other = holding(admin, [bystander, *other.values()])
    # Positive control: before the erasure every table holds the Consumer.
    assert set(holding(admin, [consumer, marks['principal']])) == set(TABLES)
    act(c['owner_actions'], 'erase_consumer', 'erase-ref-1', consumer_id=consumer, reason='顾客要求删除')
    run_erasure(c, admin, consumer)
    assert holding(admin, needles) == {}
    for table in DELETED:
        assert admin.execute(f"select count(*) from {table} where consumer_id like 'pseudonym:%%'").fetchone() == (0,), table
    # The facts stay under one pseudonym of this step.
    kept = {table: admin.execute(f"select array_agg(distinct consumer_id) from {table} where consumer_id like 'pseudonym:%%'").fetchone()[0] for table in PSEUDONYMISED}
    assert all(kept[t] and len(kept[t]) == 1 for t in PSEUDONYMISED), kept
    assert len({v[0] for v in kept.values()}) == 1
    assert admin.execute("select scope,request_scope from runtime.nexloop_scope_denials where consumer_id like 'pseudonym:%%'").fetchone() == ({'erased': True}, {'erased': True})
    assert admin.execute("select count(*) from runtime.nexloop_native_web_transports where principal_id like 'pseudonym:%%'").fetchone() == (1,)
    tomb = admin.execute("select counts from runtime.nexloop_erasure_tombstones where item_class='consumer_references'").fetchone()[0]
    assert all(tomb[k] >= 1 for k in ('consumer_owners', 'commitments', 'role_run_bindings', 'scope_denials', 'native_web_events')), tomb
    # The bystander is untouched.
    assert holding(admin, [bystander, *other.values()]) == before_other


def test_a_retention_hold_keeps_every_reference(erasing, admin):
    c = erasing
    consumer = c['consumer']
    seed(admin, consumer, 'held')
    act(c['owner_actions'], 'hold_retention', 'hold-ref', scope_kind='consumer', scope_ref=consumer, basis='争议处理中')
    act(c['owner_actions'], 'erase_consumer', 'erase-ref-2', consumer_id=consumer, reason='顾客要求删除')
    for _ in range(5):
        keeper(c, host=lambda run_id: 'purged').run_once()
    assert request(admin, consumer)[1] == 'completed_with_holds'
    assert set(holding(admin, [consumer])) >= set(DELETED + PSEUDONYMISED)
    assert admin.execute("select count(*) from runtime.nexloop_erasure_tombstones where item_class='consumer_references'").fetchone() == (0,)


def test_outside_an_erasure_the_new_guards_still_refuse(erasing, admin):
    """The two v2 guards admit the erasure scope only: the same erasure values written without it are refused."""
    import pytest
    consumer = erasing['consumer']
    seed(admin, consumer, 'guard')
    for sql, match in (("update control.nexloop_effect_contexts set consumer_id='pseudonym:'||repeat('0',32) where consumer_id=%s", 'identity immutable'),
                       ("update runtime.nexloop_scope_denials set scope='{\"erased\":true}' where consumer_id=%s", 'immutable'),
                       ("delete from runtime.nexloop_scope_denials where consumer_id=%s", 'immutable'),
                       ("update runtime.nexloop_relay_handoffs set consumer_id='pseudonym:'||repeat('0',32) where consumer_id=%s", 'append-only')):
        with pytest.raises(Exception, match=match), admin.transaction():
            admin.execute(sql, (consumer,))
