"""NX-032 (L5 review 2026-10-11): the APP_HOST stage Compose example orchestrates every process of the connection budget, each with
its own secret files, the common hardening and a command its real argument parser accepts; the API's browser identity pool is a
budget entry; doctor --deploy-gate compares Compose and budget both ways. No container is started."""
import copy
import json
import os
import re
import shutil
import sys
import subprocess

import pytest
import yaml

from nexloop_eios.local_json_delivery import arguments as local_json_delivery_arguments
from nexloop_eios import background_services, connection_budget, deploy_checks, effect_worker, message_relay_cli, outbound_messages, runtime_worker
from nexloop_eios.browser_identity import open_browser_identity
from test_deploy_render import ROOT, STAGE, interpolate

sys.path.insert(0, str(ROOT / "scripts/deploy"))

BUDGET = json.loads((STAGE / 'connection-budget.v1.json').read_text())
SHARED = {'signing_key', 'pg_ca', 'host_ca', 'host_control_key'}
FORBIDDEN_ENV = re.compile(r'^(MODEL_API_KEY|EMBEDDING_API_KEY|NEXLOOP_DATABASE_URL|MODEL_PROVIDER|NEXLOOP_TEST_.*|.*PASSWORD.*|.*_DSN|DATABASE_URL)$')


@pytest.fixture(scope='module')
def compose():
    return yaml.safe_load(interpolate((STAGE / 'compose.app.example.yaml').read_text(), STAGE / 'compose.app.env.example'))


def test_budget_registers_the_identity_pool_and_stays_within_60():
    result = connection_budget.evaluate(BUDGET)
    assert result['passed'] and result['details']['application_pool_total'] == 59  # NX-037: + webchat-delivery 2
    assert result['details']['services']['api-browser-identity'] == 6 and 'scheduler' not in result['details']['services']
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
        if name not in ('app-net', 'agent-host', 'agent-link-relay', 'action-worker', 'webchat-delivery', 'claim-extraction-worker',
                        'claim-matcher'):
            assert spec['network_mode'] == 'service:app-net', name
        if name in ('claim-extraction-worker', 'claim-matcher'):
            # G-CLAIM-EGRESS: their own egress network (PG and the model allowlist only), never the application namespace.
            assert 'network_mode' not in spec and list(spec['networks']) == ['claim_egress'], name
        if name != 'app-net':
            assert spec['read_only'] is True and spec['cap_drop'] == ['ALL'], name
            assert spec['security_opt'] == ['no-new-privileges:true'] and not spec.get('ports'), name
        for key in (spec.get('environment') or {}):
            assert not FORBIDDEN_ENV.match(key), (name, key)
        for secret in spec.get('secrets', []):
            owners.setdefault(secret, set()).add(name)
    shared = {s for s, o in owners.items() if len(o) > 1}
    # The two API processes share their files, and so do the two relays (dispatcher 2026-10-13: same configuration, own vaults).
    assert shared <= SHARED | {'pg_ca'} | {s for s in owners if s.startswith('api_') and owners[s] == {'api-1', 'api-2'}} \
        | {s for s in owners if s.startswith('message_relay_') and owners[s] == {'message-relay', 'message-relay-2'}}, shared
    assert set(services['agent-host']['secrets']) == {'host_cert', 'host_key', 'host_transport_key', 'guard_ca', 'host_guard_key', 'host_runtime_config', 'host_model_configuration', 'host_model_tariff', 'host_model_key'}
    # Model credentials only where a model is called; channel provider configuration only in the action worker.
    assert owners['claim_extraction_worker_model'] == {'claim-extraction-worker'} and owners['claim_matcher_model'] == {'claim-matcher'}
    # NX-037: every provider route's configuration, credential and CA only in the action worker (the enterprise credential
    # exists nowhere else); the delivery service holds its own files.
    assert {s for s, o in owners.items() if s.startswith('action_worker_') and 'provider' in s and o == {'action-worker'}} == {
        'action_worker_provider_routes', 'action_worker_webchat_provider', 'action_worker_webchat_provider_credential', 'action_worker_webchat_provider_ca',
        'action_worker_enterprise_provider', 'action_worker_enterprise_provider_credential', 'action_worker_enterprise_provider_ca'}
    assert not any('enterprise' in s for s in services['webchat-delivery']['secrets'])
    # Each secret file lives in its own service's directory (shared/ for the four shared ones).
    for secret, spec in compose['secrets'].items():
        directory = spec['file'].split('/secrets/')[1].split('/')[0]
        assert directory == 'shared' if secret in SHARED else directory != 'shared', secret
    env = (STAGE / 'compose.app.env.example').read_text()
    assert not [l for l in env.splitlines() if l and not l.startswith('#') and FORBIDDEN_ENV.match(l.split('=', 1)[0])]


PARSERS = {name: (lambda svc: lambda argv: background_services._parser(svc).parse_args(argv))(name) for name in background_services.SERVICES}
PARSERS.update({'message-relay': message_relay_cli.arguments, 'message-relay-2': message_relay_cli.arguments, 'outbound-recorder': outbound_messages._arguments, 'action-worker': effect_worker._arguments,
                'webchat-delivery': local_json_delivery_arguments,
                'runtime-worker': runtime_worker._arguments})


def test_every_command_is_accepted_by_its_real_parser_and_names_only_its_own_secret_files(compose):
    services = compose['services']
    assert set(PARSERS) | {'api-1', 'api-2', 'domain-worker', 'gateway', 'agent-host', 'app-net', 'app-link-relay', 'agent-link-relay'} == set(services)
    for name, parse in PARSERS.items():
        command = services[name]['command']
        parse(command)  # raises SystemExit when a required argument is missing
        for value in command:
            if value.startswith('/run/secrets/'):
                assert value.split('/run/secrets/')[1] in services[name]['secrets'], (name, value)
    api = services['api-1']['command']
    assert api[api.index('--identity-pool-max') + 1] == '3' and api[api.index('--mode') + 1] == 'stage'
    assert 'python -m nexloop_eios.domain_worker' in services['domain-worker']['command'][0]


def test_agent_host_has_its_own_namespace_and_only_two_relayed_links(compose):
    """NX-037 D4: the Agent Host is not in the application namespace; the agent link is internal; exactly two relay rules per side."""
    services = compose['services']
    host = services['agent-host']
    assert 'network_mode' not in host and set(host['networks']) == {'agent_egress', 'agent_link'}
    assert set(services['app-net']['networks']) == {'app', 'agent_link'} and compose['networks']['agent_link']['internal'] is True
    assert services['agent-link-relay']['network_mode'] == 'service:agent-host'
    from nexloop_eios.loopback_relay import parse_rule
    for name in ('app-link-relay', 'agent-link-relay'):
        command = services[name]['command']
        rules = [parse_rule(command[i + 1]) for i, a in enumerate(command) if a == '--rule']
        assert len(rules) == 2, name
    app_rules = [parse_rule(r) for r in services['app-link-relay']['command'][1::2]]
    assert {r[0][1] for r in app_rules} == {8100, 8200}  # Host control out, guard in; nothing else crosses


def test_the_relay_carries_end_to_end_tls_with_the_loopback_certificate(tmp_path):
    """Two relays in a chain (as across the two namespaces); the client still verifies the server's 127.0.0.1 certificate."""
    import asyncio, socket, ssl, threading, sys
    sys.path.insert(0, str(ROOT / 'scripts/deploy'))
    import issue_certs
    from nexloop_eios import loopback_relay
    issue_certs.root(tmp_path / 'root')
    issue_certs.intermediate(tmp_path / 'root', tmp_path / 'ca')
    issue_certs.leaf('server', tmp_path / 'ca', tmp_path / 'srv', 'host-loopback', sans=['IP:127.0.0.1'])
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / 'srv/host-loopback.chain.crt', tmp_path / 'srv/host-loopback.key')
    def free():
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]
    target, middle, front = free(), free(), free()
    listener = socket.socket(); listener.bind(('127.0.0.1', target)); listener.listen()
    def serve():
        conn, _ = listener.accept()
        with server_ctx.wrap_socket(conn, server_side=True) as tls:
            tls.sendall(tls.recv(64)[::-1])
    threading.Thread(target=serve, daemon=True).start()
    ready = threading.Event()
    rules = [loopback_relay.parse_rule(f'127.0.0.1:{front}=127.0.0.1:{middle}'), loopback_relay.parse_rule(f'127.0.0.1:{middle}=127.0.0.1:{target}')]
    threading.Thread(target=lambda: asyncio.run(loopback_relay.serve(rules, ready)), daemon=True).start()
    assert ready.wait(5)
    client_ctx = ssl.create_default_context(cafile=str(tmp_path / 'root/ca.crt'))
    with socket.create_connection(('127.0.0.1', front), timeout=5) as raw, client_ctx.wrap_socket(raw, server_hostname='127.0.0.1') as tls:
        tls.sendall(b'abc')
        assert tls.recv(64) == b'cba'
    listener.close()
    for bad in ('127.0.0.1:1', 'host:1=127.0.0.1:2', '127.0.0.1:0=127.0.0.1:2'):
        with pytest.raises(ValueError):
            loopback_relay.parse_rule(bad)


def test_every_context_produced_for_the_host_has_the_host_protocol(compose):
    """A message Run Context is built by the relay (v6 with --context-strategy, else v2) and by the reply guarantor's fallback
    (v6, its policy's strategy); the Host refuses any other protocol than its configured context_input_protocol."""
    host = json.loads((STAGE / 'agent-host.runtime-config.example.json').read_text())['context_input_protocol']
    strategies = {s['strategy_id']: s['protocol'] for s in json.loads((ROOT / 'deploy/configuration/context-strategies.v1.json').read_text())['strategies']}

    def option(command, name):
        return command[command.index(name) + 1] if name in command else None
    relay = compose['services']['message-relay']['command']
    strategy = option(relay, '--context-strategy')
    assert (strategies[strategy] if strategy else 'nexloop.context-pack.v2') == host
    policies = sorted((ROOT / 'deploy/configuration').glob('reply-guarantee.v*.json'), key=lambda p: int(p.stem.rsplit('.v', 1)[1]))
    fallback = json.loads(policies[-1].read_text())['fallback']['context_strategy']
    assert option(compose['services']['reply-guarantor']['command'], '--policy-file') and strategies[fallback] == host



def test_only_the_gateway_gets_one_capability_back(compose):
    """2026-10-15 first start: the caddy binary's file capability needs NET_BIND_SERVICE in the bounding set to exec at all."""
    added = {name: spec.get('cap_add') for name, spec in compose['services'].items() if spec.get('cap_add')}
    assert added == {'gateway': ['NET_BIND_SERVICE']}


def test_the_real_dispatch_switch_is_one_compose_variable_closed_by_default_and_only_true_or_false():
    """Calibration gap 1 (2026-10-17): the deployment layer REAL_DISPATCH_ENABLED comes from the private compose env, default false;
    the x-service anchor and the Agent Host take the same variable and nothing else sets it; app-net extends real-dispatch.<value>.yaml,
    so a value other than true or false fails `docker compose config`."""
    text = (STAGE / 'compose.app.example.yaml').read_text()
    raw = yaml.safe_load(text)
    reference = '${REAL_DISPATCH_ENABLED:-false}'
    setters = sorted(name for name, s in raw['services'].items() if 'REAL_DISPATCH_ENABLED' in (s.get('environment') or {}))
    assert raw['x-service']['environment'] == {'REAL_DISPATCH_ENABLED': reference}
    assert raw['services']['agent-host']['environment'] == {'REAL_DISPATCH_ENABLED': reference}
    # Every other service carries it only through the anchor (YAML merge gives the same mapping).
    assert all(raw['services'][n]['environment']['REAL_DISPATCH_ENABLED'] == reference for n in setters)
    assert len(re.findall(r'^\s+REAL_DISPATCH_ENABLED:\s', text, re.M)) == 2 and text.count(reference) == 3
    assert raw['services']['app-net']['extends'] == {'file': 'real-dispatch.${REAL_DISPATCH_ENABLED:-false}.yaml', 'service': 'real-dispatch'}
    assert sorted(p.name for p in STAGE.glob('real-dispatch.*.yaml')) == ['real-dispatch.false.yaml', 'real-dispatch.true.yaml']
    env = STAGE / 'compose.app.env.example'
    assert 'REAL_DISPATCH_ENABLED=false' in env.read_text().splitlines()
    for overrides, want in (({}, 'false'), ({'REAL_DISPATCH_ENABLED': ''}, 'false'), ({'REAL_DISPATCH_ENABLED': 'true'}, 'true')):
        rendered = yaml.safe_load(interpolate(text, env, overrides))
        values = {s['environment']['REAL_DISPATCH_ENABLED'] for s in rendered['services'].values() if s.get('environment')}
        assert values == {want} and rendered['services']['app-net']['extends']['file'] == f'real-dispatch.{want}.yaml'
    docker = shutil.which('docker')
    if docker:
        def config(value):
            return subprocess.run([docker, 'compose', '-f', 'compose.app.example.yaml', '--env-file', 'compose.app.env.example', 'config', '--format', 'json'],
                                  cwd=STAGE, capture_output=True, text=True, timeout=60, env={**os.environ, 'REAL_DISPATCH_ENABLED': value})
        for value in ('false', 'true'):
            run = config(value)
            assert run.returncode == 0, run.stderr[-2000:]
            services = json.loads(run.stdout)['services']
            assert {n for n, s in services.items() if (s.get('environment') or {}).get('REAL_DISPATCH_ENABLED') == value} == set(services) - {'app-net'}
        for value in ('yes', '1', 'open'):
            assert config(value).returncode != 0, value


def test_the_calibration_run_entry_form_gets_what_it_reads_from_the_action_worker():
    """Calibration gap 2 (run-calibration-20261017 §2.1): the run entry runs in a one-off container with --network container:<action
    worker>, --volumes-from <action worker>:ro and --user 10001. That form holds only if the rendered Compose gives the action worker
    every file the run entry reads by container path (both provider files and the credential and CA files they name, as file
    secrets, i.e. bind mounts that --volumes-from carries), the action worker's files belong to 10001, the WebChat delivery whose
    health the guard GETs on 127.0.0.1 shares the action worker's namespace, and the switch variable is in its environment."""
    import provider_routes as P
    rendered = yaml.safe_load(interpolate((STAGE / 'compose.app.example.yaml').read_text(), STAGE / 'compose.app.env.example'))
    worker = rendered['services']['action-worker']
    mounted = set(worker['secrets'])
    for config_secret, credential, ca in P.CREDENTIALS.values():
        assert {config_secret, credential, ca} <= mounted
        assert all('file' in rendered['secrets'][name] for name in (config_secret, credential, ca))  # file secrets: bind mounts
    assert set(P.FILES) <= mounted
    assert rendered['services']['webchat-delivery']['network_mode'] == 'service:action-worker'
    assert worker['environment']['REAL_DISPATCH_ENABLED'] == 'false'
    spec = json.loads((STAGE / 'deploy-gate.app.example.json').read_text())
    assert spec['secret_uids']['action-worker'] == 10001 and 'user' not in worker  # the backend image's service user, 10001
    # The run entry's --webchat-provider-file and --enterprise-provider-file are these container paths.
    assert {f'/run/secrets/{name}' for name in ('action_worker_webchat_provider', 'action_worker_enterprise_provider')} <= {f'/run/secrets/{n}' for n in mounted}
