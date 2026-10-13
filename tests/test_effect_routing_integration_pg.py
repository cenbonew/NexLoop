"""NX-037 provider routing with the governed enterprise delivery Action (L2 gap B slice 1, 0400) on actual PostgreSQL: the actual
effect Worker (an independent process, as deployed) with ProviderRoutes over two local synthetic providers, the NexLoop delivery
for nexloop.service.request and the enterprise endpoint for nexloop.enterprise.deliver.

A plan fixture binds one effect Action, so the reply and the enterprise intent live in two databases (parametrized); every worker
holds the same two routes, so the provider that must stay silent is always reachable."""
import multiprocessing
import signal
import time
from contextlib import contextmanager

import pytest

from effect_execution_fixture import execution_plan, governed_effect_executor  # noqa: F401
from support.effect_provider import effect_provider
from support.tenants import TENANT_A
from test_real_dispatch_switch_pg import refusals, set_switch

REPLY_ACTION, DELIVER_ACTION = 'nexloop.service.request', 'nexloop.enterprise.deliver'
DELIVER = 'eios:action:nexloop.enterprise.deliver:1'
CASES = {'reply': (None, REPLY_ACTION, {'message': '合成回复'}), 'enterprise-deliver': ('enterprise-deliver', DELIVER_ACTION, {'service': '合成企业交付订单'})}


def _routed_worker(configuration, origins, pipe):
    """The deployed worker loop's one tick, with one provider per Action (no default)."""
    from nexloop_eios.backend import open_backend
    from nexloop_eios.effect_dispatch import EffectDispatcher, ProviderRoutes
    from nexloop_eios.effect_provider import EffectProviderConfiguration, HttpEffectProvider
    private = dict(configuration)
    token, world = private.pop('service_token'), private.pop('world')
    with open_backend(**private) as backend:
        executor = backend.authenticate(token, world=world)
        routes = ProviderRoutes({action: HttpEffectProvider(EffectProviderConfiguration(origin, test_loopback_http=True, timeout=1))
                                 for action, origin in origins.items()})
        pipe.send({'event': 'worker_ready'})
        result = EffectDispatcher(executor, routes, lease_seconds=3).run_once()
        pipe.send({'event': 'settled', **{k: result.get(k) for k in ('claimed', 'status', 'provider_state', 'business_action_success')}})
    pipe.close()


@contextmanager
def worker(fixture, webchat, enterprise):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_routed_worker, args=(fixture.executor_configuration(), {REPLY_ACTION: webchat.origin, DELIVER_ACTION: enterprise.origin}, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20) and parent.recv() == {'event': 'worker_ready'}
        yield process, parent
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)
        parent.close()


def tick(fixture, webchat, enterprise):
    with worker(fixture, webchat, enterprise) as (process, pipe):
        assert pipe.poll(20)
        result = pipe.recv()
        process.join(10)
    assert process.exitcode == 0, result
    return result


def submit(fixture, action, parameters):
    first = fixture.ports[0].submit_effect_intent(parameters=parameters, action=action)
    assert first['state'] == 'accepted'
    return first['intent_id']


def requests(server):
    return [tuple(r) for r in server.control('snapshot')['requests']]


def open_switches(admin, monkeypatch):
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
    set_switch(admin, TENANT_A, 'tenant', '*', True, 'real')
    set_switch(admin, TENANT_A, 'action_type', DELIVER, True)


@pytest.mark.parametrize('execution_plan,case', [(None, 'reply'), ('enterprise-deliver', 'enterprise-deliver')], indirect=['execution_plan'])
def test_each_intent_reaches_only_its_own_provider_and_the_claim_names_its_action(governed_effect_executor, admin, tmp_path, monkeypatch, case):  # noqa: F811
    fixture = governed_effect_executor
    _, action, parameters = CASES[case]
    if action == DELIVER_ACTION:
        open_switches(admin, monkeypatch)
    intent = submit(fixture, action, parameters)
    assert admin.execute('select action_name from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone() == (action,)
    # 4. The claim's Action is the intent's Action, taken from action_claim.key.action_stable_name of the PG claim.
    with fixture.current_executor() as executor:
        job = executor.claim_effect(lease_seconds=3)
    assert job['intent_id'] == intent and job['stage'] == 'fresh' and job['action'] == action
    time.sleep(3.1)  # that claim's lease lapses; the routed worker claims again
    with effect_provider(tmp_path / 'webchat.sqlite') as webchat, effect_provider(tmp_path / 'enterprise.sqlite') as enterprise:
        result = tick(fixture, webchat, enterprise)
        assert result['claimed'] is True and result['status'] == 'dispatching' and result['provider_state'] == 'accepted', result
        own, other = (webchat, enterprise) if action == REPLY_ACTION else (enterprise, webchat)
        assert requests(own) == [('POST', intent, 202)] and requests(other) == []


@pytest.mark.parametrize('execution_plan', ['enterprise-deliver'], indirect=True)
def test_after_a_kill_the_new_worker_only_queries_the_enterprise_provider(governed_effect_executor, admin, tmp_path, monkeypatch):  # noqa: F811
    """The enterprise provider commits, the worker is killed before it records anything; the new worker queries that provider
    (GET), never re-sends (no second POST) and never asks the other provider."""
    fixture = governed_effect_executor
    open_switches(admin, monkeypatch)
    intent = submit(fixture, DELIVER_ACTION, CASES['enterprise-deliver'][2])
    with effect_provider(tmp_path / 'webchat.sqlite') as webchat, effect_provider(tmp_path / 'enterprise.sqlite') as enterprise:
        enterprise.control('pause')
        with worker(fixture, webchat, enterprise) as (old, pipe):
            assert enterprise.wait('accepted_committed')['intent_id'] == intent
            assert not pipe.poll(), 'a local outcome escaped the paused provider response'
            old.kill()
            old.join(10)
            assert old.exitcode == -signal.SIGKILL
        assert requests(enterprise) == [('POST', intent, 202)]
        enterprise.control('release')
        time.sleep(3.1)  # the killed worker's lease lapses
        result = tick(fixture, webchat, enterprise)
        assert result['claimed'] is True and result['status'] == 'dispatching' and result['provider_state'] == 'accepted', result
        assert requests(enterprise) == [('POST', intent, 202), ('GET', intent, 200)]
        assert requests(webchat) == []
        assert enterprise.control('snapshot')['effects'] == 1


@pytest.mark.parametrize('execution_plan,case', [(None, 'reply'), ('enterprise-deliver', 'enterprise-deliver')], indirect=['execution_plan'])
def test_with_the_real_dispatch_switches_closed_the_enterprise_provider_gets_nothing_and_replies_go_on(governed_effect_executor, admin, tmp_path, monkeypatch, case):  # noqa: F811
    fixture = governed_effect_executor
    _, action, parameters = CASES[case]
    monkeypatch.delenv('REAL_DISPATCH_ENABLED', raising=False)  # all three layers closed
    intent = submit(fixture, action, parameters)
    with effect_provider(tmp_path / 'webchat.sqlite') as webchat, effect_provider(tmp_path / 'enterprise.sqlite') as enterprise:
        result = tick(fixture, webchat, enterprise)
        assert requests(enterprise) == []
        if action == DELIVER_ACTION:
            assert result['status'] == 'admission_unavailable' and refusals(admin, 'NXC07', intent) >= 1
            assert requests(webchat) == []
        else:
            # The reply is not an external effect (categories v3; the loopback delivery): the switches do not apply.
            assert result['status'] == 'dispatching' and requests(webchat) == [('POST', intent, 202)]
