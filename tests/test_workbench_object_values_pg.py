"""Workbench Object property values (L4 capability gap D-G1, owner 2026-10-20; ADR-025, ADR-035): GET /api/v1/workbench/objects/{type}/{id}
and the values carried by GET /api/v1/workbench/objects/{type}. Real PostgreSQL and workbench login cookies, synthetic data on the
world model W2 fixture (tests/test_workbench_objects_pg.seed).

Without the member's READ nothing is shown; an owner-restricted group is named only (withheld); every read leaves its audit row; a
Consumer being erased shows no value; another tenant's object is never reached; nothing is written to a business object."""
import jsonschema
from psycopg.types.json import Jsonb

from erasure_support import mark_erasing
from support.tenants import TENANT_B
from test_workbench_objects_pg import audit, contract, hexid, seed
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench, workbench_login)


def objects_digest(admin):
    return admin.execute("select md5(coalesce(string_agg(o::text,'|' order by o.type_name,o.object_id),'')) from ontology.objects o").fetchone()[0]


def restrict(admin, groups):
    admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
        (TENANT, 'property_group_restriction', ['Consumer'], Jsonb({'tenant_id': TENANT, 'type_name': 'Consumer', 'restricted_groups': groups,
                                                                    'decision': 'owner 2026-10-20 (synthetic)'})))


def by_name(value):
    return {p['name']: p for p in value['properties']}


def test_values_are_shown_with_the_member_read_and_audited(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    centre, referrers, related, goals = seed(w)
    before_objects = objects_digest(admin)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        before = audit(admin)
        got = client.get(f'/api/v1/workbench/objects/Consumer/{centre}')
        assert got.status_code == 200, got.text
        value = got.json()
        jsonschema.validate(value, contract('workbench-object-values'))
        props = by_name(value)
        assert props['favorite_sport'] == {'name': 'favorite_sport', 'status': 'ok', 'value': '网球', 'value_type': 'text'}
        assert props['referrer'] == {'name': 'referrer', 'status': 'missing'}
        # One audited object read for the drawer (ADR-025, purpose world_model).
        assert audit(admin) - before == 1
        # The list page carries the same values, one audit row per listed object.
        before = audit(admin)
        page = client.get('/api/v1/workbench/objects/Consumer?limit=100').json()
        jsonschema.validate(page, contract('workbench-object-page'))
        listed = {x['object_id']: by_name(x) for x in page['items']}
        assert listed[centre]['favorite_sport']['value'] == '网球'
        assert all(listed[r]['referrer'] == {'name': 'referrer', 'status': 'ok', 'value': centre, 'value_type': 'text'} for r in referrers)
        assert audit(admin) - before == len(page['items']) == 5
    assert objects_digest(admin) == before_objects  # read only


def test_without_the_read_nothing_is_shown(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    centre, referrers, related, goals = seed(w)
    with w['client']() as client:
        # I35: the owner reads MessageAssignment through its roles v52 read list; the operator derives no read of it (ADR-035 §4).
        assert workbench_login(client, w['operator']).status_code == 200
        # MessageAssignment for the operator: 403, no value, no id on the list page.
        denied = client.get(f'/api/v1/workbench/objects/MessageAssignment/{goals[0]}')
        assert denied.status_code == 403 and 'active' not in denied.text
        listed = client.get('/api/v1/workbench/objects/MessageAssignment').json()
        assert listed['items'] == [] and listed['invisible'] == 30
        # Another tenant's object is unknown here (the tenant comes from the session): 404, no value.
        other = client.get(f'/api/v1/workbench/objects/Consumer/{hexid("w2-other-tenant")}')
        assert other.status_code == 404 and centre not in other.text
        assert client.get(f'/api/v1/workbench/objects/Consumer/{centre}?tenant_id={TENANT_B}').status_code == 422
    for human in (w['outsider'], w['customer']):  # not a workbench member with the read Action
        with w['client']() as client:
            assert workbench_login(client, human).status_code == 200
            got = client.get(f'/api/v1/workbench/objects/Consumer/{centre}')
            assert got.status_code == 403 and '网球' not in got.text


def test_an_owner_restricted_group_is_named_only(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    centre, referrers, related, goals = seed(w)
    restrict(admin, ['preference'])
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        got = client.get(f'/api/v1/workbench/objects/Consumer/{centre}')
        assert got.status_code == 200
        props = by_name(got.json())
        assert props['favorite_sport'] == {'name': 'favorite_sport', 'status': 'withheld', 'reason': 'restricted'}
        assert props['referrer'] == {'name': 'referrer', 'status': 'withheld', 'reason': 'restricted'}
        assert '网球' not in got.text
        page = client.get('/api/v1/workbench/objects/Consumer?limit=100')
        assert '网球' not in page.text and centre in page.text  # the object stays listed; its restricted values do not


def test_a_consumer_being_erased_shows_no_value(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    centre, referrers, related, goals = seed(w)
    mark_erasing(admin, TENANT, centre)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        before = audit(admin)
        got = client.get(f'/api/v1/workbench/objects/Consumer/{centre}')
        assert got.status_code == 403 and '网球' not in got.text
        page = client.get('/api/v1/workbench/objects/Consumer?limit=100')
        assert centre not in page.text and '网球' not in page.text
        # Its id as another object's value (the referrers' referrer) is withheld too.
        refs = [by_name(x)['referrer'] for x in page.json()['items'] if x['object_id'] in referrers]
        assert len(refs) == 3 and all(r == {'name': 'referrer', 'status': 'withheld', 'reason': 'erasing'} for r in refs)
        assert audit(admin) - before == len(page.json()['items'])  # only the readable ones were read (and audited)
