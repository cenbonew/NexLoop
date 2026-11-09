"""NX-062 M4b (docs/implementation/NX-062-M4-projections.md; ADR-041 amendment A1; L6 M4-2, M4-5, N1, N2) on real PostgreSQL and the
real HTTP application: the read verbs object.neighbors and commitment.list.

object.neighbors: depth 1 through governed relations and the registered reference properties; only visible neighbours of a type
granted for the verb, type and id only; nothing counted of the rest; a hidden centre is the same 404. commitment.list: the visible
Commitments with the registered properties only (no promised words, names, events, evidence or exception text), open / ended / all,
no total. Both refuse a verb outside the scope or without a grant, follow a revocation at once, and leave object.get / object.list as
they were. Keys are generated at run time."""
import dataclasses
import json
import secrets
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from commercial_fixture import publish_registry_actions
from nexloop_eios.http_api import create_app
from support.tenants import TENANT_A as TENANT
from test_machine_lifecycle_pg import desk, owner  # noqa: F401
from test_machine_reads_pg import COMMITMENT_FIELDS, MARKERS, commitment, grant, hexid, publish_types, put, record
from test_machine_realm_pg import bearer, publish, valid
from test_workbench_actions_http import act
from test_workbench_read_pg import ORIGIN, browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

SCHEMAS = {'object.get': 'machine-read-object', 'object.list': 'machine-read-object-list', 'object.neighbors': 'machine-read-object-neighbors',
           'commitment.list': 'machine-read-commitment-list'}


@pytest.fixture
def reads(desk):
    w = desk
    admin = w['admin']
    base = w['config']
    config = dataclasses.replace(base, machine_key_peppers_file=w['peppers_file'],
                                 workbench=dataclasses.replace(base.workbench, machine_key_peppers_file=w['peppers_file']))
    w = {**w, 'config': config, 'client': lambda: TestClient(create_app(config), base_url=ORIGIN)}
    publish_registry_actions(admin, ('nexloop.machine_client.grant', 'nexloop.machine_client.revoke_grant'), tenant=TENANT)
    publish(w)
    publish_types(admin)
    with w['client']() as client:
        csrf = owner(client, w)
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 200
        issued = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'real', 'expires_in_days': 30,
                                                         'scope': ['commitment.list', 'object.get', 'object.list', 'object.neighbors']}).json()
    return {**w, 'key': issued['key']}


def read(client, w, verb, **params):
    return valid(client.post('/machine/v1/read', headers=bearer(w['key']), json={'verb': verb, 'params': params}), SCHEMAS.get(verb, 'machine-error'))


def relate(admin, source, target, name='commitment_record', world='real'):
    admin.execute("set session_replication_role=replica")
    try:
        admin.execute("""insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,created_at)
            values(%s,%s,%s,%s,1,%s,%s,%s,%s,%s)""", (secrets.token_hex(16), TENANT, world, name, source[0], source[1], target[0], target[1], datetime.now(UTC)))
    finally:
        admin.execute("set session_replication_role=origin")


def test_neighbors_show_only_visible_granted_types_and_count_nothing_hidden(reads):
    w = reads
    admin = w['admin']
    consumer, erasing = hexid(), hexid()
    centre = put(admin, 'Commitment', commitment(consumer))
    older = put(admin, 'Commitment', commitment(consumer))
    admin.execute("set session_replication_role=replica")
    admin.execute("update ontology.objects set properties=properties||jsonb_build_object('supersedes',%s::text) where object_id=%s", (older, centre))
    admin.execute("set session_replication_role=origin")
    hidden = put(admin, 'Commitment', {**commitment(erasing), 'supersedes': centre})       # incoming property edge, hidden by erasure
    rec = put(admin, 'CommercialRecord', record())
    other_rec = put(admin, 'CommercialRecord', record())
    relate(admin, ('Commitment', centre), ('CommercialRecord', rec))
    relate(admin, ('Commitment', centre), ('CommercialRecord', other_rec))
    relate(admin, ('Commitment', centre), ('Consumer', consumer), 'made_to')               # a Consumer neighbour: never a machine type
    relate(admin, ('Commitment', centre), ('CommercialRecord', rec), 'test_world_link', world='test')  # another world's relation: never shown
    admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','m4b-erasure',%s,'owner','synthetic','synthetic','blocking',1)""", (TENANT, erasing))
    grant(w, 'object.neighbors', ['Commitment'])
    with w['client']() as client:
        only_commitments = read(client, w, 'object.neighbors', type_name='Commitment', object_id=centre)
        assert only_commitments.status_code == 200, only_commitments.text
        body = only_commitments.json()
        assert set(body) == {'type_name', 'object_id', 'depth', 'groups'} and body['depth'] == 1
        assert [(g['direction'], g['via'], g['type_name'], [i['object_id'] for i in g['items']], g['more']) for g in body['groups']] == [
            ('outgoing', 'property:supersedes', 'Commitment', [older], False)]
        assert hidden not in only_commitments.text and consumer not in only_commitments.text and 'invisible' not in only_commitments.text
        grant(w, 'object.neighbors', ['Commitment', 'CommercialRecord'])
        both = read(client, w, 'object.neighbors', type_name='Commitment', object_id=centre).json()
        groups = {(g['direction'], g['via'], g['type_name']): sorted(i['object_id'] for i in g['items']) for g in both['groups']}
        assert groups == {('outgoing', 'property:supersedes', 'Commitment'): [older],
                          ('outgoing', 'commitment_record', 'CommercialRecord'): sorted([rec, other_rec])}
        assert not [m for m in MARKERS.values() if m in json.dumps(both, ensure_ascii=False)]
        # A hidden centre, an absent one, an ungranted type: the same 404.
        assert [read(client, w, 'object.neighbors', type_name=t, object_id=i).status_code for t, i in
                (('Commitment', hidden), ('Commitment', hexid()), ('Consumer', consumer))] == [404, 404, 404]
        # Granted but outside a key's scope: a rotated key holding object.get only is refused.
        csrf = owner(client, w)
        narrow = act(client, csrf, 'rotate_machine_key', {'previous_key_id': admin.execute("select key_id::text from authz.nexloop_machine_keys where status='active'").fetchone()[0],
                                                           'overlap_days': 1, 'scope': ['object.get'], 'expires_in_days': 10}).json()['key']
        assert read(client, {**w, 'key': narrow}, 'object.neighbors', type_name='Commitment', object_id=centre).status_code == 403
        # Retired in the allowlist while granted: refused.
        admin.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values('object.neighbors',2,'retired','read','retired',%s,'test')",
                      ('migration:0441_nx062_machine_reads_b.sql',))
        assert read(client, w, 'object.neighbors', type_name='Commitment', object_id=centre).status_code == 403
        admin.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values('object.neighbors',3,'active','read','again',%s,'test')",
                      ('migration:0441_nx062_machine_reads_b.sql',))
        assert read(client, w, 'object.neighbors', type_name='Commitment', object_id=centre).status_code == 200
        grant(w, 'object.neighbors', [], revoke=True)
        assert read(client, w, 'object.neighbors', type_name='Commitment', object_id=centre).status_code == 403


def test_commitment_list_projection_groups_and_no_total(reads, pg):
    w = reads
    admin = w['admin']
    consumer, erasing = hexid(), hexid()
    open_one = put(admin, 'Commitment', commitment(consumer))
    ended = put(admin, 'Commitment', {**commitment(consumer), 'status': 'fulfilled'})
    hidden = put(admin, 'Commitment', commitment(erasing))
    admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','m4b-list',%s,'owner','synthetic','synthetic','completed',1)""", (TENANT, erasing))
    with w['client']() as client:
        assert read(client, w, 'commitment.list').status_code == 403  # in scope, not granted
        grant(w, 'commitment.list', ['Commitment'])
        opened = read(client, w, 'commitment.list')
        assert opened.status_code == 200 and set(opened.json()) == {'commitments', 'next_after'}
        assert [c['commitment_id'] for c in opened.json()['commitments']] == [open_one] and opened.json()['next_after'] is None
        item = opened.json()['commitments'][0]
        assert set(item) == {'commitment_id', 'revision', 'registered_at', 'properties'} and set(item['properties']) <= COMMITMENT_FIELDS
        assert [c['commitment_id'] for c in read(client, w, 'commitment.list', status_group='ended').json()['commitments']] == [ended]
        everything = read(client, w, 'commitment.list', status_group='all')
        assert sorted(c['commitment_id'] for c in everything.json()['commitments']) == sorted([open_one, ended])
        assert hidden not in everything.text and not [m for m in MARKERS.values() if m in everything.text]
        for bad in ({'status_group': 'closed'}, {'after': 'x'}, {'consumer_id': consumer}, {'tenant_id': TENANT}):
            assert read(client, w, 'commitment.list', **bad).status_code == 422, bad
        # Revoked by the owner (API connection), read on a second separate connection: refused at once.
        from nexloop_eios import machine_keys as K
        key = K.parse(w['key'])
        with psycopg.connect(pg, autocommit=True) as second:
            second.execute('set role nexloop_api')
            sql = "select authz.nexloop_machine_read(%s,%s,'commitment.list','{}'::jsonb)"
            assert second.execute(sql, (key.prefix_id, K.digest(key, w['peppers'], 1).hex())).fetchone()[0]['commitments']
            grant(w, 'commitment.list', [], revoke=True)
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match='not_permitted'):
                second.execute(sql, (key.prefix_id, K.digest(key, w['peppers'], 1).hex()))
    counts = dict(((v, t), n) for v, t, n in admin.execute('select verb,object_type,calls::int from runtime.nexloop_machine_read_counts').fetchall())
    assert counts[('commitment.list', 'Commitment')] == 4


def test_the_earlier_verbs_are_unchanged_and_the_wrapper_keeps_its_grantees(reads):
    w = reads
    admin = w['admin']
    c1 = put(admin, 'Commitment', commitment(hexid()))
    grant(w, 'object.get', ['Commitment'])
    with w['client']() as client:
        assert read(client, w, 'object.get', type_name='Commitment', object_id=c1).status_code == 200
        assert read(client, w, 'object.list', type_name='Commitment').status_code == 403  # in scope, not granted
        listed = client.get('/machine/v1/capabilities', headers=bearer(w['key'])).json()['capabilities']
        assert [c['capability'] for c in listed] == ['object.get']
    holders = lambda f: {r for (r,) in admin.execute("""select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
        where p.oid=%s::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner""", (f,)).fetchall()}
    assert holders('authz.nexloop_machine_read_before_reads_b(text,text,text,jsonb)') == set()
    assert holders('authz.nexloop_machine_read(text,text,text,jsonb)') == {'nexloop_api'}
    for f in ('authz.nexloop_machine_read_gate(text,text,text,text)', 'authz.nexloop_machine_neighbors(text,text,text,text,text[])'):
        assert holders(f) == set()
