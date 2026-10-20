"""Enterprise world model W2: instance browsing (GET /api/v1/workbench/objects/{type}) and the depth-1 neighbourhood
(GET /api/v1/workbench/objects/{type}/{id}/neighbors), verbs `objects` / `object_neighbors`.

Real PostgreSQL and workbench login cookies, synthetic data, on the slice 1 fixture (test_workbench_read_pg). SQL returns
candidates only; each object (and, for a property edge, the referencing property) is shown only when the ADR-025 member derivation
derives its READ, through the audited read (purpose 'world_model'). Unreadable candidates are counted as invisible, candidates
beyond 25 a group as collapsed. Another tenant's objects never appear; non-members get 403. Admin only seeds (a Consumer type version
with property groups and the owner restriction fact as configuration stand-ins, synthetic objects and one relation) and probes.
"""
import hashlib
import json
from pathlib import Path

import jsonschema

from psycopg.types.json import Jsonb

from support.tenants import TENANT_B
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench, workbench_login)


def contract(name):
    return json.loads((Path(__file__).resolve().parents[1] / f'packages/contracts/{name}.schema.json').read_text())


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def obj(admin, type_name, object_id, props, tenant=TENANT, version=1):
    admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
        values(%s,'real',%s,%s,%s,%s,'synthetic',%s,clock_timestamp(),clock_timestamp())""", (tenant, type_name, object_id, version, Jsonb(props), 'w2-' + object_id[:8]))


def seed(w):
    """The fixture's Consumer as the centre (type version 96: referrer in group 'preference'); three Consumers referring to it, one
    Consumer it is related to, 30 MessageAssignments referring to it (a type no member derives), and a Consumer of another tenant referring to it."""
    admin, centre = w['admin'], w['f']['consumer']
    definition = {'type_name': 'Consumer', 'version': 96, 'properties': [{'property_name': 'referrer', 'value_type': 'string'},
        {'property_name': 'favorite_sport', 'value_type': 'string'}],
        'property_groups': [{'group_name': 'preference', 'property_names': ['referrer', 'favorite_sport']}]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',96,%s)", (TENANT, Jsonb(definition)))
    admin.execute("update ontology.objects set schema_version=96,properties=%s where tenant_id=%s and type_name='Consumer' and object_id=%s",
                  (Jsonb({'favorite_sport': '网球'}), TENANT, centre))
    referrers = [hexid(f'w2-referrer-{n}') for n in range(3)]
    for r in referrers:
        obj(admin, 'Consumer', r, {'referrer': centre}, version=96)
    related = hexid('w2-related')
    obj(admin, 'Consumer', related, {}, version=96)
    admin.execute("""insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,created_at)
        values('w2-relation',%s,'real','knows',1,'Consumer',%s,'Consumer',%s,clock_timestamp())""", (TENANT, centre, related))
    goals = [hexid(f'w2-goal-{n}') for n in range(30)]
    for g in goals:
        obj(admin, 'MessageAssignment', g, {'consumer_id': centre, 'state': 'active'})
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_B,))
    obj(admin, 'Consumer', hexid('w2-other-tenant'), {'referrer': centre}, tenant=TENANT_B)
    return centre, referrers, related, goals


def audit(admin):
    return admin.execute("select count(*) from runtime.nexloop_workbench_read_audit where read_purpose='world_model'").fetchone()[0]


def test_neighbourhood_checks_every_neighbour_counts_the_rest_and_caps_groups(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    centre, referrers, related, goals = seed(w)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        before = audit(admin)
        got = client.get(f'/api/v1/workbench/objects/Consumer/{centre}/neighbors')
        assert got.status_code == 200, got.text
        n = got.json()
        jsonschema.validate(n, contract('workbench-object-neighbors'))
        assert (n['depth'], n['group_limit']) == (1, 25)
        groups = {(g['direction'], g['via'], g['type_name']): g for g in n['groups']}
        # Readable Consumers referring to the centre (object READ + the referrer property READ on them).
        g = groups[('incoming', 'property:referrer', 'Consumer')]
        assert sorted(x['object_id'] for x in g['items']) == sorted(referrers) and (g['invisible'], g['collapsed']) == (0, 0)
        # The governed relation.
        g = groups[('outgoing', 'knows', 'Consumer')]
        assert [x['object_id'] for x in g['items']] == [related] and g['invisible'] == 0
        # 30 MessageAssignments (a type no member derives, ADR-035 §4): at most 25 checked, 5 collapsed; no id leaks.
        g = groups[('incoming', 'property:consumer_id', 'MessageAssignment')]
        assert (g['items'], g['invisible'], g['collapsed']) == ([], 25, 5)
        assert not any(goal in got.text for goal in goals)
        # Another tenant's object referring to the same id never appears.
        assert hexid('w2-other-tenant') not in got.text
        # Every member read is audited with its purpose (ADR-025): the centre, three referrers, one related Consumer at least.
        assert audit(admin) - before >= 5
        # Owner restriction of the group: the property edge is no longer derivable, its neighbours become invisible.
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
            (TENANT, 'property_group_restriction', ['Consumer'], Jsonb({'tenant_id': TENANT, 'type_name': 'Consumer', 'restricted_groups': ['preference'], 'decision': 'owner 2026-10-19 (synthetic)'})))
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        g = {(x['direction'], x['via'], x['type_name']): x for x in client.get(f'/api/v1/workbench/objects/Consumer/{centre}/neighbors').json()['groups']}
        assert g[('incoming', 'property:referrer', 'Consumer')]['items'] == [] and g[('incoming', 'property:referrer', 'Consumer')]['invisible'] == 3
        assert [x['object_id'] for x in g[('outgoing', 'knows', 'Consumer')]['items']] == [related]  # object READ only: still visible
        # A centre the member may not read is refused; an unknown one is 404; malformed input 422.
        assert client.get(f'/api/v1/workbench/objects/MessageAssignment/{goals[0]}/neighbors').status_code == 403
        assert client.get(f'/api/v1/workbench/objects/Consumer/{hexid("missing")}/neighbors').status_code == 404
        assert client.get(f'/api/v1/workbench/objects/Consumer/{centre}/neighbors?depth=2').status_code == 422
        assert client.get('/api/v1/workbench/objects/consumer/' + centre + '/neighbors').status_code == 422


def test_instance_pages_list_readable_ids_only_and_count_the_rest(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    centre, referrers, related, goals = seed(w)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        before = audit(admin)
        consumers = client.get('/api/v1/workbench/objects/Consumer?limit=2')
        assert consumers.status_code == 200
        page = consumers.json()
        jsonschema.validate(page, contract('workbench-object-page'))
        assert len(page['items']) == 2 and page['invisible'] == 0 and page['total'] == 5 and page['next_after'] == page['items'][-1]['object_id']
        assert set(page['items'][0]) == {'object_id', 'schema_version', 'revision', 'updated_at'}  # no property
        rest = client.get('/api/v1/workbench/objects/Consumer?after=' + page['next_after']).json()
        assert len(rest['items']) == 3 and rest['next_after'] is None
        assert {x['object_id'] for x in page['items'] + rest['items']} == {centre, related, *referrers}  # never the other tenant's
        assert audit(admin) - before == 5
        listed = client.get('/api/v1/workbench/objects/MessageAssignment').json()
        assert (listed['items'], listed['invisible'], listed['total']) == ([], 30, 30) and not any(g in str(listed) for g in goals)
        assert client.get('/api/v1/workbench/objects/Consumer?limit=101').status_code == 422
        assert client.get('/api/v1/workbench/objects/Consumer?tenant_id=' + TENANT_B).status_code == 422
    for human in (w['outsider'], w['customer']):
        with w['client']() as client:
            assert workbench_login(client, human).status_code == 200
            assert client.get('/api/v1/workbench/objects/Consumer').status_code == 403
            assert client.get(f'/api/v1/workbench/objects/Consumer/{centre}/neighbors').status_code == 403
