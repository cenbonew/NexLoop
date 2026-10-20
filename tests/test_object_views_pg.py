"""Object View, read only (capability gap P2, owner 2026-10-20; the Object Set pattern of ADR-036): governed owner publication
(versioned, immutable, in the vendored ObjectViewDefinition shape), a layout-only workbench read, and values that keep their own
READ - a View never shows a restricted or unreadable property.

Real PostgreSQL. Reads run on the world model W2 fixture (test_workbench_objects_pg: owner and operator members, Consumer with the
property group `preference`); the governed path on the outreach slice fixture. Admin seeds synthetic types, objects, a relation type,
the owner restriction fact as a configuration stand-in, and publishes the read tests' views through the SQL handler the governed entry
calls.
"""
import json
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from eios.ontology.definitions import ObjectViewDefinition
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT as COMMITMENT_TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from erasure_support import mark_erasing
from support.tenants import TENANT_B
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_object_sets_pg import hexid, restrict, seed_type
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_objects_pg import obj
from test_workbench_read_pg import (TENANT, browser_business, conversations, provision, published_action, workbench,  # noqa: F401
    workbench_login)

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
VIEW = {'stable_name': 'consumer.profile', 'object_type': 'Consumer', 'title_property': 'sport',
        'sections': [{'name': 'sport', 'title': '运动偏好', 'fields': ['sport', 'level']}, {'name': 'money', 'title': '消费能力', 'fields': ['income']}],
        'relations': ['plays_with'], 'reason': '客户详情按运动与消费分区'}


def relation(admin, tenant=TENANT):
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    admin.execute("insert into ontology.relation_type_versions(tenant_id,relation_name,version,definition) values(%s,'plays_with',1,%s)",
                  (tenant, Jsonb({'relation_name': 'plays_with', 'source_type': 'Consumer', 'target_type': 'Consumer', 'cardinality': 'many_to_many'})))


def publish(admin, body, tenant=TENANT):
    return admin.execute("select control.nexloop_governed_publish_object_view(%s,'real','owner-principal','owner-subject',%s,%s)",
                         (tenant, 'intent-' + hexid(str(body))[:12], Jsonb({'operation': 'publish_object_view', 'request_id': 'r', **body}))).fetchone()[0]


def test_definitions_follow_the_vendored_contract_and_are_versioned_and_immutable(workbench):
    admin = workbench['admin']
    seed_type(admin)
    relation(admin)
    assert publish(admin, VIEW) == {'stable_name': 'consumer.profile', 'version': 1, 'changed': True}
    assert publish(admin, VIEW)['changed'] is False  # the same layout: no new version
    assert publish(admin, {**VIEW, 'title_property': None, 'reason': '不设标题'})['version'] == 2
    rows = admin.execute('select version,definition,definition_digest,object_type from control.nexloop_object_view_definitions order by version').fetchall()
    for _, definition, digest, object_type in rows:  # the stored definition is a valid vendored ObjectViewDefinition
        v = ObjectViewDefinition.model_validate_json(json.dumps(definition))
        assert v.source.object_type.stable_name == object_type == 'Consumer' and v.status.value == 'published' and len(digest) == 64
        assert [f.property_name for f in v.fields] == ['sport', 'level', 'income'] and [r.stable_name for r in v.relations] == ['plays_with']
        assert v.functions == () and v.actions == () and v.created_at <= datetime.now(UTC)
    groups = rows[0][1]['display_hints']['property_groups']
    assert [(g['group_name'], g['display_name'], g['property_names']) for g in groups] == [('sport', '运动偏好', ['sport', 'level']), ('money', '消费能力', ['income'])]
    assert rows[0][1]['display_hints']['title_property'] == 'sport' and 'title_property' not in rows[1][1]['display_hints']
    for statement in ("update control.nexloop_object_view_definitions set reason='x'", 'delete from control.nexloop_object_view_definitions'):
        with pytest.raises(psycopg.Error, match='immutable'), admin.transaction():
            admin.execute(statement)
    for body, code in (({**VIEW, 'object_type': 'Unknown'}, 'unknown_object_type'),
                       ({**VIEW, 'sections': [{'name': 'a', 'title': 'A', 'fields': ['missing']}]}, 'unknown_property'),
                       ({**VIEW, 'sections': [{'name': 'a', 'title': 'A', 'fields': ['sport']}, {'name': 'b', 'title': 'B', 'fields': ['sport']}]}, 'duplicate'),
                       ({**VIEW, 'sections': [{'name': 'a', 'title': ' ', 'fields': ['sport']}]}, 'invalid_section'),
                       ({**VIEW, 'title_property': 'note'}, 'unknown_property'),  # a title outside the view's fields
                       ({**VIEW, 'relations': ['no_such_link']}, 'unknown_relation'),
                       ({**VIEW, 'functions': []}, 'invalid'),
                       ({**VIEW, 'stable_name': 'Bad'}, 'invalid')):
        with pytest.raises(psycopg.Error, match=code), admin.transaction():
            publish(admin, body)
    # A view keeps its type: a new version may not move it to another type.
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Ticket',1,%s)",
                  (TENANT, Jsonb({'type_name': 'Ticket', 'version': 1, 'properties': [{'property_name': 'state', 'value_type': 'string'}]})))
    with pytest.raises(psycopg.Error, match='object_type_changed'), admin.transaction():
        publish(admin, {'stable_name': 'consumer.profile', 'object_type': 'Ticket', 'sections': [{'name': 'a', 'title': 'A', 'fields': ['state']}], 'reason': 'x'})


def test_the_layout_read_and_the_values_keep_their_own_read(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    seed_type(admin)
    relation(admin)
    oid = hexid('ov-consumer')
    obj(admin, 'Consumer', oid, {'sport': '网球', 'level': 4, 'income': 9000, 'note': 'n'}, version=97)
    publish(admin, VIEW)
    restrict(admin, ['wealth'])  # income's group is restricted after publication
    # Another tenant's view of the same type is never read here.
    relation(admin, TENANT_B)
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',1,%s)",
                  (TENANT_B, Jsonb({'type_name': 'Consumer', 'version': 1, 'properties': [{'property_name': 'secret_b', 'value_type': 'string'}]})))
    publish(admin, {'stable_name': 'other.view', 'object_type': 'Consumer', 'sections': [{'name': 'b', 'title': '他人', 'fields': ['secret_b']}], 'reason': 'b'}, tenant=TENANT_B)
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        got = client.get('/api/v1/workbench/object-views/Consumer', headers={'Origin': ORIGIN})
        assert got.status_code == 200, got.text
        view = got.json()['view']
        assert (view['stable_name'], view['version'], view['object_type'], view['title_property']) == ('consumer.profile', 1, 'Consumer', 'sport')
        assert view['sections'] == [{'name': 'sport', 'title': '运动偏好', 'fields': ['sport', 'level']}, {'name': 'money', 'title': '消费能力', 'fields': ['income']}]
        assert view['relations'] == ['plays_with'] and 'secret_b' not in got.text and '他人' not in got.text
        assert '9000' not in got.text and '网球' not in got.text  # layout only: no value
        # No View for the type: null, the client keeps its default layout.
        none = client.get('/api/v1/workbench/object-views/Ticket', headers={'Origin': ORIGIN})
        assert none.status_code == 200 and none.json() == {'view': None}
        assert client.get('/api/v1/workbench/object-views/bad', headers={'Origin': ORIGIN}).status_code == 422
        assert client.get('/api/v1/workbench/object-views/Consumer?x=1', headers={'Origin': ORIGIN}).status_code == 422
        # The values of the View's fields come from the existing object read: income stays withheld although the View lists it.
        values = client.get(f'/api/v1/workbench/objects/Consumer/{oid}', headers={'Origin': ORIGIN})
        assert values.status_code == 200, values.text
        props = {p['name']: p for p in values.json()['properties']}
        assert props['income']['status'] == 'withheld' and props['income']['reason'] == 'restricted' and '9000' not in values.text
        assert props['sport']['status'] == 'ok'
        # NX-029: once the Consumer is being erased its object shows nothing, whatever the View lists.
        mark_erasing(admin, TENANT, oid)
        gone = client.get(f'/api/v1/workbench/objects/Consumer/{oid}', headers={'Origin': ORIGIN})
        assert gone.status_code in (403, 404) and '网球' not in gone.text
        assert client.get('/api/v1/workbench/object-views/Consumer', headers={'Origin': ORIGIN}).json()['view']['stable_name'] == 'consumer.profile'
    for human in (w['outsider'], w['customer']):
        with w['client']() as client:
            assert workbench_login(client, human).status_code == 200
            assert client.get('/api/v1/workbench/object-views/Consumer', headers={'Origin': ORIGIN}).status_code == 403


@PLAN
def test_only_the_owner_publishes_through_the_governed_action(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.object_view.publish',))
    body = {'stable_name': 'consumer.basic', 'object_type': 'Consumer', 'sections': [{'name': 'basic', 'title': '基本', 'fields': ['display_name']}],
            'relations': [], 'title_property': 'display_name', 'reason': '客户基本信息'}
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert act(client, csrf, 'publish_object_view', body).status_code == 403
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        done = act(client, csrf, 'publish_object_view', body)
        assert done.status_code == 200 and done.json()['version'] == 1, done.text
        refused = act(client, csrf, 'publish_object_view', {**body, 'object_type': 'Nothing'})
        assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'unknown_object_type'}
        assert act(client, csrf, 'publish_object_view', {**body, 'extra': 1}).status_code == 422
    row = admin.execute('select tenant_id,version,published_by,request_intent,reason from control.nexloop_object_view_definitions').fetchone()
    assert row[0] == COMMITMENT_TENANT and row[1] == 1 and row[2] and row[3] and row[4] == '客户基本信息'
