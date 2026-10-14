"""NX-035 deploy slice: `nexloop-backup pg` with pgBackRest records the manifest rows (schema nexloop-backup-manifest/2) and a
failed verify keeps the migration gate closed.

Runs on the disposable archived PG 18 cluster of backup_cluster; pgBackRest itself is the stand-in tests/support/fake_pgbackrest.py
(command line `version`, `info --output=json`, `backup`, `verify`), fed with this cluster's system identifier and LSNs, so the
manifest's own checks (same cluster, LSN order, evidence) are real. No network, no real repository."""
import json
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

import nexloop_eios.backup as backup
from backup_cluster import cluster, pg, switch_wal  # noqa: F401
from eios.adapters.postgres.migrations import Migration
from nexloop_eios.bootstrap import catalog
from test_backup_pg import db, recorder  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / 'tests/support/fake_pgbackrest.py'
PINNED = json.loads((ROOT / 'versions.lock.json').read_text())['deployment_tools']['pgbackrest']['version']
SECRET = 'synthetic-cipher-passphrase-not-real'


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def rest(cluster, db, tmp_path):  # noqa: F811
    system_id = db.execute('select system_identifier::text from pg_control_system()').fetchone()[0]
    lsn = db.execute('select pg_current_wal_lsn()::text').fetchone()[0]
    wal = db.execute('select pg_walfile_name(pg_current_wal_lsn())').fetchone()[0]
    state = tmp_path / 'pgbackrest-state.json'
    state.write_text(json.dumps({'version': PINNED, 'system_id': system_id, 'lsn_start': lsn, 'lsn_stop': lsn, 'wal_start': wal, 'wal_stop': wal}))
    command = [sys.executable, str(FAKE), str(state)]
    config = private(tmp_path / 'pgbackrest.conf', f'[global]\nrepo1-cipher-pass={SECRET}\n')

    def set_state(**changes):
        state.write_text(json.dumps({**json.loads(state.read_text()), **changes}))

    def take(**kw):
        return backup.take_pgbackrest_backup(recorder_conninfo=recorder(cluster), command=command, stanza='nexloop', config_file=config,
                                             expected_version=PINNED, **kw)

    def rows():
        return db.execute("select backup_id,kind,verify_status,tool,tool_version,verification from runtime.nexloop_backup_manifests "
                          "where kind like 'pg_%' order by manifest_id").fetchall()
    return dict(state=state, command=command, config=config, set_state=set_state, take=take, rows=rows, system_id=system_id, tmp=tmp_path)


def test_a_verified_pgbackrest_backup_records_tool_version_and_evidence_and_opens_the_gate(cluster, db, rest):  # noqa: F811
    done = rest['take']()
    assert done['verify_status'] == 'verified' and done['tool_version'] == PINNED and done['system_identifier'] == rest['system_id']
    (pid, pkind, pstatus, ptool, pversion, pver), (fid, fkind, fstatus, ftool, fversion, evidence) = rest['rows']()
    assert (pid, pkind, pstatus, ptool, pversion, pver) == (done['backup_id'], 'pg_full', 'pending', 'pgbackrest', PINNED, None)
    assert (fid, fkind, fstatus, ftool, fversion) == (done['backup_id'], 'pg_full', 'verified', 'pgbackrest', PINNED)
    assert evidence['schema'] == 'nexloop-backup-verification/1' and evidence['tool'] == 'pgbackrest' and evidence['label'] == done['label']
    assert evidence['type'] == 'full' and evidence['repo_key'] == 1 and evidence['stanza'] == 'nexloop' and evidence['db_version'] == '18'
    assert evidence['verify'] == {'exit_code': 0, 'summary': 'P00   INFO: verify command end: completed successfully'}
    assert evidence['backup_error'] is False and evidence['info_status'] == {'code': 0, 'message': 'ok'}
    assert set(evidence) == {'schema', 'tool', 'tool_version', 'stanza', 'repo_key', 'label', 'type', 'prior', 'timestamp_start', 'timestamp_stop',
                             'archive_start', 'archive_stop', 'size', 'repository_size', 'backup_error', 'info_status', 'db_version', 'backrest_format', 'verify'}
    calls = json.loads(rest['state'].read_text())['calls']
    assert ['--config=<set>'] == calls[0][-1:] and ['--stanza=nexloop', 'version'] == [a for a in calls[0] if not a.startswith('--config')]
    assert ['--stanza=nexloop', '--repo=1', '--type=full', 'backup'] in [[a for a in c if a != '--config=<set>'] for c in calls]
    switch_wal(db, cluster)
    assert backup.migration_gate(db) == {**backup.migration_gate(db), 'allowed': True, 'reasons': []}


@pytest.mark.parametrize('failure', ['verify', 'backup_error', 'stanza_status'])
def test_a_failed_verification_is_recorded_failed_and_blocks_the_migration_gate(cluster, db, rest, failure):  # noqa: F811
    marker = Migration(version='9999', name='nx035_pgbackrest_marker', checksum='0' * 64, sql='create table control.nx035_pgbackrest_marker(x int);')
    rest['set_state'](**{'verify': {'verify_rc': 1}, 'backup_error': {'backup_error': True}, 'stanza_status': {'status_code': 2}}[failure])
    done = rest['take']()
    assert done['verify_status'] == 'failed'
    evidence = rest['rows']()[-1][5]
    assert rest['rows']()[-1][2] == 'failed' and evidence['schema'] == 'nexloop-backup-verification/1'
    if failure == 'verify':
        assert evidence['verify']['exit_code'] == 1
    switch_wal(db, cluster)
    with pytest.raises(backup.MigrationRefused) as refused:
        backup.migrate(db, migrations=catalog() + [marker])
    assert 'latest_backup_not_verified' in refused.value.reasons
    assert db.execute("select to_regclass('control.nx035_pgbackrest_marker') is null").fetchone() == (True,)
    # A later verified backup opens it again; the failed row stays as the record of the attempt.
    rest['set_state'](verify_rc=0, backup_error=False, status_code=0)
    assert rest['take']()['verify_status'] == 'verified'
    switch_wal(db, cluster)
    assert backup.migration_gate(db)['allowed'] is True
    assert [r[2] for r in rest['rows']()] == ['pending', 'failed', 'pending', 'verified']


def test_an_incomplete_backup_another_cluster_or_another_version_records_no_completed_row(cluster, db, rest):  # noqa: F811
    with pytest.raises(backup.BackupUnavailable, match='not the pinned'):
        backup.take_pgbackrest_backup(recorder_conninfo=recorder(cluster), command=rest['command'], stanza='nexloop', expected_version='0.0.1')
    assert rest['rows']() == []  # nothing recorded before the version is the pinned one
    rest['set_state'](fail_backup=True)
    with pytest.raises(backup.BackupUnavailable, match='backup failed'):
        rest['take']()
    assert [r[2] for r in rest['rows']()] == ['pending']
    rest['set_state'](fail_backup=False, system_id='1')
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        rest['take']()  # the manifest refuses a backup of another cluster
    assert [r[2] for r in rest['rows']()] == ['pending', 'pending']
    assert backup.migration_gate(db)['reasons'][:1] == ['no_backup_manifest']


def test_a_pgbackrest_row_cannot_be_verified_without_its_evidence(cluster, db):  # noqa: F811
    with psycopg.connect(recorder(cluster), autocommit=True) as c:
        c.execute('select authz.nexloop_record_backup(%s)', (Jsonb({'backup_id': 'manual-F', 'repo': 'repo1', 'kind': 'pg_full', 'verify_status': 'pending',
                                                                     'tool': 'pgbackrest', 'tool_version': PINNED}),))
        sid = db.execute('select system_identifier::text from pg_control_system()').fetchone()[0]
        final = {'backup_id': 'manual-F', 'repo': 'repo1', 'kind': 'pg_full', 'verify_status': 'verified', 'completed_at': backup._now(), 'system_identifier': sid,
                 'timeline': 1, 'start_lsn': '0/1000000', 'stop_lsn': '0/1000100'}
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            c.execute('select authz.nexloop_record_backup(%s)', (Jsonb(final),))
        for bad in ({'schema': 'other'}, {'schema': 'nexloop-backup-verification/1', 'pad': 'x' * 17000}):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                c.execute('select authz.nexloop_record_backup(%s)', (Jsonb({**final, 'verification': bad}),))
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            c.execute('select authz.nexloop_record_backup(%s)', (Jsonb({'backup_id': 'x-F', 'repo': 'repo1', 'kind': 'pg_full', 'verify_status': 'pending',
                                                                         'tool': 'rsync'}),))


def test_the_cli_reads_credentials_only_from_private_files_and_prints_none(cluster, db, rest, capsys):  # noqa: F811
    dsn = private(rest['tmp'] / 'recorder.dsn', recorder(cluster))
    command = rest['tmp'] / 'pgbackrest-command.json'
    command.write_text(json.dumps(rest['command']))
    args = ['pg', '--recorder-dsn-file', str(dsn), '--pgbackrest-command-file', str(command), '--stanza', 'nexloop',
            '--pgbackrest-config-file', str(rest['config']), '--expected-version', PINNED]
    assert backup.backup_main(args) == 0
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result['ok'] and result['verify_status'] == 'verified' and SECRET not in out
    stored = db.execute('select coalesce(string_agg(verification::text,\'\'),\'\') from runtime.nexloop_backup_manifests').fetchone()[0]
    assert SECRET not in stored and str(rest['config']) not in stored
    # A configuration file others can read is refused before anything runs; so are the pg_basebackup arguments mixed in.
    os.chmod(rest['config'], 0o644)
    assert backup.backup_main(args) == 1 and json.loads(capsys.readouterr().out)['ok'] is False
    os.chmod(rest['config'], 0o600)
    assert backup.backup_main(args + ['--pg-bin', '/x']) == 1
    capsys.readouterr()
    assert [r[2] for r in rest['rows']()] == ['pending', 'verified']
