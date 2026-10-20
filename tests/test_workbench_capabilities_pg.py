"""Enterprise world model W3: the workbench capability catalog (GET /api/v1/workbench/capabilities, verb `capabilities`).

Real PostgreSQL, real workbench login cookies, synthetic data, on the slice 1 fixture (test_workbench_read_pg). The catalog is
read-only metadata: the governed capability registry, the tenant's business Actions with their effect categories, the inbound
channel namespaces, the effect Actions active catalog offerings deliver through, and the real dispatch switches, the latter from
the same SQL source as the metrics read (0153). Another tenant's Actions, offerings and switches never appear; a workbench
non-member and the customer get 403; nothing is written. Admin only seeds (switch rows as the governed handler leaves them,
effect category declarations as trusted configuration writes them) and probes.
"""
import json
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

from support.tenants import TENANT_B
from system_catalog_fixture import publish_system_catalog
from test_real_dispatch_switch_pg import owner_sql, set_switch
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench, workbench_login)

SEND = 'eios:action:nexloop.service.request:1'
ROOT = Path(__file__).resolve().parents[1]


def seed(admin):
    """One tenant Action with a declared effect category, one active offering delivering through it, open switches; and the same in
    another tenant (which must stay invisible)."""
    action = admin.execute("select resource_id,definition,capability from control.nexloop_action_definitions where tenant_id=%s and world='real' order by resource_id limit 1",
                           (TENANT,)).fetchone()
    assert action, 'the fixture publishes tenant Actions'
    resource, definition, _ = action
    admin.execute("""insert into control.nexloop_action_effect_categories(tenant_id,world,resource_id,definition_digest,category,notification_parameters,manifest_version)
        values(%s,'real',%s,%s,'non_contact_service','{}',3)""", (TENANT, resource, 'a' * 64))
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_B,))
    other = 'eios:action:Other.tenant_only:1'
    # The other tenant's Action: the catalog's definition of the same Action, renamed so it is told apart (system_catalog_fixture).
    publish_system_catalog(admin, TENANT_B, (), (definition['stable_name'],), variants={definition['stable_name']: lambda d: d.update(stable_name='Other.tenant_only')})
    assert admin.execute("select 1 from control.nexloop_action_definitions where tenant_id=%s and resource_id=%s", (TENANT_B, other)).fetchone()
    for tenant, delivery in ((TENANT, 'nexloop.service.request:1'), (TENANT_B, 'nexloop.other_only:1')):
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','ServiceOffering',%s,1,%s,'synthetic',%s,clock_timestamp(),clock_timestamp())""",
                      (tenant, uuid.uuid4().hex * 2, Jsonb({'active': True, 'delivery_action': delivery, 'title': 'synthetic', 'valid_until': '2099-01-01T00:00:00Z'}), 'capabilities-' + tenant))
    set_switch(admin, TENANT, 'tenant', '*', True, 'test')
    set_switch(admin, TENANT, 'action_type', SEND, True)
    set_switch(admin, TENANT_B, 'tenant', '*', True, 'real')
    return resource, other


def counts(admin):
    return admin.execute("""select (select count(*) from control.nexloop_action_definitions),(select count(*) from control.nexloop_real_dispatch_switches),
        (select count(*) from control.nexloop_action_effect_categories),(select count(*) from runtime.nexloop_workbench_read_audit),
        (select count(*) from runtime.audit_events),(select authority_revision from control.nexloop_tenants where tenant_id=%s)""", (TENANT,)).fetchone()


def test_owner_and_operator_read_the_catalog_of_their_own_tenant_only(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    resource, other = seed(admin)
    before = counts(admin)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        got = client.get('/api/v1/workbench/capabilities')
        assert got.status_code == 200 and got.headers['cache-control'] == 'no-store'
        c = got.json()
        import jsonschema
        jsonschema.validate(c, json.loads((ROOT / 'packages/contracts/workbench-capabilities.schema.json').read_text()))
        # The governed capability registry (0124): every registered capability, including the owner's real dispatch switch.
        registry = admin.execute('select capability,operation,subject_rule from control.nexloop_governed_capabilities order by capability').fetchall()
        assert [(x['capability'], x['operation'], x['subject_rule']) for x in c['capabilities']] == [tuple(r) for r in registry]
        # The tenant's business Actions with their effect categories; undeclared ones say so (closed by default).
        actions = {a['resource_id']: a for a in c['actions']}
        tenant_actions = {r[0] for r in admin.execute("select resource_id from control.nexloop_action_definitions where tenant_id=%s and world='real'", (TENANT,))}
        assert set(actions) == tenant_actions and other not in actions
        assert actions[resource]['effect_category'] == 'non_contact_service' and actions[resource]['effect_category_declared'] is True
        assert all(a['effect_category'] is None and a['effect_category_declared'] is False for r, a in actions.items() if r != resource)
        # Channels and delivery routes: this tenant's offerings only; provider endpoints are deployment configuration.
        assert {x['namespace'] for x in c['channels']} == {r[0] for r in admin.execute('select namespace from control.nexloop_provider_namespaces')}
        assert c['routes'] == [{'delivery_action': 'nexloop.service.request:1', 'active_offerings': 1}] and c['provider_routes'] == 'deployment_configuration'
        # The switches are exactly what the metrics read shows (same SQL source), and never the other tenant's.
        assert c['real_dispatch'] == owner_sql(admin, 'select runtime.nexloop_metrics_snapshot(%s,%s)', (TENANT, 'real'))['real_dispatch']
        assert c['real_dispatch']['tenant']['enabled'] is True and c['real_dispatch']['tenant']['data_mode'] == 'test'
        assert [a['action_type'] for a in c['real_dispatch']['actions']] == [SEND]
        # Metadata only: no Consumer, Message or conversation identifiers.
        assert w['f']['consumer'] not in got.text and w['conversation']['id'] not in got.text and w['refusal']['id'] not in got.text
        assert client.get('/api/v1/workbench/capabilities?tenant_id=' + TENANT_B).status_code == 422
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        assert client.get('/api/v1/workbench/capabilities').json()['real_dispatch'] == c['real_dispatch']
    # Read-only: no definition, switch, category, audit or authority change.
    assert counts(admin) == before


def test_non_members_and_the_customer_are_refused(workbench):
    w = workbench
    provision(w)
    with w['client']() as client:
        assert client.get('/api/v1/workbench/capabilities').status_code == 401
    for human in (w['outsider'], w['customer']):
        with w['client']() as client:
            assert workbench_login(client, human).status_code == 200
            denied = client.get('/api/v1/workbench/capabilities')
            assert denied.status_code == 403 and 'capabilities' not in denied.text


def test_routes_count_only_offerings_the_catalog_would_admit(workbench):
    """PR #12 review: an offering counts as a route only while authz.nexloop_assert_service_offering would admit it (active and
    valid_until strictly in the future): expired, expiring exactly now, without valid_until or inactive offerings are not routes."""
    w = workbench
    provision(w)
    admin = w['admin']

    def offering(label, **props):
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','ServiceOffering',%s,1,%s,'synthetic',%s,clock_timestamp(),clock_timestamp())""",
                      (TENANT, uuid.uuid4().hex * 2, Jsonb({'active': True, 'delivery_action': 'nexloop.' + label + ':1', 'title': 'synthetic', **props}), 'route-' + label))
    offering('future', valid_until='2099-01-01T00:00:00Z')
    offering('expired', valid_until='2020-01-01T00:00:00Z')
    now = admin.execute('select clock_timestamp()').fetchone()[0]
    offering('expires_now', valid_until=now.isoformat())  # the boundary: valid_until <= clock_timestamp() is refused
    offering('no_validity')
    offering('inactive', active=False, valid_until='2099-01-01T00:00:00Z')
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        routes = client.get('/api/v1/workbench/capabilities').json()['routes']
    assert routes == [{'delivery_action': 'nexloop.future:1', 'active_offerings': 1}], routes
