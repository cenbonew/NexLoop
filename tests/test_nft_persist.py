"""G-NFT-PERSIST: the stage firewall table is loaded at boot by deploy/stage/systemd/nexloop-nft.service.example from a transaction
file written by render.py --nft-transaction.

Checked here: the unit (oneshot, RemainAfterExit, before Docker and required by it, an assert on the load file, start/reload with
`nft -f`, stop deleting only inet nexloop_stage, nothing about another table or `flush ruleset`); the transaction file (exactly
`table` + `delete table` + `include` of the rendered rules, 0600, refusing relative or odd paths); and, when an nft binary exists,
`nft -c -f` on the transaction file with the rendered app-host and data-host rules. Neither the development machine (macOS) nor
the CI user has nft: that check is then skipped with its reason (explicitly, never silently passed)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexloop_eios import stage_render as R
from test_stage_systemd_units import unit

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'deploy/stage'
NFT = shutil.which('nft')


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


@pytest.mark.skipif(NFT is None, reason='no nft binary (macOS development machine, rootless CI user): `nft -c -f` on the transaction file '
                                         'is not run here; run it on the host before enabling the unit (run-42 §6a)')
@pytest.mark.parametrize('host', ['app-host', 'data-host'])
def test_nft_accepts_the_transaction_file_with_the_rendered_rules(tmp_path, host):
    env = tmp_path / 'stage.env'
    env.write_text((STAGE / 'stage.env.example').read_text())
    env.chmod(0o600)
    rules, _ = R.render((STAGE / f'firewall/{host}.nft.template').read_text(), R.load_env(env))
    path = tmp_path / f'{host}.rules.nft'
    path.write_text(rules)
    load = tmp_path / f'{host}.nft'
    load.write_text(R.nft_transaction(path))
    run = subprocess.run([NFT, '-c', '-f', str(load)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
