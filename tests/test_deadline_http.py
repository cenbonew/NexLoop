"""D2 (eios-capability-gap, dispatcher 2026-10-22): a hard total deadline on the side-effect-free outbound calls.

A fake provider that accepts the TCP connection and then never answers (no TLS handshake, as a stalled network or a stuck proxy) is
called by the shared entry and by each client that uses it: embeddings, Claim extraction, decision evaluation. Each returns a
recognizable timeout within its deadline, the request child is reaped, and the fake server sees its connection closed. Nothing here
reaches a real provider.
"""
import multiprocessing
import socket
import threading
import time

import pytest

from nexloop_eios.deadline_http import DeadlineExceeded, DeadlineTransportError, post

SLACK = 1.5  # process start and the kill, on a loaded machine


class StalledServer:
    """Accepts connections and holds them open without a byte; counts the ones the client closed."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted, self.closed = 0, 0
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.1)
        while not self.stop.is_set():
            try:
                connection, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            threading.Thread(target=self._hold, args=(connection,), daemon=True).start()

    def _hold(self, connection):
        connection.settimeout(0.1)
        while not self.stop.is_set():
            try:
                if connection.recv(4096) == b'':  # the client went away
                    self.closed += 1
                    return
            except socket.timeout:
                continue
            except OSError:
                self.closed += 1
                return

    def url(self, path='/v1/x'):
        return f'https://127.0.0.1:{self.port}{path}'

    def close(self):
        self.stop.set()
        self.sock.close()


@pytest.fixture
def stalled():
    server = StalledServer()
    yield server
    server.close()


def released(server):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (multiprocessing.active_children() or server.closed < server.accepted):
        time.sleep(0.05)
    return not multiprocessing.active_children() and server.closed == server.accepted


def test_a_stalled_provider_ends_at_the_total_deadline_and_releases_everything(stalled):
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        post(stalled.url(), {'Content-Type': 'application/json'}, b'{}', deadline_seconds=0.6, ca='system')
    assert time.monotonic() - started < 0.6 + SLACK
    assert stalled.accepted == 1 and released(stalled)


def test_a_refused_connection_is_a_transport_failure_not_a_timeout():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]  # closed again: nothing listens
    with pytest.raises(DeadlineTransportError) as error:
        post(f'https://127.0.0.1:{port}/', {}, b'', deadline_seconds=2, ca='system')
    assert error.value.code == 'transport' and not isinstance(error.value, TimeoutError)


@pytest.mark.parametrize('url,deadline', [('http://127.0.0.1/', 1), ('https://127.0.0.1/', 0), ('https://127.0.0.1/', 121), ('https://u:p@h/', 1)])
def test_only_bounded_https_calls(url, deadline):
    with pytest.raises(ValueError):
        post(url, {}, b'', deadline_seconds=deadline, ca='system')


def test_embeddings_time_out_within_their_deadline(stalled):
    from types import SimpleNamespace
    from nexloop_eios.embedding_provider import ArkMultimodalEmbeddingProvider, EmbeddingUnavailable
    profile = SimpleNamespace(validation_mode='verified', dimension=8, mode='multimodal', model='synthetic-embedding',
                              location=stalled.url('/api/v3/embeddings/multimodal'), credential_for_provider=lambda: 'synthetic-key')
    provider = ArkMultimodalEmbeddingProvider(profile, timeout=0.5, attempts=2)  # an embedding has no effect: a timeout is retried
    started = time.monotonic()
    with pytest.raises(EmbeddingUnavailable, match='transport unavailable'):
        provider.embed('合成文本')
    assert time.monotonic() - started < 2 * 0.5 + 2 * SLACK and provider.transport_failures == 2
    assert stalled.accepted == 2 and released(stalled)


def test_claim_extraction_times_out_as_a_timeout_code(stalled):
    from types import SimpleNamespace
    from nexloop_eios.conversation_extraction import ExtractionProviderUnavailable, OpenAICompatibleExtractionProvider
    profile = SimpleNamespace(provider='deepseek', model_id='synthetic-model', base_url=f'https://127.0.0.1:{stalled.port}/v1',
                              credential_for_provider=lambda: 'synthetic-key')
    started = time.monotonic()
    with pytest.raises(ExtractionProviderUnavailable) as error:
        OpenAICompatibleExtractionProvider(profile, timeout=0.5).complete('system', '{}')
    assert error.value.code == 'timeout' and time.monotonic() - started < 0.5 + SLACK
    assert released(stalled)


def test_decision_evaluation_transport_times_out(stalled):
    from nexloop_eios import decision_eval as de
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        de.https_transport(stalled.url(), {'Content-Type': 'application/json'}, b'{}', 0.5)
    assert time.monotonic() - started < 0.5 + SLACK and released(stalled)
    assert de.DecisionEvalClient.__init__.__kwdefaults__['transport'] is de.https_transport  # the client's default transport


def test_effect_calls_never_use_the_hard_deadline():
    """An external effect's unknown result is queried and reconciled first, never killed and retried blindly."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'packages/eios-core/src/nexloop_eios'
    for name in ('effect_provider.py', 'outreach.py', 'incentives.py'):
        assert 'deadline_http' not in (root / name).read_text(), name


def test_embedding_timeouts_are_counted_at_the_budget_upper_bound(stalled):
    """L6 D2 review 2: each timed-out attempt may have been billed: a call and one token per character, never undercounted."""
    from types import SimpleNamespace
    from nexloop_eios.embedding_provider import ArkMultimodalEmbeddingProvider, EmbeddingUnavailable
    profile = SimpleNamespace(validation_mode='verified', dimension=8, mode='multimodal', model='synthetic-embedding',
                              location=stalled.url('/api/v3/embeddings/multimodal'), credential_for_provider=lambda: 'synthetic-key')
    provider = ArkMultimodalEmbeddingProvider(profile, timeout=0.4, attempts=2)
    with pytest.raises(EmbeddingUnavailable):
        provider.embed('合成文本四字')
    assert (provider.calls, provider.usage_tokens) == (2, 2 * len('合成文本四字'))


CHILD = r'''
import json, os, ssl, socket, sys, threading, http.server
sys.path.insert(0, sys.argv[1])
from nexloop_eios.deadline_http import DeadlineTransportError, post
cert, key = sys.argv[2], sys.argv[3]
class Answer(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length'] or 0))
        self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *a): pass
server = http.server.HTTPServer(('127.0.0.1', 0), Answer)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
server.socket = context.wrap_socket(server.socket, server_side=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
proxy = socket.socket(); proxy.bind(('127.0.0.1', 0)); proxy.listen(8); proxy.settimeout(0.2)
url = f'https://127.0.0.1:{server.server_address[1]}/v1/x'
for name in ('HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ[name] = f'http://127.0.0.1:{proxy.getsockname()[1]}'
os.environ['SSL_CERT_FILE'] = cert  # would make the private certificate trusted if the environment were used
os.environ['NO_PROXY'] = ''
out = {'private_ca': post(url, {}, b'{}', deadline_seconds=5, ca=cert)[0]}
try:
    post(url, {}, b'{}', deadline_seconds=5, ca='certifi'); out['certifi'] = 'trusted'
except DeadlineTransportError as error:
    out['certifi'] = error.code
try:
    proxy.accept(); out['proxy'] = 'used'
except OSError:
    out['proxy'] = 'unused'
print(json.dumps(out))
'''


def test_the_environment_never_chooses_the_proxy_or_the_ca(tmp_path):
    """L6 D2 review 1: trust_env=False and the caller's CA. A fresh interpreter, so the forkserver starts with the proxy and CA
    variables set: the request still goes direct, a private CA given by the caller works, and SSL_CERT_FILE does not make the
    certifi bundle trust that certificate."""
    import json
    import subprocess
    import sys
    from pathlib import Path
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    src = Path(__file__).resolve().parents[1] / 'packages/eios-core/src'
    done = subprocess.run([sys.executable, '-c', CHILD, str(src), str(cert), str(key)], capture_output=True, text=True, timeout=60,
                          env={k: v for k, v in __import__('os').environ.items() if 'PROXY' not in k.upper()} | {'PYTHONPATH': str(src)})
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {'private_ca': 200, 'certifi': 'transport', 'proxy': 'unused'}
