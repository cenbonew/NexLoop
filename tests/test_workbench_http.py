"""NX-028 slice 1 HTTP boundary without PostgreSQL: malformed requests are 422 before any port, the dependency-down
state is 503 (never an empty 200), unknown query parameters are refused, and /workbench/* serves the built page."""
from fastapi.testclient import TestClient

from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app

ORIGIN = 'https://synthetic.example'


def app(tmp_path):
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'index.html').write_text('<!doctype html><div id="root"></div>')
    missing = tmp_path / 'missing'
    browser = BrowserConfiguration(missing, missing, 'synthetic-a', 'synthetic-browser-app', ORIGIN)
    workbench = BrowserConfiguration(missing, missing, 'synthetic-a', 'synthetic-workbench-app', ORIGIN)
    return create_app(ApiConfiguration(missing, missing, tmp_path / 'artifacts', 'missing', browser, web_root=web, workbench=workbench))


def test_malformed_requests_are_422_and_dependency_down_is_503(tmp_path):
    with TestClient(app(tmp_path), base_url=ORIGIN) as client:
        for path in ['/api/v1/workbench/consumers?limit=0', '/api/v1/workbench/consumers?limit=201', '/api/v1/workbench/consumers?after=xyz',
                     '/api/v1/workbench/consumers/ABC', '/api/v1/workbench/actions?state=sideways', '/api/v1/workbench/commitments?all=yes',
                     '/api/v1/workbench/overview?tenant_id=other', '/api/v1/workbench/contact?consumer_id=' + 'a' * 64 + '&consumer_id=' + 'b' * 64,
                     '/api/v1/workbench/conversations/' + 'a' * 63, '/api/v1/workbench/settings?world=simulation']:
            assert client.get(path).status_code == 422, path
        down = client.get('/api/v1/workbench/overview')
        assert down.status_code == 503 and down.json()['code'] == 'dependency_unavailable' and down.json()['retryable'] is True
        assert client.post('/api/v1/workbench/auth/login', headers={'Origin': ORIGIN}, json={'username': 'x', 'password': 'y'}).status_code == 503
        assert client.post('/api/v1/workbench/auth/login', headers={'Origin': 'https://other.example'}, json={}).status_code == 403


def test_workbench_paths_serve_the_built_page(tmp_path):
    with TestClient(app(tmp_path), base_url=ORIGIN) as client:
        for path in ['/workbench', '/workbench/goals', '/workbench/commitments/' + 'a' * 64]:
            page = client.get(path)
            assert page.status_code == 200 and 'id="root"' in page.text and page.headers['cache-control'] == 'no-store'
        assert client.get('/api/v1/workbench/unknown').status_code == 404
