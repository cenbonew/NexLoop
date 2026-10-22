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
# ADR-037 (owner 2026-10-22): the simulation process group has its own budget, outside the application's 60; the application, the
# simulation group and the operations reserve (administration, CI, backups) together fit max_connections minus the superuser
# reserve: 60 + 10 + 7 <= 80 - 3.
SIMULATION_POOL_LIMIT = 10
OPERATIONS_RESERVE = 7
SUPERUSER_RESERVED = 3
SCHEMA = 'nexloop.connection-budget.v1'
_NAME = re.compile('[a-z][a-z0-9-]{0,63}')
_SERVICE_KEYS = {'name', 'processes', 'pool_max'}
_RUNTIME_KEYS = {'name', 'processes', 'guard_workers', 'dispatcher_pool_max', 'guard_pool_max', 'dispatch_loops'}


# NX-031 D7: every long-running process that opens a PostgreSQL pool. A deployment budget must declare each of them, so the
# sum the doctor checks is the real ceiling; a missing entry fails the check (fixed here, like the limits).
BASE_SERVICES = ('api', 'api-browser-identity', 'domain-worker', 'action-worker', 'webchat-delivery', 'runtime-worker', 'message-relay', 'outbound-recorder')
LAUNCHERS = ('plan-reevaluator-launcher', 'reply-guarantor-launcher')


def required_services():
    from nexloop_eios.background_services import SERVICES
    return (*BASE_SERVICES, *SERVICES, *LAUNCHERS)


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
        # NX-031: the dispatch loops share the dispatcher pool (one short transaction at a time each); they add no pool.
        _positive(service['dispatch_loops'], 'dispatch_loops', 16)
        per_process = guard if workers == 1 else dispatcher + workers * guard
    else:
        if set(service) != _SERVICE_KEYS:
            raise BudgetInvalid('service entry has unexpected fields')
        per_process = _positive(service['pool_max'], 'pool_max', 32)
    return processes * per_process


def dispatch_loops(document):
    """Concurrent Runs the declared Runtime Workers can dispatch: processes x dispatch_loops over every runtime entry."""
    return sum(service['processes'] * service['dispatch_loops'] for service in document['services'] if 'guard_workers' in service)


def _simulation(document):
    """ADR-037: the simulation section (its own services, every process that connects - a single connection counts as 1)."""
    section = document.get('simulation')
    if type(section) is not dict or set(section) != {'services'} or type(section['services']) is not list or not section['services']:
        raise BudgetInvalid('connection budget simulation section is invalid')
    names = [service.get('name') if type(service) is dict else None for service in section['services']]
    if len(set(names)) != len(names) or set(names) & {s.get('name') for s in document['services'] if type(s) is dict}:
        raise BudgetInvalid('simulation service names must be unique and not application services')
    if any(type(n) is not str or not n.startswith('sim-') for n in names):
        raise BudgetInvalid('simulation service names start with sim-')
    return {service['name']: service_connections(service) for service in section['services']}


def evaluate(document, *, max_connections=None):
    """Check a parsed budget document (and, when given, the server's max_connections)."""
    if type(document) is not dict or document.get('schema') != SCHEMA or type(document.get('services')) is not list:
        raise BudgetInvalid('connection budget document is invalid')
    if set(document) - {'schema', 'decision', 'services', 'simulation'}:
        raise BudgetInvalid('connection budget document has unexpected fields')
    if not document['services']:
        raise BudgetInvalid('connection budget declares no services')
    names = [service.get('name') if type(service) is dict else None for service in document['services']]
    if len(set(names)) != len(names):
        raise BudgetInvalid('service names must be unique')
    per_service = {service['name']: service_connections(service) for service in document['services']}
    total = sum(per_service.values())
    missing = sorted(set(required_services()) - set(per_service))
    # Every dispatch loop keeps one Run active on the APP_HOST's Agent Host; more loops than the Host's deployment limit
    # (ADR-022 §4) would only wait in its admission queue and time out the dispatcher's start request.
    from nexloop_eios.host_concurrency import MAXIMUM_HOST_ACTIVE_RUNS
    loops = dispatch_loops(document)
    simulation = _simulation(document)
    simulation_total = sum(simulation.values())
    checks = {'application_pool_total': total <= APPLICATION_POOL_LIMIT, 'all_services_declared': not missing,
              'runtime_dispatch_loops': loops <= MAXIMUM_HOST_ACTIVE_RUNS, 'simulation_pool_total': simulation_total <= SIMULATION_POOL_LIMIT}
    if max_connections is not None:
        checks['max_connections'] = type(max_connections) is int and max_connections >= MIN_MAX_CONNECTIONS
        # ADR-037: both budgets and the operations reserve fit the server's capacity (max_connections minus the superuser reserve).
        checks['server_capacity'] = (type(max_connections) is int
                                     and total + simulation_total + OPERATIONS_RESERVE <= max_connections - SUPERUSER_RESERVED)
    return {'checks': checks, 'passed': all(checks.values()),
            'details': {'services': per_service, 'application_pool_total': total, 'missing_services': missing,
                        'application_pool_limit': APPLICATION_POOL_LIMIT, 'max_connections': max_connections,
                        'min_max_connections': MIN_MAX_CONNECTIONS, 'runtime_dispatch_loops': loops,
                        'host_active_runs_limit': MAXIMUM_HOST_ACTIVE_RUNS, 'simulation_services': simulation,
                        'simulation_pool_total': simulation_total, 'simulation_pool_limit': SIMULATION_POOL_LIMIT,
                        'operations_reserve': OPERATIONS_RESERVE, 'superuser_reserved': SUPERUSER_RESERVED}}


def load(path):
    try:
        return json.loads(open(path, encoding='utf-8').read())
    except (OSError, ValueError):
        raise BudgetInvalid('connection budget file is unreadable') from None
