"""PostgreSQL connection budget of a deployment (11_DEPLOYMENT §6, ADR-024).

The versioned deployment declares every service process that opens a PostgreSQL
pool (deploy/stage/connection-budget.v1.json). The policy limits below are fixed
here, not read from that file, so a deployment file cannot loosen them:
the sum of pool max x process count must stay within APPLICATION_POOL_LIMIT and
the server must allow at least MIN_MAX_CONNECTIONS (the difference is reserved
for operations and recovery).
"""
import json
import re

APPLICATION_POOL_LIMIT = 60
MIN_MAX_CONNECTIONS = 80
SCHEMA = 'nexloop.connection-budget.v1'
_NAME = re.compile('[a-z][a-z0-9-]{0,63}')
_SERVICE_KEYS = {'name', 'processes', 'pool_max'}
_RUNTIME_KEYS = {'name', 'processes', 'guard_workers', 'dispatcher_pool_max', 'guard_pool_max'}


class BudgetInvalid(ValueError):
    pass


def _positive(value, field, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise BudgetInvalid(f'{field} must be an integer 1..{maximum}')
    return value


def service_connections(service):
    """Connections one declared service may hold at most."""
    if type(service) is not dict or type(service.get('name')) is not str or not _NAME.fullmatch(service['name']):
        raise BudgetInvalid('service name must be a lowercase identifier')
    processes = _positive(service.get('processes'), 'processes', 64)
    if 'guard_workers' in service:
        # Runtime Worker (ADR-024): N=1 -> one Backend serves dispatcher and guard (guard pool);
        # N>1 -> dispatcher-only parent pool + N guard child pools.
        if set(service) != _RUNTIME_KEYS:
            raise BudgetInvalid('runtime worker entry has unexpected fields')
        workers = _positive(service['guard_workers'], 'guard_workers', 16)
        dispatcher = _positive(service['dispatcher_pool_max'], 'dispatcher_pool_max', 32)
        guard = _positive(service['guard_pool_max'], 'guard_pool_max', 32)
        if guard < 2:
            raise BudgetInvalid('guard_pool_max must be at least 2')
        per_process = guard if workers == 1 else dispatcher + workers * guard
    else:
        if set(service) != _SERVICE_KEYS:
            raise BudgetInvalid('service entry has unexpected fields')
        per_process = _positive(service['pool_max'], 'pool_max', 32)
    return processes * per_process


def evaluate(document, *, max_connections=None):
    """Check a parsed budget document (and, when given, the server's max_connections)."""
    if type(document) is not dict or document.get('schema') != SCHEMA or type(document.get('services')) is not list:
        raise BudgetInvalid('connection budget document is invalid')
    if not document['services']:
        raise BudgetInvalid('connection budget declares no services')
    names = [service.get('name') if type(service) is dict else None for service in document['services']]
    if len(set(names)) != len(names):
        raise BudgetInvalid('service names must be unique')
    per_service = {service['name']: service_connections(service) for service in document['services']}
    total = sum(per_service.values())
    checks = {'application_pool_total': total <= APPLICATION_POOL_LIMIT}
    if max_connections is not None:
        checks['max_connections'] = type(max_connections) is int and max_connections >= MIN_MAX_CONNECTIONS
    return {'checks': checks, 'passed': all(checks.values()),
            'details': {'services': per_service, 'application_pool_total': total,
                        'application_pool_limit': APPLICATION_POOL_LIMIT, 'max_connections': max_connections,
                        'min_max_connections': MIN_MAX_CONNECTIONS}}


def load(path):
    try:
        return json.loads(open(path, encoding='utf-8').read())
    except (OSError, ValueError):
        raise BudgetInvalid('connection budget file is unreadable') from None
