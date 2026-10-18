"""Owner 2026-10-18 (option B), on catalog PostgreSQL: objects indexed before the embedding profile get their vectors once the
recall-index service activates it (0217 backfill, once per profile, active profile only), and embedding calls are counted in the
tenant's model budget (NX-022 reserve: capped at the limit, then refused). Deterministic test embedding; no provider call."""
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from nexloop_eios import recall_index_worker
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.embedding_budget import EmbeddingBudget, EmbeddingTariff
from nexloop_eios.embedding_provider import DeterministicTestEmbeddingProvider
from nexloop_eios.goal_controls import ControlPlane
from multi_authority_fixture import seed_multi_authority
from test_claim_matching_pg import env  # noqa: F401
from test_work_feeds_pg import INDEX_FEED, drain, feed_rows, index_worker


def vectors(admin, tenant):
    return admin.execute("select count(*) from ontology.nexloop_recall_embeddings e join ontology.nexloop_recall_entries r using(entry_id) "
                         "where r.tenant_id=%s and r.index_kind='instance'", (tenant,)).fetchone()[0]


def test_objects_from_before_the_profile_are_queued_once_and_get_vectors(env):  # noqa: F811
    f = env;admin = f['admin'];tenant = f['tenant']
    worker = index_worker(f)
    drain(worker)
    assert vectors(admin, tenant) > 0
    # As on stage: the existing objects were indexed without a profile (FTS and n-gram only); nothing is queued.
    admin.execute("delete from ontology.nexloop_recall_embeddings where tenant_id=%s", (tenant,))
    admin.execute("delete from ontology.nexloop_recall_backfills where tenant_id=%s", (tenant,))
    recall_index_worker._activated.clear()
    assert vectors(admin, tenant) == 0 and feed_rows(admin, 'recall-instance') == []
    # The service starts with its embedding: it activates the profile and queues every object of its types, once.
    totals = drain(worker)
    assert totals['indexed'] == 2 and vectors(admin, tenant) > 0
    assert admin.execute("select profile_id,items from ontology.nexloop_recall_backfills where tenant_id=%s", (tenant,)).fetchall() == [
        (f['provider'].profile_id, 2)]
    recall_index_worker._activated.clear()  # a restarted process: the backfill is not repeated
    assert drain(worker) == {} and feed_rows(admin, 'recall-instance') == []
    # Only the tenant's active profile, and only by the recall-index service role.
    _, token = seed_multi_authority(admin, f['worker'], [INDEX_FEED], identity_suffix='-other-profile', tenant=tenant)
    session = authenticate_service(f['worker'], token, world='real')
    other = recall_index_worker.RecallIndexer(f['worker'], session, provider=DeterministicTestEmbeddingProvider(32), expected_dimension=32)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='active profile'):
        other.backfill_profile({'Consumer': None})
    for role in ('nexloop_api', 'nexloop_action_worker', 'nexloop_scheduler'):
        assert admin.execute("select has_function_privilege(%s,'control.nexloop_recall_backfill_profile(text,text,text,text[])','execute')",
                             (role,)).fetchone() == (False,)


def test_embedding_calls_are_reserved_in_the_tenant_model_budget(env):  # noqa: F811
    f = env;admin = f['admin'];tenant = f['tenant']
    session, _ = seed_multi_authority(admin, f['worker'], [INDEX_FEED], identity_suffix='-embedding-budget', tenant=tenant)
    tariff = EmbeddingTariff(f['provider'].model, 'CNY', __import__('decimal').Decimal('1'))
    budget = EmbeddingBudget(f['worker'], session, tariff=tariff, service='recall-indexer')
    provider = DeterministicTestEmbeddingProvider(64)
    provider.embed('防水登山鞋')
    assert budget.allowed() is True and budget.record(provider) is None  # no model budget: nothing reserved, never refused
    now = datetime.now(UTC)
    admin.execute("insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision) "
                  "values(%s,'real','model','CNY','0.000010',%s,%s,1)", (tenant, now - timedelta(days=1), now + timedelta(days=1)))
    provider.usage_tokens = 4
    budget.record(provider)
    provider.usage_tokens = 20
    budget.record(provider)
    rows = admin.execute("select amount::text,unit,source_ref like 'embedding:%%' from control.nexloop_budget_consumption where tenant_id=%s and budget_kind='model' "
                         "and consumption_id like 'embedding:recall-indexer:%%' order by recorded_at", (tenant,)).fetchall()
    assert rows == [('0.000004', 'CNY', True), ('0.000006', 'CNY', True)]  # the second capped at the rest of the limit
    assert budget.allowed() is False and ControlPlane(f['worker'], session).budget_status('model')['exhausted'] is True
