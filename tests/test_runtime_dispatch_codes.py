"""NX-031 (lane-history-fix): the runtime worker's Host client keeps each failure cause as its own code instead of swallowing
them all as runtime_transport_unavailable: a request over the Host's 262144-byte limit (refused before sending, and the Host's
413), any other Host status (its number), and an answer that fails validation. Transport-only TLS server, no EIOS authority."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl

import pytest

from test_agent_host import files
from nexloop_eios.host_control import HostControlConfiguration
from nexloop_eios.runtime_dispatch import RuntimeDispatchError, RuntimeHostClient, _RETRYABLE

BODY = {'activation_ref': 'activation_transport_only', 'command': {'run_id': 'transport-only', 'request_id': 'transport-only'}}


def serve(tmp_path, status, body, content_type='application/json'):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(len(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, *args):
            pass

    server = Server(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / 'host-cert.pem', tmp_path / 'host-key.pem')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, calls


@pytest.mark.parametrize(('status', 'body', 'content_type', 'code'), [
    (413, b'{"code":"runtime_request_too_large"}', 'application/json', 'runtime_request_too_large'),
    (500, b'{"code":"dependency_unavailable"}', 'application/json', 'runtime_host_status_500'),
    (404, b'{"code":"not_found"}', 'application/json', 'runtime_host_status_404'),
    (503, b'{"code":"dependency_unavailable"}', 'application/json', 'runtime_host_status_503'),
    (200, b'{"run_id":"other"}', 'application/json', 'runtime_response_invalid'),
    (200, b'not json', 'application/json', 'runtime_response_invalid'),
    (200, b'{}', 'text/plain', 'runtime_response_invalid'),
])
def test_each_cause_has_its_own_code(tmp_path, status, body, content_type, code):
    _, key = files(tmp_path)
    server, thread, calls = serve(tmp_path, status, body, content_type)
    try:
        configuration = HostControlConfiguration(f'https://127.0.0.1:{server.server_address[1]}', key, tmp_path / 'host-cert.pem')
        with pytest.raises(RuntimeDispatchError) as failure:
            RuntimeHostClient(configuration).request('inspect', BODY, deadline=time.monotonic() + 2)
        assert failure.value.code == code and calls
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


def test_a_body_over_the_host_limit_is_refused_before_sending(tmp_path):
    _, key = files(tmp_path)
    server, thread, calls = serve(tmp_path, 202, b'{}')
    try:
        configuration = HostControlConfiguration(f'https://127.0.0.1:{server.server_address[1]}', key, tmp_path / 'host-cert.pem')
        body = {**BODY, 'input': 'x' * 262144}
        with pytest.raises(RuntimeDispatchError) as failure:
            RuntimeHostClient(configuration).request('start', body, deadline=time.monotonic() + 2)
        assert failure.value.code == 'runtime_request_too_large' and calls == []
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


def test_only_ambiguous_causes_are_retried():
    assert 'runtime_request_too_large' not in _RETRYABLE
    assert {'runtime_transport_unavailable', 'runtime_transport_timeout', 'runtime_response_invalid'} <= _RETRYABLE


def test_inspect_polling_backs_off_to_a_bound():
    from nexloop_eios.runtime_dispatch import POLL_MAX_SECONDS, next_poll_interval
    interval, seen = .05, []
    for _ in range(8):
        seen.append(interval)
        interval = next_poll_interval(interval, .05)
    assert seen == [.05, .1, .2, .4, .5, .5, .5, .5] and POLL_MAX_SECONDS == .5
    assert next_poll_interval(1, 1) == 1  # a configured base above the bound is kept
    # Over a typical 11 s deterministic Run: 26 inspects instead of 220 at a fixed 50 ms.
    total, polls, interval = 0, 0, .05
    while total < 11:
        total += interval; polls += 1; interval = next_poll_interval(interval, .05)
    assert polls <= 26
