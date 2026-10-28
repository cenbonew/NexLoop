"""NX-062 C8-1: the API route inventory release gate without a database (docs/design/c8-route-capability-inventory.md v2 §4).

G-1 the routes the complete configuration mounts are exactly the inventory (and every route registration of create_app takes part),
G-2 the realms without a cookie session are fixed sets, G-3 every port exists and the handler is bound to it, the workbench operations
are exactly OPERATIONS and the registry, G-4 every governed capability is reached or explained, G-6 the served web build holds only
publishable files; F-1 (Q-a) no runtime OpenAPI document. G-5 (no credential, no access) is tests/test_api_route_inventory_pg.py."""
import ast
import copy
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexloop_eios import api_route_inventory as I
from nexloop_eios import catalog_manifests as C
from nexloop_eios.goal_controls import CAPABILITIES
from nexloop_eios.workbench_actions_http import OPERATIONS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_web_build as W  # noqa: E402

# G-2: every realm that does not check a cookie session, per deployment mode, written out here as well as in the inventory
# (design §6: two places). F-2 (dispatcher 2026-10-26): the test-world service-token read exists in --mode test alone.
_SHARED = {
    'public': {('GET', '/health/live'), ('GET', '/health/ready')},
    'public_static': {('GET', '/workbench'), ('GET', '/workbench/{rest:path}'), ('MOUNT', '/')},
    'webhook_signature': {('POST', '/api/v1/webhooks/commercial/{connector_id}')},
    'pre_session': {('POST', '/api/v1/auth/login'), ('POST', '/api/v1/auth/csrf'), ('POST', '/api/v1/workbench/auth/login'),
                    ('POST', '/api/v1/workbench/auth/csrf'), ('POST', '/api/v1/workbench/auth/invitation/accept'),
                    ('POST', '/api/v1/workbench/auth/password/reset'),
                    ('GET', '/api/v1/workbench/auth/oidc/start'), ('GET', '/api/v1/workbench/auth/oidc/callback'),
                    ('GET', '/api/v1/workbench/auth/oidc/providers')},  # NX-058 slice F
}
FIXED = {'test': {**_SHARED, 'service_bearer': {('GET', '/api/v1/artifacts/{artifact_id}')}},
         'stage': {**_SHARED, 'service_bearer': set()}}


@pytest.fixture(scope='module')
def inventory():
    return I.load()


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    return I.complete_app(tmp_path_factory.mktemp('complete'))


def test_the_inventory_follows_its_schema_and_refuses_what_the_schema_cannot_say(inventory, tmp_path):
    assert inventory['schema_version'] == 'nexloop-api-routes/1' and len(inventory['routes']) == 98   # 97 routes and one mount (batch 32 added the members read, NX-058 slice F the three SSO routes)
    def refused(change):
        bad = copy.deepcopy(inventory)
        change(bad)
        path = tmp_path / 'bad.json'
        path.write_text(json.dumps(bad))
        with pytest.raises(I.InventoryInvalid):
            I.load(path)
    refused(lambda d: d['routes'][0].update(realm='everyone'))                                     # unknown realm
    refused(lambda d: d['routes'].append(dict(d['routes'][0])))                                    # duplicate route
    refused(lambda d: next(r for r in d['routes'] if r['realm'] == 'consumer_session' and r['authorization']['kind'] == 'port')
            .update(authorization={'kind': 'workbench_operation'}))                                # kind not of that realm
    refused(lambda d: next(r for r in d['routes'] if r['method'] == 'MOUNT').update(handler='nexloop_eios.http_api:live'))
    refused(lambda d: d['routes'][1]['authorization'].update(port='nexloop_eios'))                 # port outside the pattern
    refused(lambda d: d['routes'][1].pop('purpose'))


@pytest.mark.parametrize('mode', I.MODES)
def test_g1_each_mode_mounts_exactly_its_inventory_and_no_openapi(inventory, mode, tmp_path):
    mounted = I.routes(I.complete_app(tmp_path, mode))
    assert I.diff(inventory, mounted, mode) == {'unlisted': [], 'not_mounted': [], 'handler_differs': []}
    assert (('GET', '/api/v1/artifacts/{artifact_id}') in mounted) is (mode == 'test')                    # F-2
    assert len(mounted) == {'test': 98, 'stage': 97}[mode]
    assert not {p for _, p in mounted} & {'/openapi.json', '/docs', '/redoc', '/docs/oauth2-redirect'}


def test_g1_reports_every_direction_of_a_difference(inventory, app):
    mounted = I.routes(app)
    assert I.diff(inventory, mounted) == {'unlisted': [], 'not_mounted': [], 'handler_differs': []}
    paths = {p for _, p in mounted}
    assert not paths & {'/openapi.json', '/docs', '/redoc', '/docs/oauth2-redirect'}
    # An inventory entry deleted while its route is still mounted: the route is unlisted again (L6 S-2).
    trimmed = {**inventory, 'routes': [r for r in inventory['routes'] if (r['method'], r['path']) != ('GET', '/api/v1/workbench/metrics')]}
    assert I.diff(trimmed, mounted) == {'unlisted': ['GET /api/v1/workbench/metrics'], 'not_mounted': [], 'handler_differs': []}
    # A route deleted from the code but still listed is reported (the other direction).
    listed_only = {k: v for k, v in mounted.items() if k != ('GET', '/api/v1/workbench/metrics')}
    assert I.diff(inventory, listed_only)['not_mounted'] == ['GET /api/v1/workbench/metrics']
    # A handler swapped behind a listed route is reported.
    swapped = {**mounted, ('GET', '/api/v1/workbench/metrics'): 'nexloop_eios.workbench_http:alerts'}
    assert I.diff(inventory, swapped)['handler_differs'] == ['GET /api/v1/workbench/metrics: listed nexloop_eios.workbench_http:metrics, mounted nexloop_eios.workbench_http:alerts']
    # A new route nobody reviewed is never bound automatically.
    assert I.diff(inventory, {**mounted, ('GET', '/api/v1/invented'): 'nexloop_eios.http_api:live'})['unlisted'] == ['GET /api/v1/invented']


def test_g1_every_route_registration_of_create_app_takes_part_in_the_complete_configuration(monkeypatch, tmp_path):
    """L6 R-3: every include_router, @app.<method>, add_api_route and mount call of create_app runs in the complete configuration."""
    source = (ROOT / 'packages/eios-core/src/nexloop_eios/http_api.py').read_text()
    create = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == 'create_app')
    sites = {'include_router': 0, 'mount': 0, 'add_api_route': 0, 'decorator': 0}
    for node in ast.walk(create):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'app':
            if node.func.attr in ('include_router', 'mount', 'add_api_route'):
                sites[node.func.attr] += 1
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sites['decorator'] += sum(1 for d in node.decorator_list if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                                      and isinstance(d.func.value, ast.Name) and d.func.value.id == 'app'
                                      and d.func.attr in ('get', 'post', 'put', 'patch', 'delete', 'head', 'api_route'))
    calls = {'include_router': [], 'mount': 0}
    original_include, original_mount = FastAPI.include_router, FastAPI.mount
    monkeypatch.setattr(FastAPI, 'include_router', lambda self, router, **kw: (calls['include_router'].append(len(router.routes)),
                                                                                original_include(self, router, **kw))[1])
    monkeypatch.setattr(FastAPI, 'mount', lambda self, *a, **kw: (calls.__setitem__('mount', calls['mount'] + 1), original_mount(self, *a, **kw))[1])
    mounted = I.routes(I.complete_app(tmp_path))   # --mode test: the one mode in which every registration runs (F-2's route included)
    assert len(calls['include_router']) == sites['include_router'] and all(n > 0 for n in calls['include_router'])
    assert calls['mount'] == sites['mount'] == 1 and sites['add_api_route'] == 0
    direct = [k for k, h in mounted.items() if h.startswith('nexloop_eios.http_api:')]
    assert len(direct) == sites['decorator']                       # health x2, artifacts, the two workbench page routes


@pytest.mark.parametrize('mode', I.MODES)
def test_g2_realms_without_a_cookie_session_are_fixed_sets(inventory, mode):
    by_realm = {realm: set() for realm in I.REALM_KINDS}
    for r in I.listed(inventory, mode):
        by_realm[r['realm']].add((r['method'], r['path']))
    for realm, expected in FIXED[mode].items():
        assert by_realm[realm] == expected, (mode, realm)
    assert set(FIXED[mode]) | {'consumer_session', 'workbench_session'} == set(I.REALM_KINDS)
    bearer = next(r for r in inventory['routes'] if r['realm'] == 'service_bearer')
    assert bearer['authorization']['world'] == 'test' and bearer['modes'] == ['test']   # design §7 F-2


def test_f2_the_test_world_artifact_read_is_not_mounted_outside_test_mode(tmp_path):
    with TestClient(I.complete_app(tmp_path, 'stage'), base_url='https://app.invalid') as client:
        response = client.get('/api/v1/artifacts/' + 'd' * 32, headers={'Authorization': 'Bearer not-a-service-token'})
    assert response.status_code == 404 and response.json()['code'] == 'not_found'      # no route: never authenticated


def test_g3_every_port_exists_and_every_handler_is_bound_to_its_port(inventory):
    operations, sql = I.backend_operations(), I._sql_functions()
    ported = [r for r in inventory['routes'] if 'port' in r['authorization']]
    assert len(ported) == 78                                       # 76 port routes (batch 32 added the members read), the service token and the webhook
    for r in ported:
        port = r['authorization']['port']
        assert I.port_exists(port, operations, sql), port
        assert I.handler_mentions(r['handler'], I.port_name(port)), (r['method'], r['path'], port)
    # The checks fail closed: an invented port or a handler that never names it.
    assert not I.port_exists('backend.workbench:invented', operations, sql) and not I.port_exists('sql:authz.invented', operations, sql)
    assert not I.port_exists('nexloop_eios.commercial.CommercialObserver.invented')
    assert not I.handler_mentions('nexloop_eios.workbench_http:metrics', 'alerts')
    # Every operation of the backend's closed allow-lists is reached by a route or listed as not on HTTP, never both.
    used = {r['authorization']['port'] for r in ported}
    offline = {x['port'] for x in inventory['port_operations_not_on_http']}
    allowed = {f'backend.{realm}:{o}' for realm, ops in operations.items() for o in ops}
    assert allowed <= used | offline and not used & offline and offline <= allowed


def test_g3_the_workbench_operations_are_exactly_the_dispatch_and_the_registry(inventory):
    listed = {x['operation']: x['capability'] for x in inventory['operations']}
    assert set(listed) == set(OPERATIONS)
    assert listed == {o: CAPABILITIES[o] for o in OPERATIONS}
    registry = C.governed_capabilities()
    assert all(registry.get(capability) == operation for operation, capability in listed.items())


def test_g4_every_governed_capability_is_reached_or_explained(inventory):
    registry = set(C.governed_capabilities())
    on_http = {x['capability'] for x in inventory['operations']}
    explained = {x['capability'] for x in inventory['capabilities_not_on_http']}
    assert not on_http & explained and on_http | explained == registry
    assert len(explained) == 9  # NX-038 G1: goals.budget.set is on HTTP (set_budget)


def test_f1_no_runtime_openapi_document_and_the_schema_still_builds(app):
    with TestClient(app, base_url='https://app.invalid') as client:
        for path in ('/openapi.json', '/docs', '/redoc'):
            for method in ('GET', 'HEAD'):
                assert client.request(method, path).status_code == 404, (method, path)
    assert app.openapi()['paths']                                  # contract use (app.openapi()) is unaffected


def test_g6_the_served_web_build_holds_only_publishable_files(tmp_path):
    def build(files):
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        for name, content in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(content)
        return W.problems(root)
    good = {'index.html': b'<!doctype html>', 'assets/index-abc.js': b'console.log(1)', 'assets/index-abc.css': b'a{}', 'brand/logo.png': b'png'}
    assert build(good) == []
    assert build({**good, 'assets/index-abc.js.map': b'{}'}) == ['assets/index-abc.js.map: source map']
    assert build({**good, 'assets/index-abc.js': b'x\n//# sourceMappingURL=index-abc.js.map'}) == ['assets/index-abc.js: sourceMappingURL comment']
    assert build({**good, 'assets/index-abc.css': b'a{}/*# sourceMappingURL=x.css.map */'}) == ['assets/index-abc.css: sourceMappingURL comment']
    assert build({**good, 'index.html': b'<script>//# sourceMappingURL=inline.js.map\n</script>'}) == ['index.html: sourceMappingURL comment']
    assert build({**good, '.env': b'X=1'}) == ['.env: dotfile']
    assert build({**good, 'src/main.tsx': b'x'}) == ['src/main.tsx: suffix not in the build list']
    assert build({k: v for k, v in good.items() if k != 'index.html'}) == ['index.html missing']
    linked = tmp_path / 'linked'
    linked.mkdir()
    (linked / 'index.html').write_bytes(b'x')
    (linked / 'other.html').symlink_to(linked / 'index.html')
    assert W.problems(linked) == ['other.html: symbolic link']
    assert W.main([str(tmp_path / 'absent')]) == 1


def test_the_modes_are_one_list_in_the_api_the_schema_and_the_cli(capsys):
    """L6 S-6: http_api.MODES (the inventory imports it), the Schema's modes enum and nexloop-api --mode choices agree."""
    from nexloop_eios import http_api
    schema = json.loads((ROOT / 'deploy/authorization/api-routes.schema.json').read_text())
    assert I.MODES is http_api.MODES and I.MODES == ('test', 'stage')
    assert schema['$defs']['route']['properties']['modes']['items']['enum'] == list(I.MODES)
    with pytest.raises(SystemExit):
        http_api.parse(['--mode', 'invented'])
    assert "choose from test, stage" in capsys.readouterr().err
    # The configuration has no default mode and refuses any other value (L6 S-4).
    with pytest.raises(TypeError):
        http_api.ApiConfiguration(Path('d'), Path('k'), Path('a'), 'key')
    with pytest.raises(ValueError):
        http_api.ApiConfiguration(Path('d'), Path('k'), Path('a'), 'key', mode='production')
