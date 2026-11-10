"""R76 step 5 (dispatcher 2026-11-10): `python -m nexloop_eios.machine_access publish-rate-policy|publish-note-limits
--bootstrap-dsn-file` on a host that holds only the bootstrap DSN: the publication runs inside the configurator window of the deploy
tools, leaves one revision each (an unchanged file writes nothing more) and the window is closed again; --database-url-file and
--bootstrap-dsn-file are exactly one of the two. Synthetic data."""
import json

import pytest

import assembly_dryrun_fixture as A
from nexloop_eios import machine_access as M
from test_assembly_dryrun_pg import stage, tree  # noqa: F401

COMMANDS = {'publish-rate-policy': ('machine-rate-limits.v1.json', 'control.nexloop_machine_rate_policies'),
            'publish-note-limits': ('machine-note-limits.v1.json', 'control.nexloop_machine_note_limits')}


def count(admin, table):
    return admin.execute(f'select count(*) from {table}').fetchone()[0]


def window_closed(admin):
    return admin.execute("select not rolcanlogin and rolpassword is null from pg_authid where rolname='nexloop_configurator'").fetchone()[0]


@pytest.mark.parametrize('command', sorted(COMMANDS))
def test_publication_through_the_bootstrap_window_leaves_one_revision(stage, admin, capsys, command):  # noqa: F811
    policy, table = COMMANDS[command]
    argv = [command, '--policy', str(A.ROOT / 'deploy/configuration' / policy), '--bootstrap-dsn-file', str(stage.bootstrap_dsn)]
    before = count(admin, table)
    capsys.readouterr()
    assert M.main(argv) == 0, capsys.readouterr().out
    assert 'error' not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert count(admin, table) == before + 1 and window_closed(admin)
    assert M.main(argv) == 0 and count(admin, table) == before + 1  # unchanged file: no second revision
    assert window_closed(admin)


@pytest.mark.parametrize('command', sorted(COMMANDS))
def test_exactly_one_dsn_option(stage, admin, command, tmp_path):  # noqa: F811
    policy, table = COMMANDS[command]
    base = [command, '--policy', str(A.ROOT / 'deploy/configuration' / policy)]
    before = count(admin, table)
    for extra in ([], ['--database-url-file', str(tmp_path / 'c'), '--bootstrap-dsn-file', str(stage.bootstrap_dsn)]):
        with pytest.raises(SystemExit) as refused:
            M.main(base + extra)
        assert refused.value.code == 2
    assert count(admin, table) == before and window_closed(admin)
