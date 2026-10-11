"""NX-035 local part, AT-050 local evidence and AT-059 with a real backup: a point-in-time restore drill on a disposable cluster.

The source is a disposable archived PG 18 cluster (backup_cluster) carrying the NX-029 erasure fixture: a real base backup
(pg_basebackup as the non-superuser replication role, pg_verifybackup), a named restore point marked by the recorder with
counts and sampled row hashes, then — after the restore point — a contact restriction and an owner's erasure of the
Consumer, exported into the hash-chained journal. The restore runs into a new directory on a new socket-only port; the
restored database comes from before the erasure, the journal replay erases the Consumer again before anything is served,
and the drill stops in verified_readonly. Synthetic data; RPO/RTO here are local measurements, not deployment evidence.
"""
import json
from pathlib import Path
import stat
from datetime import UTC, datetime

import psycopg
import pytest

import nexloop_eios.backup as backup
from backup_cluster import cluster, free_port, pg, switch_wal  # noqa: F401
from erasure_support import ERASING  # noqa: F401
from test_erasure_pg import ACTIONS, KEEPER, act, erasing, keeper, request  # noqa: F401
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import fresh

# The bootstrap head the restored lineage must report: the last migration of the catalog.
CATALOG_HEAD=json.loads((Path(__file__).resolve().parents[1]/'packages/eios-core/src/eios/migrations/catalog.json').read_text())['migrations'][-1]['version']

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


def test_at050_pitr_drill_restores_before_an_erasure_and_the_journal_erases_again(erasing, admin, cluster, tmp_path):
    c = erasing
    consumer = c['consumer']
    recorder = cluster['as_role']('nexloop_backup_recorder')
    journal = tmp_path / 'journal'
    journal.mkdir(mode=0o700)
    commitment, source = fresh(c)
    base = backup.take_pg_backup(recorder_conninfo=recorder, replication_conninfo=cluster['as_role']('nexloop_backup'), pg_bin=cluster['bin'],
                                 target=cluster['root'] / 'base')
    assert base['verify_status'] == 'verified'
    backup.export_journal(recorder_conninfo=recorder, journal_dir=journal)
    mark = backup.drill_mark(recorder, 'd1')
    assert mark['restore_point'] == 'drill-d1' and set(mark['tables']) == set(admin.execute('select runtime.nexloop_drill_tables()').fetchone()[0])
    assert 'SECRET' not in json.dumps(mark) and all(set(t) == {'max_id', 'count', 'samples'} for t in mark['tables'].values())
    # After the restore point: the customer refuses contact, then the owner erases the Consumer (both lost by the PITR).
    c.inbound('以后别再给我发消息了')
    act(c['owner_actions'], 'erase_consumer', 'erase-after-mark', consumer_id=consumer, reason='负责人确认删除')
    assert admin.execute("select active from control.nexloop_contact_restrictions where consumer_id=%s", (consumer,)).fetchone() == (True,)
    exported = backup.export_journal(recorder_conninfo=recorder, journal_dir=journal)
    failure_time = datetime.now(UTC)  # the simulated failure: right after the last journal export
    switch_wal(admin, cluster)
    # Segments: 0600, ids, hashes, sequence numbers and times only — never the customer's words or message text.
    for path in journal.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        text = path.read_text()
        assert '以后别再给我发消息了' not in text and '"matched_text"' not in text and '"body"' not in text and '"release_reason"' not in text
    kinds = {e['kind'] for e in backup.read_journal(journal, 0)}
    assert {'deletion', 'contact_restriction'} <= kinds
    # The restore tool refuses an occupied port, a non-empty target, a missing fence confirmation.
    socket_dir = cluster['root'] / 's2'
    socket_dir.mkdir(mode=0o700)
    common = dict(pg_bin=cluster['bin'], base_backup=cluster['root'] / 'base', wal_archive=cluster['archive'], socket_dir=socket_dir, owner_user='nexloop_bootstrap',
                  restore_ref='drill-d1', window_start=mark['marked_at'], window_end=failure_time, journal_dir=journal, evidence=[],
                  recovery_target_name=mark['restore_point'], backup_id=base['backup_id'], hba_method='trust', running_ports=(cluster['port'],))
    with pytest.raises(backup.RestoreRefused, match='port'):
        backup.restore(**common, target=cluster['root'] / 'r0', port=cluster['port'], old_primary_fenced=True)
    with pytest.raises(backup.RestoreRefused, match='fenced'):
        backup.restore(**common, target=cluster['root'] / 'r0', port=free_port())
    with pytest.raises(backup.RestoreRefused, match='not empty'):
        backup.restore(**common, target=cluster['data'], port=free_port(), old_primary_fenced=True)
    instance, report = backup.restore(**common, target=cluster['root'] / 'restored', port=free_port(), old_primary_fenced=True)
    try:
        with psycopg.connect(instance.conninfo, autocommit=True) as restored:
            # Before anything is served: the Consumer erased again by the replay (AT-059), the restriction back, no session.
            assert report['journal_entries'] >= 2 and report['replay']['deletions'] >= 1 and report['replay']['contact_restrictions'] >= 1
            assert restored.execute("select state from control.nexloop_deletion_replays where restore_ref='drill-d1'").fetchone() == ('replayed',)
            assert restored.execute('select runtime.nexloop_consumer_erasing(%s,%s,%s)', (TENANT, 'real', consumer)).fetchone() == (True,)
            assert restored.execute("select active,matched_text from control.nexloop_contact_restrictions where consumer_id=%s", (consumer,)).fetchone() == (True, '')
            assert restored.execute('select count(*) from control.nexloop_browser_sessions').fetchone() == (0,)
            assert restored.execute("select count(*) from authz.nexloop_run_credentials where status='active'").fetchone() == (0,)
            # Only the restoring owner (here the cluster's bootstrap superuser) can log in.
            assert restored.execute(r"select array_agg(rolname) from pg_roles where rolname like 'nexloop\_%' and rolcanlogin").fetchone() == (['nexloop_bootstrap'],)
            # Deletion watermark: the restored database's journal position lies between the backup's start watermark and the
            # last export, and every entry above it was replayed.
            started = admin.execute("select journal_seq,deletion_watermark from runtime.nexloop_backup_manifests where backup_id=%s and verify_status='verified'",
                                    (base['backup_id'],)).fetchone()
            assert started[0] <= report['journal_from'] < exported['exported_seq']
            assert report['journal_entries'] == exported['exported_seq'] - report['journal_from']
            assert restored.execute('select coalesce(max(seq),0) from control.nexloop_deletion_ledger where seq<=%s', (10**12,)).fetchone()[0] >= started[1]
            assert report['roles_set_nologin'] >= 3 and report['lineage']['revision'] == CATALOG_HEAD
            # Fence: verified_readonly, never opened in a drill; dispatch refused (NXR01) until a scope is opened.
            state = restored.execute('select state,rto_seconds,window_start,window_end from control.nexloop_restore_state').fetchone()
            assert state[0] == 'verified_readonly' and state[1] > 0
            with restored.transaction():
                restored.execute('set local role nexloop_owner')
                assert restored.execute('select runtime.nexloop_restore_readiness()').fetchone()[0]['dispatch'] is False
                with pytest.raises(psycopg.Error, match='restored database not verified'):
                    restored.execute("select runtime.nexloop_restore_assert_open(%s,'real')", (TENANT,))
            # Effect window: every channel without time evidence is paused, and the report lists it with its counts.
            for row in report['window']['channels']:
                assert row['paused'] is True and row['time_queryable'] is False
            # Consistency at the restore point: append-only counts and sampled rows, the migration catalog.
            compare = restored.execute('select runtime.nexloop_drill_compare(%s)', (psycopg.types.json.Jsonb(mark),)).fetchone()[0]
            assert compare['count_mismatches'] == 0 and compare['sample_mismatches'] == 0 and compare['catalog_equal'] is True
            assert compare['tables_compared'] == len(mark['tables']) and compare['samples_compared'] >= 1
            rto, rpo = float(state[1]), (failure_time - datetime.fromisoformat(mark['marked_at'])).total_seconds()
    finally:
        instance.stop()
    # The source cluster is untouched by the restore (still the erased state of the old timeline, archiving on).
    assert admin.execute('select runtime.nexloop_consumer_erasing(%s,%s,%s)', (TENANT, 'real', consumer)).fetchone() == (True,)
    assert admin.execute("select current_setting('archive_mode')").fetchone() == ('on',)
    checks = {'counts_equal': True, 'samples_equal': True, 'catalog_equal': True, 'journal_replayed': report['journal_entries'],
              'consumer_erased_again': True, 'roles_nologin': True, 'channels_paused': len(report['window']['channels'])}
    assert backup.record_drill_result(recorder, 'd1', passed=True, rto_seconds=rto, rpo_seconds=rpo, checks=checks)['recorded'] is True
    block = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'backup'", (TENANT,)).fetchone()[0]
    assert block['drill_rto_seconds'] == pytest.approx(rto) and block['drill_rpo_seconds'] == pytest.approx(rpo, abs=0.01) and block['drill_age_seconds'] < 60
    print(json.dumps({'drill': 'd1', 'rto_seconds': rto, 'rpo_seconds': round(rpo, 3), 'tool_seconds': report['tool_seconds'],
                      'journal_entries': report['journal_entries'], 'channels_paused': len(report['window']['channels'])}))

