"""NX-035 D1 (migration 0197): nexloop_backup reads the protected server settings pgBackRest needs, and gains nothing else.

The stage deployment (2026-10-14) found pgBackRest 2.59.3 refusing stanza-create and check as nexloop_backup ("unable to select some
rows from pg_settings"); 0197 grants pg_read_all_settings WITH INHERIT TRUE (the role is NOINHERIT). Checked here on the disposable
archived cluster of backup_cluster after a catalog bootstrap:
- the membership exists with inherit true, and the role, connected as itself, reads data_directory and other protected settings;
- it is still no superuser, has no new attribute and no other role membership, cannot read files or business tables;
- with a real pgBackRest binary (opt-in: NEXLOOP_TEST_PGBACKREST=<path to pgbackrest>), stanza-create, check, a full backup and verify
  run as nexloop_backup; without it that case is skipped with its reason."""
import os
import shutil
import subprocess
from pathlib import Path

import psycopg
import pytest

from backup_cluster import cluster, pg  # noqa: F401
from test_backup_pg import db  # noqa: F401


def as_backup(cluster):  # noqa: F811
    return psycopg.connect(cluster['as_role']('nexloop_backup'), autocommit=True)


def test_nexloop_backup_is_a_member_of_pg_read_all_settings_with_inherit(db, cluster):  # noqa: F811
    rows = db.execute("""select g.rolname, m.inherit_option, m.set_option, m.admin_option from pg_auth_members m
        join pg_roles g on g.oid = m.roleid join pg_roles u on u.oid = m.member where u.rolname = 'nexloop_backup'""").fetchall()
    assert rows == [('pg_read_all_settings', True, True, False)]
    applied = db.execute("select max(version) from control.schema_migrations").fetchone()[0]
    assert applied >= '0197'


def test_nexloop_backup_reads_the_protected_settings_pgbackrest_needs(db, cluster):  # noqa: F811
    with as_backup(cluster) as backup:
        assert backup.execute('select current_user').fetchone()[0] == 'nexloop_backup'
        data_directory = backup.execute('show data_directory').fetchone()[0]
        assert data_directory == db.execute('show data_directory').fetchone()[0]
        # pgBackRest's own query reads every row of pg_settings, including superuser-only ones such as data_directory.
        hidden = backup.execute("select count(*) from pg_settings where name in ('data_directory','archive_command','config_file','hba_file')").fetchone()[0]
        assert hidden == 4
        assert backup.execute("select setting from pg_settings where name = 'archive_mode'").fetchone()[0] == 'on'


def test_nexloop_backup_gains_nothing_else(db, cluster):  # noqa: F811
    attributes = db.execute("""select rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolbypassrls
        from pg_roles where rolname = 'nexloop_backup'""").fetchone()
    assert attributes == (False, False, False, False, True, True, False)  # 0146 unchanged: login, replication, noinherit
    with as_backup(cluster) as backup:
        for function in ("pg_read_file('postgresql.conf')", "pg_ls_dir('.')", "pg_read_binary_file('PG_VERSION')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                backup.execute('select ' + function)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            backup.execute('select count(*) from control.nexloop_tenants')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            backup.execute("set role pg_write_server_files")
        assert backup.execute("select pg_has_role('nexloop_backup', 'pg_read_server_files', 'member')").fetchone()[0] is False
        assert backup.execute("select pg_has_role('nexloop_backup', 'pg_monitor', 'member')").fetchone()[0] is False


PGBACKREST = os.environ.get('NEXLOOP_TEST_PGBACKREST')


@pytest.mark.skipif(not PGBACKREST, reason='no pgBackRest binary given: set NEXLOOP_TEST_PGBACKREST=<path to pgbackrest> to run the real '
                                           'stanza-create/check/backup/verify as nexloop_backup (the CI host one-off cluster check of 2026-10-14 did this)')
def test_real_pgbackrest_runs_as_the_non_superuser_nexloop_backup(db, cluster, tmp_path):  # noqa: F811
    binary = shutil.which(PGBACKREST) or PGBACKREST
    assert Path(binary).is_file(), 'NEXLOOP_TEST_PGBACKREST does not name a pgBackRest binary'
    repo, log, lock, spool = (tmp_path / name for name in ('repo', 'log', 'lock', 'spool'))
    for d in (repo, log, lock, spool):
        d.mkdir(mode=0o700)
    config = tmp_path / 'pgbackrest.conf'
    config.write_text(f"[global]\nrepo1-path={repo}\nlog-path={log}\nlock-path={lock}\nspool-path={spool}\nlog-level-console=warn\n"
                      f"start-fast=y\narchive-check=y\n\n[nexloop]\npg1-path={cluster['data']}\npg1-socket-path={cluster['socket']}\n"
                      f"pg1-port={cluster['port']}\npg1-user=nexloop_backup\npg1-database=postgres\n")
    config.chmod(0o600)
    db.execute(f"alter system set archive_command = '{binary} --config={config} --stanza=nexloop archive-push %p'")
    db.execute('select pg_reload_conf()')

    def run(*args):
        result = subprocess.run([binary, f'--config={config}', '--stanza=nexloop', *args], capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, (args, result.stdout[-2000:], result.stderr[-2000:])
        return result
    run('stanza-create')
    run('check')
    run('--type=full', 'backup')
    run('verify')
    assert db.execute("select rolsuper from pg_roles where rolname = 'nexloop_backup'").fetchone()[0] is False
