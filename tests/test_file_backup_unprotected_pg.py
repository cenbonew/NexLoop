"""NX-035 D6 (migration file_backup_unprotected, alert rules v20): backup.file_backup_unprotected_seconds.

For the snapshot's tenant, world real: the oldest object the file backup inventory lists that became backupable after the start of
the latest verified snapshot of its kind (or with none of its kind) - absent while everything is covered or nothing is backupable.
The backup cluster with 0194's inventory helpers (synthetic rows injected by the test superuser); verified rows through the real
recorder. The alert through the real evaluator: test_file_backup_unprotected_alert_pg.py."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import nexloop_eios.backup as backup
from backup_cluster import cluster, pg  # noqa: F401
from nexloop_eios.observability import load_rules, validate_rules
from support.tenants import TENANT_A, TENANT_B
from test_file_backup_pg import artifact, db, inject, run  # noqa: F401

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'


def unprotected(admin, tenant, world='real'):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,%s)->'backup'->'file_backup_unprotected_seconds'", (tenant, world)).fetchone()[0]


def became_backupable(admin, *, artifact_key=None, run_id=None, ago):
    """As the real paths leave it: an Artifact finalized `ago` (updated_at then, its upload lease ending within 30 s of it); a Run whose
    task's terminal event was written `ago` (runtime.job_events, one row per transition)."""
    if artifact_key:
        inject(admin, """update runtime.nexloop_local_artifacts set updated_at=clock_timestamp()-%s::interval,
            lease_until=clock_timestamp()-%s::interval+interval '20 seconds' where object_key=%s""", (ago, ago, artifact_key))
    if run_id:
        inject(admin, """insert into runtime.job_events(tenant_id,job_id,sequence,status,message,created_at)
            select j.tenant_id,j.job_id,coalesce((select max(sequence) from runtime.job_events e where e.job_id=j.job_id),0)+1,j.status,
                   'synthetic terminal',clock_timestamp()-%s::interval
            from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id where b.run_id=%s::uuid""",
               (ago, run_id))


def rewritten_later(admin, *, artifact_key=None, run_id=None):
    """L6 S5: a later write that is no new data - a retention change on the Artifact, a result or status echo on the task row."""
    if artifact_key:
        inject(admin, """update runtime.nexloop_local_artifacts set retention_until=retention_until+interval '30 days',updated_at=clock_timestamp()
            where object_key=%s""", (artifact_key,))
    if run_id:
        inject(admin, """update runtime.jobs j set updated_at=clock_timestamp(),result='{"rewritten":true}' from authz.nexloop_runtime_run_bindings b
            where b.run_id=%s::uuid and j.tenant_id=b.tenant_id and j.job_id=b.task_id""", (run_id,))


def verified(cluster, kind, started_ago_minutes):
    started = datetime.now(UTC) - timedelta(minutes=started_ago_minutes)
    iso = lambda t: t.isoformat().replace('+00:00', 'Z')  # noqa: E731
    backup.record(cluster['as_role']('nexloop_backup_recorder'), {
        'backup_id': 'c' * 64 if kind == 'files_artifact' else 'd' * 64, 'repo': 'repo1', 'kind': kind, 'verify_status': 'verified',
        'started_at': iso(started), 'completed_at': iso(started + timedelta(minutes=1)), 'file_manifest_digest': 'e' * 64, 'file_count': 1, 'bytes': 1})


def test_absent_without_backupable_objects_and_never_for_what_the_inventory_leaves_out(cluster, db):
    assert unprotected(db, TENANT_A) is None
    for status in ('pending', 'deleting', 'deleted'):
        key, _ = artifact(db, TENANT_A, status=status)
        became_backupable(db, artifact_key=key, ago='3 hours')
    key, _ = artifact(db, TENANT_A, expires=-timedelta(days=1))
    became_backupable(db, artifact_key=key, ago='3 hours')
    key, _ = artifact(db, TENANT_A, world='sim:synthetic')
    became_backupable(db, artifact_key=key, ago='3 hours')
    for kw in ({'status': 'running'}, {'purge': True}, {'world': 'sim:synthetic'}):
        became_backupable(db, run_id=run(db, TENANT_A, **kw), ago='3 hours')
    assert unprotected(db, TENANT_A) is None
    # Another tenant's backupable Artifact is that tenant's, not this one's; other worlds never carry the key.
    key, _ = artifact(db, TENANT_B)
    became_backupable(db, artifact_key=key, ago='3 hours')
    assert unprotected(db, TENANT_A) is None and 10790 <= unprotected(db, TENANT_B) <= 10860
    assert unprotected(db, TENANT_B, world='sim:synthetic') is None


def test_never_backed_up_counts_from_the_oldest_backupable_object_of_either_kind(cluster, db):
    key, _ = artifact(db, TENANT_A)
    became_backupable(db, artifact_key=key, ago='90 minutes')
    assert 5390 <= unprotected(db, TENANT_A) <= 5460
    finished = run(db, TENANT_A, status='failed')
    became_backupable(db, run_id=finished, ago='5 hours')
    assert 17990 <= unprotected(db, TENANT_A) <= 18060
    # The block is there even before any PG backup (status unavailable): the file backup's own gap is not hidden by it.
    block = db.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'backup'", (TENANT_A,)).fetchone()[0]
    assert block['status'] == 'unavailable' and set(block) == {'status', 'file_backup_unprotected_seconds'}


def test_a_verified_snapshot_covers_what_was_backupable_when_it_started_per_kind(cluster, db):
    old, _ = artifact(db, TENANT_A)
    became_backupable(db, artifact_key=old, ago='2 hours')
    finished = run(db, TENANT_A)
    became_backupable(db, run_id=finished, ago='3 hours')
    verified(cluster, 'files_artifact', started_ago_minutes=40)
    # The Artifact is covered; the Run is not (no files_runtime snapshot): 3 hours.
    assert 10790 <= unprotected(db, TENANT_A) <= 10860
    verified(cluster, 'files_runtime', started_ago_minutes=40)
    assert unprotected(db, TENANT_A) is None
    # Backupable while that snapshot ran (after its start, before its end): its inventory was read at the start, so not covered.
    during, _ = artifact(db, TENANT_A)
    became_backupable(db, artifact_key=during, ago='39 minutes 30 seconds')
    assert 2360 <= unprotected(db, TENANT_A) <= 2430
    inject(db, "update runtime.nexloop_local_artifacts set status='deleted' where object_key=%s", (during,))
    assert unprotected(db, TENANT_A) is None
    # Backupable after the latest verified start: not in that snapshot. It counts from when it became backupable.
    new, _ = artifact(db, TENANT_A)
    became_backupable(db, artifact_key=new, ago='30 minutes')
    assert 1790 <= unprotected(db, TENANT_A) <= 1860
    # A failed snapshot covers nothing.
    backup.record(cluster['as_role']('nexloop_backup_recorder'), {
        'backup_id': 'f' * 64, 'repo': 'repo1', 'kind': 'files_artifact', 'verify_status': 'failed', 'started_at': backup._now(),
        'completed_at': backup._now(), 'file_manifest_digest': 'e' * 64, 'file_count': 0, 'bytes': 0})
    assert 1790 <= unprotected(db, TENANT_A) <= 1860
    verified_now = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
    backup.record(cluster['as_role']('nexloop_backup_recorder'), {
        'backup_id': 'a' * 64, 'repo': 'repo1', 'kind': 'files_artifact', 'verify_status': 'verified', 'started_at': verified_now,
        'completed_at': backup._now(), 'file_manifest_digest': 'e' * 64, 'file_count': 2, 'bytes': 2})
    assert unprotected(db, TENANT_A) is None


def test_a_later_write_after_the_end_does_not_make_a_covered_object_unprotected_again(cluster, db):
    """L6 S5: the moment an object became backupable is one no later update moves."""
    key, _ = artifact(db, TENANT_A)
    became_backupable(db, artifact_key=key, ago='2 hours')
    finished = run(db, TENANT_A)
    became_backupable(db, run_id=finished, ago='2 hours')
    verified(cluster, 'files_artifact', started_ago_minutes=40)
    verified(cluster, 'files_runtime', started_ago_minutes=40)
    assert unprotected(db, TENANT_A) is None
    rewritten_later(db, artifact_key=key, run_id=finished)
    assert unprotected(db, TENANT_A) is None
    # Not even with the Artifact's updated_at alone far in the past or the future: its lease bounds the moment.
    inject(db, "update runtime.nexloop_local_artifacts set updated_at=clock_timestamp()+interval '1 day' where object_key=%s", (key,))
    assert unprotected(db, TENANT_A) is None
    # Only terminal transitions count: a later non-terminal event row of the finished task moves nothing.
    inject(db, """insert into runtime.job_events(tenant_id,job_id,sequence,status,message,created_at)
        select j.tenant_id,j.job_id,100,'running','synthetic echo',clock_timestamp() from authz.nexloop_runtime_run_bindings b
        join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id where b.run_id=%s::uuid""", (finished,))
    assert unprotected(db, TENANT_A) is None
    # A task without any terminal event counts from its creation (fixed): created 10 minutes ago, after the snapshot started; its
    # updated_at, rewritten later, moves nothing.
    bare = run(db, TENANT_A)
    inject(db, "update runtime.jobs j set created_at=clock_timestamp()-interval '10 minutes',updated_at=clock_timestamp()+interval '1 hour' "
               "from authz.nexloop_runtime_run_bindings b where b.run_id=%s::uuid and j.tenant_id=b.tenant_id and j.job_id=b.task_id", (bare,))
    assert 590 <= unprotected(db, TENANT_A) <= 660
    inject(db, "delete from authz.nexloop_runtime_run_bindings where run_id=%s::uuid", (bare,))
    # A requeued task that ends again is new data: its new terminal event, after the snapshot started, counts.
    became_backupable(db, run_id=finished, ago='5 minutes')
    assert 290 <= unprotected(db, TENANT_A) <= 360


def test_rules_v20_are_v19_plus_the_unprotected_rule():
    v19, v20 = json.loads((RULES / 'alert-rules.v19.json').read_text()), load_rules(RULES / 'alert-rules.v20.json')
    assert v20['version'] == 20 and v20['rules'][:len(v19['rules'])] == v19['rules'] and len(v20['rules']) == len(v19['rules']) + 1 == 77
    assert {k: v20['rules'][-1][k] for k in ('rule_id', 'metric', 'condition', 'threshold', 'for_seconds', 'severity')} == {
        'rule_id': 'file_backup_unprotected', 'metric': 'backup.file_backup_unprotected_seconds', 'condition': 'gt', 'threshold': 3600,
        'for_seconds': 0, 'severity': 'warning'}
    assert v20['decision'].startswith(v19['decision']) and 'v20 (NX-035 D6' in v20['decision']
    validate_rules(v20)
