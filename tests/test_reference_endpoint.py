"""NX-037: the reference enterprise delivery endpoint (an external stand-in, not NexLoop). Local TLS only, synthetic data."""
import importlib.util
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import ssl

import httpx
import pytest
from nexloop_eios.effect_provider import (EffectProviderConfiguration, EffectProviderQueryUnavailable, EffectProviderUnknown,
                                          HttpEffectProvider)

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


endpoint_module = load(ROOT / 'apps/reference-endpoint/reference_endpoint.py', 'reference_endpoint')
conformance = load(ROOT / 'scripts/nx037/endpoint_conformance.py', 'endpoint_conformance')


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


@contextmanager
def running(tmp_path):
    cert, key = tmp_path / 'endpoint-cert.pem', tmp_path / 'endpoint-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    cert.chmod(0o600)
    key.chmod(0o600)
    service, staff = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    data = tmp_path / 'endpoint-data'
    server = endpoint_module.make_server(data_dir=data, listen='127.0.0.1:0', service_token=service, staff_token=staff,
                                        tls_cert_file=str(cert), tls_key_file=str(key))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = 'https://127.0.0.1:' + str(server.server_address[1])
    credential = private(tmp_path / 'service-credential', service)
    try:
        yield {'origin': origin, 'cert': cert, 'service': service, 'staff': staff, 'credential': credential, 'data': data, 'server': server}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def client(e, timeout=2.0):
    return HttpEffectProvider(EffectProviderConfiguration(e['origin'], credential_file=str(e['credential']), ca_file=str(e['cert']),
                                                          timeout=timeout))


def http(e):
    return httpx.Client(base_url=e['origin'], verify=ssl.create_default_context(cafile=str(e['cert'])), trust_env=False, timeout=10, follow_redirects=False)


def request(parameters=None):
    parameters = {'delivery': 'synthetic'} if parameters is None else parameters
    return {'intent_id': str(uuid.uuid4()), 'payload_digest': endpoint_module.digest(parameters), 'parameters': parameters}


def bearer(e):
    return {'Authorization': 'Bearer ' + e['service']}


def test_conformance_check_passes_every_item(tmp_path, capsys):
    with running(tmp_path) as e:
        report = conformance.run(EffectProviderConfiguration(e['origin'], credential_file=str(e['credential']), ca_file=str(e['cert'])))
    assert report['ok'] is True, [c for c in report['checks'] if not c['ok']]
    assert len(report['checks']) == 7
    output = capsys.readouterr()
    assert e['service'] not in output.out + output.err


def test_idempotent_replay_conflict_and_not_found(tmp_path):
    with running(tmp_path) as e, http(e) as h:
        body = request()
        first = h.post('/v1/effects', json=body, headers={**bearer(e), 'Idempotency-Key': body['intent_id']})
        assert first.status_code == 202 and first.json()['state'] == 'accepted' and first.json()['payload_digest'] == body['payload_digest']
        replay = h.post('/v1/effects', json=body, headers={**bearer(e), 'Idempotency-Key': body['intent_id']})
        assert replay.status_code == 200 and replay.json() == first.json()
        other = request({'delivery': 'different'})
        conflict = {**other, 'intent_id': body['intent_id']}
        refused = h.post('/v1/effects', json=conflict, headers={**bearer(e), 'Idempotency-Key': body['intent_id']})
        assert refused.status_code == 409 and refused.json() == first.json()
        assert h.get('/v1/effects/' + body['intent_id'], headers=bearer(e)).json() == first.json()
        absent = str(uuid.uuid4())
        missing = h.get('/v1/effects/' + absent, headers=bearer(e))
        assert missing.status_code == 404 and missing.json() == {'intent_id': absent, 'payload_digest': None, 'state': 'not_found',
                                                                  'provider_reference': None}
        # Key must equal the intent; digest must match the parameters.
        wrong_key = h.post('/v1/effects', json=request(), headers={**bearer(e), 'Idempotency-Key': str(uuid.uuid4())})
        bad = request()
        bad['payload_digest'] = '0' * 64
        wrong_digest = h.post('/v1/effects', json=bad, headers={**bearer(e), 'Idempotency-Key': bad['intent_id']})
        assert wrong_key.status_code == 400 and wrong_digest.status_code == 400
    with endpoint_module.Store(e['data']).connect() as db:
        assert db.execute('select count(*) from effects').fetchone()[0] == 1
        assert db.execute('pragma journal_mode').fetchone()[0] == 'wal'


def test_service_authentication_is_required(tmp_path):
    with running(tmp_path) as e, http(e) as h:
        body = request()
        for headers in ({}, {'Authorization': 'Bearer ' + secrets.token_urlsafe(32)}, {'Authorization': 'Bearer ' + e['staff']}):
            assert h.post('/v1/effects', json=body, headers={**headers, 'Idempotency-Key': body['intent_id']}).status_code == 401
            assert h.get('/v1/effects/' + body['intent_id'], headers=headers).status_code == 401
        assert endpoint_module.Store(e['data']).get(body['intent_id']) is None


def staff_session(e, h):
    login = h.post('/staff/login', data={'token': e['staff']})
    assert login.status_code == 303 and 'HttpOnly' in login.headers['set-cookie'] and 'Secure' in login.headers['set-cookie']
    page = h.get('/staff')
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    return page, csrf.group(1) if csrf else None


def test_staff_marks_are_what_get_returns(tmp_path):
    with running(tmp_path) as e, http(e) as h:
        handled, failed = request(), request({'delivery': 'second'})
        provider = client(e)
        for body in (handled, failed):
            assert provider.dispatch(**body).state == 'accepted'
        assert h.post('/staff/login', data={'token': e['service']}).status_code == 401
        assert h.post('/staff/mark', data={'csrf': 'x', 'intent_id': handled['intent_id'], 'state': 'fulfilled'}).status_code == 401
        page, csrf = staff_session(e, h)
        assert handled['intent_id'] in page.text and '待处理' in page.text
        assert h.post('/staff/mark', data={'csrf': 'wrong', 'intent_id': handled['intent_id'], 'state': 'fulfilled'}).status_code == 403
        assert h.post('/staff/mark', data={'csrf': csrf, 'intent_id': handled['intent_id'], 'state': 'fulfilled'}).status_code == 303
        assert h.post('/staff/mark', data={'csrf': csrf, 'intent_id': failed['intent_id'], 'state': 'failed'}).status_code == 303
        assert provider.query(intent_id=handled['intent_id'], payload_digest=handled['payload_digest']).state == 'fulfilled'
        assert h.get('/v1/effects/' + failed['intent_id'], headers=bearer(e)).json()['state'] == 'failed'
        # The current NexLoop client does not know `failed`: it reports the query as unavailable, never as fulfilled.
        with pytest.raises(EffectProviderQueryUnavailable):
            provider.query(intent_id=failed['intent_id'], payload_digest=failed['payload_digest'])
        # A mark is final.
        h.post('/staff/mark', data={'csrf': csrf, 'intent_id': failed['intent_id'], 'state': 'fulfilled'})
        assert h.get('/v1/effects/' + failed['intent_id'], headers=bearer(e)).json()['state'] == 'failed'


def test_drill_delayed_post_is_persisted_and_unknown_then_the_query_finds_it(tmp_path):
    with running(tmp_path) as e:
        provider = client(e)
        state = endpoint_module.drill(e['data'], delay_next_posts=1, delay_seconds=3.0)
        assert state['delay_posts'] == 1
        body = request()
        started = time.monotonic()
        with pytest.raises(EffectProviderUnknown):
            provider.dispatch(**body)
        assert time.monotonic() - started < 2.6
        stored = endpoint_module.Store(e['data']).get(body['intent_id'])
        assert stored is not None and stored['state'] == 'accepted'
        assert provider.query(intent_id=body['intent_id'], payload_digest=body['payload_digest']).provider_reference == stored['provider_reference']
        # The drill was used once; the next new request is answered at once.
        assert provider.dispatch(**request({'delivery': 'after'})).state == 'accepted'


def test_drill_get_unavailable_window(tmp_path):
    with running(tmp_path) as e:
        provider = client(e)
        body = request()
        provider.dispatch(**body)
        endpoint_module.drill(e['data'], get_unavailable_seconds=1)
        with pytest.raises(EffectProviderQueryUnavailable):
            provider.query(intent_id=body['intent_id'], payload_digest=body['payload_digest'])
        time.sleep(1.2)
        assert provider.query(intent_id=body['intent_id'], payload_digest=body['payload_digest']).state == 'accepted'


def test_drill_only_from_the_local_cli(tmp_path, capsys):
    with running(tmp_path) as e, http(e) as h:
        for path in ('/drill', '/v1/drill', '/staff/drill'):
            assert h.post(path, json={'delay_next_posts': 1}, headers=bearer(e)).status_code == 404
        assert endpoint_module.main(['drill', '--data-dir', str(e['data']), '--delay-next-posts', '2']) == 0
        assert json.loads(capsys.readouterr().out)['delay_posts'] == 2
        assert endpoint_module.main(['drill', '--data-dir', str(e['data']), '--clear']) == 0
        capsys.readouterr()
        control = e['data'] / 'drill-control.json'
        assert control.stat().st_mode & 0o777 == 0o600
        control.chmod(0o644)
        assert endpoint_module.main(['drill', '--data-dir', str(e['data']), '--status']) == 2
        with pytest.raises(ValueError):
            control.chmod(0o600)
            endpoint_module.drill(e['data'], delay_next_posts=11)


def test_cli_refuses_shared_tokens_and_non_private_files(tmp_path):
    same = secrets.token_urlsafe(32)
    a, b = private(tmp_path / 'a', same), private(tmp_path / 'b', same)
    key = private(tmp_path / 'key', 'unused')
    args = ['serve', '--data-dir', str(tmp_path / 'd'), '--listen', '127.0.0.1:0', '--tls-cert-file', str(key), '--tls-key-file', str(key),
            '--service-token-file', str(a), '--staff-token-file', str(b)]
    assert endpoint_module.main(args) == 2
    loose = tmp_path / 'loose'
    loose.write_text(secrets.token_urlsafe(32))
    loose.chmod(0o644)
    assert endpoint_module.main(args[:-4] + ['--service-token-file', str(loose), '--staff-token-file', str(b)]) == 2


def test_deployment_template_is_parameterised_and_non_root():
    compose = (ROOT / 'deploy/stage/reference-endpoint/compose.example.yaml').read_text()
    dockerfile = (ROOT / 'apps/reference-endpoint/Dockerfile').read_text()
    env = (ROOT / 'deploy/stage/reference-endpoint/compose.env.example').read_text()
    assert 'user: "10002:10002"' in compose and 'read_only: true' in compose and 'cap_drop: [ALL]' in compose
    assert 'no-new-privileges:true' in compose and 'nexloop' not in compose.split('services:')[1].split('volumes:')[0].replace('NexLoop', '')
    assert 'USER 10002:10002' in dockerfile and 'pip install' not in dockerfile
    for line in env.splitlines():
        if line and not line.startswith('#'):
            assert re.fullmatch(r'[A-Z_]+=<[A-Z_]+>', line), line
    assert not re.search(r'\b\d{1,3}(\.\d{1,3}){3}\b', compose + env)
