"""NX-035 local part: backup roles, the manifest, real base backups and the migration gate (AT-056), the M12 backup block.

Runs on a disposable archived PG 18 cluster (backup_cluster.archived `pg`); backups are real pg_basebackup runs as the
non-superuser replication role and verified by pg_verifybackup. No exemption from the gate exists or is used."""
import argparse
import inspect
import json
import os
import stat
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

import nexloop_eios.backup as backup
from backup_cluster import as_role, cluster, pg, switch_wal  # noqa: F401
from eios.adapters.postgres.migrations import Migration
from nexloop_eios.bootstrap import bootstrap, catalog
from nexloop_eios.observability import load_rules

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
BUSINESS_TABLES = ['ontology.objects', 'runtime.nexloop_conversation_messages', 'control.nexloop_contact_restrictions', 'runtime.nexloop_effect_intents',
                   'control.nexloop_browser_sessions', 'authz.nexloop_service_credentials', 'control.nexloop_deletion_ledger']
NX035_TABLES = ['runtime.nexloop_backup_manifests', 'control.nexloop_restore_journal', 'runtime.nexloop_restore_drills',
                'control.nexloop_restore_state', 'control.nexloop_restore_channels']


@pytest.fixture
def db(cluster, admin):
    bootstrap(admin)
    return admin


def recorder(cluster):
    return cluster['as_role']('nexloop_backup_recorder')


def full_backup(cluster, name, **kw):
    return backup.take_pg_backup(recorder_conninfo=recorder(cluster), replication_conninfo=cluster['as_role']('nexloop_backup'),
                                 pg_bin=cluster['bin'], target=cluster['root'] / name, **kw)


def gate(db):
    return backup.migration_gate(db)


def test_backup_roles_read_no_table_and_the_manifest_takes_no_free_text(cluster, db):
    for role in ('nexloop_backup_recorder', 'nexloop_backup'):
        assert db.execute('select rolsuper,rolcreaterole,rolbypassrls from pg_roles where rolname=%s', (role,)).fetchone() == (False, False, False)
        for table in BUSINESS_TABLES + NX035_TABLES:
            assert db.execute("select has_table_privilege(%s,%s,'select')", (role, table)).fetchone() == (False,), (role, table)
        with as_role(cluster, role) as c:
            for table in ('ontology.objects', 'runtime.nexloop_backup_manifests', 'control.nexloop_restore_journal'):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f'select 1 from {table} limit 1')
    assert db.execute("select rolreplication from pg_roles where rolname='nexloop_backup'").fetchone() == (True,)
    # Application roles cannot record, export or mark; the backup roles cannot reach the restore steps.
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_configurator', 'nexloop_metrics', 'nexloop_backup'):
        for f in ('authz.nexloop_record_backup(jsonb)', 'authz.nexloop_restore_journal_since(bigint,integer)', 'authz.nexloop_drill_mark(text)'):
            assert db.execute("select has_function_privilege(%s,%s,'execute')", (role, f)).fetchone() == (False,), (role, f)
    for f in ('runtime.nexloop_restore_replay(text,jsonb)', 'runtime.nexloop_restore_begin(text,jsonb,timestamptz,timestamptz,integer)', 'authz.nexloop_migration_gate()'):
        assert db.execute("select has_function_privilege('nexloop_backup_recorder',%s,'execute')", (f,)).fetchone() == (False,)
    # Labels, hashes, LSNs, numbers and times only.
    entry = {'backup_id': '20261011-010000F', 'repo': 'repo1', 'kind': 'pg_full', 'verify_status': 'pending'}
    for bad in ({**entry, 'note': 'ok'}, {**entry, 'backup_id': 'full backup of customer data'}, {**entry, 'repo': 'nas'},
                {**entry, 'key_ref': 'see attached'}, {**entry, 'release_id': {'x': 1}}):
        with pytest.raises(psycopg.Error, match='backup manifest invalid'):
            backup.record(recorder(cluster), bad)
    first = backup.record(recorder(cluster), entry)
    assert first['replay'] is False and backup.record(recorder(cluster), entry) == {**first, 'replay': True}
    row = db.execute("select system_identifier,catalog_revision,deletion_watermark,journal_seq,started_at is not null from runtime.nexloop_backup_manifests").fetchone()
    assert row[0] == str(db.execute('select system_identifier from pg_control_system()').fetchone()[0]) and row[1] == catalog()[-1].version
    # The completed row must name the same cluster; a backup of another cluster is refused.
    with pytest.raises(psycopg.Error, match='backup manifest invalid'):
        backup.record(recorder(cluster), {**entry, 'verify_status': 'verified', 'completed_at': backup._now(), 'system_identifier': '1', 'timeline': 1,
                                          'start_lsn': '0/2000028', 'stop_lsn': '0/2000138'})
    with pytest.raises(psycopg.Error, match='append-only'):
        db.execute('update runtime.nexloop_backup_manifests set repo=%s', ('repo2',))


def test_snapshot_backup_block_is_unavailable_without_a_backup_and_numbers_after(cluster, db):
    snap = lambda: db.execute("select runtime.nexloop_metrics_snapshot('t-none','real')->'backup'").fetchone()[0]
    assert snap() == {'status': 'unavailable'}  # AT-045: never zero
    result = full_backup(cluster, 'b1')
    assert result['verify_status'] == 'verified'
    switch_wal(db, cluster)
    block = snap()
    assert block['status'] == 'ok' and 0 <= block['age_seconds'] < 120 and block['verify_failed'] == 0 and block['wal_archive_failing'] == 0
    assert block['repo_backup_age_seconds'].keys() == {'repo1'} and block['journal_export_lag_seconds'] == 0
    assert 'file_backup_age_seconds' not in block and 0 <= block['drill_age_seconds'] < 120  # absent, not zero; drill counted from the backup
    assert all(isinstance(v, (int, float)) for k, v in block.items() if k not in ('status', 'repo_backup_age_seconds'))
    # Alert rules v3: every new backup rule names a number the snapshot provides (or an absent metric), thresholds per design.
    rules = {r['rule_id']: r for r in load_rules(ROOT / 'deploy/configuration/alert-rules.v3.json')['rules']}
    v2 = json.loads((ROOT / 'deploy/configuration/alert-rules.v2.json').read_text())
    assert v2['version'] == 2 and {r['rule_id'] for r in v2['rules']} < set(rules)  # v2 unchanged, v3 is a new file
    assert rules['backup_age'] == next(r for r in v2['rules'] if r['rule_id'] == 'backup_age')
    expected = {'backup_age': 93600, 'wal_archive_lag': 300, 'wal_archive_failing': 0, 'offsite_backup_age': 93600, 'file_backup_age': 3600,
                'restore_journal_lag': 300, 'restore_drill_overdue': 35 * 86400, 'backup_verify_failed': 0}
    for rule_id, threshold in expected.items():
        r = rules[rule_id]
        assert r['threshold'] == threshold and r['metric'].startswith('backup.'), rule_id
        value = block.get(r['metric'].split('.', 1)[1])
        if r.get('selector'):value = (value or {}).get(r['selector'])
        assert value is None or isinstance(value, (int, float)), rule_id


def test_at056_no_migration_without_a_verified_fresh_backup_of_this_cluster(cluster, db, tmp_path, monkeypatch):
    marker = Migration(version='9999', name='nx035_gate_marker', checksum='0' * 64, sql='create table control.nx035_gate_marker(x int);')
    migrations = catalog() + [marker]

    def refused(*reasons, applied=False):
        with pytest.raises(backup.MigrationRefused) as caught:
            backup.migrate(db, migrations=migrations)
        assert set(reasons) <= set(caught.value.reasons), caught.value.reasons
        assert db.execute("select to_regclass('control.nx035_gate_marker') is not null").fetchone() == (applied,)
        return caught.value.reasons

    refused('no_backup_manifest')
    # A backup whose verification fails is refused too (here the data file is damaged before pg_verifybackup reads it).
    damaged = cluster['root'] / 'damaging-bin'
    damaged.mkdir()
    for tool in ('pg_basebackup',):
        (damaged / tool).symlink_to(cluster['bin'] / tool)
    (damaged / 'pg_verifybackup').write_text(f'#!/bin/sh\nprintf x >> "$1/PG_VERSION"\nexec "{cluster["bin"]}/pg_verifybackup" "$@"\n')
    (damaged / 'pg_verifybackup').chmod(0o700)
    failed = backup.take_pg_backup(recorder_conninfo=recorder(cluster), replication_conninfo=cluster['as_role']('nexloop_backup'), pg_bin=damaged,
                                   target=cluster['root'] / 'b-damaged')
    assert failed['verify_status'] == 'failed'
    switch_wal(db, cluster)
    assert refused('latest_backup_not_verified') == ['latest_backup_not_verified']
    assert full_backup(cluster, 'b-good')['verify_status'] == 'verified'
    switch_wal(db, cluster)
    assert gate(db) == {**gate(db), 'allowed': True, 'reasons': []}
    # The restore fence blocks migrations.
    with db.transaction():
        db.execute('set local role nexloop_owner')
        db.execute("select runtime.nexloop_restore_begin('gate-check','{}'::jsonb,now(),now(),1)")
    refused('restore_fence_active')
    with db.transaction():
        db.execute('set local session_replication_role=replica')
        db.execute("update control.nexloop_restore_state set state='open',replayed_at=now(),reconciled_at=now(),verified_at=now(),window_end=now(),opened_at=now()")
    assert gate(db)['allowed'] is True
    # Aged, another cluster's, an unexported journal entry, a failing archive: each refuses.
    with db.transaction():
        db.execute('set local session_replication_role=replica')
        db.execute("update runtime.nexloop_backup_manifests set started_at=started_at-interval '2 hours',completed_at=completed_at-interval '2 hours' where verify_status<>'pending'")
    refused('backup_too_old')
    with db.transaction():
        db.execute('set local session_replication_role=replica')
        db.execute("update runtime.nexloop_backup_manifests set started_at=now(),completed_at=now(),system_identifier='1' where verify_status='verified'")
    refused('backup_from_another_cluster')
    with db.transaction():
        db.execute('set local session_replication_role=replica')
        db.execute("update runtime.nexloop_backup_manifests set system_identifier=(select system_identifier::text from pg_control_system()) where verify_status='verified'")
        db.execute("insert into control.nexloop_restore_journal(kind,entry,recorded_at) values('revocation','{\"subject\":\"service_credential\"}',now()-interval '10 minutes')")
    refused('restore_journal_export_lagging')
    exported = backup.export_journal(recorder_conninfo=recorder(cluster), journal_dir=tmp_path)
    assert exported['exported_seq'] >= 1 and gate(db)['allowed'] is True
    db.execute("alter system set archive_command='false'")
    db.execute('select pg_reload_conf()')
    db.execute('select pg_switch_wal()')
    import time
    deadline = time.monotonic() + 30
    while db.execute('select last_failed_time from pg_stat_archiver').fetchone()[0] is None:
        assert time.monotonic() < deadline
        time.sleep(0.2)
    refused('wal_archive_failing')
    db.execute('alter system reset archive_command')
    db.execute('select pg_reload_conf()')
    # Allowed: the reviewed migration runs, and a later migration needs a new backup (catalog changed after the backup).
    deadline = time.monotonic() + 30
    while not gate(db)['allowed']:
        assert time.monotonic() < deadline, gate(db)
        time.sleep(0.5)
    assert backup.migrate(db, migrations=migrations) == ['9999']
    refused('migrated_after_backup', applied=True)
    # CLI: refused with the reasons, exit 1, and no migration file is read while refused.
    monkeypatch.setattr('nexloop_eios.bootstrap.catalog', lambda: (_ for _ in ()).throw(AssertionError('migration files read')))
    dsn = tmp_path / 'dsn'
    dsn.write_text(cluster['dsn'])
    dsn.chmod(0o600)
    assert backup.migrate_main(['--database-url-file', str(dsn)]) == 1


def test_at056_no_exemption_exists():
    """No switch, flag or environment value bypasses the gate (dispatcher ruling Q3)."""
    source = inspect.getsource(backup)
    assert 'os.environ' not in source and 'getenv' not in source
    for word in ('skip', 'force', 'exempt', 'bypass', 'unsafe', 'no-backup', 'allow'):
        assert f"'--{word}" not in source and f'"--{word}' not in source
    parser_options = []
    original = argparse.ArgumentParser.add_argument
    def spy(self, *names, **kw):
        parser_options.extend(names)
        return original(self, *names, **kw)
    argparse.ArgumentParser.add_argument = spy
    try:
        with pytest.raises(SystemExit):
            backup.migrate_main(['--help'])
    finally:
        argparse.ArgumentParser.add_argument = original
    assert set(parser_options) == {'--database-url-file', '-h', '--help'}


def test_a_broken_journal_chain_stops_the_restore(tmp_path):
    (tmp_path / 'journal-000000000001-000000000001.json').write_text(json.dumps(
        {'schema': backup.SEGMENT_SCHEMA, 'from_seq': 1, 'to_seq': 1, 'prev_sha256': backup.GENESIS, 'entries': [{'seq': 1, 'kind': 'deletion', 'entry': {}}]}))
    (tmp_path / 'journal-000000000002-000000000002.json').write_text(json.dumps(
        {'schema': backup.SEGMENT_SCHEMA, 'from_seq': 2, 'to_seq': 2, 'prev_sha256': 'f' * 64, 'entries': [{'seq': 2, 'kind': 'deletion', 'entry': {}}]}))
    with pytest.raises(backup.JournalBroken):
        backup.read_journal(tmp_path, 0)
