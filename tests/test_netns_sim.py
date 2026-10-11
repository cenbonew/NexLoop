"""NX-032 slice 4: the rootless-Docker two-host simulation, checked without starting it (no Docker daemon here; the CI host runs
scripts/deploy/netns_sim/run.py). The preparation is real: certificates, rendered server configuration, ACL and probe specs."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / 'scripts/deploy/netns_sim'
sys.path.insert(0, str(SIM))
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import run as sim  # noqa: E402
import probe_isolation  # noqa: E402

CLASSES = {'authenticated', 'reached_authentication', 'server_refused', 'auth_failed', 'client_refused', 'pong', 'acl_refused', 'reachable', 'unreachable'}


def test_preparation_renders_the_stage_templates_and_keeps_secrets_where_they_belong(tmp_path):
    specs = sim.prepare(tmp_path / 'work')
    work = tmp_path / 'work'
    hba = (work / 'data/pg_hba.conf').read_text()
    assert 'hostssl nexloop_stage nexloop_api,nexloop_metrics 192.0.2.10/32 scram-sha-256 clientcert=verify-full' in hba and '__' not in hba
    valkey = (work / 'data/valkey.conf').read_text()
    assert 'port 0' in valkey and 'tls-auth-clients yes' in valkey
    acl = (work / 'data/valkey.acl').read_text()
    cache_password = (work / 'probe/cache_password').read_text()
    assert 'user default off' in acl and cache_password not in acl and re.search(r'#[0-9a-f]{64}', acl)
    # Server keys only on the data side, client keys only on the probe side; every key 0600.
    assert sorted(p.name for p in (work / 'data').rglob('*.key')) == ['server.key', 'server.key']
    assert not any('server' in p.name for p in (work / 'probe').rglob('*.key'))
    for key in work.rglob('*.key'):
        assert oct(key.stat().st_mode & 0o777) == '0o600', key
    # Specs: every probe's target exists and expects a known class; no secret value inside a spec.
    for location, spec in specs.items():
        assert set(spec) == {'location', 'targets', 'probes'}
        for p in spec['probes']:
            assert p['target'] in spec['targets'] and p['expect'] in CLASSES and p['kind'] in probe_isolation.KINDS, (location, p)
        text = (work / 'probe' / f'spec-{location}.json').read_text()
        assert cache_password not in text and 'BEGIN' not in text
    assert [p['code'] for p in specs['outsider']['probes']] == ['data_pg', 'data_valkey', 'app_api_internal', 'app_gateway']
    assert {p['expect'] for p in specs['host']['probes']} == {'unreachable'}
    agent = {p['code']: p['expect'] for p in specs['agent']['probes']}
    assert agent['data_pg_from_agent'] == agent['unlisted_target_from_agent'] == 'unreachable' and agent['model_allowlisted'] == 'reachable'
    assert {p['code']: p['expect'] for p in specs['app']['probes']}['model_from_application_namespace'] == 'unreachable'
    assert (work / 'probe/loopback_relay.py').is_file()
    assert (work / 'probe/probe_isolation.py').read_text() == (ROOT / 'scripts/deploy/probe_isolation.py').read_text()  # the shared script


def compose_env(tmp_path):
    images = sim.lock_images()
    return {'SIM_WORK': str(tmp_path), 'SIM_DB': sim.DB, 'SIM_POSTGRES_IMAGE': images['postgres'], 'SIM_VALKEY_IMAGE': images['valkey'],
            'SIM_PYTHON_IMAGE': images['python']}


def test_compose_topology_and_docker_compose_config(tmp_path):
    spec = yaml.safe_load((SIM / 'compose.sim.yaml').read_text())
    services = spec['services']
    assert set(services) == {'data', 'data-valkey', 'app', 'agent', 'model', 'neighbour', 'outsider'}
    assert set(services['outsider']['networks']) == {'mgmt_net'} and set(services['app']['networks']) == {'data_net', 'mgmt_net', 'agent_link'}
    assert set(services['neighbour']['networks']) == {'data_net'} and services['data-valkey']['network_mode'] == 'service:data'
    # NX-037 D4: the Agent Host's own namespace (internal link + its egress), the model host only on that egress.
    assert set(services['agent']['networks']) == {'agent_link', 'agent_egress'} and set(services['model']['networks']) == {'agent_egress'}
    assert 'agent_egress' not in services['app']['networks'] and spec['networks']['agent_link']['internal'] is True
    assert all(s.get('pull_policy') == 'never' for s in services.values())
    assert all(not s.get('ports') for s in services.values())  # nothing is published to the host
    images = sim.lock_images()
    assert all(re.fullmatch(r'[a-z0-9./-]+@sha256:[0-9a-f]{64}', ref) for ref in images.values())
    docker = shutil.which('docker')
    if docker:
        env = {**compose_env(tmp_path), 'PATH': '/usr/bin:/bin:' + str(Path(docker).parent)}
        run = subprocess.run([docker, 'compose', '-f', str(SIM / 'compose.sim.yaml'), 'config', '--format', 'json'], capture_output=True, text=True,
                             env=env, timeout=60)
        assert run.returncode == 0, run.stderr[-1500:]
        rendered = json.loads(run.stdout)['services']
        assert rendered['data']['image'] == images['postgres'] and rendered['outsider']['image'] == images['python']


def test_prepare_only_cli_starts_nothing(tmp_path):
    run = subprocess.run([sys.executable, '-I', str(SIM / 'run.py'), '--out', str(tmp_path / 'reports'), '--prepare-only'], capture_output=True, text=True,
                         timeout=120)
    out = json.loads(run.stdout)
    assert run.returncode == 0 and out['prepared'] is True and set(out['locations']) == {'app', 'agent', 'neighbour', 'outsider', 'host'}
    assert not (tmp_path / 'reports').exists()
    (tmp_path / 'reports').mkdir()
    again = subprocess.run([sys.executable, '-I', str(SIM / 'run.py'), '--out', str(tmp_path / 'reports')], capture_output=True, text=True, timeout=60)
    assert again.returncode == 2 and json.loads(again.stdout)['error'] == 'out directory exists'
