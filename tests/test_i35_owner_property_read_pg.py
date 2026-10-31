"""I35 diagnosis (scripts/ops/i35_owner_property_read.sql) on real PostgreSQL: the script runs read-only, and it shows that the owner
reads no Consumer v1 profile property - 0123 derives a Consumer property only inside a property group and v1 declares none. Synthetic
tenant only."""
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A as TENANT
from test_workbench_read_pg import ROLES
from test_workbench_read_pg import browser_business, conversations, identity, provision, published_action, uow, workbench  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ops/i35_owner_property_read.sql'


def statements():
    text = SCRIPT.read_text().replace(":'tenant'", '%(tenant)s')
    body = [line for line in text.splitlines() if not line.startswith('\\') and not line.startswith('--')]
    return [s.strip() for s in '\n'.join(body).split(';') if s.strip() and s.strip() not in ('begin transaction read only', 'rollback')]


def test_the_diagnosis_runs_read_only_and_shows_the_consumer_profile_unreadable(workbench):
    w = workbench; admin = w['admin']
    provision(w)
    consumer = admin.execute("select object_id from ontology.objects where tenant_id=%s and world='real' and type_name='Consumer' limit 1",
                                                  (TENANT,)).fetchone()
    assert consumer is not None  # the workbench fixture's customer owns a Consumer; never a silent skip
    results = []
    with admin.transaction():
        admin.execute('set transaction read only')
        for sql in statements():
            results.append(admin.execute(sql, {'tenant': TENANT}).fetchall())
    per_type = {r[0]: r for r in results[-1]}
    assert per_type['Message'][1:4] == (True, 5, 5)  # the owner reads every Message field (0123)
    # The fixture's Consumer is a bare v1 (no declared property), so section 5 has no row for it; the profile property the stage
    # Consumer declares (consumer_profile_schema, no property group) is not derived for the owner although the object is.
    target = 'eios:{}:Consumer/' + consumer[0]  # the row's object_id
    may = "select authz.nexloop_workbench_member_may_read(%s,'real','owner',%s)"
    assert admin.execute(may, (TENANT, target.format('object'))).fetchone()[0] is True
    assert admin.execute(may, (TENANT, target.format('property') + '/display_name')).fetchone()[0] is False


MAY = "select authz.nexloop_workbench_member_may_read(%s,'real',%s,%s)"


def may(admin, role, target):
    return admin.execute(MAY, (TENANT, role, target)).fetchone()[0]


def profile_consumer(admin):
    """The fixture's Consumer moved to a Consumer v2 declaring the stage profile (consumer_profile_schema) plus one grouped property
    whose group the owner restricts - a disposable database, rows written as migrations and governed Actions would leave them."""
    from nexloop_eios.consumer_profile import consumer_profile_schema
    definition = consumer_profile_schema().model_dump(mode='json')
    definition.update(version=2, property_groups=[{'group_name': 'preference', 'property_names': ['locale']}])
    consumer = admin.execute("select object_id from ontology.objects where tenant_id=%s and world='real' and type_name='Consumer' limit 1", (TENANT,)).fetchone()[0]
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',2,%s)", (TENANT, Jsonb(definition)))
        admin.execute("update ontology.objects set schema_version=2,properties=%s where tenant_id=%s and world='real' and type_name='Consumer' and object_id=%s",
                      (Jsonb({'display_name': '合成客户', 'locale': 'zh-CN', 'timezone': 'Asia/Shanghai', 'lifecycle_status': 'active'}), TENANT, consumer))
        admin.execute("insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,'property_group_restriction',array['Consumer'],%s) "
                      "on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload",
                      (TENANT, Jsonb({'tenant_id': TENANT, 'type_name': 'Consumer', 'restricted_groups': ['preference']})))
    return consumer


def test_the_owner_reads_every_listed_type_but_never_a_restricted_group_or_an_erasing_consumer(workbench):
    """I35 (roles v52 read_types, migration workbench_read_types): the owner reads the Consumer profile and any listed type; a property in
    an owner-restricted group stays unreadable; operator and reviewer read exactly what they read before; an erasing Consumer gives
    nothing to anyone."""
    from erasure_support import mark_erasing
    w = workbench; admin = w['admin']
    consumer = profile_consumer(admin)  # consumer_profile_schema: the six profile properties, none of them in a group
    obj, prop = 'eios:object:Consumer/' + consumer, 'eios:property:Consumer/' + consumer + '/'
    # Before roles v52 is applied (no read list): 0123 derives no ungrouped profile property, as on stage (I35 diagnosis).
    assert may(admin, 'owner', obj) and not may(admin, 'owner', prop + 'display_name')
    provision(w)
    # The same read after v52: the owner reads it.
    assert may(admin, 'owner', prop + 'display_name')
    assert admin.execute("select control.nexloop_workbench_role_read_types(%s,'owner')", (TENANT,)).fetchone()[0] == ROLES['read_types']['owner']
    assert len(ROLES['read_types']['owner']) == 20 and ROLES['read_types']['operator'] == ROLES['read_types']['reviewer'] == []
    # The owner: the profile, but not the restricted group's property, nor a property the type does not declare.
    assert may(admin, 'owner', obj) and all(may(admin, 'owner', prop + p) for p in ('display_name', 'timezone', 'lifecycle_status', 'contact_preferences'))
    assert not may(admin, 'owner', prop + 'locale') and not may(admin, 'owner', prop + 'undeclared')
    # Operator and reviewer: unchanged (0123: an ungrouped Consumer property is not derived; the reviewer reads no Consumer).
    assert may(admin, 'operator', obj) and not may(admin, 'operator', prop + 'display_name') and not may(admin, 'operator', prop + 'locale')
    assert not may(admin, 'reviewer', obj) and not may(admin, 'reviewer', prop + 'display_name')
    # I35-c: the list only widens the owner. No role, or a role outside the three, derives nothing (a customer principal is never a
    # member, so it reaches here without a role); the reviewer's Message rule (evidence of a pending definition only) is unchanged.
    message = admin.execute("select object_id from ontology.objects where tenant_id=%s and world='real' and type_name='Message' limit 1", (TENANT,)).fetchone()[0]
    for role in (None, 'customer', 'Owner'):
        assert not may(admin, role, obj) and not may(admin, role, prop + 'display_name') and not may(admin, role, 'eios:object:Message/' + message)
    assert not may(admin, 'reviewer', 'eios:object:Message/' + message) and not may(admin, 'reviewer', 'eios:property:Message/' + message + '/body')
    assert may(admin, 'owner', 'eios:property:Message/' + message + '/body') and may(admin, 'operator', 'eios:property:Message/' + message + '/body')
    # Another listed type: the owner reads it, the operator still does not.
    conversation = admin.execute("select object_id from ontology.objects where tenant_id=%s and world='real' and type_name='Conversation' "
                                 "and properties->>'consumer_id'=%s limit 1", (TENANT, consumer)).fetchone()[0]
    assert may(admin, 'owner', 'eios:object:Conversation/' + conversation) and not may(admin, 'operator', 'eios:object:Conversation/' + conversation)
    assert may(admin, 'owner', 'eios:property:Conversation/' + conversation + '/consumer_id')
    # I35-a: objects of newly listed types that name the Consumer (consumer_id, and RoleExecutionCeiling's consumer_ids array).
    linked = {'MessageAssignment': {'consumer_id': consumer}, 'ConsumerOwnership': {'consumer_id': consumer, 'principal_id': 'p'},
              'RoleExecutionCeiling': {'consumer_ids': ['0' * 64, consumer], 'active': True}}
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # synthetic rows; the type shape checks are not under test
        for type_name, props in linked.items():
            admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s) on conflict do nothing",
                          (TENANT, type_name, Jsonb({'type_name': type_name, 'version': 1, 'properties': [{'property_name': n, 'value_type': 'json'} for n in props]})))
            admin.execute("insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at) "
                          "values(%s,'real',%s,%s,1,%s,'synthetic',%s,clock_timestamp(),clock_timestamp())",
                          (TENANT, type_name, consumer[:-8] + 'ab' * 4, Jsonb(props), 'i35-' + type_name))
    ids = {t: consumer[:-8] + 'ab' * 4 for t in linked}
    assert all(may(admin, 'owner', f'eios:object:{t}/{i}') for t, i in ids.items())
    # An erasing Consumer: nothing of it, nor of the Conversation or any listed object that belongs to it.
    mark_erasing(admin, TENANT, consumer)
    assert not may(admin, 'owner', obj) and not may(admin, 'owner', prop + 'display_name') and not may(admin, 'operator', obj)
    assert not may(admin, 'owner', 'eios:object:Conversation/' + conversation)
    assert not any(may(admin, 'owner', f'eios:object:{t}/{i}') for t, i in ids.items())


def test_the_owner_reads_a_newly_listed_type_through_the_audited_read(workbench):
    """The workbench values of a Conversation (a type no role read before v52) through the one audited object read: one audit row of
    kind 'object' for the owner; the operator is still refused."""
    from test_workbench_read_pg import workbench_login
    w = workbench; admin = w['admin']
    provision(w)
    conversation = admin.execute("select object_id from ontology.objects where tenant_id=%s and world='real' and type_name='Conversation' limit 1", (TENANT,)).fetchone()[0]
    path = f'/api/v1/workbench/objects/Conversation/{conversation}'
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        got = client.get(path)
        assert got.status_code == 200, got.text
        assert {p['name']: p['status'] for p in got.json()['properties']}.get('consumer_id') == 'ok', got.text
    audit = admin.execute("select role,object_kind,read_purpose from runtime.nexloop_workbench_read_audit where tenant_id=%s and target_resource=%s",
                          (TENANT, 'eios:object:Conversation/' + conversation)).fetchall()
    assert audit and set(audit) == {('owner', 'object', 'world_model')}
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        assert client.get(path).status_code in (403, 404)
