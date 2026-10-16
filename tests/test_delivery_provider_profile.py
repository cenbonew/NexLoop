"""Delivery CA (stage 2026-10-16): webchat-delivery computed its provider profile from the server certificate chain while the action
worker's route used the CA, so the delivery authority (0175) refused every delivery and each effect ended unknown. The deploy gate
compares both profiles (deploy_checks.delivery_provider_profile); the stage Compose and the secret generator give both sides the same
CA."""
import json
import os
from pathlib import Path

import yaml

from nexloop_eios import deploy_checks as D
from nexloop_eios.effect_provider import EffectProviderConfiguration, HttpEffectProvider, profile_digest_of, profile_identity

ROOT = Path(__file__).resolve().parents[1]
CA = '-----BEGIN CERTIFICATE-----\nsynthetic-ca\n-----END CERTIFICATE-----\n'
CHAIN = '-----BEGIN CERTIFICATE-----\nsynthetic-leaf\n-----END CERTIFICATE-----\n' + CA


def private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def stage(tmp_path, *, delivery_ca=CA, route_origin='https://127.0.0.1:9443', route=True):
    secrets = tmp_path / 'secrets'
    files = {
        'action_worker_provider_routes': private(secrets / 'action-worker/provider-routes', json.dumps(
            {'schema': 'nexloop-effect-provider-routes/1', 'routes': {'nexloop.service.request': '/run/secrets/action_worker_webchat_provider'} if route else {}})),
        'action_worker_webchat_provider': private(secrets / 'action-worker/webchat-provider-config', json.dumps(
            {'origin': route_origin, 'credential_file': '/run/secrets/action_worker_webchat_provider_credential',
             'ca_file': '/run/secrets/action_worker_webchat_provider_ca', 'timeout': 2})),
        'action_worker_webchat_provider_ca': private(secrets / 'action-worker/webchat-provider-ca.crt', CA),
        'webchat_delivery_provider_ca': private(secrets / 'webchat-delivery/provider-ca.crt', delivery_ca),
        'webchat_delivery_cert': private(secrets / 'webchat-delivery/server.crt', CHAIN),
    }
    return {'services': {'action-worker': {'command': ['--provider-routes-file', '/run/secrets/action_worker_provider_routes']},
                         'webchat-delivery': {'command': ['--origin', 'https://127.0.0.1:9443', '--provider-ca-file', '/run/secrets/webchat_delivery_provider_ca']}},
            'secrets': {name: {'file': path} for name, path in files.items()}}


UIDS = {'action-worker': os.geteuid(), 'webchat-delivery': os.geteuid()}


def test_the_same_ca_and_origin_pass():
    def run(tmp):
        return D.delivery_provider_profile(stage(tmp), UIDS)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        assert run(Path(tmp)) == {'checks': {'webchat_delivery_profile_matches_route': True}, 'details': {'webchat_delivery_profile': []}}


def test_the_stage_mistake_is_caught(tmp_path):
    """The server certificate chain as --provider-ca-file (the stage's configuration before the fix)."""
    compose = stage(tmp_path)
    compose['services']['webchat-delivery']['command'][-1] = '/run/secrets/webchat_delivery_cert'
    result = D.delivery_provider_profile(compose, UIDS)
    assert result['checks']['webchat_delivery_profile_matches_route'] is False and result['details']['webchat_delivery_profile'] == ['trust_digest_differs']


def test_other_differences_and_missing_inputs_are_named(tmp_path):
    assert D.delivery_provider_profile(stage(tmp_path / 'a', route_origin='https://127.0.0.1:9444'), UIDS)['details']['webchat_delivery_profile'] == ['origin_differs']
    assert D.delivery_provider_profile(stage(tmp_path / 'b', route=False), UIDS)['details']['webchat_delivery_profile'] == ['input_route_missing']
    other = {'action-worker': os.geteuid() + 1, 'webchat-delivery': os.geteuid()}
    assert D.delivery_provider_profile(stage(tmp_path / 'c'), other)['details']['webchat_delivery_profile'] == ['input_owner_differs']
    assert D.delivery_provider_profile({}, UIDS)['details']['webchat_delivery_profile'] == ['input_arguments_missing']


def test_the_identity_is_the_one_the_provider_computes(tmp_path):
    ca = private(tmp_path / 'ca.crt', CA)
    provider = HttpEffectProvider(EffectProviderConfiguration('https://127.0.0.1:9443', credential_file=str(tmp_path / 'c'), ca_file=ca))
    assert provider.profile_digest == profile_digest_of(profile_identity('https://127.0.0.1:9443', None, CA.strip()))  # read_private_text strips


def test_the_stage_compose_gives_webchat_delivery_its_own_ca_copy():
    doc = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
    command = [str(c) for c in doc['services']['webchat-delivery']['command']]
    assert command[command.index('--provider-ca-file') + 1] == '/run/secrets/webchat_delivery_provider_ca'
    assert 'webchat_delivery_provider_ca' in doc['services']['webchat-delivery']['secrets']
    assert doc['secrets']['webchat_delivery_provider_ca']['file'].endswith('/secrets/webchat-delivery/provider-ca.crt')
