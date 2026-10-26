"""EIOS gap E4 (NX-061): API request metrics without PostgreSQL. The middleware counts every request by route template (never the
raw path), method and status in the vendored eios.api.metrics registry and by request class in observability.API; the per-route
text is served on an optional loopback port only. The SQL side (kind 'api', snapshot block) is tests/test_observability_pg.py."""
import socket
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import observability
from nexloop_eios.http_api import ApiConfiguration, create_app, parse
from nexloop_eios.observability import API_SLOW_MS, ApiTimings


def config(tmp_path, **kw):
    return ApiConfiguration(tmp_path / 'missing-dsn', tmp_path / 'missing-key', tmp_path / 'never-create', 'absent', **kw,mode='test')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_request_classes_count_errors_and_slow_requests():
    timings = ApiTimings()
    for method, status, ms in [('GET', 200, 10), ('GET', 200, API_SLOW_MS), ('HEAD', 502, 30), ('POST', 201, 900), ('DELETE', 404, 5)]:
        timings.observe(method, status, ms)
    samples, _ = timings.drain()
    read, write = samples['read'], samples['write']
    assert (read['calls'], read['errors'], read['slow']) == (3, 1, 1) and 'timeouts' not in read
    assert (write['calls'], write['errors'], write['slow']) == (2, 0, 1)  # a 404 is not a server error
    assert sum(v for k, v in read.items() if k.startswith('h_')) == 3
    assert timings.drain()[0] == {}


def test_the_middleware_labels_route_templates_and_never_raw_paths(tmp_path, monkeypatch):
    timings = ApiTimings()
    monkeypatch.setattr(observability, 'API', timings)
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        assert client.get('/health/live').status_code == 200 and client.get('/health/live').status_code == 200
        assert client.get('/api/v1/artifacts/synthetic-artifact-0001').status_code == 401
        assert client.get('/no/such/synthetic-path-0001').status_code == 404
    text = app.state.route_metrics.render(storage_mode='postgresql', revision='x')
    assert 'nexloop_api_requests_total{method="GET",route="/health/live",status="200"} 2' in text
    assert 'nexloop_api_requests_total{method="GET",route="/api/v1/artifacts/{artifact_id}",status="401"} 1' in text
    assert 'route="unmatched",status="404"' in text
    assert 'synthetic-artifact-0001' not in text and 'synthetic-path-0001' not in text
    assert 'le="0.5"' in text  # the PRD 8 read target is a bucket boundary
    samples, _ = timings.drain()
    assert samples['read']['calls'] == 4


def test_the_per_route_text_is_on_an_optional_loopback_port_only(tmp_path):
    port = free_port()
    app = create_app(config(tmp_path, api_metrics_port=port))
    with TestClient(app) as client:
        client.get('/health/live')
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/internal/api-metrics', timeout=5) as answer:
            body = answer.read().decode()
            assert answer.status == 200 and 'nexloop_api_requests_total{method="GET",route="/health/live",status="200"} 1' in body
        with pytest.raises(urllib.error.HTTPError) as other:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=5)
        assert other.value.code == 404
    with pytest.raises(OSError):  # closed with the app
        urllib.request.urlopen(f'http://127.0.0.1:{port}/internal/api-metrics', timeout=2)
    assert create_app(config(tmp_path)).state.route_metrics is not None  # without a port nothing listens


def test_the_metrics_port_must_be_separate_and_unprivileged(tmp_path):
    base = ['--database-url-file', str(tmp_path / 'd'), '--signing-key-file', str(tmp_path / 'k'), '--artifact-root', str(tmp_path / 'a'),
            '--signing-key-id', 'synthetic', '--mode', 'test', '--port', '8100']
    assert parse(base + ['--api-metrics-port', '8101'], environ={})[0].api_metrics_port == 8101
    assert parse(base, environ={})[0].api_metrics_port is None
    for bad in ('8100', '80'):
        with pytest.raises(SystemExit):
            parse(base + ['--api-metrics-port', bad], environ={})
