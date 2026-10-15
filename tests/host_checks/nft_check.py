"""G-NFT-PERSIST host check (run-42 §6a step 3): `nft -c -f` on the boot load file with the rendered app-host and data-host rules.

Not part of the default test run: the file name does not match pytest's test_*.py, so `pytest` (testpaths = tests) never collects
it and it never produces a skip (the release gate requires skipped = 0). The CI user cannot run `nft -c` (no CAP_NET_ADMIN; an
unprivileged user namespace is refused by kernel.apparmor_restrict_unprivileged_userns). Run it on each host, as root, explicitly:

    sudo env PYTHONPATH=packages/eios-core/src python3 -m pytest -q tests/host_checks/nft_check.py

Without nft or without the permission it FAILS (never skips)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from nexloop_eios import stage_render as R

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / 'deploy/stage'


@pytest.mark.parametrize('host', ['app-host', 'data-host'])
def test_nft_accepts_the_transaction_file_with_the_rendered_rules(tmp_path, host):
    nft = shutil.which('nft') or '/usr/sbin/nft'
    assert Path(nft).exists(), 'nft not installed: this host check needs it'
    env = tmp_path / 'stage.env'
    env.write_text((STAGE / 'stage.env.example').read_text())
    env.chmod(0o600)
    rules, _ = R.render((STAGE / f'firewall/{host}.nft.template').read_text(), R.load_env(env))
    path = tmp_path / f'{host}.rules.nft'
    path.write_text(rules)
    load = tmp_path / f'{host}.nft'
    load.write_text(R.nft_transaction(path))
    run = subprocess.run([nft, '-c', '-f', str(load)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
