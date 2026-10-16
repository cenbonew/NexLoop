"""0220 real dispatch switch (NX-037 prerequisite) on clean catalog PostgreSQL.

An external effect is dispatched only when the deployment switch REAL_DISPATCH_ENABLED (the worker process's environment),
the tenant switch and the Action switch are all open; otherwise it is refused before any provider request (NXC07). Only the
human owner opens or closes the tenant and Action switches through the governed entry (workbench HTTP here, production
create_app, members applied by the real configurator, role manifest v9); each change is a new control revision, so a
snapshot taken before it is superseded. Whether an effect is external comes from the effect category declaration
(external_effect, 0112 + 0153): undeclared is external. The test cluster declares NexLoop's loopback channels not external
(tests/support/loopback_effects.py); these tests remove that declaration to make the effect external. Actual governed
effect executor, independent effect Worker and loopback provider (commitment_fixture); synthetic data.
"""
import json
import secrets
import time

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios import doctor
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app
from nexloop_eios.workbench_roles import apply_members, compile_members
from support.effect_provider import effect_provider
from support.tenants import TENANT_A, TENANT_B
from support.loopback_effects import make_external
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_incentives_pg import incentives  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import CALLER, ROLES, WORKBENCH_APP, add_human, members_file, workbench_login, write_facts

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)  # the tests using the effect executor
SEND = 'eios:action:nexloop.service.request:1'
INCENTIVE = 'eios:action:nexloop.incentive.issue:1'


def owner_sql(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute(sql, params).fetchone()[0]


def refusals(admin, code, intent=None):
    return admin.execute('select count(*) from runtime.nexloop_dispatch_refusals where code=%s and (%s::uuid is null or intent_id=%s::uuid)',
                         (code, intent, intent)).fetchone()[0]


def private(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(0o600)
    return path


@pytest.fixture
def switching(commitments, identity, uow, admin, pg, tmp_path):
    c = commitments
    publish_registry_actions(admin, ('nexloop.real_dispatch.set',))
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    owner, operator = add_human(admin, 'owner'), add_human(admin, 'operator')
    admin.execute('alter role nexloop_configurator login')
    configurator = private(tmp_path, 'configurator-dsn', make_conninfo(pg, user='nexloop_configurator'))
    members = members_file((owner, 'owner'), (operator, 'operator'))
    apply_members(ROLES, members, database_url_file=configurator)
    write_facts(admin, compile_members(ROLES, members))
    signer = c['signer']
    (tmp_path / 'app-artifacts').mkdir(mode=0o700)
    config = ApiConfiguration(private(tmp_path, 'business-dsn', make_conninfo(pg, user='nexloop_api')), private(tmp_path, 'authority-key', signer.material),
        tmp_path / 'app-artifacts', signer.key_id, BrowserConfiguration(private(tmp_path, 'identity-dsn', identity[0]), private(tmp_path, 'rate-key', secrets.token_hex(32)),
        TENANT, 'synthetic-browser-app', ORIGIN), execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, ORIGIN))

    def switch(member, **body):
        with TestClient(create_app(config), base_url=ORIGIN) as client:
            csrf = workbench_login(client, member).json()['csrf_token']
            body = {'action_type': None, 'data_mode': None, 'reason': 'synthetic switch change', **body}
            return act(client, csrf, 'set_real_dispatch', body)
    c.update(switch=switch, owner_member=owner, operator_member=operator, configurator=configurator, config=config)
    return c


@PLAN
def test_an_external_effect_needs_all_three_layers_and_a_change_supersedes_in_flight_work(switching, admin, tmp_path, monkeypatch):
    c = switching
    monkeypatch.delenv('REAL_DISPATCH_ENABLED', raising=False)
    # A declared category without external_effect is external (fail closed): the loopback declaration is removed.
    c.configure_categories()
    make_external(admin, TENANT, SEND)
    for body in ({'scope_kind': 'tenant', 'enabled': True, 'data_mode': 'real'}, {'scope_kind': 'action_type', 'enabled': True, 'action_type': SEND}):
        assert c['switch'](c['owner_member'], **body).status_code == 200
    first = c.submit({'service': '外部服务交付'})['intent_id']
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        # Deployment switch closed: refused before the provider, recorded with NXC07.
        assert c.dispatch(provider)['status'] == 'admission_unavailable' and provider.control('snapshot')['requests'] == []
        assert refusals(admin, 'NXC07', first) == 1
        # All three open: dispatched once.
        monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
        time.sleep(3.1)
        c.deliver(provider, first)
        assert provider.control('snapshot')['effects'] == 1
        # In flight while the owner closes the Action: the earlier snapshot is superseded at dispatch (NXC02), never sent.
        in_flight = c.submit({'service': '外部服务交付（关闭前）'})['intent_id']
        assert c['switch'](c['owner_member'], scope_kind='action_type', enabled=False, action_type=SEND).status_code == 200
        assert c.dispatch(provider)['status'] == 'admission_unavailable'
        assert refusals(admin, 'NXC02', in_flight) == 1
        # New work after the change is refused by the closed Action layer (the worker keeps re-claiming the older refused
        # intent first, so the layer is read where the admission reads it).
        assert layer(admin, TENANT, SEND) == 'action'
        assert provider.control('snapshot')['effects'] == 1  # nothing else reached the provider
        assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (in_flight,)).fetchone()[0] != 'fulfilled'


def set_switch(admin, tenant, kind, ref, enabled, data_mode=None, world='real'):
    """As the governed handler leaves a switch (control event + row); the owner-only entry is tested through HTTP."""
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        rev = admin.execute("select control.nexloop_nx022_bump(%s,%s,'real_dispatch',%s,%s,'{}'::jsonb,'synthetic-owner','synthetic-intent-'||gen_random_uuid())",
                            (tenant, world, kind, ref)).fetchone()[0]
        admin.execute('''insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
            values(%s,%s,%s,%s,%s,%s,%s,'synthetic','synthetic-owner','synthetic') on conflict(tenant_id,world,scope_kind,scope_ref)
            do update set enabled=excluded.enabled,data_mode=excluded.data_mode,revision=excluded.revision''', (tenant, world, kind, ref, enabled, data_mode, rev))


def layer(admin, tenant, resource, world='real', deployment=True):
    with admin.transaction():
        admin.execute("select set_config('nexloop.real_dispatch_enabled',%s,true)", ('true' if deployment else 'false',))
        return admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (tenant, world, resource)).fetchone()[0]


def test_each_layer_is_fail_closed_and_only_the_opened_tenant_action_passes(catalog_admin):
    admin, a, b = catalog_admin, TENANT_A, TENANT_B
    assert layer(admin, a, SEND) == 'tenant'  # nothing recorded: closed
    assert layer(admin, a, SEND, deployment=False) == 'deployment'
    set_switch(admin, a, 'tenant', '*', True, 'real')
    assert layer(admin, a, SEND) == 'action'
    set_switch(admin, a, 'action_type', SEND, True)
    assert layer(admin, a, SEND) is None  # the one open path
    assert layer(admin, a, INCENTIVE) == 'action'  # another Action of the same tenant
    assert layer(admin, b, SEND) == 'tenant'  # another tenant
    assert layer(admin, a, SEND, world='test') == 'world' and layer(admin, a, SEND, world='simulation') == 'world'
    set_switch(admin, a, 'tenant', '*', True, 'test')
    assert layer(admin, a, SEND) == 'tenant'  # a test data-mode tenant is closed
    set_switch(admin, a, 'tenant', '*', False, 'real')
    assert layer(admin, a, SEND) == 'tenant'
    for value in ('', 'TRUE', '1', 'yes'):
        with admin.transaction():
            admin.execute("select set_config('nexloop.real_dispatch_enabled',%s,true)", (value,))
            assert admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (a, 'real', SEND)).fetchone()[0] == 'deployment'


@pytest.fixture
def catalog_admin(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    # NX-052: the synthetic tenants exist before any tenant-scoped configuration row (tenant foreign keys).
    for tenant in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    return admin


def test_external_is_the_default_and_only_an_explicit_false_for_this_definition_is_not(catalog_admin):
    admin, tenant, definition = catalog_admin, TENANT_A, {'stable_name': 'nexloop.service.request', 'version': 1}
    external = lambda d: admin.execute('select control.nexloop_effect_is_external(%s,%s,%s,%s)', (tenant, 'real', SEND, Jsonb(d))).fetchone()[0]
    assert external(definition) is True  # undeclared
    digest = admin.execute('select control.nexloop_definition_digest(%s)', (Jsonb(definition),)).fetchone()[0]
    admin.execute('''insert into control.nexloop_action_effect_externality(tenant_id,world,resource_id,definition_digest,external_effect,manifest_version)
        values(%s,'real',%s,%s,false,2)''', (tenant, SEND, digest))
    assert external(definition) is False
    assert external({**definition, 'changed': True}) is True  # another definition of the same Action
    assert admin.execute('select control.nexloop_effect_is_external(%s,%s,%s,null)', (tenant, 'real', SEND)).fetchone()[0] is True
    with pytest.raises(psycopg.Error, match='append-only'):
        admin.execute('update control.nexloop_action_effect_externality set external_effect=true')


@PLAN
def test_trusted_configuration_records_external_effect_and_refuses_an_invalid_value(switching, admin):
    from nexloop_eios.effect_categories import EffectCategoriesRejected, apply, validate
    c = switching
    make_external(admin, TENANT, SEND)
    manifest = json.loads((__import__('pathlib').Path(__file__).resolve().parents[1] / 'deploy/configuration/action-effect-categories.v2.json').read_text())
    manifest['actions'] = [a for a in manifest['actions'] if a['stable_name'] == 'nexloop.service.request']
    for bad in ('false', 0, None):
        with pytest.raises(EffectCategoriesRejected):
            validate({**manifest, 'actions': [{**manifest['actions'][0], 'external_effect': bad}]})
    with pytest.raises(psycopg.Error, match='entry invalid'):  # the SQL refuses it too
        _apply_raw(c, {**manifest, 'actions': [{**manifest['actions'][0], 'external_effect': 'no'}]})
    assert apply(manifest, TENANT, database_url_file=c['configurator'])['externality_written'] == 1
    definition = admin.execute('select definition from control.nexloop_action_definitions where tenant_id=%s and world=%s and resource_id=%s and active',
                               (TENANT, 'real', SEND)).fetchone()[0]
    assert admin.execute('select control.nexloop_effect_is_external(%s,%s,%s,%s)', (TENANT, 'real', SEND, Jsonb(definition))).fetchone()[0] is False


def _apply_raw(c, manifest):
    """The configurator's SQL function with a manifest the Python validation would refuse."""
    from nexloop_eios.postgres_artifacts import canonical_payload
    with psycopg.connect(c['configurator'].read_text()) as db:
        db.execute('select control.nexloop_configure_effect_categories(%s,%s,%s::jsonb)', (TENANT, 'real', canonical_payload(manifest)))


@PLAN
def test_only_the_owner_changes_a_switch_and_every_change_is_a_new_control_revision(switching, admin):
    c = switching
    before = owner_sql(admin, 'select coalesce((select revision from control.nexloop_control_heads where tenant_id=%s and world=%s),0)', (TENANT, 'real'))
    assert c['switch'](c['operator_member'], scope_kind='tenant', enabled=True, data_mode='real').status_code == 403
    assert c['switch'](c['owner_member'], scope_kind='tenant', enabled=True).status_code == 409  # the tenant scope needs its data mode (handler)
    assert c['switch'](c['owner_member'], scope_kind='action_type', enabled=True, action_type='not-a-resource').status_code == 422
    opened = c['switch'](c['owner_member'], scope_kind='tenant', enabled=True, data_mode='real')
    assert opened.status_code == 200 and opened.json()['control_revision'] == before + 1
    closed = c['switch'](c['owner_member'], scope_kind='tenant', enabled=False, data_mode='real')
    assert closed.json()['control_revision'] == before + 2
    events = admin.execute("select event_kind,scope_kind,scope_ref,detail->>'enabled' from control.nexloop_control_events where tenant_id=%s and revision>%s order by revision",
                           (TENANT, before)).fetchall()
    assert events == [('real_dispatch', 'tenant', '*', 'true'), ('real_dispatch', 'tenant', '*', 'false')]
    # The owner's workbench metrics view shows the current switches (states and versions only).
    shown = owner_sql(admin, 'select runtime.nexloop_metrics_snapshot(%s,%s)', (TENANT, 'real'))['real_dispatch']
    assert shown == {'world': 'real', 'tenant': {'enabled': False, 'data_mode': 'real', 'revision': before + 2}, 'actions': []}
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_configurator'):
        assert admin.execute("select has_table_privilege(%s,'control.nexloop_real_dispatch_switches','insert')", (role,)).fetchone() == (False,)
        assert admin.execute("select has_function_privilege(%s,'control.nexloop_governed_set_real_dispatch(text,text,text,text,text,jsonb)','execute')", (role,)).fetchone() == (False,)


def test_readiness_and_doctor_report_states_and_counts_only(catalog_admin, pg, tmp_path, monkeypatch):
    from nexloop_eios import deployment_readiness as readiness
    admin = catalog_admin
    set_switch(admin, TENANT_A, 'tenant', '*', True, 'real')
    set_switch(admin, TENANT_A, 'action_type', SEND, True)
    set_switch(admin, TENANT_B, 'tenant', '*', True, 'test')
    set_switch(admin, TENANT_B, 'action_type', SEND, True)
    status, body = readiness.evaluate(lambda: psycopg.connect(make_conninfo(pg, user='nexloop_api')), environ={'REAL_DISPATCH_ENABLED': 'true'})
    assert status == 200 and body['dispatch_switch'] == 'open' and body['real_dispatch'] == {'tenants_open': 1, 'actions_open': 1}
    dsn = private(tmp_path, 'metrics-dsn', make_conninfo(pg, user='nexloop_metrics'))
    report = doctor.real_dispatch(dsn, environment={})
    assert report == {'checks': {'real_dispatch_status': True, 'deployment_switch_valid': True},
                      'details': {'real_dispatch': {'deployment': 'closed', 'tenants_open': 1, 'actions_open': 1}}}
    assert doctor.real_dispatch(dsn, environment={'REAL_DISPATCH_ENABLED': 'on'})['checks']['deployment_switch_valid'] is False
    assert doctor.real_dispatch(private(tmp_path, 'api-dsn', make_conninfo(pg, user='nexloop_api')), environment={})['checks']['real_dispatch_status'] is False


@PLAN
def test_an_external_incentive_is_refused_while_only_another_action_is_open(incentives, admin, monkeypatch):
    from test_incentives_pg import intent, issue, seed_budget, seed_offering, tick
    c = incentives
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
    make_external(admin, TENANT, INCENTIVE)  # as a real incentive channel: nothing declares it otherwise
    set_switch(admin, TENANT, 'tenant', '*', True, 'real')
    set_switch(admin, TENANT, 'action_type', SEND, True)  # only the service Action is open
    seed_budget(admin, 1000)
    offer = seed_offering(c, admin)
    issue(c, 'rds-1', offer, 100)
    assert tick(c) == {'admitted': 0, 'refused': 1, 'fulfilled': 0, 'failed': 0, 'unknown': 0}
    assert intent(admin, 'rds-1')[1:3] == ('refused', 'real_dispatch_closed') and c['ledger'].requests == []  # terminal, never sent
    set_switch(admin, TENANT, 'action_type', INCENTIVE, True)
    issue(c, 'rds-2', offer, 100)
    assert tick(c)['fulfilled'] == 1 and intent(admin, 'rds-2')[1] == 'fulfilled' and len(c['ledger'].requests) == 1
    assert intent(admin, 'rds-1')[1] == 'refused' and tick(c)['admitted'] == 0  # the refused intent is not sent again
