"""0221 (owner 2026-10-19, PR #14 review): vector recall quality for the read-only metrics role, not the bootstrap superuser.
On catalog PostgreSQL with an active profile and indexed definitions (deterministic test embedding): coverage and the nearest
definition counts through authz.nexloop_recall_quality as nexloop_metrics, samples as refs and numbers only, and the doctor."""
import os

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios import doctor
from test_claim_matching_pg import env  # noqa: F401


def test_the_metrics_role_reads_recall_quality_without_any_text(env, tmp_path):  # noqa: F811
    f = env;admin = f['admin'];tenant = f['tenant']
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_metrics')) as db:
        quality = db.execute('select authz.nexloop_recall_quality(%s,%s)', (tenant, 5)).fetchone()[0]
        assert quality['active_profile'] == f['provider'].profile_id
        assert set(quality['coverage']) == {'definition', 'instance'} and all(v['without_vector'] == 0 for v in quality['coverage'].values())
        n = quality['neighbours']
        assert n['definition_texts'] > 0 and n['close'] <= n['definition_texts'] and n['close_without_literal_overlap'] <= n['close']
        assert all(set(s) == {'ref', 'neighbour_ref', 'vector_distance', 'trigram_similarity'} for s in quality['samples'])
        bodies = {r[0] for r in admin.execute('select body from ontology.nexloop_recall_entries where tenant_id=%s', (tenant,)).fetchall()}
        def strings(value):
            if isinstance(value, dict):return [x for v in value.values() for x in strings(v)]
            if isinstance(value, list):return [x for v in value for x in strings(v)]
            return [value] if isinstance(value, str) else []
        assert not set(strings(quality)) & bodies  # refs and numbers only: no indexed text is returned
        # The metrics role reads nothing else of the index, and only through this function.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select count(*) from ontology.nexloop_recall_entries')
        db.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select runtime.nexloop_recall_quality(%s,5)', (tenant,))
        db.rollback()
        assert db.execute('select authz.nexloop_recall_quality(%s,0)', ('no-such-tenant',)).fetchone()[0] == {'active_profile': None}
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker'):
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_recall_quality(text,integer)','execute')", (role,)).fetchone() == (False,)
    # The doctor: passes when a profile is active and every entry has its vector; fails when vectors are missing.
    dsn = tmp_path / 'metrics-dsn';dsn.write_text(make_conninfo(f['pg'], user='nexloop_metrics'));os.chmod(dsn, 0o600)
    assert all(doctor.recall_quality(dsn, tenant)['checks'].values())
    admin.execute('delete from ontology.nexloop_recall_embeddings where tenant_id=%s and entry_id=(select min(entry_id) from ontology.nexloop_recall_embeddings where tenant_id=%s)',
                  (tenant, tenant))
    assert doctor.recall_quality(dsn, tenant)['checks']['recall_vectors_complete'] is False
