"""NX-037 provider routing: one effect worker, one provider per Action (nexloop.service.request -> the NexLoop delivery service,
nexloop.enterprise.deliver -> the enterprise HTTPS endpoint), no default. The ledger is a recording stand-in for the protected PG
ports (the claim gives the Action; admission and query authorization are keyed by the provider's profile digest); the two
providers are real HttpEffectProvider instances against two separate synthetic provider servers (tests/support/effect_provider.py)."""
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from nexloop_eios.effect_dispatch import EffectDispatcher, ProviderRoutes
from nexloop_eios.effect_provider import EffectProviderConfiguration, HttpEffectProvider
from nexloop_eios.postgres_artifacts import canonical_payload
from support.effect_provider import effect_provider

WEBCHAT = 'nexloop.service.request'
ENTERPRISE = 'nexloop.enterprise.deliver'  # L2's new Action (name fixed by the dispatcher); only its name is used here


def provider(origin):
    return HttpEffectProvider(EffectProviderConfiguration(origin, test_loopback_http=True, timeout=1))


class Ledger:
    """The protected ports as the dispatcher sees them; `accepts` is the provider digest an intent is bound to (0044)."""

    def __init__(self, job, accepts=None):
        self.job, self.accepts, self.calls = job, accepts, []
        self.parameters = {'message': 'synthetic'}

    def claim_effect(self, *, lease_seconds):
        return self.job

    def prepare_effect_dispatch(self, *, intent_id, fence, provider_profile_digest):
        self.calls.append(('prepare', provider_profile_digest))
        return {'parameters': self.parameters, 'provider_payload_digest': hashlib.sha256(canonical_payload(self.parameters).encode()).hexdigest()}

    def authorize_effect_query(self, *, intent_id, fence, provider_profile_digest):
        self.calls.append(('authorize_query', provider_profile_digest))
        if provider_profile_digest != self.accepts:
            raise PermissionError('effect provider binding differs')
        return {'provider_payload_digest': hashlib.sha256(canonical_payload(self.parameters).encode()).hexdigest(), 'query_id': str(uuid.uuid4())}

    def record_effect_observation(self, **kw):
        self.calls.append(('observe', kw['provider_profile_digest']))
        return {'state': 'dispatching', 'provider_state': kw['provider_state'], 'business_action_success': False, 'governed_claim_finalized': False}

    record_effect_query_observation = record_effect_observation

    def record_effect_unknown(self, **kw):
        self.calls.append(('unknown',))

    def record_unrouted(self, *, intent_id):
        self.calls.append(('unrouted', intent_id))
        return True


def job(action, stage='fresh', intent=None):
    return {'intent_id': intent or str(uuid.uuid4()), 'fence': 1, 'lease_until': None, 'stage': stage, 'action': action}


def requests(server):
    return [tuple(r) for r in server.control('snapshot')['requests']]


@pytest.fixture
def servers(tmp_path):
    with effect_provider(tmp_path / 'webchat.sqlite') as webchat, effect_provider(tmp_path / 'enterprise.sqlite') as enterprise:
        yield webchat, enterprise


def test_each_action_goes_to_its_own_provider_and_never_the_other(servers):
    webchat, enterprise = servers
    routes = ProviderRoutes({WEBCHAT: provider(webchat.origin), ENTERPRISE: provider(enterprise.origin)})
    for action, server, other in ((WEBCHAT, webchat, enterprise), (ENTERPRISE, enterprise, webchat)):
        intent = str(uuid.uuid4())
        ledger = Ledger(job(action, intent=intent))
        result = EffectDispatcher(ledger, routes, lease_seconds=30).run_once()
        assert result['claimed'] is True and result['status'] == 'dispatching'
        assert ledger.calls[0] == ('prepare', routes.for_action(action).profile_digest)  # admitted under that provider's binding
        assert ('POST', intent, 202) in requests(server) and not any(r[1] == intent for r in requests(other))


def test_an_action_without_a_route_is_never_admitted_or_sent(servers):
    webchat, enterprise = servers
    routes = ProviderRoutes({WEBCHAT: provider(webchat.origin), ENTERPRISE: provider(enterprise.origin)})
    import io
    import json as _json
    from nexloop_eios.structured_log import configure
    stream = io.StringIO()
    configure('effect-worker', stream=stream)
    for action in ('nexloop.incentive.issue', None):
        ledger = Ledger(job(action))
        ledger.parameters = {'message': 'secret customer text'}
        assert EffectDispatcher(ledger, routes, lease_seconds=30).run_once() == {'claimed': True, 'status': 'unrouted', 'business_action_success': False}
        assert ledger.calls == [('unrouted', ledger.job['intent_id'])]  # no admission, no query; recorded as NXE01
    assert requests(webchat) == [] and requests(enterprise) == []
    # One warning line per unrouted intent: the Action name and the intent id (and the code), never a parameter.
    lines = [_json.loads(l) for l in stream.getvalue().splitlines()]
    assert [(l['level'], l['event'], sorted(l['detail'])) for l in lines] == [('warning', 'effect route unconfigured', ['action', 'code', 'intent_id'])] * 2
    assert lines[0]['detail']['action'] == 'nexloop.incentive.issue' and lines[0]['detail']['code'] == 'NXE01'
    assert 'secret customer text' not in stream.getvalue()


def test_an_unknown_result_is_queried_with_the_provider_that_sent_it(servers):
    """Reconciliation (no new reservation, so no Action on the claim): the ledger authorizes the query only for the provider the
    intent was bound to at dispatch; the other provider is never asked."""
    webchat, enterprise = servers
    routes = ProviderRoutes({WEBCHAT: provider(webchat.origin), ENTERPRISE: provider(enterprise.origin)})
    intent = str(uuid.uuid4())
    sent = Ledger(job(ENTERPRISE, intent=intent))
    EffectDispatcher(sent, routes, lease_seconds=30).run_once()
    ledger = Ledger(job(None, stage='reconcile', intent=intent), accepts=routes.for_action(ENTERPRISE).profile_digest)
    result = EffectDispatcher(ledger, routes, lease_seconds=30).run_once()
    assert result['claimed'] is True and result['status'] == 'dispatching'
    assert ('GET', intent, 200) in requests(enterprise) and not any(r[1] == intent for r in requests(webchat))
    assert ledger.calls[-1] == ('observe', routes.for_action(ENTERPRISE).profile_digest)
    nobody = Ledger(job(None, stage='reconcile'), accepts='0' * 64)  # bound to no configured provider: no query at all
    assert EffectDispatcher(nobody, routes, lease_seconds=30).run_once()['status'] == 'query_unavailable'


def test_routes_refuse_shared_endpoints_or_credentials_and_bad_timeouts(tmp_path):
    a = provider('http://127.0.0.1:20001')
    with pytest.raises(ValueError):
        ProviderRoutes({WEBCHAT: a, ENTERPRISE: provider('http://127.0.0.1:20001')})  # one origin for two Actions
    with pytest.raises(ValueError):
        ProviderRoutes({})
    secret = tmp_path / 'shared-credential'
    secret.write_text('x' * 32)
    one = HttpEffectProvider(EffectProviderConfiguration('https://127.0.0.1:20003', credential_file=str(secret)))
    two = HttpEffectProvider(EffectProviderConfiguration('https://127.0.0.1:20004', credential_file=str(secret)))
    with pytest.raises(ValueError):
        ProviderRoutes({WEBCHAT: one, ENTERPRISE: two})  # one credential for two channels
    slow = HttpEffectProvider(EffectProviderConfiguration('http://127.0.0.1:20005', test_loopback_http=True, timeout=20))
    with pytest.raises(ValueError):
        EffectDispatcher(None, ProviderRoutes({WEBCHAT: a, ENTERPRISE: slow}), lease_seconds=30)  # timeout above lease/3


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_the_worker_routes_file_is_strict(tmp_path):
    from nexloop_eios import effect_worker
    configs = {name: tmp_path / f'{name}.json' for name in ('webchat', 'enterprise')}  # deliberately absent
    base = {'schema': effect_worker.ROUTES_SCHEMA, 'routes': {WEBCHAT: str(configs['webchat']), ENTERPRISE: str(configs['enterprise'])}}
    for bad in ({**base, 'schema': 'other'}, {**base, 'extra': 1}, {**base, 'routes': {}}, {**base, 'routes': {'Bad Name': str(configs['webchat'])}},
                {**base, 'routes': {WEBCHAT: 'relative/path.json'}}):
        with pytest.raises((ValueError, OSError)):
            effect_worker._routes(private(tmp_path / f'routes-{uuid.uuid4().hex}.json', json.dumps(bad)))
    with pytest.raises(OSError):  # the provider files of a valid routes file must exist and be private
        effect_worker._routes(private(tmp_path / 'routes.json', json.dumps(base)))
    common = ['--database-url-file', 'a', '--signing-key-file', 'b', '--service-credential-file', 'c', '--artifact-root', 'd', '--world', 'real']
    assert effect_worker._arguments(common + ['--provider-routes-file', 'r']).provider_routes_file == Path('r')
    for wrong in (common, common + ['--provider-routes-file', 'r', '--provider-config-file', 'p']):
        with pytest.raises(SystemExit):
            effect_worker._arguments(wrong)
