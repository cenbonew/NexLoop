"""S87 (migration schema_watch_one_estimator): the Schema publication watch compares Context volume within one estimator. A watch
that spans the estimator change (v1 packs before, v2 after: several times the used for the same content) is not a Context
regression; the same growth within one estimator still is. Real PostgreSQL, synthetic rows."""
import uuid

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A as TENANT


@pytest.fixture
def db(admin):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT,))
    return admin


def watch(admin):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into runtime.nexloop_schema_watches(tenant_id,world,publication_id,type_name,published_at,watch_until) "
                      "values(%s,'real',%s,'Consumer',now()-interval '2 days',now()+interval '5 days')", (TENANT, str(uuid.uuid4())))


def packs(admin, n, used, estimator, age):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for _ in range(n):
            admin.execute("insert into runtime.nexloop_context_packs(tenant_id,world,context_id,run_id,protocol,strategy_ref,pack_digest,artifact_id,budget_report,insufficient,created_at) "
                          f"values(%s,'real',%s,%s,'nexloop.context-pack.v6','context-strategy:recent_plus_required@2',%s,%s,%s,'[]',now()-interval '{age}')",
                          (TENANT, uuid.uuid4(), uuid.uuid4(), 'b' * 64, 'c' * 32, Jsonb({'estimator': estimator, 'used': used, 'available': 12100})))


def publication(admin):
    w = admin.execute("select runtime.nexloop_schema_watch(%s,'real')", (TENANT,)).fetchone()[0]
    [p] = w['publications']
    return p


def test_an_estimator_change_inside_a_watch_is_not_a_context_regression(db):
    watch(db)
    packs(db, 20, 400, 'conservative-bytes-half-v1', '4 days')
    packs(db, 20, 3000, 'conservative-bytes-half-v2', '1 day')
    p = publication(db)
    # Only the newest estimator's packs are compared: none before the publication, so no Context claim.
    assert (p['context_packs_before'], p['context_packs_after'], p['context_over_limit'], p['regressing']) == (0, 20, False, False)


def test_growth_within_one_estimator_is_still_a_regression(db):
    watch(db)
    packs(db, 20, 400, 'conservative-bytes-half-v1', '4 days')
    packs(db, 20, 400, 'conservative-bytes-half-v2', '4 days')
    packs(db, 20, 3000, 'conservative-bytes-half-v2', '1 day')
    p = publication(db)
    assert (p['context_packs_before'], p['context_packs_after'], p['context_over_limit'], p['suggestion']) == (20, 20, True, 'hide')
