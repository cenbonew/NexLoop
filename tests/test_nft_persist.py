"""G-NFT-PERSIST: the stage firewall table is loaded at boot by deploy/stage/systemd/nexloop-nft.service.example from a transaction
file written by render.py --nft-transaction.

Checked here: the unit (oneshot, RemainAfterExit, before Docker and required by it, an assert on the load file, start/reload with
`nft -f`, stop deleting only inet nexloop_stage, nothing about another table or `flush ruleset`); the transaction file (exactly
`table` + `delete table` + `include` of the rendered rules, 0600, refusing relative or odd paths); and the host check's placement;
`nft -c -f` on the transaction file with the rendered rules is a host check (tests/host_checks/nft_check.py), outside the default
collection: the CI user cannot run nft -c and the release gate allows no skip. A guard here keeps it outside."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexloop_eios import stage_render as R
from test_stage_systemd_units import unit

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'deploy/stage'


def test_the_unit_loads_only_the_nexloop_table_before_docker_and_fails_closed():
    u = unit('nexloop-nft.service.example')
    load = '<NEXLOOP_ROOT>/config/firewall/<host>.nft'
    assert u['Service']['Type'] == ['oneshot'] and u['Service']['RemainAfterExit'] == ['yes']
    assert u['Service']['ExecStart'] == ['/usr/sbin/nft -f ' + load] and u['Service']['ExecReload'] == ['/usr/sbin/nft -f ' + load]
    assert u['Service']['ExecStop'] == ['/usr/sbin/nft delete table inet nexloop_stage']
    assert u['Unit']['AssertPathExists'] == [load] and 'ConditionPathExists' not in u['Unit']
    assert 'docker.service' in u['Unit']['Before'][0].split()  # before Docker starts the Compose containers (restart policy)
    assert 'docker.service' not in ' '.join(u['Unit'].get('After', []))
    assert u['Install']['RequiredBy'] == ['docker.service'] and u['Install']['WantedBy'] == ['multi-user.target']
    text = (STAGE / 'systemd/nexloop-nft.service.example').read_text()
    commands = [l for l in text.splitlines() if l.startswith('Exec')]
    assert all('nexloop_stage' in c or c.endswith(load) for c in commands)
    assert 'flush' not in ' '.join(commands) and 'ufw' not in ' '.join(commands).lower()


def test_the_transaction_file_replaces_exactly_the_nexloop_table(tmp_path):
    rules = '/srv/nexloop/config/firewall/app-host.rules.nft'
    text = R.nft_transaction(rules)
    body = [l for l in text.splitlines() if not l.startswith('#')]
    assert body == ['table inet nexloop_stage', 'delete table inet nexloop_stage', f'include "{rules}"']
    for bad in ('relative/rules.nft', '/srv/nexloop/../etc/rules.nft', '/srv/next loop/rules.nft', '/srv/"x".nft'):
        with pytest.raises(R.Refused):
            R.nft_transaction(bad)
    out = tmp_path / 'app-host.nft'
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/deploy/render.py'), '--nft-transaction', rules, '--out', str(out)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and json.loads(run.stdout) == {'rendered': True, 'nft_transaction': True, 'table': 'inet nexloop_stage'}
    assert out.read_text() == text and out.stat().st_mode & 0o777 == 0o600
    again = subprocess.run([sys.executable, str(ROOT / 'scripts/deploy/render.py'), '--nft-transaction', rules, '--out', str(out)],
                           capture_output=True, text=True)
    assert again.returncode == 2 and 'output exists' in again.stdout


def test_the_nft_host_check_is_outside_the_default_collection():
    """The release gate requires skipped = 0, and the CI user cannot run `nft -c`: the nft check lives in tests/host_checks/nft_check.py,
    which the default run (pyproject testpaths = tests, default python_files test_*.py / *_test.py) does not collect, and which is run on
    the hosts explicitly (run-42 §6a step 3)."""
    import tomllib
    options = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['pytest']['ini_options']
    assert options['testpaths'] == ['tests'] and 'python_files' not in options  # pytest's default patterns apply
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'packages/eios-core/src')}
    default = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider', 'tests/host_checks'], cwd=ROOT,
                             capture_output=True, text=True, env=env)
    assert default.returncode == 5 and 'nft_check' not in default.stdout, default.stdout  # 5 = nothing collected
    explicit = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider', 'tests/host_checks/nft_check.py'],
                              cwd=ROOT, capture_output=True, text=True, env=env)
    assert explicit.returncode == 0 and explicit.stdout.count('nft_check.py::') == 2, explicit.stdout
