"""NX-035 `nexloop-restore` (and `nexloop-backup drill-mark`) on a disposable archived PG 18 cluster (backup_cluster).

The CLI restores into a NEW instance (empty --pg-path, free --port, socket only) to a named restore point (--target-name) or a
time (--target-time), from a pg_basebackup directory (local) or pgBackRest (deployment; here a stand-in executable that
records its arguments and lays down the same base backup with recovery settings, since pgBackRest is not installed on the
development machine), then fences, replays, reconciles, verifies the lineage and the open_scope compensating controls, and
ends in verified_readonly. Credentials only from private files. Synthetic data; local measurements only."""
import json
import os
import stat
import sys
import time
from datetime import UTC, datetime

import psycopg
import pytest

import nexloop_eios.backup as backup
from backup_cluster import cluster, free_port, pg, switch_wal  # noqa: F401
from nexloop_eios.bootstrap import bootstrap

TENANT = '00000000-0000-4000-8000-00000000000a'


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


STUB = '''#!{python}
"""pgBackRest stand-in: records its arguments, then restores the base backup into --pg1-path with recovery settings."""
import json, pathlib, shutil, sys
args = sys.argv[1:]
pathlib.Path({log!r}).write_text(json.dumps(args))
opts = dict(a[2:].split('=', 1) for a in args if a.startswith('--') and '=' in a)
assert args[-1] == 'restore' and opts['target-action'] == 'promote'
target = pathlib.Path(opts['pg1-path'])
shutil.copytree({base!r}, target, dirs_exist_ok=True)
(target / 'recovery.signal').write_text('')
kind = 'recovery_target_name' if opts['type'] == 'name' else 'recovery_target_time'
with open(target / 'postgresql.auto.conf', 'a') as conf:
    conf.write("restore_command = 'cp \\"{archive}/%f\\" \\"%p\\"'\\n")
    conf.write(f"{{kind}} = '{{opts['target']}}'\\n")
'''


@pytest.fixture
def restorable(admin, cluster, tmp_path):
    """A bootstrapped cluster with one active tenant, a verified base backup, an exported journal, a drill mark (CLI) and,
    after it, a committed write and an archived WAL segment."""
    bootstrap(admin)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT,))
    recorder = cluster['as_role']('nexloop_backup_recorder')
    base = backup.take_pg_backup(recorder_conninfo=recorder, replication_conninfo=cluster['as_role']('nexloop_backup'), pg_bin=cluster['bin'],
                                 target=cluster['root'] / 'base')
    assert base['verify_status'] == 'verified'
    journal = tmp_path / 'journal'
    journal.mkdir(mode=0o700)
    backup.export_journal(recorder_conninfo=recorder, journal_dir=journal)
    recorder_file = private(tmp_path / 'recorder.dsn', recorder)
    mark_file = tmp_path / 'mark.json'
    assert backup.backup_main(['drill-mark', '--recorder-dsn-file', str(recorder_file), '--drill-id', 'c1', '--out', str(mark_file)]) == 0
    assert stat.S_IMODE(mark_file.stat().st_mode) == 0o600
    time.sleep(1.1)
    after_mark = datetime.now(UTC)
    time.sleep(1.1)
    # A committed transaction after the target time: a time target stops at the first commit after it (else recovery ends first).
    admin.execute('create table public.restore_cli_after(at timestamptz)')
    admin.execute('insert into public.restore_cli_after values(clock_timestamp())')
    switch_wal(admin, cluster)
    sock = cluster['root'] / 'rs'
    sock.mkdir(mode=0o700)
    return {'base': cluster['root'] / 'base', 'journal': journal, 'mark': mark_file, 'after_mark': after_mark, 'socket': sock, 'tmp': tmp_path}


def args(cluster, r, *extra, target='restored'):
    return ['--pg-bin', str(cluster['bin']), '--pg-path', str(cluster['root'] / target), '--port', str(free_port()), '--socket-dir', str(r['socket']),
            '--owner-user', 'nexloop_bootstrap', '--hba-method', 'trust', '--restore-ref', 'cli-' + target, '--journal-dir', str(r['journal']),
            '--running-port', str(cluster['port']), *extra]


def run(argv, capsys):
    code = backup.restore_main(argv)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_restore_cli_refuses_before_touching_anything(admin, cluster, restorable, capsys):
    r = restorable
    base = ['--source', 'basebackup', '--base-backup', str(r['base']), '--wal-archive', str(cluster['archive']), '--target-name', 'drill-c1',
            '--drill-mark-file', str(r['mark'])]
    code, out = run([*base, *args(cluster, r, target='r1')], capsys)  # no --old-primary-fenced
    assert code == 1 and out == {'passed': False, 'refused': 'old primary must be fenced first (--old-primary-fenced)'}
    assert not (cluster['root'] / 'r1').exists() or not any((cluster['root'] / 'r1').iterdir())
    code, out = run([*base, *args(cluster, r, '--old-primary-fenced', target='data')], capsys)  # the live data directory
    assert code == 1 and out['refused'] == 'restore target is not empty'
    loose = r['tmp'] / 'pgbackrest.conf'
    loose.write_text('[global]\nrepo1-cipher-pass=synthetic\n')
    loose.chmod(0o644)
    code, out = run(['--source', 'pgbackrest', '--pgbackrest-bin', '/bin/false', '--pgbackrest-config-file', str(loose), '--stanza', 'nexloop',
                     '--target-time', r['after_mark'].isoformat(), *args(cluster, r, '--old-primary-fenced', target='r2')], capsys)
    assert code == 1 and out == {'passed': False, 'error': 'PermissionError'} and 'synthetic' not in json.dumps(out)
    with pytest.raises(SystemExit) as exited:
        backup.restore_main([*base, '--target-time', r['after_mark'].isoformat(), *args(cluster, r, '--old-primary-fenced', target='r3')])
    assert exited.value.code == 2


def test_restore_cli_from_a_base_backup_to_a_named_point_with_the_drill_mark(admin, cluster, restorable, capsys):
    r = restorable
    report_file = r['tmp'] / 'report.json'
    code, out = run(['--source', 'basebackup', '--base-backup', str(r['base']), '--wal-archive', str(cluster['archive']), '--target-name', 'drill-c1',
                     '--drill-mark-file', str(r['mark']), '--report-file', str(report_file), '--stop',
                     *args(cluster, r, '--old-primary-fenced')], capsys)
    assert code == 0 and out['passed'] is True and out['state'] == 'verified_readonly' and out['instance'] == 'stopped', out
    checks = out['checks']
    assert all(checks['controls'].values()) and checks['drill_compare']['count_mismatches'] == 0 and checks['drill_compare']['catalog_equal'] is True
    assert checks['counts']['active_tenants'] == checks['counts']['scopes'] == 1 and checks['counts']['scopes_open'] == 0
    assert stat.S_IMODE(report_file.stat().st_mode) == 0o600 and json.loads(report_file.read_text()) == out
    assert admin.execute("select current_setting('archive_mode')").fetchone() == ('on',)  # the source cluster is untouched
    pyproject = (backup.Path(backup.__file__).parents[2] / 'pyproject.toml').read_text()
    assert 'nexloop-restore = "nexloop_eios.backup:restore_main"' in pyproject  # the console entry point


def test_restore_cli_with_pgbackrest_to_a_time(admin, cluster, restorable, capsys):
    r = restorable
    log = r['tmp'] / 'pgbackrest-args.json'
    stub = r['tmp'] / 'pgbackrest'
    stub.write_text(STUB.format(python=sys.executable, log=str(log), base=str(r['base']), archive=str(cluster['archive'])))
    stub.chmod(0o700)
    config = private(r['tmp'] / 'pgbackrest.conf', '[global]\nrepo1-cipher-pass=synthetic-not-a-secret\n')
    target = r['after_mark'].isoformat()
    code, out = run(['--source', 'pgbackrest', '--pgbackrest-bin', str(stub), '--pgbackrest-config-file', str(config), '--stanza', 'nexloop', '--repo', '2',
                     '--target-time', target, '--stop', *args(cluster, r, '--old-primary-fenced', target='pitr')], capsys)
    assert code == 0 and out['passed'] is True and out['checks']['drill_compare'] is None, out
    called = json.loads(log.read_text())
    assert called == [f'--config={config}', '--stanza=nexloop', '--repo=2', f'--pg1-path={cluster["root"] / "pitr"}', '--type=time', f'--target={target}',
                      '--target-action=promote', '--archive-mode=off', 'restore']
    assert 'synthetic-not-a-secret' not in json.dumps(out)
    conf = (cluster['root'] / 'pitr' / 'postgresql.auto.conf').read_text()
    assert "listen_addresses = ''" in conf and "archive_mode = 'off'" in conf
    assert (cluster['root'] / 'pitr' / 'pg_hba.conf').read_text() == 'local all nexloop_bootstrap trust\n'
