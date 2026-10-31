"""Owner S73 (2026-10-31): the workbench 入口 page links the reference endpoint staff page only when the deployment gives its URL
(compose REFERENCE_ENDPOINT_STAFF_URL -> nexloop-api --reference-endpoint-staff-url -> GET /api/v1/workbench/entries). Empty means
null; anything but a plain HTTPS URL is refused at start; the read needs the caller's own workbench session."""
import dataclasses
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from eios.identity.errors import CredentialInvalid

from nexloop_eios import http_api, workbench_http
from nexloop_eios.browser_http import BrowserConfiguration

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://app.invalid'
STAFF = 'https://endpoint.invalid:9443/staff'


def config(url=None):
    return BrowserConfiguration(Path('identity-dsn'), Path('rate-key'), 'tenant-synthetic', 'app-workbench', ORIGIN, reference_endpoint_staff_url=url)


@pytest.mark.parametrize('url', ['http://endpoint.invalid/staff', 'https://user:pw@endpoint.invalid/staff', 'https://endpoint.invalid/staff?x=1',
                                 'https://endpoint.invalid/staff#top', 'https:///staff', ' https://endpoint.invalid/staff', '', 'https://e.invalid/' + 'a' * 600])
def test_only_a_plain_https_url_is_accepted(url):
    with pytest.raises(ValueError):
        config(url)
    assert config(STAFF).reference_endpoint_staff_url == STAFF and config().reference_endpoint_staff_url is None


def test_the_cli_reads_empty_as_none_and_refuses_a_url_without_the_workbench(capsys):
    with pytest.raises(SystemExit):
        http_api.parse(['--mode', 'test', '--database-url-file', 'd', '--signing-key-file', 'k', '--artifact-root', 'a', '--signing-key-id', 'x',
                        '--reference-endpoint-staff-url', STAFF])
    assert 'needs the workbench' in capsys.readouterr().err


class Session:
    restricted = False


def client(monkeypatch, url, inspect):
    class Service:
        def __init__(self, *a, **k):
            pass

        def inspect(self, cookie):
            return inspect()
    monkeypatch.setattr(workbench_http, 'BrowserSessionService', Service)
    app = FastAPI()
    app.state.workbench_store = object()
    app.include_router(workbench_http.router(config(url), ports_for_workbench=lambda request, session: object()))
    return TestClient(app, base_url=ORIGIN)


@pytest.mark.parametrize('url', [STAFF, None])
def test_a_member_reads_the_url_or_null(monkeypatch, url):
    response = client(monkeypatch, url, Session).get('/api/v1/workbench/entries')
    assert response.status_code == 200 and response.json() == {'reference_endpoint_staff_url': url}
    assert response.headers['cache-control'] == 'no-store'


def test_no_session_is_401_and_parameters_are_422(monkeypatch):
    def refused():
        raise CredentialInvalid()
    c = client(monkeypatch, STAFF, refused)
    assert c.get('/api/v1/workbench/entries').status_code == 401
    assert c.get('/api/v1/workbench/entries?x=1').status_code == 422


def test_compose_passes_the_variable_with_an_empty_default_and_the_env_example_leaves_it_empty():
    compose = (ROOT / 'deploy/stage/compose.app.example.yaml').read_text(encoding='utf-8')
    assert compose.count('--reference-endpoint-staff-url, "${REFERENCE_ENDPOINT_STAFF_URL:-}"') == 2  # api-1 and api-2
    env = (ROOT / 'deploy/stage/compose.app.env.example').read_text(encoding='utf-8').splitlines()
    assert 'REFERENCE_ENDPOINT_STAFF_URL=' in env
