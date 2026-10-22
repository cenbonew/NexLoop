"""NX-040 F3/F4 Schema publication in the workbench login realm (dispatcher 2026-10-21): the routes call the existing
SchemaPublicationPort. Real PG, real workbench login cookies and the production create_app; synthetic data.

- The owner (and a reviewer: F3's role list is ontology.schema.review) reads the basis and the check, and publishes; the operator and a
  customer get 403; another tenant's proposal is unknown (404).
- Every publication goes through the F4 permit (an accepted nx040-gates-1 report): without it, permit_missing and nothing written.
- A refused change (F1) answers its code through check and publish and writes nothing.
"""
from nexloop_eios.workbench_roles import compile_members
from test_browser_http import ORIGIN
from test_nx040_schema_publication_pg import proposal
from test_workbench_read_pg import ROLES, members_file, workbench, workbench_login, write_facts, configure  # noqa: F401
from test_workbench_read_pg import TENANT, browser_business, conversations, identity, published_action, uow  # noqa: F401
from support.tenants import TENANT_B

BASE = '/api/v1/workbench/schema-proposals'


def csrf_of(client):
    return client.post('/api/v1/workbench/auth/csrf', headers={'Origin': ORIGIN}).json()['csrf_token']


def counts(admin):
    return (admin.execute('select count(*) from ontology.object_type_versions where tenant_id=%s', (TENANT,)).fetchone()[0],
            admin.execute('select count(*) from control.nexloop_action_definitions where tenant_id=%s', (TENANT,)).fetchone()[0],
            admin.execute('select count(*) from ontology.nexloop_schema_publications').fetchone()[0])


def setup(w, *members):
    configure(w, *members)
    write_facts(w['admin'], compile_members(ROLES, members_file(*members)))
    return {'admin': w['admin'], 'tenant': TENANT}


def post(client, path, body, key):
    return client.post(path, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf_of(client), 'Idempotency-Key': key}, json=body)


def test_the_owner_reads_checks_and_publishes_through_the_port_and_the_operator_gets_403(workbench):
    w = workbench
    admin = w['admin']
    f = setup(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    before = counts(admin)
    # The operator holds no ontology.schema.review: no basis, no check, no publication.
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        assert client.get(f'{BASE}/{pid}/basis').status_code == 403 and client.get(f'{BASE}/{pid}/check').status_code == 403
        denied = post(client, f'{BASE}/{pid}/publish', {'rationale': 'synthetic'}, 'synthetic-schema-denied-01')
        assert denied.status_code == 403 and denied.json()['code'] == 'forbidden'
    with w['client']() as client:  # a customer in the workbench realm is never a member
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get(f'{BASE}/{pid}/basis').status_code == 403
    assert counts(admin) == before
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        basis = client.get(f'{BASE}/{pid}/basis')
        assert basis.status_code == 200 and basis.json()['operation'] == 'add_optional_property' and basis.json()['type_name'] == 'Consumer'
        check = client.get(f'{BASE}/{pid}/check').json()
        assert check['compatibility']['allowed'] is True and check['next_version'] == check['parent_version'] + 1
        assert counts(admin) == before  # reads write nothing
        # Input checks before the port: no CSRF, a bad key, a bad body, a malformed id.
        assert client.post(f'{BASE}/{pid}/publish', headers={'Origin': ORIGIN, 'Idempotency-Key': 'synthetic-schema-0001'}, json={'rationale': 'x'}).status_code == 401
        assert post(client, f'{BASE}/{pid}/publish', {'rationale': 'x'}, 'short').status_code == 422
        assert post(client, f'{BASE}/{pid}/publish', {'rationale': 'x', 'extra': 1}, 'synthetic-schema-0001').status_code == 422
        assert client.get(f'{BASE}/not-a-uuid/basis').status_code == 422
        out = post(client, f'{BASE}/{pid}/publish', {'rationale': '负责人发布（合成）'}, 'synthetic-schema-0001')
        assert out.status_code == 200 and out.json()['published'] is True, out.text
        assert out.json()['permit'].startswith('permit:')
    assert counts(admin)[0] == before[0] + 1 and counts(admin)[2] == before[2] + 1
    assert admin.execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'published'


def test_without_a_release_permit_or_with_a_refused_change_nothing_is_published(workbench):
    w = workbench
    admin = w['admin']
    f = setup(w, (w['owner'], 'owner'))
    missing = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'shoe_size', 'value_type': 'integer'})
    admin.execute('set session_replication_role=replica')
    admin.execute('delete from runtime.nexloop_evo_release_permits where candidate_id=(select candidate_id from ontology.nexloop_schema_proposals where proposal_id=%s)', (missing,))
    admin.execute('set session_replication_role=origin')
    refused = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'raw_blob', 'value_type': 'json'})
    before = counts(admin)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        out = post(client, f'{BASE}/{missing}/publish', {'rationale': 'synthetic'}, 'synthetic-schema-0002').json()
        assert out == {'published': False, 'codes': ['permit_missing'], 'source': 'permit'}
        check = client.get(f'{BASE}/{refused}/check').json()['compatibility']
        assert check['allowed'] is False and 'value_type_not_allowed' in check['codes']
        out = post(client, f'{BASE}/{refused}/publish', {'rationale': 'synthetic'}, 'synthetic-schema-0003').json()
        assert out['published'] is False and 'value_type_not_allowed' in out['codes']
    assert counts(admin) == before


def test_another_tenants_proposal_is_unknown(workbench):
    w = workbench
    admin = w['admin']
    setup(w, (w['owner'], 'owner'))
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_B,))
    other = proposal({'admin': admin, 'tenant': TENANT_B}, 'add_object_type', 'Coupon', {'properties': [{'property_name': 'code', 'value_type': 'string'}]})
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        assert client.get(f'{BASE}/{other}/basis').status_code == 404 and client.get(f'{BASE}/{other}/check').status_code == 404
        assert post(client, f'{BASE}/{other}/publish', {'rationale': 'synthetic'}, 'synthetic-schema-0004').status_code == 404
    assert admin.execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (other,)).fetchone()[0] == 'frozen'


def test_check_lists_the_published_definitions_the_proposal_may_affect(workbench):
    """EIOS gap A1 (NX-061): the check answers, before publishing, which Object Sets / Views / Functions / Actions depend on the
    proposal's type, and which of them on the property it changes (the vendored eios.ontology.impact graph over 0319's rows)."""
    from psycopg.types.json import Jsonb
    from test_object_sets_pg import publish as publish_set, seed_type
    from test_object_views_pg import publish as publish_view
    w = workbench
    admin = w['admin']
    f = setup(w, (w['owner'], 'owner'))
    seed_type(admin)  # Consumer v97: sport, level, income, note
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Store',1,%s)",
                  (TENANT, Jsonb({'type_name': 'Store', 'version': 1, 'properties': [{'property_name': 'city', 'value_type': 'string'}],
                                  'property_groups': [{'group_name': 'place', 'property_names': ['city']}]})))
    assert publish_set(admin, {'stable_name': 'consumer.by_level', 'object_type': 'Consumer', 'reason': '按等级',
                               'filters': [{'property': 'level', 'operator': 'gte', 'value': 3}], 'projection': ['sport']})['version'] == 1
    assert publish_set(admin, {'stable_name': 'consumer.by_sport', 'object_type': 'Consumer', 'reason': '按运动',
                               'filters': [{'property': 'sport', 'operator': 'eq', 'value': '网球'}], 'projection': ['sport']})['version'] == 1
    assert publish_set(admin, {'stable_name': 'store.by_city', 'object_type': 'Store', 'reason': '按城市',
                               'filters': [{'property': 'city', 'operator': 'eq', 'value': '上海'}], 'projection': ['city']})['version'] == 1
    assert publish_view(admin, {'stable_name': 'consumer.card', 'object_type': 'Consumer', 'title_property': 'sport', 'reason': '卡片',
                                'sections': [{'name': 'main', 'title': '主要', 'fields': ['sport', 'level']}], 'relations': []})['version'] == 1
    level = proposal(f, 'add_vocabulary_value', 'Consumer', {'property_name': 'level', 'value': '5'})
    added = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    before = counts(admin)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        out = client.get(f'{BASE}/{level}/check').json()['impact']
        names = {(d['definition_type'], d['stable_name']): d for d in out['dependents']}
        assert out['type_name'] == 'Consumer' and out['changed_properties'] == ['level'] and out['unreadable'] == []
        assert ('object_set', 'store.by_city') not in names  # another type's Definition is not listed
        assert any(k[0] == 'action' for k in names)  # the Consumer Actions of the fixture depend on the type too
        assert {k for k in names if k[0] in ('object_set', 'object_view')} == {
            ('object_set', 'consumer.by_level'), ('object_set', 'consumer.by_sport'), ('object_view', 'consumer.card')}
        assert names[('object_set', 'consumer.by_level')]['changed_properties'] == ['level']
        assert names[('object_view', 'consumer.card')]['changed_properties'] == ['level']
        assert names[('object_set', 'consumer.by_sport')]['changed_properties'] == []  # depends on the type, not on `level`
        assert names[('object_set', 'consumer.by_level')]['pinned_type_version'] == 97
        assert out['affected_by_changed_properties'] == sum(1 for d in out['dependents'] if d['changed_properties']) >= 2
        assert out['dependents'][0]['changed_properties']  # the property-affected ones come first
        new = client.get(f'{BASE}/{added}/check').json()['impact']
        assert new['changed_properties'] == ['preferred_store'] and new['affected_by_changed_properties'] == 0  # nothing reads it yet
        assert {(d['definition_type'], d['stable_name']) for d in new['dependents']} >= {('object_set', 'consumer.by_level'), ('object_view', 'consumer.card')}
    with w['client']() as client:  # the operator holds no ontology.schema.review: no check, so no impact either
        configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
        write_facts(admin, compile_members(ROLES, members_file((w['owner'], 'owner'), (w['operator'], 'operator'))))
        assert workbench_login(client, w['operator']).status_code == 200
        assert client.get(f'{BASE}/{level}/check').status_code == 403
    assert counts(admin) == before  # the check writes nothing
