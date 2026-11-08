"""L6 (R72 condition): r8's verify connection (roles.verify, on stage the bootstrap DSN file the inspector uses) is read only by
construction - s6_common.connect_read_only sets default_transaction_read_only and reads it back, anything but on refuses - and it
serves bootstrap.verify alone: a write on it fails and verify changes nothing. Real PostgreSQL."""
import sys
from pathlib import Path

import psycopg
import pytest

from nexloop_eios.bootstrap import bootstrap
from test_s6_stage_evolution_pg import everything

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import s6_common as C  # noqa: E402
import s6_stage_schema as S  # noqa: E402


@pytest.fixture
def verify_ports(admin, pg, tmp_path):
    bootstrap(admin)
    dsn = tmp_path / 'verify.dsn'
    dsn.write_text(pg)  # locally the superuser, as the stage bootstrap DSN: only the script makes it read only
    dsn.chmod(0o600)
    config = {'roles': {'verify': {'dsn_file': str(dsn)}}}
    return S.ports_from(config, None), dsn


def test_verify_runs_read_only_and_changes_nothing(admin, verify_ports):
    ports, _ = verify_ports
    before = everything(admin)
    got = ports.verify()
    assert set(got) == {'revision', 'migration_count', 'digest'} and got['migration_count'] > 0
    assert everything(admin) == before
    with ports.verify_connect() as db:
        assert db.execute('show default_transaction_read_only').fetchone()[0] == 'on'
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute("insert into control.nexloop_tenants(tenant_id,status) values('s6-verify-probe','active')")


def test_a_verify_connection_that_does_not_read_back_on_is_refused(verify_ports, monkeypatch):
    _, dsn = verify_ports

    class Off:
        closed = False

        def execute(self, sql, *args):
            return type('R', (), {'fetchone': lambda self: ('off',)})()

        def close(self):
            Off.closed = True
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **k: Off())
    with pytest.raises(C.Refused, match='verify is not read only'):
        C.connect_read_only(str(dsn), who='verify')
    assert Off.closed is True
    ports = S.ports_from({'roles': {'verify': {'dsn_file': str(dsn)}}}, None)
    with pytest.raises(C.Refused, match='verify is not read only'):
        ports.verify()
