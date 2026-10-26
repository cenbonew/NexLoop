"""NX-062 C8: the reviewed inventory of every HTTP route of nexloop-api and how it is authorized (docs/design/c8-route-capability-inventory.md v2).

deploy/authorization/api-routes.v1.json lists each (method, path) the complete configuration mounts - both browser realms on and a built
web root, the way the stage serves the workbench - with its realm (which credential is checked before anything else: none, the built
pages, a consumer or workbench cookie session, a Bearer service token, the connector signature, or an entry before a session exists)
and what authorizes it past that (a port operation the handler delegates to, the workbench write dispatch, or nothing more).
Mounts (the static web root) are one entry each, never expanded. A route mounted in some deployment modes only lists them
(`modes`; F-2: the test-world Artifact read exists in --mode test alone); without `modes` a route is mounted in every mode. The inventory is written and reviewed by hand; this module only
loads and checks it and lists the routes an application actually mounts, so a route nobody reviewed fails the release gate
(tests/test_api_route_inventory.py, tests/test_api_route_inventory_pg.py) instead of being bound automatically.

Idea from the upstream src/eios/authz/inventory.py (frozen release commit; exact source set, an explicit public allowlist, no
automatic binding of an unreviewed route), rewritten for NexLoop: NexLoop authorizes by governed Action and many routes share one,
so there is no one-to-one route-to-resource map; nothing is copied.

    python -m nexloop_eios.api_route_inventory --diff     prints what differs between the inventory and the mounted routes (exit 1)
"""
import argparse
import ast
import importlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MANIFEST = ROOT / 'deploy/authorization/api-routes.v1.json'
SCHEMA = ROOT / 'deploy/authorization/api-routes.schema.json'
SOURCE = Path(__file__).resolve().parent
MOUNT = 'MOUNT'
MODES = ('test', 'stage')  # http_api --mode

# realm -> the authorization kinds it may carry (design §3.2).
REALM_KINDS = {
    'public': {'public'},
    'public_static': {'static'},
    'pre_session': {'pre_session'},
    'consumer_session': {'port', 'session_only'},
    'workbench_session': {'port', 'workbench_operation', 'session_only'},
    'service_bearer': {'service_bearer'},
    'webhook_signature': {'webhook_signature'},
}


class InventoryInvalid(ValueError):
    pass


def load(path=MANIFEST, schema_path=SCHEMA):
    """The inventory, validated against its JSON Schema and the cross-field rules the Schema cannot state."""
    import jsonschema
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    try:
        jsonschema.validate(value, json.loads(Path(schema_path).read_text(encoding='utf-8')), cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as error:
        raise InventoryInvalid('schema: ' + error.message[:200]) from None
    keys = [(r['method'], r['path']) for r in value['routes']]
    if len(set(keys)) != len(keys):
        raise InventoryInvalid('duplicate route')
    for r in value['routes']:
        if r['authorization']['kind'] not in REALM_KINDS[r['realm']]:
            raise InventoryInvalid(f"kind {r['authorization']['kind']} not allowed in realm {r['realm']}: {r['method']} {r['path']}")
        # A mount is a static entry with the 'static' handler, and only a mount has that handler.
        if (r['method'] == MOUNT) != (r['handler'] == 'static') or (r['method'] == MOUNT and r['authorization']['kind'] != 'static'):
            raise InventoryInvalid('a mount is static and only a mount has the static handler: ' + r['path'])
    for field, key in (('operations', 'operation'), ('capabilities_not_on_http', 'capability'), ('port_operations_not_on_http', 'port')):
        names = [x[key] for x in value[field]]
        if len(set(names)) != len(names):
            raise InventoryInvalid('duplicate in ' + field)
    return value


def complete_app(directory, mode='test'):
    """create_app in the complete configuration (design §4) of one mode: both browser realms, workbench member secrets and a built
    web root."""
    from nexloop_eios.browser_http import BrowserConfiguration
    from nexloop_eios.http_api import ApiConfiguration, create_app
    directory = Path(directory)
    web = directory / 'web'
    web.mkdir(exist_ok=True)
    (web / 'index.html').write_text('<!doctype html><title>synthetic</title>', encoding='utf-8')
    origin = 'https://app.invalid'
    browser = BrowserConfiguration(directory / 'identity-dsn', directory / 'rate-key', 'tenant-synthetic', 'app-browser', origin)
    workbench = BrowserConfiguration(directory / 'identity-dsn', directory / 'rate-key', 'tenant-synthetic', 'app-workbench', origin,
                                     identity_token_key_file=directory / 'token-key')
    return create_app(ApiConfiguration(directory / 'dsn', directory / 'key', directory / 'artifacts', 'absent', mode=mode,
                                       browser=browser, workbench=workbench, web_root=web))


def _handler(endpoint):
    return endpoint.__module__ + ':' + endpoint.__name__


def routes(app):
    """{(method, path): handler} of everything the application mounts; a mount is (MOUNT, its path) with handler 'static'."""
    found = {}

    def walk(entries, prefix=''):
        for entry in entries:
            kind = type(entry).__name__
            if kind == 'Mount':
                found[(MOUNT, (prefix + entry.path) or '/')] = 'static'
            elif hasattr(entry, 'path'):
                for method in sorted(getattr(entry, 'methods', None) or ('WEBSOCKET',)):
                    found[(method, prefix + entry.path)] = _handler(entry.endpoint)
            elif hasattr(entry, 'original_router'):  # FastAPI's included router
                context = getattr(entry, 'include_context', None)
                walk(entry.original_router.routes, prefix + (getattr(context, 'prefix', '') or ''))
            else:
                raise InventoryInvalid('unknown route entry: ' + kind)
    walk(app.routes)
    return found


def listed(inventory, mode='test'):
    """The inventory's routes of one mode."""
    return [r for r in inventory['routes'] if mode in r.get('modes', MODES)]


def diff(inventory, mounted, mode='test'):
    expected = {(r['method'], r['path']): r['handler'] for r in listed(inventory, mode)}
    return {'unlisted': sorted(f'{m} {p}' for m, p in set(mounted) - set(expected)),
            'not_mounted': sorted(f'{m} {p}' for m, p in set(expected) - set(mounted)),
            'handler_differs': sorted(f'{m} {p}: listed {expected[(m, p)]}, mounted {mounted[(m, p)]}'
                                      for m, p in set(expected) & set(mounted) if expected[(m, p)] != mounted[(m, p)])}


def backend_operations():
    """The closed operation allow-lists of Backend._invoke_workbench and Backend._invoke_browser (set literals in backend.py)."""
    tree = ast.parse((SOURCE / 'backend.py').read_text(encoding='utf-8'))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ('_invoke_workbench', '_invoke_browser'):
            for item in ast.walk(node):
                if isinstance(item, ast.Assign) and [t.id for t in item.targets if isinstance(t, ast.Name)] == ['allowed']:
                    found[node.name[len('_invoke_'):]] = {e.value for e in item.value.elts}
    if set(found) != {'workbench', 'browser'}:
        raise InventoryInvalid('backend allow-lists not found')
    return found


def _sql_functions():
    names = set()
    for f in (SOURCE.parent / 'eios/migrations').glob('*.sql'):
        names |= {m.group(1).lower() for m in re.finditer(r'(?i)create(?:\s+or\s+replace)?\s+function\s+([a-z_]+\.[a-z0-9_]+)\s*\(', f.read_text())}
    return names


def port_exists(port, operations=None, sql=None):
    """backend.workbench:<op> / backend.browser:<op> (in the backend's allow-list and served), sql:<schema.function> (a migration
    defines it), or an importable dotted name module.Class.method / module.function."""
    if port.startswith('backend.'):
        realm, _, operation = port[len('backend.'):].partition(':')
        operations = operations if operations is not None else backend_operations()
        if operation not in operations.get(realm, ()):
            return False
        if realm == 'workbench':
            from nexloop_eios.workbench_reads import WorkbenchQueries
            return callable(getattr(WorkbenchQueries, operation, None))
        from nexloop_eios.backend import BrowserServices
        return callable(getattr(BrowserServices, operation, None))
    if port.startswith('sql:'):
        return port[4:].lower() in (sql if sql is not None else _sql_functions())
    parts = port.split('.')
    for split in range(len(parts) - 1, 0, -1):
        try:
            target = importlib.import_module('.'.join(parts[:split]))
        except ImportError:
            continue
        for name in parts[split:]:
            target = getattr(target, name, None)
            if target is None:
                return False
        return callable(target)
    return False


def port_name(port):
    """The name the handler must mention to be bound to its port: the operation, the function or the method."""
    return re.split(r'[.:]', port)[-1]


def handler_mentions(handler, name):
    """True when the handler's body - or a function of its module it calls directly - names `name` (identifier, attribute or string)."""
    module, _, function = handler.partition(':')
    tree = ast.parse(Path(importlib.import_module(module).__file__).read_text(encoding='utf-8'))
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node)

    word = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])')

    def mentions(node):
        # An identifier or attribute of that name, or the name as a whole word in a string (a verb, an SQL function call).
        for item in ast.walk(node):
            if isinstance(item, ast.Name) and item.id == name or isinstance(item, ast.Attribute) and item.attr == name:
                return True
            if isinstance(item, ast.Constant) and isinstance(item.value, str) and word.search(item.value):
                return True
        return False
    for node in functions.get(function, []):
        if mentions(node):
            return True
        called = {c.func.id for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        if any(mentions(other) for callee in called for other in functions.get(callee, []) if other is not node):
            return True
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description='NexLoop API route inventory (NX-062 C8)')
    p.add_argument('--diff', action='store_true', required=True)
    p.parse_args(argv)
    inventory = load()
    result = {}
    for mode in MODES:
        with tempfile.TemporaryDirectory(prefix='nexloop-routes-') as directory:
            result[mode] = diff(inventory, routes(complete_app(directory, mode)), mode)
    print(json.dumps(result, indent=1))
    return 1 if any(v for r in result.values() for v in r.values()) else 0


if __name__ == '__main__':
    sys.exit(main())
