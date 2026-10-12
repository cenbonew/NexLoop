"""NX-031: the guard answers every failed runtime authorization 503 (update 10: unavailable is not decided at the guard), counts
503 answers and connections dropped at the bound as guard errors, and logs only a code-only cause, never the error text.
Actual HTTPS guard, fake worker (transport only)."""
import logging
import threading

import psycopg

from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.observability import GuardTimings
from test_agent_host import files
from test_runtime_guard_transport import PATH, BODY, client

SENTINEL = 'synthetic-guard-classification-secret-sentinel'


class ClassifyingWorker:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def authorize_runtime_activation(self, **_):
        self.calls += 1
        raise self.error


def test_guard_answers_503_counts_the_error_and_logs_no_error_text(tmp_path, monkeypatch, caplog):
    error, status = _wrapped(psycopg.errors.InsufficientPrivilege(SENTINEL)), 503
    from nexloop_eios import observability
    from nexloop_eios.runtime_control import create_runtime_guard_server
    timings = GuardTimings()
    monkeypatch.setattr(observability, 'GUARD', timings)
    _, key = files(tmp_path)
    worker = ClassifyingWorker(error)
    server = create_runtime_guard_server(worker, port=0, key_file=key, certificate_file=tmp_path / 'host-cert.pem', tls_key_file=tmp_path / 'host-key.pem')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    caplog.set_level(logging.WARNING)
    try:
        with client(key) as http:
            response = http.post(f'https://127.0.0.1:{server.server_port}{PATH}', headers={'Authorization': 'Bearer ' + key.read_text()}, json=BODY)
        assert response.status_code == status and response.json() == {'authorized': False} and worker.calls == 1
    finally:
        server.shutdown(); server.server_close(); thread.join(3)
    sample, _ = timings.drain()
    assert sample['calls'] == 1 and sample['errors'] == 1
    assert SENTINEL not in caplog.text and 'InsufficientPrivilege' in caplog.text and '42501' in caplog.text


def test_dropped_connections_count_as_error_calls_without_latency():
    timings = GuardTimings()
    timings.drop(); timings.drop()
    assert timings.drain()[0] == {'calls': 2, 'timeouts': 0, 'errors': 2}
    timings.observe(100.0); timings.drop()
    sample, _ = timings.drain()
    assert sample['calls'] == 2 and sample['errors'] == 1 and sample['max_ms'] == 100.0


def _wrapped(inner):
    try:
        try:
            raise inner
        except Exception:
            raise AuthorizationUnavailable('service authentication denied') from None
    except AuthorizationUnavailable as outer:
        return outer


def test_failure_detail_is_code_only_and_finds_the_sqlstate_behind_wrappers():
    from nexloop_eios.structured_log import failure_detail
    assert failure_detail(_wrapped(psycopg.errors.InsufficientPrivilege('secret detail text'))) == {'code': 'InsufficientPrivilege', 'sqlstate': '42501'}
    assert failure_detail(ValueError('secret detail text')) == {'code': 'ValueError'}
