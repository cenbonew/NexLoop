"""P0 2026-11-10 (L6 Q6): a new object is written at its type's latest published version, never silently at version 1.

Business setup (the CLI, real governed Actions) after Consumer v2 is published as F3 leaves it: with Consumer.create:2 and the owner's
grant following to it, the new Consumer is v2; without the grant, or without any create Action for v2, setup refuses and writes no
Consumer at all; the readiness self-check reports the uncovered type. Synthetic data."""

from psycopg.types.json import Jsonb

from nexloop_eios.schema_publication import successors
from system_catalog_fixture import insert_active_actions
from test_business_setup_pg import business_plan, invoke  # noqa: F401
from test_effect_scope_successor_proof_pg import ADDED, follow_facts
from test_trusted_configuration_pg import configured  # noqa: F401

CREATE1 = 'eios:action:Consumer.create:1'


def publish_consumer_v2(admin, tenant, *, create_successor=True):
    old = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=1", (tenant,)).fetchone()[0]
    new = {**old, 'version': 2, 'properties': list(old['properties']) + [ADDED]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',2,%s)", (tenant, Jsonb(new)))
    bound = [{'definition': d, 'capability': c} for d, c in admin.execute("""select definition,capability from control.nexloop_action_definitions
        where tenant_id=%s and world='real' and active and definition->'object_types' @> '[{"stable_name":"Consumer"}]'""", (tenant,)).fetchall()]
    rows = [r for r in successors({'actions': bound}, new) if create_successor or r['definition']['stable_name'] != 'Consumer.create']
    insert_active_actions(admin, tenant, rows)
    admin.execute("update ontology.objects set schema_version=2 where tenant_id=%s and world='real' and type_name='Consumer' and schema_version=1", (tenant,))


def consumers(admin):
    return admin.execute("select schema_version from ontology.objects where type_name='Consumer' order by created_at").fetchall()


def coverage(admin):
    return admin.execute('select authz.nexloop_schema_create_coverage()').fetchone()[0]


def test_a_new_consumer_after_the_publication_is_written_at_v2(business_plan, admin):  # noqa: F811
    f = business_plan
    tenant = f['f']['tenant']
    publish_consumer_v2(admin, tenant)
    follow_facts(admin, tenant, CREATE1, CREATE1[:-1] + '2')
    result = invoke(f)
    assert result.returncode == 0, result.stdout + result.stderr
    assert consumers(admin) == [(2,)]
    assert coverage(admin) == {'uncovered': 0}


def test_without_a_grant_on_the_create_successor_setup_refuses_and_writes_no_consumer(business_plan, admin):  # noqa: F811
    f = business_plan
    publish_consumer_v2(admin, f['f']['tenant'])
    result = invoke(f)
    assert result.returncode != 0 and consumers(admin) == []
    assert coverage(admin) == {'uncovered': 0}  # the type is covered; this caller is not granted


def test_without_a_create_action_for_v2_setup_refuses_and_readiness_reports_it(business_plan, admin):  # noqa: F811
    f = business_plan
    publish_consumer_v2(admin, f['f']['tenant'], create_successor=False)
    result = invoke(f)
    assert result.returncode != 0 and consumers(admin) == []
    assert coverage(admin) == {'uncovered': 1}


def test_readiness_counts_nothing_before_a_publication(business_plan, admin):  # noqa: F811
    assert coverage(admin) == {'uncovered': 0}
