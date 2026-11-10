"""R76 step 5 (dispatcher 2026-11-10): `scripts/deploy/machine_limits_publish.py publish-rate-policy|publish-note-limits
--bootstrap-dsn-file` on a host that holds only the bootstrap DSN: the publication (nexloop_eios.machine_access) runs inside the
configurator window, leaves one revision each (an unchanged file writes nothing more) and the window is closed again; without
--bootstrap-dsn-file it refuses. The package itself keeps --database-url-file only (no deploy-tool import in the wheel). Synthetic data."""
import json

import pytest

import assembly_dryrun_fixture as A
from nexloop_eios import machine_access as M
from test_stage_secrets import S  # noqa: F401  (puts scripts/deploy on sys.path)
import machine_limits_publish as W  # noqa: E402
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
    assert W.main(argv) == 0, capsys.readouterr().out
    assert 'error' not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert count(admin, table) == before + 1 and window_closed(admin)
    assert W.main(argv) == 0 and count(admin, table) == before + 1  # unchanged file: no second revision
    assert window_closed(admin)


@pytest.mark.parametrize('command', sorted(COMMANDS))
def test_the_bootstrap_dsn_file_is_required(stage, admin, command):  # noqa: F811
    policy, table = COMMANDS[command]
    base = [command, '--policy', str(A.ROOT / 'deploy/configuration' / policy)]
    before = count(admin, table)
    for main, extra in ((W.main, []), (M.main, ['--bootstrap-dsn-file', str(stage.bootstrap_dsn)])):
        with pytest.raises(SystemExit) as refused:
            main(base + extra)
        assert refused.value.code == 2
    assert count(admin, table) == before and window_closed(admin)
