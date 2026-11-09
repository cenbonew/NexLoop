"""follow_latest_version after an NX-040 F3 Schema publication (migration schema_publication_successor_coexistence; stage R73
2026-11-09: Consumer v2 was published through authz.nexloop_schema_proposal_publish, which records no NX-044 review decision, so
register reported follow_latest_refused for claim_matcher -> Consumer.edit:2).

A real human publication (browser session with ontology.schema.review, F3 transaction), then the trusted configurator: the lineage
names the publication; doctor and apply derive exactly one grant, claim_matcher -> Consumer.edit:2, named after the publication;
a publication not recorded as a human's is not a source, and one with a changed permission digest cannot be recorded at all."""
import json
import secrets
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios import service_grants as G
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_claim_matching_pg import env  # noqa: F401
from test_nx040_schema_publication_pg import port, proposal
from test_review_decisions_pg import MANIFEST_PATH, TENANT, review  # noqa: F401

pytestmark = pytest.mark.parametrize('env', [TENANT], indirect=True)
EDIT2 = 'eios:action:Consumer.edit:2'


def configurator(f):
    f['admin'].execute('alter role nexloop_configurator login')
    path = f['tmp'] / 'configurator-dsn'
    path.write_text(make_conninfo(f['pg'], user='nexloop_configurator'))
    path.chmod(0o600)
    return path


def published(f):
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    out = port(f).publish(pid, rationale='顾客常提到门店', idempotency_key='synthetic-follow-publication-01')
    assert out['published'] is True and out['successor_actions'] == [EDIT2]
    return out['publication_id']


def test_a_human_schema_publication_is_followed_by_the_declared_service_grant_only(review):  # noqa: F811
    f = review
    publication = published(f)
    dsn = configurator(f)
    row = next(r for r in G.action_lineage(dsn, TENANT) if r['resource_id'] == EDIT2)
    assert row['schema_publication_id'] == publication and row['review_decision_id'] is None and row['active'] is True
    manifest = G.load(MANIFEST_PATH)
    doctor = G.doctor(manifest, TENANT, database_url_file=dsn)
    assert [(g['principal'], g['resource_id']) for g in doctor['follow_latest']['derived']] == [('claim_matcher', EDIT2)]
    assert doctor['follow_latest']['refused'] == []
    assert doctor['follow_latest']['derived'][0]['purpose'].endswith(f'(schema publication {publication})')
    tokens = {p['credential_reference']: secrets.token_urlsafe(48) for p in manifest['principals']}

    def private(name, value):
        path = f['tmp'] / name
        path.write_text(value)
        path.chmod(0o600)
        return path
    applied = G.apply(manifest, TENANT, database_url_file=dsn, signing_key_file=private('signing-key', f['signer'].material.hex()),
                      signing_key_id=f['signer'].key_id, service_secrets_file=private('service-secrets', json.dumps(tokens)),
                      credential_expires_at=datetime.now(UTC) + timedelta(hours=2))
    # Exactly one grant on a successor: the declared one (the rest of grants_added is the manifest's own first apply).
    successors = [(g['resource_id'], g['operation']) for g in applied['grants_added'] if g['resource_id'].endswith(':2')]
    assert successors == [(EDIT2, 'execute')] and applied['follow_latest']['refused'] == []


def test_a_publication_not_recorded_as_a_humans_is_no_source_and_a_changed_digest_cannot_be_recorded(review):  # noqa: F811
    f = review
    admin = f['admin']
    publication = published(f)
    dsn = configurator(f)
    manifest = G.load(MANIFEST_PATH)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # past the append-only trigger, rolled back below
        admin.execute("update ontology.nexloop_schema_publications set published_by='service:synthetic' where publication_id=%s", (publication,))
    try:
        row = next(r for r in G.action_lineage(dsn, TENANT) if r['resource_id'] == EDIT2)
        assert row['schema_publication_id'] is None
        doctor = G.doctor(manifest, TENANT, database_url_file=dsn)
        assert doctor['follow_latest']['derived'] == []
        assert [(r['successor'], r['reason']) for r in doctor['follow_latest']['refused']] == [(EDIT2, 'not_published_by_review_decision')]
    finally:
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute("update ontology.nexloop_schema_publications set published_by='human:restored' where publication_id=%s", (publication,))
    with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update ontology.nexloop_schema_publications set permission_digest_after=%s where publication_id=%s", ('0' * 64, publication))
