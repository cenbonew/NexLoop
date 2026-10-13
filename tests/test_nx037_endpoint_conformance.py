"""NX-037 gap D: the enterprise endpoint conformance check and the "result temporarily unknown" drill relay.

Synthetic endpoints only (tests/support/effect_provider.py and local servers below); no real endpoint, credential or network.
"""
import asyncio
import importlib.util
import json
import secrets
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from nexloop_eios.effect_provider import (EffectProviderConfiguration, EffectProviderQueryUnavailable, EffectProviderUnknown,
                                          HttpEffectProvider)
from support.effect_provider import effect_provider, payload_digest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/nx037' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conformance = load('endpoint_conformance')
drill = load('unknown_drill_relay')


def loopback(origin, timeout=2.0):
    return EffectProviderConfiguration(origin, timeout=timeout, test_loopback_http=True)


def failed(report):
    return {c['name']: c['code'] for c in report['checks'] if not c['ok']}


# ---- conformance: the synthetic endpoint passes every check ---------------------------------------------------------------

def test_conforming_endpoint_passes_every_check(tmp_path):
    with effect_provider(tmp_path / 'endpoint.sqlite') as endpoint:
        report = conformance.run(loopback(endpoint.origin))
        snapshot = endpoint.control('snapshot')
    assert report['ok'] is True, failed(report)
    assert [c['name'] for c in report['checks']] == ['post_new', 'post_replay', 'post_conflict', 'get_found', 'get_not_found',
                                                    'client_end_to_end']
    assert all(c['elapsed_ms'] is not None and c['elapsed_ms'] < 2000 for c in report['checks'])
    # One effect for the replayed intent (idempotent, conflict kept the original) and one for the client check.
    assert snapshot['effects'] == 2
    methods = [request[0] + str(request[2]) for request in snapshot['requests']]
    assert methods == ['POST202', 'POST202', 'POST409', 'GET200', 'GET404', 'POST202', 'GET200']


# ---- conformance: each deviation is named ----------------------------------------------------------------------------------

@contextmanager
def deviant(mode, delay=0.0):
    effects = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type='application/json'):
            content = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if delay:
                time.sleep(delay)
            intent, digest = body['intent_id'], body['payload_digest']
            with lock:
                if intent in effects and effects[intent]['payload_digest'] != digest:
                    if mode == 'overwrite_conflict':
                        effects[intent] = {'payload_digest': digest, 'reference': 'ref-' + secrets.token_hex(4)}
                        return self.send(202, receipt(intent, effects[intent]))
                    return self.send(409, receipt(intent, effects[intent]))
                if intent not in effects or mode == 'non_idempotent':
                    effects[intent] = {'payload_digest': digest, 'reference': 'ref-' + secrets.token_hex(4)}
            value = receipt(intent, effects[intent])
            if mode == 'no_echo':
                value['payload_digest'] = '0' * 64
            if mode == 'extra_field':
                value['note'] = 'x'
            self.send(201 if mode == 'status201' else 202, value, 'text/plain' if mode == 'not_json' else 'application/json')

        def do_GET(self):
            intent = self.path.rsplit('/', 1)[-1]
            with lock:
                found = effects.get(intent)
            if found is None:
                if mode == 'absent_200':
                    return self.send(200, {'intent_id': intent, 'payload_digest': None, 'state': 'not_found', 'provider_reference': None})
                return self.send(404, {'intent_id': intent, 'payload_digest': None, 'state': 'not_found', 'provider_reference': None})
            self.send(200, receipt(intent, found))

    def receipt(intent, effect):
        return {'intent_id': intent, 'payload_digest': effect['payload_digest'], 'state': 'accepted', 'provider_reference': effect['reference']}

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize('mode,expected', [
    ('status201', {'post_new': 'unexpected_status'}),
    ('not_json', {'post_new': 'content_type_not_json'}),
    ('extra_field', {'post_new': 'fields_invalid'}),
    ('no_echo', {'post_new': 'payload_digest_not_echoed'}),
    ('non_idempotent', {'post_replay': 'replay_not_idempotent'}),
    ('overwrite_conflict', {'post_conflict': 'conflict_not_409', 'get_found': 'payload_digest_not_echoed'}),
    ('absent_200', {'get_not_found': 'unexpected_status'}),
])
def test_deviation_is_reported_by_name(mode, expected):
    with deviant(mode) as origin:
        report = conformance.run(loopback(origin))
    assert report['ok'] is False
    codes = failed(report)
    for name, code in expected.items():
        assert codes[name] == code
    # NexLoop's own client refuses the same deviations (except those only the protocol checks can see).
    if mode in {'status201', 'not_json', 'extra_field', 'no_echo'}:
        assert codes['client_end_to_end'].startswith('client_')


def test_slow_endpoint_misses_the_deadline():
    with deviant('ok', delay=2.5) as origin:
        report = conformance.run(loopback(origin))
    codes = failed(report)
    assert codes['post_new'] == 'deadline_exceeded'
    assert codes['client_end_to_end'] == 'client_EffectProviderUnknown'


def test_unreachable_endpoint():
    import socket
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    report = conformance.run(loopback('http://127.0.0.1:' + str(port)))
    assert failed(report)['post_new'] == 'unreachable'


# ---- conformance: TLS, CA and credential --------------------------------------------------------------------------------

def certificate(tmp_path, name):
    cert, key = tmp_path / (name + '-cert.pem'), tmp_path / (name + '-key.pem')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    cert.chmod(0o600)
    key.chmod(0o600)
    return cert, key


@contextmanager
def tls_endpoint(tmp_path, credential):
    """The synthetic endpoint behind a TLS front with Bearer checking (the fixture itself speaks loopback HTTP)."""
    cert, key = certificate(tmp_path, 'endpoint')
    with effect_provider(tmp_path / 'endpoint.sqlite') as endpoint:
        import httpx

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def forward(self, method):
                if self.headers.get('Authorization') != 'Bearer ' + credential:
                    self.send_response(401)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0)) if method == 'POST' else None
                headers = {k: v for k, v in self.headers.items() if k in ('Content-Type', 'Idempotency-Key')}
                answer = httpx.request(method, endpoint.origin + self.path, content=body, headers=headers, trust_env=False, timeout=5)
                self.send_response(answer.status_code)
                self.send_header('Content-Type', answer.headers['content-type'])
                self.send_header('Content-Length', str(len(answer.content)))
                self.end_headers()
                self.wfile.write(answer.content)

            def do_POST(self):
                self.forward('POST')

            def do_GET(self):
                self.forward('GET')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield 'https://127.0.0.1:' + str(server.server_port), cert, endpoint
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


def private(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_tls_with_the_given_ca_passes_and_a_wrong_ca_fails(tmp_path, capsys):
    credential = secrets.token_urlsafe(32)
    with tls_endpoint(tmp_path, credential) as (origin, cert, _):
        secret = private(tmp_path, 'credential', credential)
        good = conformance.run(EffectProviderConfiguration(origin, credential_file=str(secret), ca_file=str(cert)))
        other, _ = certificate(tmp_path, 'other')
        bad = conformance.run(EffectProviderConfiguration(origin, credential_file=str(secret), ca_file=str(other)))
    assert good['ok'] is True, failed(good)
    assert good['checks'][0] == {'name': 'credential_format', 'ok': True, 'code': None, 'elapsed_ms': None}
    codes = failed(bad)
    assert codes['post_new'] == 'tls_failed' and codes['client_end_to_end'] == 'client_EffectProviderUnknown'
    output = capsys.readouterr()
    assert credential not in output.out + output.err
    assert credential not in json.dumps(good) + json.dumps(bad)


def test_credential_charset_and_private_file(tmp_path):
    origin = 'https://127.0.0.1:9'
    short = private(tmp_path, 'short', 'too-short')
    report = conformance.run(EffectProviderConfiguration(origin, credential_file=str(short)))
    assert report['ok'] is False and report['checks'] == [{'name': 'credential_format', 'ok': False,
                                                           'code': 'credential_charset_or_length', 'elapsed_ms': None}]
    spaced = private(tmp_path, 'spaced', 'a' * 20 + ' ' + 'b' * 20)
    assert failed(conformance.run(EffectProviderConfiguration(origin, credential_file=str(spaced))))['credential_format']
    shared = tmp_path / 'shared'
    shared.write_text(secrets.token_urlsafe(32))
    shared.chmod(0o644)
    with pytest.raises(conformance.InputError):
        conformance.run(EffectProviderConfiguration(origin, credential_file=str(shared)))


def test_cli_writes_a_private_report_and_refuses_an_existing_one(tmp_path, capsys):
    with effect_provider(tmp_path / 'endpoint.sqlite') as endpoint:
        out = tmp_path / 'report.json'
        assert conformance.main(['--origin', endpoint.origin, '--test-loopback-http', '--out', str(out)]) == 0
        assert out.stat().st_mode & 0o777 == 0o600
        assert json.loads(out.read_text())['ok'] is True
        assert conformance.main(['--origin', endpoint.origin, '--test-loopback-http', '--out', str(out)]) == 2
    with deviant('status201') as origin:
        assert conformance.main(['--origin', origin, '--test-loopback-http', '--out', str(tmp_path / 'bad.json')]) == 1
    assert 'post_new:unexpected_status' in capsys.readouterr().out


# ---- drill relay: the answer is withheld once, the worker queries and never resends -----------------------------------------

@contextmanager
def relay(target_port, control, hold_seconds=3.0):
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    port = {}

    def started(value):
        port['value'] = value
        ready.set()

    def run():
        asyncio.set_event_loop(loop)
        task = loop.create_task(drill.serve(('127.0.0.1', 0), ('127.0.0.1', target_port), str(control), hold_seconds, started))
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    try:
        yield port['value']
    finally:
        for task in asyncio.all_tasks(loop):
            loop.call_soon_threadsafe(task.cancel)
        thread.join(10)


def arguments():
    import uuid
    parameters = {'operation': 'synthetic drill delivery'}
    return {'intent_id': str(uuid.uuid4()), 'payload_digest': payload_digest(parameters), 'parameters': parameters}


def test_armed_relay_makes_the_result_unknown_and_the_query_finds_the_committed_effect(tmp_path):
    control = tmp_path / 'drill-control.json'
    with effect_provider(tmp_path / 'endpoint.sqlite') as endpoint:
        target = int(endpoint.origin.rsplit(':', 1)[1])
        with relay(target, control) as port:
            client = HttpEffectProvider(loopback('http://127.0.0.1:' + str(port)))
            # Unarmed: a plain pass-through.
            before = arguments()
            assert client.dispatch(**before).state == 'accepted'
            assert drill.arm(str(control), 1) == {'armed': 1, 'withheld': 0}
            values = arguments()
            with pytest.raises(EffectProviderUnknown):
                client.dispatch(**values)
            assert drill.status(str(control)) == {'armed': 0, 'withheld': 1}
            # The endpoint committed it; the worker's next step is the query, which passes through.
            receipt = client.query(intent_id=values['intent_id'], payload_digest=values['payload_digest'])
            assert receipt.state == 'accepted' and receipt.payload_digest == values['payload_digest']
            snapshot = endpoint.control('snapshot')
    posts = [r for r in snapshot['requests'] if r[0] == 'POST' and r[1] == values['intent_id']]
    gets = [r for r in snapshot['requests'] if r[0] == 'GET' and r[1] == values['intent_id']]
    assert posts == [('POST', values['intent_id'], 202)] and gets == [('GET', values['intent_id'], 200)]
    assert snapshot['effects'] == 2
    assert control.stat().st_mode & 0o777 == 0o600


def test_armed_relay_over_tls_withholds_only_after_the_handshake(tmp_path):
    credential = secrets.token_urlsafe(32)
    control = tmp_path / 'drill-control.json'
    with tls_endpoint(tmp_path, credential) as (origin, cert, endpoint):
        target = int(origin.rsplit(':', 1)[1])
        secret = private(tmp_path, 'credential', credential)
        with relay(target, control) as port:
            client = HttpEffectProvider(EffectProviderConfiguration('https://127.0.0.1:' + str(port), credential_file=str(secret),
                                                                    ca_file=str(cert)))
            drill.arm(str(control), 1)
            values = arguments()
            with pytest.raises(EffectProviderUnknown):
                client.dispatch(**values)
            receipt = client.query(intent_id=values['intent_id'], payload_digest=values['payload_digest'])
            snapshot = endpoint.control('snapshot')
    assert receipt.state == 'accepted'
    assert [r for r in snapshot['requests'] if r[1] == values['intent_id']] == [('POST', values['intent_id'], 202),
                                                                                 ('GET', values['intent_id'], 200)]


def test_query_during_an_armed_window_is_unavailable_never_fulfilled(tmp_path):
    control = tmp_path / 'drill-control.json'
    with effect_provider(tmp_path / 'endpoint.sqlite') as endpoint:
        target = int(endpoint.origin.rsplit(':', 1)[1])
        with relay(target, control) as port:
            client = HttpEffectProvider(loopback('http://127.0.0.1:' + str(port)))
            values = arguments()
            assert client.dispatch(**values).state == 'accepted'
            drill.arm(str(control), 1)
            with pytest.raises(EffectProviderQueryUnavailable):
                client.query(intent_id=values['intent_id'], payload_digest=values['payload_digest'])


def test_drill_control_file_must_be_private_and_counts_are_bounded(tmp_path, capsys):
    control = tmp_path / 'control.json'
    control.write_text('{"armed": 0, "withheld": 0}')
    control.chmod(0o644)
    with pytest.raises(PermissionError):
        drill.arm(str(control), 1)
    control.chmod(0o600)
    with pytest.raises(ValueError):
        drill.arm(str(control), 11)
    assert drill.main(['arm', '--control', str(control), '--count', '2']) == 0
    assert json.loads(capsys.readouterr().out) == {'armed': 2, 'withheld': 0}
    assert drill.main(['serve', '--listen', 'not-an-address', '--target', '127.0.0.1:1', '--control', str(control)]) == 2
