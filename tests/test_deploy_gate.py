"""NX-032 slice 2: the remaining deploy-gate checks of the doctor — guard = 4 and every running PG process registered, port
bindings, roles, secret distribution, before/after inventory — and `nexloop-doctor --deploy-gate` over a full synthetic spec.
Synthetic inventories and temporary directories; roles on a disposable PostgreSQL 18; no server is contacted."""
import copy
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from nexloop_eios import deploy_checks
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.stage_render import load_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import inventory  # noqa: E402
from test_deploy_render import interpolate  # noqa: E402
from test_deploy_tls import pki  # noqa: E402,F401

BUDGET = json.loads((ROOT / 'deploy/stage/connection-budget.v1.json').read_text())


@pytest.fixture
def stage(tmp_path):
    path = tmp_path / 'stage.env'
    path.write_text((ROOT / 'deploy/stage/stage.env.example').read_text())
    path.chmod(0o600)
    return path


def running(**changes):
    services = {s['name']: {'processes': s['processes'], 'pg': True} for s in BUDGET['services']}
    services['runtime-worker']['guard_children'] = 4
    services['caddy'] = {'processes': 1, 'pg': False}
    for name, value in changes.items():
        services[name] = value
    return {'services': services}


def test_guard_is_exactly_four_and_every_running_pg_process_is_registered():
    ok = deploy_checks.guard_and_registration(BUDGET, running())
    assert all(ok['checks'].values()) and ok['details']['process_shape']['guard_workers'] == {'runtime-worker': 4}
    two = copy.deepcopy(BUDGET)
    next(s for s in two['services'] if s['name'] == 'runtime-worker')['guard_workers'] = 2
    assert deploy_checks.guard_and_registration(two, running())['checks']['guard_workers_exact'] is False
    stray = deploy_checks.guard_and_registration(BUDGET, running(**{'mystery-service': {'processes': 1, 'pg': True}}))
    assert stray['checks']['running_processes_registered'] is False and stray['details']['process_shape']['unregistered'] == ['mystery-service']
    assert deploy_checks.guard_and_registration(BUDGET, running(api={'processes': 3, 'pg': True}))['details']['process_shape']['over_declared'] == ['api']
    assert deploy_checks.guard_and_registration(BUDGET, running(**{'runtime-worker': {'processes': 1, 'pg': True, 'guard_children': 3}}))['checks'][
        'running_guard_children_exact'] is False
    missing = copy.deepcopy(BUDGET)
    missing['services'] = [s for s in missing['services'] if s['name'] != 'incentive-issuer']
    assert deploy_checks.guard_and_registration(missing, running())['checks']['budget_all_required_declared'] is False
    assert deploy_checks.guard_and_registration(BUDGET, None)['checks']['running_processes_registered'] is False  # not provided = not passed


def listen(address, port, owner='nexloop'):
    return {'address': address, 'port': port, 'owner': owner}


def test_port_bindings_follow_the_plan_on_both_hosts(stage):
    s = load_env(stage)
    app_ok = [listen(s['APP_HOST_ADDR'], 8443), listen(s['APP_HOST_ADDR'], 8080), listen('127.0.0.1', 8100), listen('127.0.0.1', 8200),
              listen('0.0.0.0', 22, 'other')]
    assert deploy_checks.port_bindings(app_ok, s, 'app')['checks']['port_bindings'] is True
    cases = {
        'internal_port_not_loopback:HOST_INTERNAL_PORT': app_ok[:2] + [listen(s['APP_HOST_ADDR'], 8100)],
        'public_port_wrong_address:GATEWAY_HTTPS_PORT': [listen('192.0.2.99', 8443), app_ok[1]],
        'nexloop_wildcard_bind': app_ok + [listen('0.0.0.0', 9999)],
        'public_port_not_listening:GATEWAY_REDIRECT_PORT': [app_ok[0]],
    }
    for code, listening in cases.items():
        assert code in deploy_checks.port_bindings(listening, s, 'app')['details']['port_bindings'], code
    data_ok = [listen(s['DATA_HOST_ADDR'], 55432), listen(s['DATA_HOST_ADDR'], 56379)]
    assert deploy_checks.port_bindings(data_ok, s, 'data')['checks']['port_bindings'] is True
    assert 'public_port_wrong_address:PG_PORT' in deploy_checks.port_bindings([listen('0.0.0.0', 55432)], s, 'data')['details']['port_bindings']
    # Before deployment: a planned port held by an existing service is a conflict (D7: change the private env, not that service).
    before = [listen('0.0.0.0', 55432, 'other')]
    assert deploy_checks.port_bindings(before, s, 'data', before_deploy=True)['details']['port_bindings'] == ['port_conflict:PG_PORT']
    assert deploy_checks.port_bindings([], s, 'data', before_deploy=True)['checks']['port_bindings'] is True


def test_roles_have_no_superpowers_and_bootstrap_identities_are_disabled(admin):
    bootstrap(admin)
    with admin.transaction():
        out = deploy_checks.roles(admin, disabled=('nexloop_owner',))
    assert out['checks'] == {'application_roles_unprivileged': True, 'bootstrap_identities_disabled': True}
    # The disposable cluster's own superuser login is the bootstrap identity here: enabled login is reported.
    assert deploy_checks.roles(admin, disabled=('nexloop_bootstrap',))['details']['roles']['bootstrap_login_enabled'] == ['nexloop_bootstrap']
    admin.execute('create role nexloop_rogue login createrole')
    out = deploy_checks.roles(admin, disabled=('nexloop_owner',))
    assert out['checks']['application_roles_unprivileged'] is False and out['details']['roles']['privileged'] == ['nexloop_rogue']


def secret_tree(root):
    for service, files in {'api': ['dsn', 'pg-client.key', 'pg-client.crt'], 'runtime-worker': ['dsn', 'guard.key'],
                           'agent-host': ['server.crt', 'server.key', 'transport.key', 'guard-ca.crt'], 'shared': ['signing.key']}.items():
        d = root / service
        d.mkdir(mode=0o700, parents=True)
        for f in files:
            (d / f).write_text('synthetic')
            (d / f).chmod(0o600)
    return root


def app_compose():
    return yaml.safe_load(interpolate((ROOT / 'deploy/stage/compose.app.example.yaml').read_text(), ROOT / 'deploy/stage/compose.app.env.example'))


def test_secret_distribution_and_the_rendered_compose(tmp_path):
    root = secret_tree(tmp_path / 'secrets')
    compose = app_compose()
    ok = deploy_checks.secret_distribution(root, compose=compose)
    assert ok['checks'] == {'secret_distribution': True, 'real_dispatch_closed': True}, ok
    problems = {
        'agent_host_holds_service_secret': lambda: (root / 'agent-host' / 'dsn').write_text('x') or (root / 'agent-host' / 'dsn').chmod(0o600),
        'file_permissions:api': lambda: (root / 'api' / 'dsn').chmod(0o644),
        'directory_permissions:shared': lambda: (root / 'shared').chmod(0o755),
    }
    for code, inject in problems.items():
        root = secret_tree(tmp_path / ('s-' + code.split(':')[0]))
        inject()
        assert code in deploy_checks.secret_distribution(root)['details']['secrets']['problems'], code
    root = secret_tree(tmp_path / 's-link')
    (root / 'api' / 'link').symlink_to(root / 'api' / 'dsn')
    assert 'not_regular_file:api' in deploy_checks.secret_distribution(root)['details']['secrets']['problems']
    root = secret_tree(tmp_path / 's-hard')
    os.link(root / 'api' / 'dsn', root / 'api' / 'dsn-copy')
    assert 'hard_linked:api' in deploy_checks.secret_distribution(root)['details']['secrets']['problems']
    root = secret_tree(tmp_path / 's-compose')
    bad = copy.deepcopy(compose)
    # The YAML anchor shares one environment mapping between services: each change gets its own copy.
    bad['services']['api']['environment'] = {**bad['services']['api']['environment'], 'DATABASE_URL': 'postgresql://synthetic'}
    bad['services']['agent-host']['secrets'] = [*bad['services']['agent-host']['secrets'], 'api_dsn']
    bad['services']['effect-worker']['environment'] = {**bad['services']['effect-worker']['environment'], 'REAL_DISPATCH_ENABLED': 'true'}
    out = deploy_checks.secret_distribution(root, compose=bad)
    assert {'secret_in_environment:api', 'agent_host_mounts_service_secret'} <= set(out['details']['secrets']['problems'])
    assert out['checks']['real_dispatch_closed'] is False and out['details']['secrets']['dispatch_open'] == ['effect-worker']
    assert 'postgresql://synthetic' not in json.dumps(out)


SS = """LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*
LISTEN 0 4096 [::]:22 [::]:*
LISTEN 0 511 127.0.0.1:631 0.0.0.0:*
LISTEN 0 4096 192.0.2.20:55432 0.0.0.0:*
"""
RULESET = "table inet filter {\n chain input { type filter hook input priority 0; }\n}\n"
NEXLOOP_TABLE = "table inet nexloop_stage {\n chain forward { drop }\n}\n"


def inv(phase, *, extra_containers=None, ruleset=RULESET, ss=SS, units='ssh.service loaded active running\n'):
    containers = {'existing-db': {'image': 'example/db@sha256:' + 'a' * 64}, **(extra_containers or {})}
    return {'phase': phase, 'listening': inventory.parse_ss(ss, planned={55432, 56379}, phase=phase), 'containers': containers,
            'compose_projects': {'existing': {}}, 'units': inventory.parse_lines(units), 'data_directories': {'existing-data': {}},
            'firewall_without_nexloop_sha256': inventory.firewall_hash(ruleset)}


def test_inventory_allows_only_nexloop_additions():
    before = inv('before', ss=SS.replace('192.0.2.20:55432', '192.0.2.20:15432'))
    assert before['listening']['192.0.2.20:15432']['owner'] == 'other' and before['listening'][':::22']['address'] == '::'
    after = inv('after', extra_containers={'nexloop-data-postgres-1': {'image': 'n@sha256:' + 'b' * 64}}, ruleset=RULESET + NEXLOOP_TABLE,
                ss=SS.replace('192.0.2.20:55432', '192.0.2.20:15432') + 'LISTEN 0 4096 192.0.2.20:55432 0.0.0.0:*\n')
    after['compose_projects']['nexloop-data'] = {}
    after['data_directories']['nexloop:/srv/nexloop'] = {}
    ok = deploy_checks.inventory_compare(before, after)
    assert ok['checks']['existing_services_unchanged'] is True and ok['details']['inventory']['added']['containers'] == 1
    changed = copy.deepcopy(after)
    changed['containers']['existing-db']['image'] = 'example/db@sha256:' + 'c' * 64
    assert deploy_checks.inventory_compare(before, changed)['details']['inventory']['problems'] == ['changed:containers']
    removed = copy.deepcopy(after)
    removed['units'] = {}
    assert deploy_checks.inventory_compare(before, removed)['details']['inventory']['problems'] == ['removed:units']
    foreign = copy.deepcopy(after)
    foreign['containers']['someone-else'] = {'image': 'x'}
    assert deploy_checks.inventory_compare(before, foreign)['details']['inventory']['problems'] == ['non_nexloop_addition:containers']
    firewall = copy.deepcopy(after)
    firewall['firewall_without_nexloop_sha256'] = inventory.firewall_hash(RULESET.replace('priority 0', 'priority 1') + NEXLOOP_TABLE)
    assert deploy_checks.inventory_compare(before, firewall)['details']['inventory']['problems'] == ['changed:firewall']
    assert '192.0.2.20' not in json.dumps(deploy_checks.inventory_compare(before, foreign))


def test_doctor_deploy_gate_runs_every_check_and_fails_on_a_missing_input(pki, stage, tmp_path, admin, pg, capsys):
    bootstrap(admin)
    private = lambda name, value: (p := tmp_path / name, p.write_text(value if isinstance(value, str) else json.dumps(value)), p.chmod(0o600), str(p))[-1]
    s = load_env(stage)
    before = inv('before', ss='LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n')
    after = copy.deepcopy(before)
    after['listening'].update({f"{s['APP_HOST_ADDR']}:8443": listen(s['APP_HOST_ADDR'], 8443), f"{s['APP_HOST_ADDR']}:8080": listen(s['APP_HOST_ADDR'], 8080),
                               '127.0.0.1:8100': listen('127.0.0.1', 8100), '127.0.0.1:8200': listen('127.0.0.1', 8200)})
    after['containers']['nexloop-app-api-1'] = {'image': 'n@sha256:' + 'b' * 64}
    dsn_text = ('host=data.stage.invalid port=55432 dbname=nexloop_stage user=nexloop_api sslmode=verify-full sslrootcert=/run/secrets/pg_ca '
                'sslcert=/run/secrets/c sslkey=/run/secrets/k')
    tls_spec = {'ca': str(pki / 'root/ca.crt'), 'valkey_conf': str(ROOT / 'deploy/stage/config/valkey.conf.template'),
                'certificates': [{'name': 'pg-server', 'chain': str(pki / 'pg/pg-server.chain.crt'), 'key': str(pki / 'pg/pg-server.key'), 'purpose': 'server',
                                  'names': ['127.0.0.1']}],
                'dsn_files': [{'name': 'api', 'path': private('api.dsn', dsn_text)}]}
    host_config = private('agent-host.json', json.dumps(json.loads((ROOT / 'deploy/stage/agent-host.runtime-config.example.json').read_text())))
    spec = {'host_role': 'app', 'stage_env': str(stage), 'connection_budget': str(ROOT / 'deploy/stage/connection-budget.v1.json'),
            'running': private('running.json', running()), 'inventory_before': private('before.json', before), 'inventory_after': private('after.json', after),
            'secret_root': str(secret_tree(tmp_path / 'secrets')), 'compose_json': private('compose.json', app_compose()), 'tls_spec': private('tls.json', tls_spec),
            'roles_database_url_file': private('roles.dsn', pg), 'disabled_roles': ['nexloop_owner'], 'agent_host_config': host_config}
    from nexloop_eios.doctor import main
    sys.argv = ['nexloop-doctor', '--deploy-gate', private('gate.json', spec)]
    code = main()
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and all(out['checks'].values()), {k: v for k, v in out['checks'].items() if not v}
    assert {'guard_workers_exact', 'running_processes_registered', 'port_bindings', 'planned_ports_free_before', 'application_roles_unprivileged',
            'secret_distribution', 'real_dispatch_closed', 'existing_services_unchanged', 'tls_certificates', 'tls_dsn_verify_full',
            'agent_host_concurrency'} <= set(out['checks'])
    text = json.dumps(out)
    assert s['APP_HOST_ADDR'] not in text and str(pki) not in text and 'data.stage.invalid' not in text
    spec.pop('running')
    sys.argv = ['nexloop-doctor', '--deploy-gate', private('gate2.json', spec)]
    assert main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out['checks']['input_provided:running'] is False and out['checks']['running_processes_registered'] is False
