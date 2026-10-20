"""scripts/deploy/post_deploy_check.py on fake docker and curl outputs: every check passes on a healthy capture and fails for its own
reason; the output holds counts, codes and container names only (no host, address, log line or secret). The database check runs on
the test PostgreSQL in a read-only transaction."""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import post_deploy_check as P  # noqa: E402

PROJECT = 'nexloop-app'
HOST = 'app.stage.invalid'
HTTPS, HTTP = f'https://{HOST}:8443', f'http://{HOST}:8080'
EXPECTED = P.shared_namespaces(P.COMPOSE)
SERVICES = sorted({*EXPECTED, *EXPECTED.values(), 'gateway'})


def capture(*, state=None, restarts=None, netns=None, http=None, logs=None, drop=()):
    docker, ps = {}, []
    for i, service in enumerate(SERVICES):
        if service in drop:
            continue
        cid = f'{i:064x}'
        ps.append(json.dumps({'ID': cid[-12:], 'Names': f'{PROJECT}-{service}-1', 'State': 'running'}))
        docker[f'inspect --format {{{{json .}}}} {cid[-12:]}'] = json.dumps({'Id': cid, 'RestartCount': (restarts or {}).get(service, 0),
            'State': {'Status': (state or {}).get(service, 'running')}, 'Config': {'Labels': {P.PROJECT_LABEL: PROJECT, P.SERVICE_LABEL: service}},
            'HostConfig': {'NetworkMode': ''}})
        docker[f'logs --since 30m {cid}'] = (logs or {}).get(service, 'ok\n')
    ids = {json.loads(docker[k])['Config']['Labels'][P.SERVICE_LABEL]: json.loads(docker[k])['Id'] for k in docker if k.startswith('inspect')}
    for k in list(docker):
        if k.startswith('inspect'):
            row = json.loads(docker[k]);service = row['Config']['Labels'][P.SERVICE_LABEL]
            if service in EXPECTED and EXPECTED[service] in ids:
                row['HostConfig']['NetworkMode'] = (netns or {}).get(service, 'container:' + ids[EXPECTED[service]])
            docker[k] = json.dumps(row)
    docker[f'ps -a --filter label={P.PROJECT_LABEL}={PROJECT} --format {{{{json .}}}}'] = '\n'.join(ps)
    status = {HTTPS + '/': 200, HTTP + '/': 301, HTTPS + '/api/v1/workbench/data-health': 401, HTTPS + P.CONTROL_ROUTE: 404, **(http or {})}
    return {'docker': docker, 'http': status}


def run(value, **over):
    args = dict(project=PROJECT, compose=P.COMPOSE, https_base=HTTPS, http_base=HTTP, routes=['/api/v1/workbench/data-health'], control=P.CONTROL_ROUTE, minutes=30)
    return P.run_checks(P.Capture(value), **{**args, **over})


def test_a_healthy_deployment_passes_every_check():
    out = run(capture())
    assert out['pass'] is True and out['failed'] == [] and set(out['checks']) == {'containers', 'netns', 'gateway', 'routes', 'logs'}
    assert out['checks']['netns']['checked'] == len(EXPECTED) and {'webchat-delivery', 'agent-link-relay'} <= set(EXPECTED)
    assert HOST not in json.dumps(out)  # no host or address in the output


@pytest.mark.parametrize('case,failed', [
    (dict(state={'gateway': 'restarting'}), 'containers'), (dict(restarts={'webchat-delivery': 2}), 'containers'),
    (dict(netns={'webchat-delivery': 'bridge'}), 'netns'), (dict(drop=('action-worker',)), 'netns'),
    (dict(http={HTTPS + '/': 502}), 'gateway'), (dict(http={HTTP + '/': 200}), 'gateway'),
    (dict(http={HTTPS + '/api/v1/workbench/data-health': 404}), 'routes'), (dict(http={HTTPS + P.CONTROL_ROUTE: 401}), 'routes'),
    (dict(logs={'gateway': 'x\nTraceback (most recent call last):\n  ...'}), 'logs'), (dict(logs={'gateway': 'runtime tick failed: x'}), 'logs'),
    (dict(logs={'gateway': 'psycopg.errors.UndefinedFunction: ...'}), 'logs'), (dict(logs={'gateway': 'SQLSTATE 42P01'}), 'logs'),
    (dict(logs={'gateway': '{"code":"dependency_unavailable"}'}), 'logs')])
def test_each_check_fails_for_its_own_reason(case, failed):
    out = run(capture(**case))
    assert out['pass'] is False and failed in out['failed'], out['failed']


def test_failures_name_containers_and_counts_never_a_log_line():
    out = run(capture(restarts={'gateway': 1}, logs={'gateway': 'Traceback (most recent call last):\nsecret-looking line\nTraceback (most recent call last):'}))
    assert out['checks']['containers']['failing'] == [{'name': f'{PROJECT}-gateway-1', 'state': 'running', 'restarts': 1}]
    assert out['checks']['logs']['hits'] == {f'{PROJECT}-gateway-1': {'traceback': 2}} and 'secret-looking' not in json.dumps(out)


def test_no_container_and_a_docker_failure_fail_closed():
    empty = capture(drop=SERVICES)
    assert run(empty)['checks']['containers'] == {'pass': False, 'containers': 0, 'failing': [], 'reason': 'no_containers'}
    broken = capture();broken['docker'] = {}
    out = run(broken)
    assert out['pass'] is False and out['checks']['containers'] == {'pass': False, 'reason': 'docker ps failed'}


def test_cli_reads_the_stage_env_and_routes_and_refuses_bad_input(tmp_path, capsys):
    env = tmp_path / 'stage.env';env.write_text(f'APP_HOST_NAME={HOST}\nGATEWAY_HTTPS_PORT=8443\nGATEWAY_REDIRECT_PORT=8080\n')
    cap = tmp_path / 'capture.json';cap.write_text(json.dumps(capture()))
    routes = tmp_path / 'routes.txt';routes.write_text('# release 25\n/api/v1/workbench/data-health\n')
    assert P.main(['--stage-env', str(env), '--capture', str(cap), '--routes-file', str(routes)]) == 0
    assert json.loads(capsys.readouterr().out)['pass'] is True
    bad = copy.deepcopy(capture());bad['http'][HTTP + '/'] = 200;cap.write_text(json.dumps(bad))
    assert P.main(['--stage-env', str(env), '--capture', str(cap), '--routes-file', str(routes)]) == 1
    assert json.loads(capsys.readouterr().out)['failed'] == ['gateway']
    assert P.main(['--stage-env', str(env), '--capture', str(cap), '--route', 'https://elsewhere/x']) == 2
    capsys.readouterr()
    env.write_text('APP_HOST_NAME=\n');assert P.main(['--stage-env', str(env), '--capture', str(cap)]) == 2


def test_database_lineage_in_a_read_only_transaction(pg, admin):
    from nexloop_eios.bootstrap import bootstrap, catalog
    bootstrap(admin)
    tail = catalog()[-1].version
    assert P.check_database(pg, tail) == {'pass': True, 'revision': tail, 'migration_count': len(catalog()), 'expected_revision': tail}
    assert P.check_database(pg, '9999')['pass'] is False
    assert P.check_database('postgresql://127.0.0.1:1/none', tail) == {'pass': False, 'reason': 'OperationalError', 'expected_revision': tail}


def test_each_simulation_dsn_logs_in_as_its_own_simulation_role(pg, admin):
    """NX-041 S6c (L6 review 4.2): every sim DSN must be its simulation role's; a real role's DSN, an unknown file name or an
    unreachable DSN fails the check, naming the file and the role only."""
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    good = {name: make_conninfo(pg, user=role) for name, role in P.SIM_DSN_ROLES.items()}
    out = P.check_sim_roles(good)
    assert out['pass'] is True and {v['role'] for v in out['dsns'].values()} == set(P.SIM_DSN_ROLES.values())
    for wrong in ('nexloop_domain_worker', 'nexloop_api', 'nexloop_sim_guard'):
        out = P.check_sim_roles({**good, 'sim_worker_dsn': make_conninfo(pg, user=wrong)})
        assert out['pass'] is False and out['dsns']['sim_worker_dsn'] == {'pass': False, 'role': wrong, 'expected_role': 'nexloop_sim_worker', 'reason': 'wrong_role'}
    assert P.check_sim_roles({**good, 'sim_other_dsn': good['sim_api_dsn']})['pass'] is False
    broken = P.check_sim_roles({'sim_guard_dsn': make_conninfo(pg, user='nexloop_sim_guard', port=1)})
    assert broken['pass'] is False and broken['dsns']['sim_guard_dsn']['reason'] == 'unreachable' and 'port=1' not in json.dumps(broken)
    assert P.check_sim_roles({})['pass'] is False


def test_a_missing_or_unknown_simulation_dsn_file_fails(pg, admin):
    """L6 re-verify: the simulation project's DSN secrets must all be present and every *_dsn file must be a known one."""
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    required = P.required_sim_dsns()
    assert required == ['sim_files_dsn', 'sim_guard_dsn', 'sim_stand_in_worker_dsn', 'sim_worker_dsn'] and set(required) <= set(P.SIM_DSN_ROLES)
    good = {name: make_conninfo(pg, user=P.SIM_DSN_ROLES[name]) for name in required}
    assert P.check_sim_roles(good, required)['pass'] is True
    missing = P.check_sim_roles({k: v for k, v in good.items() if k != 'sim_guard_dsn'}, required)
    assert missing['pass'] is False and missing['dsns']['sim_guard_dsn'] == {'pass': False, 'reason': 'missing', 'expected_role': 'nexloop_sim_guard'}
    extra = P.check_sim_roles({**good, 'old_worker_dsn': good['sim_worker_dsn']}, required)
    assert extra['pass'] is False and extra['dsns']['old_worker_dsn']['reason'] == 'unknown simulation DSN file'
    assert P.check_sim_roles({}, required)['pass'] is False


def test_every_dsn_file_of_the_simulation_secret_directory_is_checked(tmp_path, monkeypatch):
    secrets = tmp_path / 'sim'
    secrets.mkdir(mode=0o700)
    for name in ('sim_worker_dsn', 'stray_dsn', 'sim_host_cert'):
        (secrets / name).write_text('host=127.0.0.1 user=x\n')
        (secrets / name).chmod(0o600)
    assert P.sim_dsn_files(secrets) == {'sim_worker_dsn': 'host=127.0.0.1 user=x', 'stray_dsn': 'host=127.0.0.1 user=x'}  # not the certificate
    seen = {}
    monkeypatch.setattr(P, 'check_sim_roles', lambda dsns, required=None: seen.update(dsns=dsns, required=required) or {'pass': False})
    out = run(capture(), sim_dsns={'a': 'b'}, sim_required=['c'])
    assert out['pass'] is False and 'sim_roles' in out['failed'] and seen == {'dsns': {'a': 'b'}, 'required': ['c']}


@pytest.mark.parametrize('failure,reason', [
    ('auth', 'authentication_failed'), ('hba', 'authentication_failed'), ('refused', 'unreachable'),
    ('read_only', 'read_only_transaction_failed'), ('query', 'role_query_failed'), ('malformed', 'unreachable')])
def test_a_failure_is_classified_and_never_echoes_the_dsn(monkeypatch, failure, reason):
    import psycopg
    secret = 'password=SYNTHETIC_SECRET_SENTINEL host=private.invalid'

    class Fake:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *a):
            if failure == 'read_only' and sql.startswith('begin'): raise psycopg.errors.ReadOnlySqlTransaction('SYNTHETIC_SECRET_SENTINEL')
            if failure == 'query' and sql.startswith('select'): raise psycopg.errors.UndefinedFunction('SYNTHETIC_SECRET_SENTINEL')
            return self
        def fetchone(self): return ('nexloop_sim_worker', 'nexloop_sim_worker', False, False, False)

    def connect(dsn, **kw):
        if failure == 'auth': raise psycopg.OperationalError('FATAL:  password authentication failed for user "x" SYNTHETIC_SECRET_SENTINEL')
        if failure == 'hba': raise psycopg.OperationalError('FATAL:  no pg_hba.conf entry for host SYNTHETIC_SECRET_SENTINEL')
        if failure == 'refused': raise psycopg.OperationalError('connection refused SYNTHETIC_SECRET_SENTINEL')
        if failure == 'malformed': raise psycopg.ProgrammingError('invalid dsn SYNTHETIC_SECRET_SENTINEL')
        return Fake()
    monkeypatch.setattr(psycopg, 'connect', connect)
    out = P.check_sim_roles({'sim_worker_dsn': secret})
    assert out['pass'] is False and out['dsns']['sim_worker_dsn'] == {'pass': False, 'reason': reason, 'expected_role': 'nexloop_sim_worker'}
    assert 'SYNTHETIC_SECRET_SENTINEL' not in json.dumps(out)
