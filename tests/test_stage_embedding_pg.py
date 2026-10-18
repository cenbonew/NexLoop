"""Owner 2026-10-18 (option B; PR #8 review), on catalog PostgreSQL. 0217: what lacks a vector of the active profile is queued
again at every recall-index service start (objects, a dead-lettered backfill item included) and published definitions without
vectors are re-indexed; nothing is marked done for good. Embedding calls are reserved in the tenant model budget before the
provider is called; a budget outside its period or used up claims no work. Deterministic test embedding; no provider call."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from nexloop_eios import recall_index_worker
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.embedding_budget import EmbeddingBudget, EmbeddingBudgetDenied, EmbeddingTariff
from nexloop_eios.embedding_provider import DeterministicTestEmbeddingProvider
from nexloop_eios.recall_index_worker import RecallInstanceIndexWorker
from multi_authority_fixture import seed_multi_authority
from test_claim_matching_pg import env  # noqa: F401
from test_work_feeds_pg import INDEX_FEED, drain, feed_rows, index_worker


def vectors(admin, tenant, kind):
    return admin.execute("select count(*) from ontology.nexloop_recall_embeddings e join ontology.nexloop_recall_entries r using(entry_id) "
                         "where r.tenant_id=%s and r.index_kind=%s", (tenant, kind)).fetchone()[0]


def restart():
    recall_index_worker._activated.clear()


def test_what_lacks_a_vector_is_queued_again_at_every_start_and_definitions_are_reindexed(env):  # noqa: F811
    f = env;admin = f['admin'];tenant = f['tenant']
    worker = index_worker(f)
    drain(worker)
    instances, definitions = vectors(admin, tenant, 'instance'), vectors(admin, tenant, 'definition')
    assert instances > 0 and definitions > 0
    # As on stage: everything was indexed without a profile (FTS and n-gram only); nothing is queued.
    admin.execute("delete from ontology.nexloop_recall_embeddings where tenant_id=%s", (tenant,))
    admin.execute("delete from ontology.nexloop_recall_backfills where tenant_id=%s", (tenant,))
    restart()
    assert vectors(admin, tenant, 'instance') == vectors(admin, tenant, 'definition') == 0 and feed_rows(admin, 'recall-instance') == []
    # The service starts with its embedding: the published definitions get their vectors at once, the objects are queued.
    assert drain(worker)['indexed'] == 2
    assert vectors(admin, tenant, 'instance') == instances and vectors(admin, tenant, 'definition') == definitions
    assert admin.execute("select runs,last_queued,last_definition_types from ontology.nexloop_recall_backfills where tenant_id=%s", (tenant,)).fetchone() == (1, 2, 2)
    # A restart with nothing missing queues nothing (idempotent, recorded).
    restart()
    assert drain(worker) == {} and feed_rows(admin, 'recall-instance') == []
    assert admin.execute("select runs,last_queued,last_definition_types from ontology.nexloop_recall_backfills where tenant_id=%s", (tenant,)).fetchone() == (2, 0, 0)
    # A backfill item dead-lettered by a provider outage (its vectors still missing) is queued again by the next start.
    product_source = f"object:real/Product/{f['product']}"
    admin.execute("delete from ontology.nexloop_recall_embeddings where entry_id in (select entry_id from ontology.nexloop_recall_entries where source_key=%s)",
                  (product_source,))
    admin.execute("insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,status,attempts) values(%s,'real','recall-instance',%s,%s,'dead_lettered',5)",
                  (tenant, 'Product/' + f['product'], psycopg.types.json.Jsonb({'type_name': 'Product', 'object_id': f['product'], 'op': 'upsert'})))
    restart()
    assert drain(worker)['indexed'] == 1 and feed_rows(admin, 'recall-instance') == []
    assert vectors(admin, tenant, 'instance') == instances
    # Only the tenant's active profile, and only by the recall-index service role.
    _, token = seed_multi_authority(admin, f['worker'], [INDEX_FEED], identity_suffix='-other-profile', tenant=tenant)
    other = recall_index_worker.RecallIndexer(f['worker'], authenticate_service(f['worker'], token, world='real'),
                                              provider=DeterministicTestEmbeddingProvider(32), expected_dimension=32)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='active profile'):
        other.backfill_profile({'Consumer': None})
    for role in ('nexloop_api', 'nexloop_action_worker', 'nexloop_scheduler'):
        assert admin.execute("select has_function_privilege(%s,'control.nexloop_recall_backfill_profile(text,text,text,text[])','execute')",
                             (role,)).fetchone() == (False,)


def limit(admin, tenant, amount, *, start, end):
    admin.execute("insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision) "
                  "values(%s,'real','model','CNY',%s,%s,%s,1) on conflict(tenant_id,world,budget_kind) do update set limit_amount=excluded.limit_amount,"
                  "period_start=excluded.period_start,period_end=excluded.period_end", (tenant, amount, start, end))


def test_calls_are_reserved_before_the_provider_and_a_closed_budget_claims_nothing(env):  # noqa: F811
    f = env;admin = f['admin'];tenant = f['tenant']
    session, token = seed_multi_authority(admin, f['worker'], [INDEX_FEED], identity_suffix='-embedding-budget', tenant=tenant)
    tariff = EmbeddingTariff(f['provider'].model, 'CNY', Decimal('1'))
    provider = DeterministicTestEmbeddingProvider(64)
    budget = EmbeddingBudget(f['worker'], session, tariff=tariff, service='recall-indexer')
    assert budget.allowed() is True and budget.wrap(provider).embed('防水登山鞋') and provider.calls == 1  # no model budget: free
    now = datetime.now(UTC)
    # A period that has not started (the limit unused): nothing is claimed, nothing is called.
    limit(admin, tenant, '1', start=now + timedelta(days=1), end=now + timedelta(days=2))
    admin.execute("update ontology.objects set properties=properties where tenant_id=%s and type_name='Product'", (tenant,))  # queue the product again
    assert len(feed_rows(admin, 'recall-instance')) >= 1
    budget = EmbeddingBudget(f['worker'], session, tariff=tariff, service='recall-indexer')
    assert budget.allowed() is False
    # In period with a small limit: each call reserved first; the one past the limit is refused before the provider.
    limit(admin, tenant, '0.000006', start=now - timedelta(days=1), end=now + timedelta(days=1))
    budget = EmbeddingBudget(f['worker'], session, tariff=tariff, service='recall-indexer')
    assert budget.allowed() is True
    wrapped = budget.wrap(provider)
    wrapped.embed('四个汉字')
    calls = provider.calls
    with pytest.raises(EmbeddingBudgetDenied):
        wrapped.embed('超过剩余额度')
    assert provider.calls == calls
    rows = admin.execute("select amount::text from control.nexloop_budget_consumption where tenant_id=%s and consumption_id like 'embedding:recall-indexer:%%'",
                         (tenant,)).fetchall()
    assert rows == [('0.000004',)]
    # The worker with the budgeted provider: a refusal puts the item back unprocessed (pending, not dead-lettered).
    restart()
    worker = RecallInstanceIndexWorker(f['worker'], authenticate_service(f['worker'], token, world='real'), f['signer'],
                                       types={'Consumer': None, 'Product': None}, provider=wrapped, expected_dimension=64)
    admin.execute("insert into ontology.nexloop_recall_backfills(tenant_id,world,profile_id,last_queued,last_definition_types) values(%s,'real',%s,0,0) on conflict do nothing",
                  (tenant, provider.profile_id))
    admin.execute("delete from ontology.nexloop_recall_embeddings e using ontology.nexloop_recall_entries r where e.entry_id=r.entry_id and r.source_key=%s",
                  (f"object:real/Product/{f['product']}",))
    try:
        summary = worker.run_once()
    except EmbeddingBudgetDenied:
        summary = {'retry': 1}
    assert summary.get('retry', 0) >= 1
    assert all(r[5] == 'pending' for r in feed_rows(admin, 'recall-instance'))
