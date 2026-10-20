"""ADR-036 Object Set (capability gap D-G2): read-only, governed owner publication (versioned, audited), evaluated in SQL at read time
without a member snapshot, and never wider than the member's READ.

Real PostgreSQL. The governed path runs on the outreach slice fixture (owner and operator workbench logins, the governed entry);
the reads run on the world model W2 fixture (test_workbench_objects_pg: owner and operator members, a Consumer type with the
property group `preference`). Admin seeds synthetic objects, the owner restriction fact as a configuration stand-in, and publishes
definitions for the read tests through the same SQL handler the governed entry calls; nothing else is written by admin.
"""
import hashlib
import json
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from eios.ontology.definitions import ObjectSetDefinition
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT as COMMITMENT_TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_objects_pg import obj
from test_workbench_read_pg import (TENANT, browser_business, conversations, provision, published_action, workbench,  # noqa: F401
    workbench_login)

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def seed_type(admin):
    """Consumer v97: sport and level in the open group `preference`, income in the group `wealth` (restricted below), note ungrouped."""
    definition = {'type_name': 'Consumer', 'version': 97, 'properties': [
        {'property_name': 'sport', 'value_type': 'string'}, {'property_name': 'level', 'value_type': 'integer'},
        {'property_name': 'income', 'value_type': 'integer'}, {'property_name': 'note', 'value_type': 'string'}],
        'property_groups': [{'group_name': 'preference', 'property_names': ['sport', 'level']},
                            {'group_name': 'wealth', 'property_names': ['income']}]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',97,%s)", (TENANT, Jsonb(definition)))


def restrict(admin, groups):
    admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) '
                  'on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
                  (TENANT, 'property_group_restriction', ['Consumer'],
                   Jsonb({'tenant_id': TENANT, 'type_name': 'Consumer', 'restricted_groups': groups, 'decision': 'owner 2026-10-20 (synthetic)'})))


def publish(admin, body, tenant=TENANT):
    """The governed handler, as the governed entry calls it (owner principal, intent)."""
    return admin.execute("select control.nexloop_governed_publish_object_set(%s,'real','owner-principal','owner-subject',%s,%s)",
                         (tenant, 'intent-' + hexid(str(body))[:12], Jsonb({'operation': 'publish_object_set', 'request_id': 'r', **body}))).fetchone()[0]


TENNIS = {'stable_name': 'tennis.players', 'object_type': 'Consumer', 'filters': [{'property': 'sport', 'operator': 'eq', 'value': '网球'},
          {'property': 'level', 'operator': 'gte', 'value': 3}], 'sort': [{'property': 'level', 'direction': 'desc'}],
          'projection': ['sport', 'level', 'income'], 'reason': '网球中高级客户'}


def seed_objects(w):
    admin = w['admin']
    seed_type(admin)
    players = {hexid(f'os-player-{n}'): n for n in range(6)}  # levels 0..5; 3, 4, 5 match
    for oid, level in players.items():
        obj(admin, 'Consumer', oid, {'sport': '网球', 'level': level, 'income': 1000 * level}, version=97)
    obj(admin, 'Consumer', hexid('os-golf'), {'sport': '高尔夫', 'level': 5}, version=97)
    obj(admin, 'MessageAssignment', hexid('os-assignment'), {'sport': '网球'})  # another type is never a candidate
    matching = sorted((oid for oid, level in players.items() if level >= 3), key=lambda oid: -players[oid])
    return matching


def audit(admin):
    return admin.execute("select count(*) from runtime.nexloop_workbench_read_audit where read_purpose='object_set'").fetchone()[0]


def test_definitions_follow_the_vendored_contract_and_are_versioned_and_immutable(workbench):
    w = workbench
    admin = w['admin']
    seed_type(admin)
    assert publish(admin, TENNIS) == {'stable_name': 'tennis.players', 'version': 1, 'changed': True}
    assert publish(admin, TENNIS) == {'stable_name': 'tennis.players', 'version': 1, 'changed': False}  # same conditions: no new version
    assert publish(admin, {**TENNIS, 'projection': ['sport'], 'reason': '只看运动'})['version'] == 2
    rows = admin.execute("select version,definition,definition_digest,published_by,reason from control.nexloop_object_set_definitions order by version").fetchall()
    assert [r[0] for r in rows] == [1, 2] and [r[3] for r in rows] == ['owner-principal'] * 2 and rows[1][4] == '只看运动'
    for _, definition, digest, _, _ in rows:  # the stored definition is a valid vendored ObjectSetDefinition
        parsed = ObjectSetDefinition.model_validate_json(json.dumps(definition))
        assert parsed.stable_name == 'tennis.players' and parsed.snapshots_allowed is False and parsed.status.value == 'published'
        assert parsed.created_at <= datetime.now(UTC) and len(digest) == 64
    assert [f.operator.value for f in ObjectSetDefinition.model_validate_json(json.dumps(rows[0][1])).filters] == ['eq', 'gte']
    for statement in ("update control.nexloop_object_set_definitions set reason='x'", 'delete from control.nexloop_object_set_definitions'):
        with pytest.raises(psycopg.Error, match='immutable'), admin.transaction():
            admin.execute(statement)
    # Shapes the first batch does not open, unknown properties and types, and restricted / ungrouped filter properties are refused.
    for body, code in (({**TENNIS, 'filters': [{'property': 'sport', 'operator': 'like', 'value': '网'}]}, 'invalid_filter'),
                       ({**TENNIS, 'filters': [{'property': 'missing', 'operator': 'eq', 'value': 1}]}, 'invalid_filter'),
                       ({**TENNIS, 'filters': [{'property': 'level', 'operator': 'gt', 'value': 'x'}]}, 'invalid_filter'),
                       ({**TENNIS, 'object_type': 'Unknown'}, 'unknown_object_type'),
                       ({**TENNIS, 'parameters': []}, 'invalid'),
                       ({**TENNIS, 'filters': [{'property': 'note', 'operator': 'eq', 'value': 'x'}]}, 'restricted_property'),  # ungrouped
                       ({**TENNIS, 'sort': [{'property': 'income', 'direction': 'asc'}]}, None)):
        if code is None:  # income is open until the owner restricts its group
            assert publish(admin, {**body, 'stable_name': 'by.income'})['version'] == 1
            continue
        with pytest.raises(psycopg.Error, match=code), admin.transaction():
            publish(admin, body)
    restrict(admin, ['wealth'])
    with pytest.raises(psycopg.Error, match='restricted_property'), admin.transaction():
        publish(admin, {**TENNIS, 'stable_name': 'by.income2', 'filters': [{'property': 'income', 'operator': 'gt', 'value': 1}]})


def test_preview_is_evaluated_at_read_time_paged_counted_and_never_wider_than_read(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    matching = seed_objects(w)
    publish(admin, TENNIS)
    publish(admin, {**TENNIS, 'stable_name': 'by.income', 'filters': [], 'sort': [{'property': 'income', 'direction': 'asc'}]})
    restrict(admin, ['wealth'])  # after publication: income is restricted
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        listed = client.get('/api/v1/workbench/object-sets', headers={'Origin': ORIGIN})
        assert listed.status_code == 200, listed.text
        sets = {s['stable_name']: s for s in listed.json()['object_sets']}
        assert sets['tennis.players']['filters'] == [{'property': 'sport', 'operator': 'eq', 'value': '网球'}, {'property': 'level', 'operator': 'gte', 'value': 3}]
        assert sets['tennis.players']['projection'] == ['sport', 'level', 'income'] and sets['tennis.players']['version'] == 1
        before = audit(admin)
        first = client.get('/api/v1/workbench/object-sets/tennis.players/members?limit=2', headers={'Origin': ORIGIN}).json()
        assert first['status'] == 'ok' and first['total'] == 3 and first['next_offset'] == 2 and first['invisible'] == 0
        assert [i['object_id'] for i in first['items']] == matching[:2]  # level desc
        # Restricted property: listed as withheld, never shown (AT-003).
        assert first['items'][0]['values'] == {'sport': '网球', 'level': 5} and first['items'][0]['withheld'] == ['income']
        assert all('income' not in i['values'] for i in first['items']) and '5000' not in str(first)
        rest = client.get('/api/v1/workbench/object-sets/tennis.players/members?limit=2&offset=2', headers={'Origin': ORIGIN}).json()
        assert [i['object_id'] for i in rest['items']] == matching[2:] and rest['next_offset'] is None
        assert audit(admin) > before  # every object and property read is audited
        # A set sorted by a property restricted after publication is withheld as a whole: no order, no count leaks it.
        held = client.get('/api/v1/workbench/object-sets/by.income/members', headers={'Origin': ORIGIN}).json()
        assert (held['status'], held['items'], held['total']) == ('withheld', [], None)
        # Evaluated at read time, no snapshot: a new matching object appears at once.
        obj(admin, 'Consumer', hexid('os-new'), {'sport': '网球', 'level': 9}, version=97)
        now = client.get('/api/v1/workbench/object-sets/tennis.players/members', headers={'Origin': ORIGIN}).json()
        assert now['total'] == 4 and now['items'][0]['object_id'] == hexid('os-new')
        assert client.get('/api/v1/workbench/object-sets/tennis.players/members?offset=1001', headers={'Origin': ORIGIN}).status_code == 422
        assert client.get('/api/v1/workbench/object-sets/Bad Name/members', headers={'Origin': ORIGIN}).status_code in (404, 422)
        assert client.get('/api/v1/workbench/object-sets/missing.set/members', headers={'Origin': ORIGIN}).status_code == 404
    for human in (w['outsider'], w['customer']):
        with w['client']() as client:
            assert workbench_login(client, human).status_code == 200
            assert client.get('/api/v1/workbench/object-sets', headers={'Origin': ORIGIN}).status_code == 403
            assert client.get('/api/v1/workbench/object-sets/tennis.players/members', headers={'Origin': ORIGIN}).status_code == 403


def test_unreadable_objects_are_counted_not_listed(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    seed_type(admin)
    type_def = {'type_name': 'Ticket', 'version': 1, 'properties': [{'property_name': 'state', 'value_type': 'string'}],
                'property_groups': [{'group_name': 'status', 'property_names': ['state']}]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Ticket',1,%s)", (TENANT, Jsonb(type_def)))
    for n in range(3):
        obj(admin, 'Ticket', f'ticket-{n}', {'state': 'open'})
    publish(admin, {'stable_name': 'open.tickets', 'object_type': 'Ticket', 'filters': [{'property': 'state', 'operator': 'in', 'value': ['open', 'pending']}],
                    'sort': [], 'projection': ['state'], 'reason': '未结工单'})
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        page = client.get('/api/v1/workbench/object-sets/open.tickets/members', headers={'Origin': ORIGIN}).json()
        # No member derives READ on Ticket (ADR-025/ADR-035): three candidates, none listed, no id in the answer.
        assert (page['total'], page['items'], page['invisible']) == (3, [], 3) and 'ticket-' not in str(page)


@PLAN
def test_only_the_owner_publishes_through_the_governed_action(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.object_set.publish',))
    body = {'stable_name': 'all.consumers', 'object_type': 'Consumer', 'filters': [], 'sort': [], 'projection': ['display_name'], 'reason': '全部客户'}
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert act(client, csrf, 'publish_object_set', body).status_code == 403
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        done = act(client, csrf, 'publish_object_set', body)
        assert done.status_code == 200 and done.json()['version'] == 1, done.text
        refused = act(client, csrf, 'publish_object_set', {**body, 'object_type': 'Nothing'})
        assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'unknown_object_type'}
        assert act(client, csrf, 'publish_object_set', {**body, 'extra': 1}).status_code == 422
    row = admin.execute("select tenant_id,version,published_by,request_intent,reason from control.nexloop_object_set_definitions").fetchone()
    assert row[0] == COMMITMENT_TENANT and row[1] == 1 and row[2] and row[3] and row[4] == '全部客户'
