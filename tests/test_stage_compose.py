"""NX-032 (L5 review 2026-10-11): the APP_HOST stage Compose example orchestrates every process of the connection budget, each with
its own secret files, the common hardening and a command its real argument parser accepts; the API's browser identity pool is a
budget entry; doctor --deploy-gate compares Compose and budget both ways. No container is started."""
import copy
import json
import re

import pytest
import yaml

from nexloop_eios import background_services, connection_budget, deploy_checks, effect_worker, message_relay_cli, outbound_messages, runtime_worker
from nexloop_eios.browser_identity import open_browser_identity
from test_deploy_render import ROOT, STAGE, interpolate

BUDGET = json.loads((STAGE / 'connection-budget.v1.json').read_text())
SHARED = {'signing_key', 'pg_ca', 'host_ca', 'host_control_key'}
FORBIDDEN_ENV = re.compile(r'^(MODEL_API_KEY|EMBEDDING_API_KEY|NEXLOOP_DATABASE_URL|MODEL_PROVIDER|NEXLOOP_TEST_.*|.*PASSWORD.*|.*_DSN|DATABASE_URL)$')


@pytest.fixture(scope='module')
def compose():
    return yaml.safe_load(interpolate((STAGE / 'compose.app.example.yaml').read_text(), STAGE / 'compose.app.env.example'))


def test_budget_registers_the_identity_pool_and_stays_within_60():
    result = connection_budget.evaluate(BUDGET)
    assert result['passed'] and result['details']['application_pool_total'] == 57
    assert result['details']['services']['api-browser-identity'] == 8 and 'scheduler' not in result['details']['services']
    missing = copy.deepcopy(BUDGET)
    missing['services'] = [s for s in missing['services'] if s['name'] != 'api-browser-identity']
    assert connection_budget.evaluate(missing)['details']['missing_services'] == ['api-browser-identity']
    for bad in (0, 9):
        with pytest.raises(ValueError):
            with open_browser_identity('postgresql://unused', pool_max=bad):
                pass


def test_compose_and_budget_match_both_ways(compose):
    assert deploy_checks.compose_budget(BUDGET, compose) == {'checks': {'compose_matches_budget': True}, 'details': {'compose_budget': []}}
    cases = {
        'budget_entry_without_service:incentive-issuer': lambda c: c['services'].pop('incentive-issuer'),
        'process_count_differs:api': lambda c: c['services'].pop('api-2'),
        'unlabelled_service:extra': lambda c: c['services'].update(extra={'image': 'x'}),
        'service_not_in_budget:mystery': lambda c: c['services'].update(mystery={'labels': {'nexloop.budget-service': 'mystery'}}),
    }
    for code, change in cases.items():
        bad = copy.deepcopy(compose)
        change(bad)
        assert code in deploy_checks.compose_budget(BUDGET, bad)['details']['compose_budget'], code


def test_every_service_is_hardened_and_holds_only_its_own_secrets(compose):
    services = compose['services']
    owners = {}
    for name, spec in services.items():
        if name != 'app-net':
            assert spec['network_mode'] == 'service:app-net' and spec['read_only'] is True and spec['cap_drop'] == ['ALL'], name
            assert spec['security_opt'] == ['no-new-privileges:true'] and not spec.get('ports'), name
        for key in (spec.get('environment') or {}):
            assert not FORBIDDEN_ENV.match(key), (name, key)
        for secret in spec.get('secrets', []):
            owners.setdefault(secret, set()).add(name)
    shared = {s for s, o in owners.items() if len(o) > 1}
    assert shared <= SHARED | {'pg_ca'} | {s for s in owners if s.startswith('api_') and owners[s] == {'api-1', 'api-2'}}, shared
    assert set(services['agent-host']['secrets']) == {'host_cert', 'host_key', 'host_transport_key', 'guard_ca'}
    # Model credentials only where a model is called; channel provider configuration only in the action worker.
    assert owners['claim_extraction_worker_model'] == {'claim-extraction-worker'} and owners['claim_matcher_model'] == {'claim-matcher'}
    assert owners['action_worker_provider'] == {'action-worker'}
    # Each secret file lives in its own service's directory (shared/ for the four shared ones).
    for secret, spec in compose['secrets'].items():
        directory = spec['file'].split('/secrets/')[1].split('/')[0]
        assert directory == 'shared' if secret in SHARED else directory != 'shared', secret
    env = (STAGE / 'compose.app.env.example').read_text()
    assert not [l for l in env.splitlines() if l and not l.startswith('#') and FORBIDDEN_ENV.match(l.split('=', 1)[0])]


PARSERS = {name: (lambda svc: lambda argv: background_services._parser(svc).parse_args(argv))(name) for name in background_services.SERVICES}
PARSERS.update({'message-relay': message_relay_cli.arguments, 'outbound-recorder': outbound_messages._arguments, 'action-worker': effect_worker._arguments,
                'runtime-worker': runtime_worker._arguments})


def test_every_command_is_accepted_by_its_real_parser_and_names_only_its_own_secret_files(compose):
    services = compose['services']
    assert set(PARSERS) | {'api-1', 'api-2', 'domain-worker', 'gateway', 'agent-host', 'app-net'} == set(services)
    for name, parse in PARSERS.items():
        command = services[name]['command']
        parse(command)  # raises SystemExit when a required argument is missing
        for value in command:
            if value.startswith('/run/secrets/'):
                assert value.split('/run/secrets/')[1] in services[name]['secrets'], (name, value)
    api = services['api-1']['command']
    assert api[api.index('--identity-pool-max') + 1] == '4' and api[api.index('--mode') + 1] == 'stage'
    assert 'python -m nexloop_eios.domain_worker' in services['domain-worker']['command'][0]
