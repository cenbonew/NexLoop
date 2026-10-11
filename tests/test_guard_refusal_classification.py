"""NX-031 (c4low-leftovers): the guard answers a deterministic refusal of a runtime authorization with 403 and only a transient
failure with 503, so the Host retries only what can change (runtime_authorization_unavailable) and reports a refusal as
runtime_authorization_denied. A 503 and a connection dropped at the bound are counted as guard errors. Actual HTTPS guard,
fake worker (transport and classification only); the classification itself is exercised on real psycopg error types."""
import threading

import httpx
import psycopg
import pytest
from psycopg_pool import PoolTimeout

from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.observability import GuardTimings
from nexloop_eios.runtime_activation import RuntimeAuthorizationRefused, _authorization_failure
from test_agent_host import files
from test_runtime_guard_transport import PATH, BODY, client


@pytest.mark.parametrize('error', [psycopg.errors.SerializationFailure(), psycopg.errors.DeadlockDetected(), psycopg.errors.LockNotAvailable(),
                                   psycopg.errors.QueryCanceled(), psycopg.OperationalError(), PoolTimeout(), TimeoutError(), ConnectionResetError(),
                                   ])
def test_transient_failures_stay_unavailable(error):
    failure = _authorization_failure(error)
    assert type(failure) is AuthorizationUnavailable


@pytest.mark.parametrize('error', [psycopg.errors.InsufficientPrivilege(), psycopg.errors.RaiseException(), ValueError(), PermissionError(),
                                   KeyError('x'), RuntimeAuthorizationRefused('already'), AuthorizationUnavailable('decision denied')])
def test_deterministic_failures_are_refusals(error):
    failure = _authorization_failure(error)
    assert isinstance(failure, RuntimeAuthorizationRefused) and isinstance(failure, AuthorizationUnavailable)


class ClassifyingWorker:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def authorize_runtime_activation(self, **_):
        self.calls += 1
        raise self.error


@pytest.mark.parametrize(('error', 'status'), [(RuntimeAuthorizationRefused('refused'), 403), (AuthorizationUnavailable('busy'), 503)])
def test_guard_answers_refusal_403_and_transient_503_and_counts_errors(tmp_path, monkeypatch, error, status):
    from nexloop_eios import observability
    from nexloop_eios.runtime_control import create_runtime_guard_server
    timings = GuardTimings()
    monkeypatch.setattr(observability, 'GUARD', timings)
    _, key = files(tmp_path)
    worker = ClassifyingWorker(error)
    server = create_runtime_guard_server(worker, port=0, key_file=key, certificate_file=tmp_path / 'host-cert.pem', tls_key_file=tmp_path / 'host-key.pem')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with client(key) as http:
            response = http.post(f'https://127.0.0.1:{server.server_port}{PATH}', headers={'Authorization': 'Bearer ' + key.read_text()}, json=BODY)
        assert response.status_code == status and response.json() == {'authorized': False} and worker.calls == 1
    finally:
        server.shutdown(); server.server_close(); thread.join(3)
    sample, _ = timings.drain()
    assert sample['calls'] == 1 and sample['errors'] == (1 if status == 503 else 0)


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


def test_a_transient_root_cause_behind_a_wrapper_stays_unavailable():
    assert type(_authorization_failure(_wrapped(psycopg.errors.SerializationFailure()))) is AuthorizationUnavailable
    assert type(_authorization_failure(_wrapped(PoolTimeout()))) is AuthorizationUnavailable
    assert isinstance(_authorization_failure(_wrapped(psycopg.errors.InsufficientPrivilege())), RuntimeAuthorizationRefused)


def test_failure_detail_is_code_only_and_finds_the_sqlstate_behind_wrappers():
    from nexloop_eios.structured_log import failure_detail
    assert failure_detail(_wrapped(psycopg.errors.InsufficientPrivilege('secret detail text'))) == {'code': 'InsufficientPrivilege', 'sqlstate': '42501'}
    assert failure_detail(ValueError('secret detail text')) == {'code': 'ValueError'}
    refused = _authorization_failure(_wrapped(psycopg.errors.LockNotAvailable()))
    assert type(refused) is AuthorizationUnavailable
