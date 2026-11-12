"""Option B M2.1 test support: Consumer v2 published as F3 leaves it (type v2, the successor :2 of every Consumer-bound Action, the
objects moved to v2, the human publication record), register's follow_latest_version at the fact level for every successor, and the
effect Action successor :2 admitted through the trusted configuration's admission function (not the publication, H1 (a) is M2.3).
Synthetic data."""
import json
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

from nexloop_eios.schema_publication import successors
from system_catalog_fixture import insert_active_actions
from test_effect_scope_successor_proof_pg import ADDED, follow_facts

EFFECT = 'nexloop.service.request'


def admit(admin, tenant, publication, version=2, name=EFFECT):
    admin.execute('set session authorization nexloop_configurator')
    try:
        return admin.execute('select control.nexloop_admit_effect_action_successor(%s,%s,%s,%s,%s,false)',
                             (tenant, publication, name, version, 'human:owner')).fetchone()[0]
    finally:
        admin.execute('reset session authorization')


def bump_consumer_v2(admin, tenant, *, admitted=True, categories=False):
    """Returns the publication id. The caller authenticates its sessions again (authority facts changed)."""
    old = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=1", (tenant,)).fetchone()[0]
    new = {**old, 'version': 2, 'properties': list(old['properties']) + [ADDED]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',2,%s)", (tenant, Jsonb(new)))
    bound = [{'definition': d, 'capability': c} for d, c in admin.execute("""select definition,capability from control.nexloop_action_definitions
        where tenant_id=%s and world='real' and active and definition->'object_types' @> %s::jsonb""",
        (tenant, json.dumps([{'stable_name': 'Consumer', 'version': 1}]))).fetchall()]
    rows = successors({'actions': bound}, new)
    insert_active_actions(admin, tenant, rows)
    names = [f"eios:action:{r['definition']['stable_name']}:{r['definition']['version']}" for r in rows]
    for name in names:
        follow_facts(admin, tenant, name.rsplit(':', 1)[0] + ':1', name)
    admin.execute("update ontology.objects set schema_version=2 where tenant_id=%s and world='real' and type_name='Consumer' and schema_version=1", (tenant,))
    publication = str(uuid.uuid4())
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # the proposal row of the F3 transaction is not part of this setup
        admin.execute("""insert into ontology.nexloop_schema_publications(tenant_id,world,publication_id,proposal_id,kind,type_name,version_before,version_after,
            successor_actions,permission_digest_before,permission_digest_after,compatibility_report_digest,published_by,rationale)
            values(%s,'real',%s,%s,'publish','Consumer',1,2,%s,%s,%s,%s,'human:synthetic-reviewer','synthetic publication')""",
                      (tenant, publication, str(uuid.uuid4()), names, 'a' * 64, 'a' * 64, 'b' * 64))
    if admitted:
        outcome = admit(admin, tenant, publication)
        assert outcome['admitted'] is True, outcome
    if categories:  # the effect category of :2 is declared like :1's (an undeclared definition is external and customer_contact)
        declare_categories_v2(admin, tenant)
    return publication


def declare_categories_v2(admin, tenant):
    from nexloop_eios.postgres_artifacts import canonical_payload
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'deploy/configuration/action-effect-categories.v3.json').read_text())
    manifest['actions'] = [{**a, 'version': 2} for a in manifest['actions'] if a['stable_name'] == EFFECT]
    admin.execute('set session authorization nexloop_configurator')
    try:
        admin.execute('select control.nexloop_configure_effect_categories(%s,%s,%s::jsonb)', (tenant, 'real', canonical_payload(manifest)))
    finally:
        admin.execute('reset session authorization')
