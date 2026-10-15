"""G-ROUTES: scripts/deploy/provider_routes.py writes files the effect worker's own parsers accept (effect_worker._routes and
_provider), routes only published effect Actions, adds the enterprise route only when the endpoint is given, and refuses bad
input. The container paths (/run/secrets/...) are rewritten to a temporary directory holding synthetic private files, the
way the worker would see them inside the container."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from nexloop_eios import effect_worker

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provider_routes', ROOT / 'scripts/deploy/provider_routes.py')
routes_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes_tool)


def documentation_address():
    return '.'.join(str(octet) for octet in (192, 0, 2, 40))  # TEST-NET-1, built at runtime


@pytest.fixture
def secrets(tmp_path):
    """Synthetic credential and CA files under the names the stage Compose mounts."""
    root = tmp_path / 'run-secrets'
    root.mkdir(mode=0o700)
    key, cert = tmp_path / 'ca.key', tmp_path / 'ca.crt'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-keyout', str(key),
                    '-out', str(cert), '-days', '1', '-subj', '/CN=synthetic-ca'], check=True, capture_output=True)
    for _, credential, ca in routes_tool.CREDENTIALS.values():
        (root / credential).write_text('synthetic-provider-credential-0123456789')
        (root / ca).write_text(cert.read_text())
        (root / credential).chmod(0o600)
        (root / ca).chmod(0o600)
    return root


def materialize(files, root):
    """Write the generated bodies as the Compose secrets, with /run/secrets/ mapped to root."""
    source = {name: secret for secret, name in routes_tool.FILES.items()}
    for name, body in files.items():
        text = json.dumps(body).replace(routes_tool.SECRETS + '/', str(root) + '/')
        (root / source[name]).write_text(text)
        (root / source[name]).chmod(0o600)
    return root / 'action_worker_provider_routes'


def test_default_generation_routes_only_webchat_and_the_worker_accepts_it(secrets):
    files = routes_tool.build(webchat_delivery_port=18443)
    assert set(files) == {'provider-routes', 'webchat-provider-config'}
    assert files['webchat-provider-config'] == {'origin': 'https://127.0.0.1:18443', 'credential_file': '/run/secrets/action_worker_webchat_provider_credential',
                                                'ca_file': '/run/secrets/action_worker_webchat_provider_ca', 'timeout': 2.0}
    routes = effect_worker._routes(materialize(files, secrets))
    assert set(routes.routes) == {'nexloop.service.request'} and routes.for_action('nexloop.enterprise.deliver') is None


def test_the_enterprise_route_is_added_only_with_its_endpoint_and_the_worker_accepts_both(secrets):
    files = routes_tool.build(webchat_delivery_port=18443, enterprise_origin='https://endpoint.example.invalid:8443',
                              enterprise_connect_address=documentation_address())
    assert set(files['provider-routes']['routes']) == {'nexloop.service.request', 'nexloop.enterprise.deliver'}
    assert files['enterprise-provider-config']['connect_address'] == documentation_address()
    routes = effect_worker._routes(materialize(files, secrets))  # raises on anything the worker would refuse
    assert set(routes.routes) == {'nexloop.service.request', 'nexloop.enterprise.deliver'}
    for name in ('webchat-provider-config', 'enterprise-provider-config'):
        effect_worker._provider(secrets / {v: k for k, v in routes_tool.FILES.items()}[name])


def test_routed_actions_come_from_the_published_effect_actions(tmp_path):
    published = routes_tool.effect_actions()
    assert published == {'nexloop.service.request', 'nexloop.enterprise.deliver'}
    body = json.loads(routes_tool.SYSTEM_ACTIONS.read_text())
    body['actions'] = [a for a in body['actions'] if a['stable_name'] != 'nexloop.enterprise.deliver']
    reduced = tmp_path / 'system-actions.json'
    reduced.write_text(json.dumps(body))
    with pytest.raises(routes_tool.Refused, match='not a published effect Action'):
        routes_tool.build(webchat_delivery_port=18443, enterprise_origin='https://endpoint.example.invalid', enterprise_connect_address=documentation_address(),
                          system_actions=reduced)
    body['actions'] = [a for a in body['actions'] if a['stable_name'] != 'nexloop.service.request']
    reduced.write_text(json.dumps(body))
    with pytest.raises(routes_tool.Refused):
        routes_tool.build(webchat_delivery_port=18443, system_actions=reduced)


@pytest.mark.parametrize('change', [
    {'webchat_delivery_port': 80}, {'webchat_delivery_port': 70000}, {'webchat_delivery_port': '18443'},
    {'enterprise_origin': 'https://endpoint.example.invalid'},                                   # address missing
    {'enterprise_connect_address': 'ADDR'},                                                      # origin missing
    {'enterprise_origin': 'http://endpoint.example.invalid', 'enterprise_connect_address': 'ADDR'},
    {'enterprise_origin': 'https://endpoint.example.invalid', 'enterprise_connect_address': 'endpoint.example.invalid'},
    {'enterprise_origin': 'https://user:pw@endpoint.example.invalid', 'enterprise_connect_address': 'ADDR'},
    {'enterprise_origin': 'https://endpoint.example.invalid/path', 'enterprise_connect_address': 'ADDR'},
    {'timeout': 0}, {'timeout': 31},
])
def test_bad_input_is_refused(change):
    arguments = {'webchat_delivery_port': 18443, **{k: (documentation_address() if v == 'ADDR' else v) for k, v in change.items()}}
    with pytest.raises(routes_tool.Refused):
        routes_tool.build(**arguments)


def test_cli_writes_new_private_files_and_refuses_an_existing_directory(tmp_path, capsys):
    out = tmp_path / 'action-worker'
    assert routes_tool.main(['--out-dir', str(out), '--webchat-delivery-port', '18443']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {'ok': True, 'files': ['provider-routes', 'webchat-provider-config'], 'routes': ['nexloop.service.request']}
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in out.iterdir()) and (out.stat().st_mode & 0o777) == 0o700
    assert routes_tool.main(['--out-dir', str(out), '--webchat-delivery-port', '18443']) == 2
    assert json.loads(capsys.readouterr().out) == {'ok': False, 'refused': 'output directory exists'}
    assert routes_tool.main(['--out-dir', str(tmp_path / 'other'), '--webchat-delivery-port', '18443', '--enterprise-origin', 'https://e.example.invalid']) == 2
    assert not (tmp_path / 'other').exists()


def test_placeholders_satisfy_the_compose_mounts_without_being_routed(tmp_path, capsys):
    out = tmp_path / 'action-worker'
    assert routes_tool.main(['--out-dir', str(out), '--webchat-delivery-port', '18443', '--enterprise-placeholders']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['routes'] == ['nexloop.service.request'] and result['placeholders'] == ['enterprise-provider-config', 'enterprise-provider-credential']
    assert json.loads((out / 'enterprise-provider-config').read_text()) == {}
    credential = (out / 'enterprise-provider-credential').read_text().strip()
    assert len(credential) == 43 and credential.replace('-', '').replace('_', '').isalnum()
    assert not (out / 'enterprise-provider-ca.crt').exists()  # the CA is the caller's
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in out.iterdir())
    assert set(json.loads((out / 'provider-routes').read_text())['routes']) == {'nexloop.service.request'}
    # Two runs never share a placeholder credential.
    assert routes_tool.main(['--out-dir', str(tmp_path / 'again'), '--webchat-delivery-port', '18443', '--enterprise-placeholders']) == 0
    assert (tmp_path / 'again' / 'enterprise-provider-credential').read_text().strip() != credential
    # With a real endpoint placeholders are refused and nothing is written.
    assert routes_tool.main(['--out-dir', str(tmp_path / 'both'), '--webchat-delivery-port', '18443', '--enterprise-placeholders',
                             '--enterprise-origin', 'https://e.example.invalid', '--enterprise-connect-address', documentation_address()]) == 2
    assert not (tmp_path / 'both').exists()
