"""NX-033 local part, AT-054 local evidence: the CI runner startup check (rootless, no secret, no data host or port, no network for
tests), the rootless container command, the isolation probe, and the trusted policy files.

Temporary directories and loopback sockets only; the real first probe on APP_HOST is an owner-run B-class step."""
import copy
import json
import os
import re
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import nexloop_ci as ci  # noqa: E402

TEMPLATE = json.loads((ROOT / 'deploy/stage/ci/runner.example.json').read_text())


@pytest.fixture
def host(tmp_path):
    facts = {k: str(tmp_path / k.lower()) for k in ('SECRET_ROOT', 'RUNTIME_ROOT', 'ARTIFACT_ROOT', 'BACKUP_ROOT', 'BACKUP_KEY_ROOT', 'RELEASE_ARCHIVE_ROOT')}
    facts['DATA_HOSTS'] = 'data-host.invalid'
    for k, v in facts.items():
        if k.endswith('_ROOT'):
            Path(v).mkdir()
    runner = json.loads(json.dumps(TEMPLATE).replace('<CI_CACHE>', str(tmp_path / 'cache')).replace('<TRUSTED_CI_DIR>', str(tmp_path / 'trusted'))
                        .replace('<DEPENDENCY_PROXY>', 'http://proxy.invalid:3128').replace('<CI_MEMORY_MAX>', '8g'))
    return {'facts': facts, 'runner': runner, 'tmp': tmp_path}


def check(runner, facts, **kw):
    return ci.check_runner(runner, facts, **{'euid': 1001, 'groups': ['nexloop-ci'], **kw})


def test_the_template_passes_and_every_injected_exposure_is_refused(host):
    r, f = host['runner'], host['facts']
    assert check(r, f) == []
    cases = {
        'protected_path_mounted': lambda x: x['mounts'].append({'source': f['SECRET_ROOT'] + '/stage', 'target': '/s', 'read_only': True}),
        'secret_env:DATABASE_URL': lambda x: x['env'].update(DATABASE_URL='postgresql://x'),
        'secret_env:NEXLOOP_STAGE_TOKEN': lambda x: x['env'].update(NEXLOOP_STAGE_TOKEN='x'),
        'protected_path_in_env:STAGE_FILE': lambda x: x['env'].update(STAGE_FILE=f['BACKUP_KEY_ROOT'] + '/pass'),
        'data_host_in_env:PGHOST': lambda x: x['env'].update(PGHOST='data-host.invalid'),
        'data_host_added': lambda x: x['add_hosts'].append('db:data-host.invalid'),
        'ports_published': lambda x: x['publish'].append('5432:5432'),
        'test_network_not_none': lambda x: x['network'].update(test='slirp4netns'),
        'dependency_network_not_proxy': lambda x: x['network'].update(dependency='host'),
        'proxy_invalid': lambda x: x.update(proxy='http://data-host.invalid:6379'),
        'not_rootless_podman': lambda x: x.update(rootless=False),
        'privileged_container': lambda x: x.update(privileged=True),
        'container_socket_mounted': lambda x: x['mounts'].append({'source': '/run/user/1001/podman/podman.sock', 'target': '/s', 'read_only': True}),
        'writable_mount': lambda x: x['mounts'].append({'source': str(host['tmp'] / 'other'), 'target': '/other', 'read_only': False}),
        'cgroup_limits_missing': lambda x: x['cgroup'].pop('memory_max'),
    }
    for code, inject in cases.items():
        bad = copy.deepcopy(r)
        inject(bad)
        assert code in check(bad, f), code
    # Through a symlink into a protected root is still a protected root.
    link = host['tmp'] / 'innocent'
    link.symlink_to(f['RUNTIME_ROOT'])
    bad = copy.deepcopy(r)
    bad['mounts'].append({'source': str(link), 'target': '/x', 'read_only': True})
    assert 'protected_path_mounted' in check(bad, f)
    assert check(r, f, euid=0) == ['runs_as_root'] and check(r, f, groups=['docker']) == ['privileged_group']


def test_cli_refuses_to_start_with_a_violating_runner(host, capsys):
    bad = copy.deepcopy(host['runner'])
    bad['mounts'].append({'source': host['facts']['SECRET_ROOT'], 'target': '/s', 'read_only': True})
    runner, facts = host['tmp'] / 'runner.json', host['tmp'] / 'facts.env'
    runner.write_text(json.dumps(bad))
    facts.write_text(''.join(f'{k}={v}\n' for k, v in host['facts'].items()))
    code = ci.main(['check-runner', '--runner', str(runner), '--facts', str(facts)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and 'protected_path_mounted' in out['violations']
    assert host['facts']['SECRET_ROOT'] not in json.dumps(out)  # codes only
    # The controller refuses to run any job with it.
    queue = host['tmp'] / 'queue'
    code = ci.main(['run-once', '--repo', str(host['tmp']), '--queue', str(queue), '--policy', str(ROOT / '.ci-policy'), '--identities',
                    str(ROOT / 'deploy/stage/ci/identities.example.json'), '--runner', str(runner), '--facts', str(facts)])
    assert code == 2 and json.loads(capsys.readouterr().out)['reason'] == 'runner_refused'


def test_the_container_command_has_no_network_for_tests_and_only_the_proxy_for_dependencies(host):
    r = host['runner']
    test = ci.podman_argv(r, {'name': 'suite', 'network': 'test', 'env': {'UV_OFFLINE': '1'}}, '/work-dir', ['scripts/ci/check'])
    assert '--network=none' in test and '--read-only' in test and '--cap-drop=ALL' in test and '-p' not in test and '--privileged' not in test
    assert not any('PROXY' in a for a in test) and 'UV_OFFLINE=1' in test and test[-1] == 'scripts/ci/check'
    dep = ci.podman_argv(r, {'name': 'dependency-python', 'network': 'dependency'}, '/work-dir', ['uv', 'sync', '--frozen'])
    assert '--network=none' not in dep and 'HTTPS_PROXY=http://proxy.invalid:3128' in dep
    assert any(a.startswith('--memory=') for a in test) and any(a.startswith('--pids-limit=') for a in test)


def test_isolation_probe_fails_on_any_reachable_target_and_reports_codes_only(host):
    closed = socket.socket()
    closed.bind(('127.0.0.1', 0))
    closed_port = closed.getsockname()[1]
    closed.close()  # nothing listens: connection refused
    secret = Path(host['facts']['SECRET_ROOT'])
    (secret / 'stage.env').write_text('X=1')
    secret.chmod(0o000)
    try:
        targets = {'paths': {'SECRET_ROOT': str(secret), 'BACKUP_KEY': str(host['tmp'] / 'absent-key')},
                   'tcp': {'DATA_PG': f'127.0.0.1:{closed_port}'}}
        if os.geteuid() != 0:
            assert ci.isolation_probe(targets, timeout=1) == {'isolated': True, 'reachable': [], 'checked': ['BACKUP_KEY', 'DATA_PG', 'SECRET_ROOT']}
        with socket.socket() as listening:
            listening.bind(('127.0.0.1', 0))
            listening.listen()
            targets['tcp']['APP_API'] = f'127.0.0.1:{listening.getsockname()[1]}'
            secret.chmod(0o700)
            out = ci.isolation_probe(targets, timeout=1)
        assert out['isolated'] is False and out['reachable'] == ['APP_API', 'SECRET_ROOT']
        assert '127.0.0.1' not in json.dumps(out) and str(secret) not in json.dumps(out)
    finally:
        secret.chmod(0o700)


def test_trusted_policy_matches_the_suite_and_keeps_tests_offline():
    policy = ci.load_policy(ROOT / '.ci-policy')
    stages = {s['name']: s for s in policy['stages']['stages']}
    assert [s['name'] for s in policy['stages']['stages']] == ['dependency-python', 'dependency-node', 'suite', 'isolation-probe']
    # Only dependency stages have network; everything after runs offline from the cache, on loopback only.
    assert {n for n, s in stages.items() if s['network'] == 'dependency'} == {'dependency-python', 'dependency-node'}
    assert stages['suite']['env'] == {'UV_OFFLINE': '1', 'npm_config_offline': 'true'}
    # The isolation probe runs the trusted copy, not the candidate's.
    assert stages['isolation-probe']['argv'][1].startswith('/trusted/')
    # The required suites are exactly the JUnit reports scripts/ci/check writes; the deadline gate is one of them.
    # (scripts/ci/check runs the stage scripts of scripts/ci/stages/, which write them.)
    written = set(re.findall(r'\.ci-results/([a-z0-9_-]+)\.xml', ''.join(p.read_text() for p in sorted((ROOT / 'scripts/ci/stages').glob('*.sh')))))
    assert set(policy['suites']['suites']) == written and policy['suites']['deadline_gate_suite'] in written
    egress = json.loads((ROOT / '.ci-policy/egress.v1.json').read_text())
    assert 'github.com' not in egress['allow'] and set(egress['allow']) == {'pypi.org', 'files.pythonhosted.org', 'registry.npmjs.org'}


def test_templates_are_parameters_only():
    for path in (ROOT / 'deploy/stage/ci').iterdir():
        text = path.read_text()
        assert not re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text), path.name
        assert not re.search(r'/(Users|home)/', text), path.name
    roles = ci.load_identities(ROOT / 'deploy/stage/ci/identities.example.json')
    assert sorted(set(roles.values())) == sorted(ci.ROLES)
