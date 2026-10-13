"""NX-037 gap B (0400): the governed enterprise delivery Action nexloop.enterprise.deliver:1 on clean catalog PostgreSQL.

The Action is an external effect (effect categories v3): an intent reaches the enterprise endpoint only through the actual
governed path (plan step bound to the Action, Run credential for it, enterprise offering in the catalog) and an independent
effect Worker, and only while the deployment, tenant and (tenant, Action) real dispatch switches are all open. Authority is
rechecked at dispatch, an unknown provider outcome is queried before anything is sent again, and the deployment-level effect
Action allow-list cannot be written by any runtime role. Synthetic data; loopback provider (tests/support/effect_provider.py).
"""
import json
import signal
import time
from pathlib import Path

import psycopg
import pytest

from support.effect_provider import effect_provider
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from support.tenants import TENANT_A
from test_effect_dispatch import independent_worker
from test_real_dispatch_switch_pg import layer, refusals, set_switch

ROOT = Path(__file__).resolve().parents[1]
DELIVER = 'eios:action:nexloop.enterprise.deliver:1'
REPLY = 'eios:action:nexloop.service.request:1'
ENTERPRISE = pytest.mark.parametrize('execution_plan', ['enterprise-deliver'], indirect=True)


def submit(fixture, service='合成企业交付订单'):
    first = fixture.ports[0].submit_effect_intent(parameters={'service': service}, action='nexloop.enterprise.deliver')
    replay = fixture.ports[1].submit_effect_intent(parameters={'service': service}, action='nexloop.enterprise.deliver')
    assert first == replay and first['state'] == 'accepted'
    return first['intent_id']


def submit_new_step(fixture, admin, number, service):
    """Another plan step bound to the enterprise Action, its own Run and context (one intent per step slot)."""
    plan = fixture.plan
    backend = plan['backend']
    planner = backend.authenticate(plan['planner_token'], world='real')
    submitter = backend.authenticate(plan['submitter_token'], world='real')
    base = admin.execute("select properties from ontology.objects where type_name='PlanStep' and object_id=%s", (plan['step'],)).fetchone()[0]
    step = planner.create_object(action_name='PlanStep.create', action_version=1, intent_id=f'enterprise-step-{number}', type_name='PlanStep',
                                 properties=base)['object_id']
    run = submitter.issue_run_credential(action_resources=[DELIVER])
    planner.bind_effect_context(step_id=step, step_revision=1, goal_revision=1, consumer_revision=1, control_revision=1,
                                run_id=run.run_id, run_token=run.token, executor_token=plan['executor_token'], action='nexloop.enterprise.deliver')
    port = backend.authenticate_run(run.token, world='real', run_id=run.run_id)
    return port.submit_effect_intent(parameters={'service': service}, action='nexloop.enterprise.deliver')['intent_id']


def open_all(admin, monkeypatch, action=DELIVER):
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
    set_switch(admin, TENANT_A, 'tenant', '*', True, 'real')
    set_switch(admin, TENANT_A, 'action_type', action, True)


def run_worker(fixture, provider):
    with independent_worker(fixture, provider.origin) as (worker, pipe):
        assert pipe.poll(20)
        result = pipe.recv()
        worker.join(10)
    assert worker.exitcode == 0, result
    return result


@pytest.mark.parametrize('role', ['nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_identity'])
def test_effect_action_allow_list_is_migration_only(admin, role):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    rows = admin.execute('select resource_id,slot_identity from control.nexloop_effect_actions order by 1').fetchall()
    assert rows == [(DELIVER, 'enterprise.deliver:primary'), (REPLY, 'service.request:primary')]
    for statement in ("insert into control.nexloop_effect_actions(stable_name,version,slot_identity) values('nexloop.any.effect',1,'any.effect:primary')",
                      "update control.nexloop_effect_actions set version=2", 'delete from control.nexloop_effect_actions',
                      'select count(*) from control.nexloop_effect_actions'):
        with admin.transaction(force_rollback=True):
            admin.execute(f'set local role {role}')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                admin.execute(statement)
    assert admin.execute('select count(*) from control.nexloop_effect_actions').fetchone() == (2,)


@ENTERPRISE
def test_all_three_switches_gate_the_enterprise_delivery_independently_of_the_reply_action(governed_effect_executor, admin, tmp_path, monkeypatch):  # noqa: F811
    fixture = governed_effect_executor
    monkeypatch.delenv('REAL_DISPATCH_ENABLED', raising=False)
    # Tenant and Action switches open before the intent (a later change supersedes its control snapshot, NXC02).
    set_switch(admin, TENANT_A, 'tenant', '*', True, 'real')
    set_switch(admin, TENANT_A, 'action_type', DELIVER, True)
    intent = submit(fixture)
    row = admin.execute('select action_name,slot_identity from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()
    assert row == ('nexloop.enterprise.deliver', 'enterprise.deliver:primary')
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        # Deployment switch closed: refused before the provider (NXC07).
        assert run_worker(fixture, provider)['status'] == 'admission_unavailable'
        assert refusals(admin, 'NXC07', intent) >= 1 and provider.control('snapshot')['requests'] == []
        # All three open: delivered exactly once through the independent Worker.
        monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
        time.sleep(3.1)  # the refused attempt's Action lease expires
        first = run_worker(fixture, provider)
        assert first['claimed'] is True and first['status'] != 'admission_unavailable', first
        snapshot = provider.control('snapshot')
        assert snapshot['effects'] == 1 and snapshot['requests'] == [('POST', intent, 202)]
    # Each layer alone closes it; the reply Action's own switch does not open this one (layers as admission reads them).
    assert layer(admin, TENANT_A, DELIVER) is None
    assert layer(admin, TENANT_A, DELIVER, deployment=False) == 'deployment'
    set_switch(admin, TENANT_A, 'tenant', '*', False, 'real')
    assert layer(admin, TENANT_A, DELIVER) == 'tenant'
    set_switch(admin, TENANT_A, 'tenant', '*', True, 'real')
    set_switch(admin, TENANT_A, 'action_type', DELIVER, False)
    set_switch(admin, TENANT_A, 'action_type', REPLY, True)
    assert layer(admin, TENANT_A, DELIVER) == 'action'
    # And a closed tenant or Action layer refuses a new enterprise intent at dispatch, before the provider.
    later = submit_new_step(fixture, admin, 2, '关闭后的企业交付')
    with effect_provider(tmp_path / 'provider-closed.sqlite') as provider:
        assert run_worker(fixture, provider)['status'] == 'admission_unavailable'
        assert provider.control('snapshot')['requests'] == []
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (later,)).fetchone()[0] != 'fulfilled'


@ENTERPRISE
@pytest.mark.parametrize('revocation', ['source_grant', 'control'])
def test_dispatch_rechecks_current_authority_for_the_enterprise_delivery(governed_effect_executor, admin, tmp_path, monkeypatch, revocation):  # noqa: F811
    fixture = governed_effect_executor
    open_all(admin, monkeypatch)  # before the intent: a later switch change supersedes its control snapshot (NXC02)
    intent = submit(fixture)
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        with independent_worker(fixture, provider.origin, pause_before_admit=True) as (worker, pipe):
            assert pipe.poll(15) and pipe.recv() == {'event': 'before_admit'}
            if revocation == 'source_grant':
                fixture.revoke_all_source_grants()
            else:
                fixture.revoke_control()
            pipe.send('release')
            assert pipe.poll(15)
            result = pipe.recv()
            worker.join(10)
        assert worker.exitcode == 0 and result['status'] == 'admission_unavailable' and result['business_action_success'] is False
        snapshot = provider.control('snapshot')
        assert snapshot['effects'] == 0 and snapshot['requests'] == []
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()[0] not in {'fulfilled', 'confirmed', 'failed'}


@ENTERPRISE
def test_an_unknown_enterprise_outcome_is_queried_before_anything_is_sent_again(governed_effect_executor, admin, tmp_path, monkeypatch):  # noqa: F811
    fixture = governed_effect_executor
    open_all(admin, monkeypatch)  # before the intent: a later switch change supersedes its control snapshot (NXC02)
    intent = submit(fixture)
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        provider.control('pause')
        with independent_worker(fixture, provider.origin) as (old, pipe):
            assert provider.wait('accepted_committed')['intent_id'] == intent
            assert admin.execute('select state from runtime.nexloop_effect_attempts where intent_id=%s', (intent,)).fetchone()[0] == 'dispatching'
            old.kill()
            old.join(10)
            assert old.exitcode == -signal.SIGKILL
        before = provider.control('snapshot')
        assert before['effects'] == 1 and before['requests'] == [('POST', intent, 202)]
        provider.control('release')
        time.sleep(3.1)
        settled = run_worker(fixture, provider)
        assert settled['claimed'] is True and settled['business_action_success'] is False
        after = provider.control('snapshot')
        # Query first: a GET for the same intent, never a second POST.
        assert after['effects'] == 1 and after['requests'] == before['requests'] + [('GET', intent, 200)]


@ENTERPRISE
def test_categories_v3_declare_the_enterprise_delivery_external_and_customer_contact(governed_effect_executor, admin, pg, tmp_path):  # noqa: F811
    from nexloop_eios.effect_categories import apply, load
    manifest = load(ROOT / 'deploy/configuration/action-effect-categories.v3.json')
    entry = next(a for a in manifest['actions'] if a['stable_name'] == 'nexloop.enterprise.deliver')
    assert entry == {**entry, 'version': 1, 'category': 'customer_contact', 'notification_parameters': [], 'external_effect': True}
    v2 = json.loads((ROOT / 'deploy/configuration/action-effect-categories.v2.json').read_text())
    assert manifest['actions'][:len(v2['actions'])] == v2['actions']  # v3 only adds the new Action
    admin.execute('alter role nexloop_configurator login')
    dsn = tmp_path / 'configurator-dsn'
    from psycopg.conninfo import make_conninfo
    dsn.write_text(make_conninfo(pg, user='nexloop_configurator'))
    dsn.chmod(0o600)
    only_published = {**manifest, 'actions': [entry]}
    apply(only_published, TENANT_A, database_url_file=dsn)
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        assert admin.execute("select control.nexloop_effect_is_external(%s,'real',%s,(select definition from control.nexloop_action_definitions where tenant_id=%s and resource_id=%s))",
                             (TENANT_A, DELIVER, TENANT_A, DELIVER)).fetchone()[0] is True
        category = admin.execute('select category from control.nexloop_action_effect_categories where tenant_id=%s and resource_id=%s', (TENANT_A, DELIVER)).fetchone()
    assert category == ('customer_contact',)


@pytest.mark.parametrize('execution_plan', ['enterprise-mismatch'], indirect=True)
def test_an_offering_that_delivers_through_another_action_is_outside_the_catalog(governed_effect_executor, admin):  # noqa: F811
    fixture = governed_effect_executor
    with pytest.raises(Exception):
        fixture.ports[0].submit_effect_intent(parameters={'service': '目录外交付'}, action='nexloop.enterprise.deliver')
    assert admin.execute("select count(*) from runtime.nexloop_effect_intents where action_name='nexloop.enterprise.deliver'").fetchone() == (0,)


def test_service_grants_cannot_grant_the_enterprise_delivery():
    from nexloop_eios import service_grants
    assert 'nexloop.enterprise.deliver' in service_grants.FORBIDDEN_MARKERS
