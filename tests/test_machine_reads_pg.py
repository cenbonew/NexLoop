"""NX-062 M4a (docs/implementation/NX-062-M4-projections.md; ADR-041 amendment A1; L6 M4-1 to M4-5) on real PostgreSQL and the real HTTP
application: machine client grants (owner governed Actions) and the read verbs object.get and object.list.

Grants are append-only, written only by the owner's governed Actions, refused for an ungranted or retired capability and for types
the verb does not project. A read checks identity, scope, allowlist, the latest grant and its types, and the key's world; a revoked
grant refuses the next read on another connection at once. The answer is exactly the projection: metadata and the registered
properties of Commitment and CommercialRecord - never a free text, a message reference or an external order id (markers that look
nothing like hex). An object of another tenant or world, outside the grant's types, absent, or whose Consumer is being erased: the
same 404. Machine and workbench reads agree on every returned property. Keys are generated at run time."""
import dataclasses
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from nexloop_eios import business_object_types
from nexloop_eios.http_api import create_app
from support.tenants import TENANT_A as TENANT
from support.tenants import TENANT_OTHER
from test_machine_lifecycle_pg import desk, owner  # noqa: F401
from test_machine_realm_pg import bearer, publish, valid
from test_workbench_actions_http import act
from test_workbench_read_pg import ORIGIN, browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
MARKERS = {'condition': '机器读取不应出现-条件文本', 'content_ref': '机器读取不应出现-消息引用', 'external_id': '机器读取不应出现-外部单号',
           'made_by': '机器读取不应出现-执行主体', 'consumer_ref': '机器读取不应出现-消费者引用'}
COMMITMENT_FIELDS = {'due_at', 'due_precision', 'fulfillment_basis', 'late', 'made_to', 'promised_at', 'related_goal_ref', 'status', 'supersedes'}
RECORD_FIELDS = {'amount_minor', 'connector_id', 'currency', 'data_mode', 'event_count', 'kind', 'late_event_count', 'link_state', 'occurred_at',
                 'refunded_minor', 'status', 'status_at', 'verification_state'}


def hexid():
    return secrets.token_hex(32)


def publish_types(admin, tenant=TENANT):
    for row in business_object_types.trusted_object_types(ROOT / 'deploy/ontology/business-object-types.v1.json'):
        if row['type_name'] in ('Commitment', 'CommercialRecord'):
            admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s) on conflict do nothing',
                          (tenant, row['type_name'], row['version'], Jsonb(row)))


def put(admin, type_name, properties, *, tenant=TENANT, world='real', object_id=None):
    object_id = object_id or hexid()
    now = datetime.now(UTC)
    admin.execute("set session_replication_role=replica")  # synthetic objects written directly (no Action), as the read fixtures do
    try:
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,%s,%s,%s,1,%s,'synthetic','m4a',%s,%s)""", (tenant, world, type_name, object_id, Jsonb(properties), now, now))
    finally:
        admin.execute("set session_replication_role=origin")
    return object_id


def commitment(consumer):
    return {'made_to': consumer, 'made_by': {'kind': MARKERS['made_by']}, 'content_ref': {'claim': MARKERS['content_ref']}, 'promised_at': '2026-11-01T00:00:00Z',
            'due_at': '2026-11-20T00:00:00Z', 'due_precision': 'day', 'condition': MARKERS['condition'], 'status': 'open', 'late': False,
            'fulfillment_basis': 'delivery_confirmed'}


def record():
    return {'kind': 'order', 'external_id': MARKERS['external_id'], 'connector_id': 'synthetic-shop', 'data_mode': 'real', 'currency': 'CNY',
            'occurred_at': '2026-11-02T00:00:00Z', 'status': 'paid', 'amount_minor': 1200, 'refunded_minor': 0, 'consumer_ref': MARKERS['consumer_ref'],
            'link_state': 'linked', 'event_count': 1, 'late_event_count': 0}


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
    publish_types(admin, TENANT_OTHER)
    with w['client']() as client:
        csrf = owner(client, w)
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 200
        issued = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'real', 'scope': ['object.get', 'object.list'],
                                                         'expires_in_days': 30}).json()
    return {**w, 'key': issued['key']}


def grant(w, capability, types, *, revoke=False):
    with w['client']() as client:
        csrf = owner(client, w)
        body = {'client_id': 'acme-agent', 'capability': capability, **({'reason': '收回'} if revoke else {'object_types': types})}
        return act(client, csrf, 'revoke_machine_grant' if revoke else 'grant_machine_capability', body)


def read(client, w, verb, **params):
    return valid(client.post('/machine/v1/read', headers=bearer(w['key']), json={'verb': verb, 'params': params}),
                 {'object.get': 'machine-read-object', 'object.list': 'machine-read-object-list'}.get(verb, 'machine-error'))


def test_grants_are_owner_governed_append_only_and_bounded(reads):
    w = reads
    admin = w['admin']
    # A current allowlisted capability that is not a read verb (a write capability) cannot be granted here.
    admin.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values('goals.agent.propose',1,'active','write','w',%s,'test')",
                  ('migration:0440_nx062_machine_reads.sql',))
    for capability, types, reason in (('goals.agent.propose', ['Commitment'], 'capability_not_allowed'), ('object.get', ['Consumer'], 'type_not_allowed'), ('object.get', ['Commitment', 'Message'], 'type_not_allowed'),
                                      ('synthetic.unregistered', ['Commitment'], 'capability_not_allowed'),
                                      ('approval.decide', ['Commitment'], 'capability_not_allowed')):
        r = grant(w, capability, types)
        assert r.status_code == 409 and r.json()['details'] == {'reason': reason}, (capability, types, r.text)
    with w['client']() as client:
        csrf = owner(client, w)
        for bad in ({'client_id': 'acme-agent', 'capability': 'object.get', 'object_types': []},
                    {'client_id': 'acme-agent', 'capability': 'object.get', 'object_types': ['*']},
                    {'client_id': 'acme-agent', 'capability': 'object.get', 'object_types': ['Commitment'], 'tenant_id': 'x'}):
            assert act(client, csrf, 'grant_machine_capability', bad).status_code == 422, bad
        csrf_op = __import__('test_workbench_read_pg').workbench_login(client, w['operator']).json()['csrf_token']
        assert act(client, csrf_op, 'grant_machine_capability', {'client_id': 'acme-agent', 'capability': 'object.get', 'object_types': ['Commitment']}).status_code == 403
    first = grant(w, 'object.get', ['Commitment'])
    assert first.status_code == 200 and first.json()['revision'] == 1 and first.json()['changed'] is True
    assert grant(w, 'object.get', ['Commitment']).json()['changed'] is False
    assert grant(w, 'object.get', ['CommercialRecord', 'Commitment']).json()['revision'] == 2
    assert grant(w, 'object.get', [], revoke=True).json()['state'] == 'revoked'
    rows = admin.execute("select revision,state,object_types,issued_by_principal from authz.nexloop_machine_client_grants order by revision").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, 'active', ['Commitment']), (2, 'active', ['CommercialRecord', 'Commitment']), (3, 'revoked', [])]
    assert {r[3] for r in rows} == {w['owner']['principal_id']}
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute("update authz.nexloop_machine_client_grants set state='active'")
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute("delete from authz.nexloop_machine_client_grants")
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_runtime', 'nexloop_identity', 'nexloop_configurator'):
        for table in ('authz.nexloop_machine_client_grants', 'runtime.nexloop_machine_read_counts'):
            for privilege in ('select', 'insert', 'update', 'delete'):
                assert not admin.execute('select has_table_privilege(%s,%s,%s)', (role, table, privilege)).fetchone()[0], (role, table)
    # The grant Actions never enter the machine allowlist (their names match the never-grant markers: expected).
    assert admin.execute("select control.nexloop_machine_capability_forbidden('machine_client.grant') and control.nexloop_machine_capability_forbidden('machine_client.revoke_grant')").fetchone()[0]


def test_reads_return_exactly_the_projection_and_never_a_marker(reads):
    w = reads
    admin = w['admin']
    consumer = hexid()
    c1, c2 = put(admin, 'Commitment', commitment(consumer)), put(admin, 'Commitment', commitment(consumer))
    rec = put(admin, 'CommercialRecord', record())
    grant(w, 'object.get', ['Commitment', 'CommercialRecord'])
    grant(w, 'object.list', ['Commitment'])
    with w['client']() as client:
        got = read(client, w, 'object.get', type_name='Commitment', object_id=c1)
        assert got.status_code == 200, got.text
        body = got.json()
        assert set(body) == {'type_name', 'object_id', 'schema_version', 'revision', 'updated_at', 'properties'}
        assert set(body['properties']) == COMMITMENT_FIELDS & set(commitment(consumer)) and body['properties']['made_to'] == {'value': consumer, 'value_type': 'string'}
        other = read(client, w, 'object.get', type_name='CommercialRecord', object_id=rec)
        assert set(other.json()['properties']) == RECORD_FIELDS & set(record())
        listed = read(client, w, 'object.list', type_name='Commitment')
        assert listed.status_code == 200 and set(listed.json()) == {'type_name', 'items', 'next_after'}  # no total, no hidden count (L6 N2)
        assert {i['object_id'] for i in listed.json()['items']} == {c1, c2} and listed.json()['next_after'] is None
        assert all(set(i['properties']) <= COMMITMENT_FIELDS for i in listed.json()['items'])
        text = got.text + other.text + listed.text
        assert not [m for m in MARKERS.values() if m in text]
        # object.list is granted for Commitment only: CommercialRecord is not found, as an absent type would be.
        assert read(client, w, 'object.list', type_name='CommercialRecord').status_code == 404
    counts = dict(((v, t), n) for v, t, n in admin.execute('select verb,object_type,calls::int from runtime.nexloop_machine_read_counts').fetchall())
    assert counts == {('object.get', 'Commitment'): 1, ('object.get', 'CommercialRecord'): 1, ('object.list', 'Commitment'): 1}
    assert admin.execute('select count(*) from runtime.nexloop_machine_read_counts where key_prefix=%s', (w['key'][4:16],)).fetchone()[0] == 3


def test_one_not_found_for_absent_foreign_untyped_and_erasing(reads):
    w = reads
    admin = w['admin']
    consumer = hexid()
    mine = put(admin, 'Commitment', commitment(consumer))
    foreign = put(admin, 'Commitment', commitment(hexid()), tenant=TENANT_OTHER)
    test_world = put(admin, 'Commitment', commitment(hexid()), world='test')
    erasing_consumer = hexid()
    erased = put(admin, 'Commitment', commitment(erasing_consumer))
    record_id = put(admin, 'CommercialRecord', record())
    grant(w, 'object.get', ['Commitment'])
    admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','m4a-erasure',%s,'owner','synthetic','synthetic','blocking',1)""", (TENANT, erasing_consumer))
    with w['client']() as client:
        assert read(client, w, 'object.get', type_name='Commitment', object_id=mine).status_code == 200
        answers = [read(client, w, 'object.get', type_name=t, object_id=i) for t, i in
                   (('Commitment', hexid()), ('Commitment', foreign), ('Commitment', test_world), ('Commitment', erased),
                    ('CommercialRecord', record_id), ('Consumer', consumer))]
        assert [a.status_code for a in answers] == [404] * 6
        assert len({a.json()['code'] for a in answers}) == 1 and all(a.json()['details'] == {} for a in answers)
        # A value naming an erasing Consumer is never returned even on a visible object.
        mixed = put(admin, 'Commitment', {**commitment(consumer), 'supersedes': erasing_consumer})
        assert 'supersedes' not in read(client, w, 'object.get', type_name='Commitment', object_id=mixed).json()['properties']


def test_scope_allowlist_grant_and_revocation_on_two_connections(reads, pg):
    w = reads
    admin = w['admin']
    c1 = put(admin, 'Commitment', commitment(hexid()))
    with w['client']() as client:
        # In the key's scope, no grant: refused.
        assert read(client, w, 'object.get', type_name='Commitment', object_id=c1).status_code == 403
        assert client.get('/machine/v1/capabilities', headers=bearer(w['key'])).json() == {'capabilities': []}
        grant(w, 'object.get', ['Commitment'])
        assert read(client, w, 'object.get', type_name='Commitment', object_id=c1).status_code == 200
        assert [c['capability'] for c in client.get('/machine/v1/capabilities', headers=bearer(w['key'])).json()['capabilities']] == ['object.get']
        # Granted but outside this key's scope: a second key of the client with object.list only is refused object.get.
        csrf = owner(client, w)
        keys = act(client, csrf, 'rotate_machine_key', {'previous_key_id': admin.execute("select key_id::text from authz.nexloop_machine_keys where status='active'").fetchone()[0],
                                                         'overlap_days': 1, 'scope': ['object.list'], 'expires_in_days': 10}).json()
        narrow = {**w, 'key': keys['key']}
        assert read(client, narrow, 'object.get', type_name='Commitment', object_id=c1).status_code == 403
        # Revoked through the API's connection; read on a second, separate connection (as nexloop_api, the SQL entry itself): the
        # very next read after the revocation's commit is refused - nothing is cached anywhere (L6 M4-5 (i)).
        from nexloop_eios import machine_keys as K
        key = K.parse(w['key'])
        digest = K.digest(key, w['peppers'], 1).hex()
        with psycopg.connect(pg, autocommit=True) as second:
            second.execute('set role nexloop_api')
            sql = "select authz.nexloop_machine_read(%s,%s,'object.get',%s::jsonb)"
            params = json.dumps({'type_name': 'Commitment', 'object_id': c1})
            assert second.execute(sql, (key.prefix_id, digest, params)).fetchone()[0]['object_id'] == c1
            grant(w, 'object.get', [], revoke=True)
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match='not_permitted'):
                second.execute(sql, (key.prefix_id, digest, params))
        assert read(client, w, 'object.get', type_name='Commitment', object_id=c1).status_code == 403
        grant(w, 'object.get', ['Commitment'])
        # Retired in the allowlist while granted: refused, and dropped from the list.
        with psycopg.connect(pg, autocommit=True) as other:
            other.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values('object.get',2,'retired','read','retired',%s,'test')",
                          ('migration:0440_nx062_machine_reads.sql',))
        assert read(client, w, 'object.get', type_name='Commitment', object_id=c1).status_code == 403
        assert client.get('/machine/v1/capabilities', headers=bearer(w['key'])).json() == {'capabilities': []}
        # A verb outside the scope, a body naming an identity, a query string, an unknown verb.
        assert client.post('/machine/v1/read', headers=bearer(w['key']), json={'verb': 'object.get', 'params': {'type_name': 'Commitment', 'object_id': c1, 'tenant_id': TENANT}}).status_code == 422
        assert client.post('/machine/v1/read?x=1', headers=bearer(w['key']), json={'verb': 'object.get', 'params': {}}).status_code == 400
        assert read(client, w, 'commitment.list').status_code == 403


def test_machine_and_workbench_reads_agree(reads):
    """Every property the machine returns equals the workbench's own value of it (same object, same owner-visible read)."""
    from test_workbench_read_pg import workbench_login
    w = reads
    admin = w['admin']
    c1 = put(admin, 'Commitment', commitment(hexid()))
    grant(w, 'object.get', ['Commitment'])
    with w['client']() as client:
        machine = read(client, w, 'object.get', type_name='Commitment', object_id=c1).json()
        workbench_login(client, w['owner'])
        page = client.get(f'/api/v1/workbench/objects/Commitment/{c1}')
        assert page.status_code == 200, page.text
    shown = {p['name']: p for p in page.json()['properties'] if p['status'] == 'ok'}
    assert set(machine['properties']) <= set(shown)
    for name, entry in machine['properties'].items():
        value = entry['value']
        assert shown[name]['value'] == (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)), name
    assert machine['revision'] == page.json()['revision'] and machine['schema_version'] == page.json()['schema_version']


def test_hold_expiry_and_erasure_match_the_workbench(reads):
    """L6 N1. NX-029/NX-054 semantics on ontology.objects, followed as they are (no second judgment):
    * a legal hold (control.nexloop_retention_holds, 0132) only keeps data from deletion - the workbench shows a held object, so does the
      machine read, with the same values;
    * retention expiry deletes the expired object (the keeper's CommercialRecord delete, 0129 nexloop_retention_purge) - the workbench
      finds nothing and the machine read answers 404, as for an absent object;
    * a Consumer being erased hides its objects - the workbench refuses the read and the machine read answers the same 404.
    object.list leaves the hidden ones out and reports no count of them (L6 N2)."""
    from test_workbench_read_pg import workbench_login
    w = reads
    admin = w['admin']
    held_consumer, erased_consumer = hexid(), hexid()
    held = put(admin, 'Commitment', commitment(held_consumer))
    expired = put(admin, 'CommercialRecord', record())
    erased = put(admin, 'Commitment', commitment(erased_consumer))
    admin.execute("""insert into control.nexloop_retention_holds(tenant_id,world,hold_id,scope_kind,scope_ref,basis,held_by)
        values(%s,'real','m4a-hold','consumer',%s,'synthetic legal hold','synthetic-owner')""", (TENANT, held_consumer))
    admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','m4a-erased',%s,'owner','synthetic','synthetic','completed_with_holds',1)""", (TENANT, erased_consumer))
    grant(w, 'object.get', ['Commitment', 'CommercialRecord'])
    grant(w, 'object.list', ['Commitment'])
    # Expiry: what the retention keeper does to an expired CommercialRecord (0129: the row is deleted, a tombstone without content kept).
    admin.execute("set session_replication_role=replica")
    try:
        admin.execute("delete from ontology.objects where tenant_id=%s and world='real' and type_name='CommercialRecord' and object_id=%s", (TENANT, expired))
    finally:
        admin.execute("set session_replication_role=origin")
    with w['client']() as client:
        machine = {name: read(client, w, 'object.get', type_name=t, object_id=i) for name, t, i in
                   (('held', 'Commitment', held), ('expired', 'CommercialRecord', expired), ('erased', 'Commitment', erased))}
        listed = read(client, w, 'object.list', type_name='Commitment').json()
        workbench_login(client, w['owner'])
        bench = {name: client.get(f'/api/v1/workbench/objects/{t}/{i}') for name, t, i in
                 (('held', 'Commitment', held), ('expired', 'CommercialRecord', expired), ('erased', 'Commitment', erased))}
    assert machine['held'].status_code == bench['held'].status_code == 200
    shown = {p['name']: p['value'] for p in bench['held'].json()['properties'] if p['status'] == 'ok'}
    assert {k: v['value'] for k, v in machine['held'].json()['properties'].items()} == {k: shown[k] if not isinstance(v['value'], bool) else json.loads(shown[k])
                                                                                       for k, v in machine['held'].json()['properties'].items()}
    assert machine['expired'].status_code == 404 and bench['expired'].status_code == 404
    assert machine['erased'].status_code == 404 and bench['erased'].status_code in (403, 404)
    assert machine['expired'].json()['code'] == machine['erased'].json()['code'] == 'not_found'
    assert [i['object_id'] for i in listed['items']] == [held] and set(listed) == {'type_name', 'items', 'next_after'}
