#!/usr/bin/env python3
"""Post-deploy check (the dispatcher's manual read-only checks after each deployment, release 24 onwards), on APP_HOST, read only.

    post_deploy_check.py --stage-env <private stage.env> [--compose deploy/stage/compose.app.example.yaml] [--project nexloop-app]
                         [--route /api/v1/workbench/x ... | --routes-file routes.txt] [--control-route /api/v1/<absent>]
                         [--log-minutes 30] [--database-url-file <read-only DSN file> --expected-revision NNNN] [--cacert <gateway CA>]
                         [--clock-skew-warn 0.5] [--clock-skew-fail 2.0]
    post_deploy_check.py --capture capture.json ...      the same decisions on saved docker / curl outputs (tests, review)

Checks (each pass or fail, with its evidence as counts, codes and container names):
1. containers   every container of the Compose project is running (Up) and has restarted 0 times (docker ps / inspect);
2. netns        every service the Compose file runs with `network_mode: service:<x>` (the relays, webchat-delivery, the app-net
                workers) shares the network namespace of x's container (HostConfig.NetworkMode = container:<x's id>);
3. gateway      GET https://<APP_HOST_NAME>:<GATEWAY_HTTPS_PORT>/ answers 200 and http://...:<GATEWAY_REDIRECT_PORT>/ answers 301;
4. routes       each listed route answers 401 without a session (it exists) and the control route answers 404;
5. logs         in the last N minutes of each container's log: tick failed, Traceback, UndefinedFunction / 42883, 42P01,
                dependency_unavailable (counts; any is a failure);
6. database     (with --database-url-file) the lineage in a read-only transaction is exact and its revision is the expected one
                (nexloop_eios.bootstrap.verify, the doctor's measure; default expectation: this checkout's catalog tail).
7. clock_skew   (with --database-url-file) the database clock against this host's clock: a few round trips, each estimated at the
                midpoint of its round trip (skew = clock_timestamp() - (sent + received) / 2), the sample with the shortest round trip
                kept; |skew| above --clock-skew-fail (2 s) fails, above --clock-skew-warn (0.5 s) passes with a warning. Signed
                requests between the hosts carry a few seconds of tolerance; a skew past it refuses every signature.
It never writes, never prints a secret, a log line, an address or a credential: addresses and ports come from the stage env or the
arguments. Reuses running_snapshot (docker CLI, Compose labels), deploy (stage env) and bootstrap (catalog, verify).
Prints one JSON object. Exit 0 pass, 1 fail, 2 input error.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from running_snapshot import PROJECT_LABEL, SERVICE_LABEL, _labels  # noqa: E402

COMPOSE = ROOT / 'deploy/stage/compose.app.example.yaml'
LOG_PATTERNS = {'tick_failed': re.compile(r'tick failed', re.I), 'traceback': re.compile(r'Traceback \(most recent call last\)'),
                'undefined_function': re.compile(r'UndefinedFunction|\b42883\b'), 'undefined_table': re.compile(r'UndefinedTable|\b42P01\b'),
                'dependency_unavailable': re.compile(r'dependency_unavailable')}
CONTROL_ROUTE = '/api/v1/workbench/post-deploy-check-absent'


class InputError(ValueError):
    pass


# ---- the outside world: docker and curl (a live runner, or a saved capture) ----
class Live:
    def __init__(self, cacert=None):
        self.cacert = cacert

    def docker(self, *args):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError('docker ' + args[0] + ' failed')
        return result.stdout + (result.stderr if args[0] == 'logs' else '')

    def status(self, url):
        """The HTTP status of one GET, without following redirects; 0 when there is no answer. Nothing of the body is kept."""
        command = ['curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '10', '--proto', '=http,https']
        if self.cacert:
            command += ['--cacert', str(self.cacert)]
        result = subprocess.run([*command, url], capture_output=True, text=True, timeout=20)
        return int(result.stdout) if result.stdout.strip().isdigit() else 0


class Capture:
    """Saved outputs: {"docker": {"<args joined by space>": "<stdout>"}, "http": {"<url>": <status>}} (tests and review)."""

    def __init__(self, value):
        self.value = value

    def docker(self, *args):
        key = ' '.join(args)
        if key not in self.value.get('docker', {}):
            raise RuntimeError('docker ' + args[0] + ' failed')
        return self.value['docker'][key]

    def status(self, url):
        return int(self.value.get('http', {}).get(url, 0))


# ---- the checks ----
def containers(run, project):
    """[{name, service, id, state, restarts}] of the project's containers (all states, so an exited one is seen)."""
    out = []
    for line in run.docker('ps', '-a', '--filter', f'label={PROJECT_LABEL}={project}', '--format', '{{json .}}').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        info = json.loads(run.docker('inspect', '--format', '{{json .}}', row['ID']))
        labels = _labels(info.get('Config', {}).get('Labels') or row.get('Labels'))
        out.append({'name': row.get('Names', row['ID']), 'service': labels.get(SERVICE_LABEL, ''), 'id': info.get('Id', row['ID']),
                    'state': (info.get('State') or {}).get('Status', row.get('State', '')), 'restarts': int(info.get('RestartCount', 0)),
                    'network_mode': (info.get('HostConfig') or {}).get('NetworkMode', '')})
    return out


def check_containers(rows):
    bad = [{'name': r['name'], 'state': r['state'], 'restarts': r['restarts']} for r in rows if r['state'] != 'running' or r['restarts'] != 0]
    return {'pass': bool(rows) and not bad, 'containers': len(rows), 'failing': bad, **({} if rows else {'reason': 'no_containers'})}


def shared_namespaces(compose_path):
    """{service: target service} for every service the Compose file runs in another service's network namespace."""
    import yaml
    value = yaml.safe_load(Path(compose_path).read_text())
    out = {}
    for name, service in (value.get('services') or {}).items():
        mode = str((service or {}).get('network_mode') or '')
        if mode.startswith('service:'):
            out[name] = mode.split(':', 1)[1]
    return out


def check_netns(rows, expected):
    by_service = {r['service']: r for r in rows}
    bad = []
    for service, target in sorted(expected.items()):
        mine, theirs = by_service.get(service), by_service.get(target)
        if mine is None or theirs is None:
            bad.append({'service': service, 'target': target, 'reason': 'container_missing'})
        elif mine['network_mode'] not in ('container:' + theirs['id'], 'container:' + theirs['name']):
            bad.append({'service': service, 'target': target, 'reason': 'namespace_not_shared'})
    return {'pass': not bad, 'checked': len(expected), 'failing': bad}


def check_gateway(run, https_base, http_base):
    got = {'https_root': run.status(https_base + '/'), 'http_root': run.status(http_base + '/')}
    return {'pass': got == {'https_root': 200, 'http_root': 301}, 'status': got, 'expected': {'https_root': 200, 'http_root': 301}}


def check_routes(run, https_base, routes, control):
    got = {r: run.status(https_base + r) for r in routes}
    absent = run.status(https_base + control)
    bad = sorted(r for r, s in got.items() if s != 401)
    return {'pass': not bad and absent == 404, 'routes': got, 'control': {control: absent}, 'failing': bad + ([control] if absent != 404 else [])}


def check_logs(run, rows, minutes):
    counts = {}
    for r in rows:
        text = run.docker('logs', '--since', f'{minutes}m', r['id'])
        hit = {k: len(p.findall(text)) for k, p in LOG_PATTERNS.items()}
        hit = {k: v for k, v in hit.items() if v}
        if hit:
            counts[r['name']] = hit
    return {'pass': not counts, 'minutes': minutes, 'containers': len(rows), 'hits': counts}


def check_database(database_url, expected_revision):
    from nexloop_eios.bootstrap import verify
    import psycopg
    try:
        with psycopg.connect(database_url, connect_timeout=5) as db, db.transaction():
            db.execute('set transaction read only')
            got = verify(db)
    except Exception as error:  # noqa: BLE001 - the class name only, never the message (it may hold a connection detail)
        return {'pass': False, 'reason': type(error).__name__, 'expected_revision': expected_revision}
    return {'pass': got['revision'] == expected_revision, 'revision': got['revision'], 'migration_count': got['migration_count'],
            'expected_revision': expected_revision}


def estimate_clock_skew(sample, samples=5):
    """sample() -> (sent, db, received): this host's wall clock before and after one query and the database's clock in between
    (epoch seconds). Each round trip is read at its midpoint; the shortest round trip is the tightest bound (|error| <= rtt / 2)."""
    best = None
    for _ in range(samples):
        sent, db, received = sample()
        rtt = max(0.0, received - sent)
        if best is None or rtt < best[1]:
            best = (db - (sent + received) / 2, rtt)
    return best


def check_clock_skew(database_url, warn_seconds=0.5, fail_seconds=2.0, samples=5, clock=time.time):
    """The database clock minus this host's clock (seconds; positive: the database is ahead). Only the skew and round trip print."""
    import psycopg
    try:
        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as db:
            db.execute('select 1')  # the connection's first round trip is not a sample

            def sample():
                sent = clock()
                value = db.execute('select extract(epoch from clock_timestamp())::float8').fetchone()[0]
                return sent, float(value), clock()
            skew, rtt = estimate_clock_skew(sample, samples)
    except Exception as error:  # noqa: BLE001 - the class name only, never the message (it may hold a connection detail)
        return {'pass': False, 'reason': type(error).__name__}
    out = {'pass': abs(skew) <= fail_seconds, 'skew_seconds': round(skew, 3), 'round_trip_seconds': round(rtt, 3),
           'warn_seconds': warn_seconds, 'fail_seconds': fail_seconds}
    if out['pass'] and abs(skew) > warn_seconds:
        out['warning'] = 'clock_skew'
    return out


# NX-041 S6c (L6 nx041-s6c-review 4.2): each simulation DSN file and the role it must log in as (never a real role).
SIM_DSN_ROLES = {'sim_worker_dsn': 'nexloop_sim_worker', 'sim_guard_dsn': 'nexloop_sim_guard', 'sim_api_dsn': 'nexloop_sim_api',
                 'sim_stand_in_worker_dsn': 'nexloop_stand_in_worker', 'sim_files_dsn': 'nexloop_sim_files',
                 # Ninth batch: the consumer twin port (NX-042 T3; pg_hba simulation line), used by AT-046's intake part.
                 'sim_twin_port_dsn': 'nexloop_twin_port'}


SIM_COMPOSE = Path(__file__).resolve().parents[2] / 'deploy/stage/compose.simulation.example.yaml'


def required_sim_dsns(compose_path=SIM_COMPOSE):
    """The simulation DSN files the simulation project mounts (its *_dsn secrets): each must be present."""
    import yaml
    secrets = yaml.safe_load(Path(compose_path).read_text()).get('secrets') or {}
    return sorted(name for name in secrets if name.endswith('_dsn'))


def sim_dsn_files(directory):
    """Every *_dsn file of the simulation secret directory (an unknown one then fails the check), read privately."""
    from nexloop_eios.private_configuration import read_private_text
    return {p.name: read_private_text(p, maximum=4096).strip() for p in sorted(Path(directory).iterdir()) if p.name.endswith('_dsn')}


def _sim_role(dsn):
    """(role row, None) or (None, reason): unreachable, authentication_failed, read_only_transaction_failed or role_query_failed -
    a class of failure, never the DSN or a server message."""
    import psycopg
    try:
        db = psycopg.connect(dsn, connect_timeout=5, autocommit=True)  # the read-only transaction below is explicit
    except psycopg.OperationalError as error:
        state = getattr(error, 'sqlstate', None) or getattr(getattr(error, 'diag', None), 'sqlstate', None)
        text = str(error)
        auth = state in ('28P01', '28000') or 'password authentication failed' in text or 'no pg_hba.conf entry' in text
        return None, 'authentication_failed' if auth else 'unreachable'
    except Exception:  # noqa: BLE001 - a malformed DSN and the like: nothing of it is echoed
        return None, 'unreachable'
    with db:
        try:
            db.execute('begin read only')
        except Exception:  # noqa: BLE001
            return None, 'read_only_transaction_failed'
        try:
            row = db.execute('''select current_user::text,session_user::text,r.rolsuper or r.rolcreaterole or r.rolcreatedb or r.rolreplication or r.rolbypassrls,
                exists(select 1 from pg_auth_members m where m.member=r.oid),
                has_function_privilege(current_user,'authz.nexloop_real_dispatch_summary()','execute')
                from pg_roles r where r.rolname=current_user''').fetchone()
            db.execute('rollback')
            return row, None
        except Exception:  # noqa: BLE001
            return None, 'role_query_failed'


def check_sim_roles(dsns, required=None):
    """{simulation DSN file name: DSN} -> every DSN logs in as exactly its simulation role: current_user = session_user = the role
    of SIM_DSN_ROLES, no elevated attribute or role membership, and no execute right on the real dispatch summary. A required file
    (the simulation project's DSN secrets) that is missing, and any *_dsn file that is not a known simulation DSN, fail the check.
    Read only; a failure names the file, the role it got and a class of reason, never the DSN."""
    if not dsns and not required:
        return {'pass': False, 'reason': 'no simulation DSN given'}
    out, ok = {}, True
    for name in sorted(set(required or ()) - set(dsns)):
        out[name], ok = {'pass': False, 'reason': 'missing', 'expected_role': SIM_DSN_ROLES.get(name)}, False
    for name in sorted(dsns):
        expected = SIM_DSN_ROLES.get(name)
        if expected is None:
            out[name], ok = {'pass': False, 'reason': 'unknown simulation DSN file'}, False
            continue
        row, reason = _sim_role(dsns[name])
        if reason is not None:
            out[name], ok = {'pass': False, 'reason': reason, 'expected_role': expected}, False
            continue
        good = row is not None and row[0] == row[1] == expected and not row[2] and not row[3] and not row[4]
        out[name] = {'pass': good, 'role': row[0] if row else None, 'expected_role': expected}
        if not good:
            out[name]['reason'] = 'wrong_role' if row and row[0] != expected else 'elevated_or_real_access'
        ok = ok and good
    return {'pass': ok, 'dsns': out}


def run_checks(run, *, project, compose, https_base, http_base, routes, control, minutes, database_url=None, expected_revision=None, sim_dsns=None,
               sim_required=None, clock_skew=(0.5, 2.0)):
    results = {}
    try:
        rows = containers(run, project)
    except RuntimeError as error:
        rows, results['containers'] = [], {'pass': False, 'reason': str(error)}
    if 'containers' not in results:
        results['containers'] = check_containers(rows)
    results['netns'] = check_netns(rows, shared_namespaces(compose))
    results['gateway'] = check_gateway(run, https_base, http_base)
    results['routes'] = check_routes(run, https_base, routes, control)
    try:
        results['logs'] = check_logs(run, rows, minutes)
    except RuntimeError as error:
        results['logs'] = {'pass': False, 'reason': str(error)}
    if database_url is not None:
        results['database'] = check_database(database_url, expected_revision)
        results['clock_skew'] = check_clock_skew(database_url, *clock_skew)
    if sim_dsns is not None:
        results['sim_roles'] = check_sim_roles(sim_dsns, sim_required)
    failed = sorted(k for k, v in results.items() if not v['pass'])
    warnings = sorted(k for k, v in results.items() if v.get('warning'))
    return {'schema': 'nexloop-post-deploy-check/1', 'project': project, 'pass': not failed, 'failed': failed, 'warnings': warnings, 'checks': results}


def _routes(args):
    routes = list(args.route or [])
    if args.routes_file:
        routes += [line.strip() for line in Path(args.routes_file).read_text().splitlines() if line.strip() and not line.startswith('#')]
    if any(not re.fullmatch(r'/[A-Za-z0-9/_.-]{1,200}', r) for r in routes + [args.control_route]):
        raise InputError('a route is a path (/api/...) without host, query or credentials')
    return routes


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only post-deploy check (APP_HOST)')
    parser.add_argument('--stage-env', required=True, help='the private stage env (APP_HOST_NAME, GATEWAY_HTTPS_PORT, GATEWAY_REDIRECT_PORT)')
    parser.add_argument('--compose', default=str(COMPOSE))
    parser.add_argument('--project')
    parser.add_argument('--route', action='append')
    parser.add_argument('--routes-file')
    parser.add_argument('--control-route', default=CONTROL_ROUTE)
    parser.add_argument('--log-minutes', type=int, default=30)
    parser.add_argument('--database-url-file')
    parser.add_argument('--expected-revision')
    parser.add_argument('--clock-skew-warn', type=float, default=0.5, help='seconds of DB/APP_HOST clock skew that warn (with --database-url-file)')
    parser.add_argument('--clock-skew-fail', type=float, default=2.0, help='seconds of DB/APP_HOST clock skew that fail')
    parser.add_argument('--sim-secret-dir', help='NX-041: the simulation secret directory; each sim_*_dsn file in it must log in as its sim role')
    parser.add_argument('--cacert')
    parser.add_argument('--capture')
    args = parser.parse_args(argv)
    try:
        from deploy import _env
        env = _env(args.stage_env)
        host, https_port, http_port = env.get('APP_HOST_NAME'), env.get('GATEWAY_HTTPS_PORT'), env.get('GATEWAY_REDIRECT_PORT')
        if not host or not (https_port or '').isdigit() or not (http_port or '').isdigit():
            raise InputError('the stage env names APP_HOST_NAME, GATEWAY_HTTPS_PORT and GATEWAY_REDIRECT_PORT')
        if not 1 <= args.log_minutes <= 1440:
            raise InputError('log minutes 1..1440')
        if not 0 < args.clock_skew_warn <= args.clock_skew_fail <= 60:
            raise InputError('clock skew thresholds: 0 < warn <= fail <= 60 seconds')
        routes = _routes(args)
        import yaml
        project = args.project or yaml.safe_load(Path(args.compose).read_text()).get('name')
        if not project:
            raise InputError('the Compose project name')
        database_url = expected = None
        if args.database_url_file:
            from nexloop_eios.bootstrap import catalog
            from nexloop_eios.private_configuration import read_private_text
            database_url = read_private_text(args.database_url_file, maximum=4096).strip()
            expected = args.expected_revision or catalog()[-1].version
        sim_dsns = sim_required = None
        if args.sim_secret_dir:
            sim_dsns, sim_required = sim_dsn_files(args.sim_secret_dir), required_sim_dsns()
        runner = Capture(json.loads(Path(args.capture).read_text())) if args.capture else Live(args.cacert)
    except (InputError, OSError, ValueError) as error:
        print(json.dumps({'schema': 'nexloop-post-deploy-check/1', 'pass': False, 'error': type(error).__name__,
                          'detail': str(error) if isinstance(error, InputError) else None}))
        return 2
    out = run_checks(runner, project=project, compose=args.compose, https_base=f'https://{host}:{https_port}', http_base=f'http://{host}:{http_port}',
                     routes=routes, control=args.control_route, minutes=args.log_minutes, database_url=database_url, expected_revision=expected,
                     sim_dsns=sim_dsns, sim_required=sim_required, clock_skew=(args.clock_skew_warn, args.clock_skew_fail))
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0 if out['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
