"""NX-035 slice 3 on PostgreSQL: the owner-side file backup inventory (0450) and nexloop-file-backup recording a
files_artifact / files_runtime row through the unchanged 0146 recorder. restic is tests/support/restic_stand_in.py.

Synthetic rows are written by the test superuser with triggers suspended for that one statement (the same injection the
retention tests use): two tenants, Artifacts in every status, Runs with terminal, active and purge-queued tasks, and rows of
a simulation world. Disposable cluster only."""
import hashlib
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

import nexloop_eios.file_backup as fb
from backup_cluster import as_role, cluster, pg  # noqa: F401
from nexloop_eios.bootstrap import bootstrap
from support.restic_stand_in import write_executable
from support.tenants import TENANT_A, TENANT_B

NOW = datetime.now(UTC)
INVENTORY = 'authz.nexloop_file_backup_inventory(text,text,integer)'


@pytest.fixture
def db(cluster, admin):
    bootstrap(admin)
    for tenant in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    return admin


def inject(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(sql, params)


def namespace(tenant, world):
    return hashlib.sha256(tenant.encode() + b'\0' + world.encode()).hexdigest()


def artifact(admin, tenant, *, world='real', status='available', expires=timedelta(days=30), payload=None):
    payload = payload or secrets.token_bytes(64)
    artifact_id = secrets.token_hex(16)
    key = namespace(tenant, world) + '/' + artifact_id
    inject(admin, """insert into runtime.nexloop_local_artifacts(tenant_id,world,artifact_id,principal_id,sha256,size_bytes,media_type,object_key,
        retention_until,status,payload_digest,upload_worker,upload_token,upload_fence,lease_until,permit_id)
        values(%s,%s,%s,'synthetic-principal',%s,%s,'application/octet-stream',%s,%s,%s,%s,%s,'t',1,clock_timestamp(),'p')""",
           (tenant, world, artifact_id, hashlib.sha256(payload).hexdigest(), len(payload), key, NOW + expires, status, '0' * 64, secrets.token_hex(16)))
    return key, payload


def run(admin, tenant, *, status='succeeded', world='real', purge=False):
    run_id, job = uuid.uuid4(), 'file-backup-job-' + secrets.token_hex(4)
    inject(admin, """insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,
        trace_id,request_id,normalized_input,plan_snapshot,status,created_at,updated_at,world,queue)
        values(%s,'inv-1','nexloop.run','1','runtime','async',%s,'a','t','r','{}','{}',%s,clock_timestamp(),clock_timestamp(),%s,'operations')""",
           (job, tenant, status, world))
    inject(admin, """insert into authz.nexloop_runtime_run_bindings(run_id,tenant_id,world,queue,task_id,run_digest,command_digest,input_digest,owner_epoch,
        enrolled_by,created_at) values(%s,%s,%s,'operations',%s,%s,%s,%s,1,'synthetic',clock_timestamp())""",
           (run_id, tenant, world, job, secrets.token_hex(32), 'a' * 64, 'b' * 64))
    if purge:
        inject(admin, """insert into runtime.nexloop_run_purge_items(tenant_id,world,run_id,request_ref,reason) values(%s,%s,%s,'synthetic','retention_expiry')""",
               (tenant, world, run_id))
    return str(run_id)


def listed(cluster, kind, after=None, limit=100):
    with as_role(cluster, 'nexloop_backup_recorder') as c:
        return c.execute(f'select {INVENTORY.split("(")[0]}(%s,%s,%s)', (kind, after, limit)).fetchone()[0]['entries']


def test_the_inventory_lists_only_live_artifacts_and_finished_unpurged_runs_of_the_real_world(cluster, db):
    live_a, _ = artifact(db, TENANT_A)
    live_b, _ = artifact(db, TENANT_B)
    artifact(db, TENANT_A, expires=-timedelta(days=1))
    for status in ('pending', 'deleting', 'deleted'):
        artifact(db, TENANT_A, status=status)
    artifact(db, TENANT_A, world='sim:synthetic')
    done_a, done_b = run(db, TENANT_A), run(db, TENANT_B, status='failed')
    cancelled = run(db, TENANT_A, status='cancelled')
    run(db, TENANT_A, status='running')
    run(db, TENANT_A, purge=True)
    run(db, TENANT_A, world='sim:synthetic')
    entries = listed(cluster, 'files_artifact')
    assert [e['ref'] for e in entries] == sorted([live_a, live_b])
    assert all(set(e) == {'ref', 'sha256', 'bytes'} for e in entries)
    assert [e['ref'] for e in listed(cluster, 'files_runtime')] == sorted([done_a, done_b, cancelled])
    # Keyset paging across tenants: one at a time, in ref order, nothing repeated or lost.
    first = listed(cluster, 'files_runtime', limit=1)
    second = listed(cluster, 'files_runtime', after=first[0]['ref'], limit=1)
    assert first[0]['ref'] < second[0]['ref'] and len(listed(cluster, 'files_runtime', after=second[0]['ref'])) == 1
    assert fb.inventory(cluster['as_role']('nexloop_backup_recorder'), 'files_runtime', page=1) == listed(cluster, 'files_runtime')


def test_only_the_backup_recorder_may_read_the_inventory_and_only_with_valid_arguments(cluster, db):
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_runtime', 'nexloop_metrics', 'nexloop_configurator', 'nexloop_backup'):
        assert db.execute("select has_function_privilege(%s,%s,'execute')", (role, INVENTORY)).fetchone() == (False,), role
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_file_backup_inventory(%s,null,10)', ('files_artifact',))
    for kind, after, limit in (('pg_full', None, 10), ('files_artifact', None, 0), ('files_artifact', None, 1001), ('files_runtime', "x' or 1=1", 10)):
        with as_role(cluster, 'nexloop_backup_recorder') as c, pytest.raises(psycopg.Error, match='file backup inventory invalid'):
            c.execute('select authz.nexloop_file_backup_inventory(%s,%s,%s)', (kind, after, limit))
    # The recorder still reads no table.
    with as_role(cluster, 'nexloop_backup_recorder') as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute('select 1 from runtime.nexloop_local_artifacts limit 1')


def test_a_file_backup_run_records_one_row_per_kind_through_the_recorder(cluster, db, tmp_path):
    source, runs, staging, repo = tmp_path / 'artifacts', tmp_path / 'runs', tmp_path / 'staging', tmp_path / 'repo'
    for directory in (source, runs):
        directory.mkdir()
    staging.mkdir(mode=0o700)
    for _ in range(2):
        key, payload = artifact(db, TENANT_A)
        (source / key).parent.mkdir(exist_ok=True)
        (source / key).write_bytes(payload)
    finished = run(db, TENANT_A)
    (runs / finished).mkdir(mode=0o700)
    store = sqlite3.connect(runs / finished / 'runtime.sqlite')
    store.execute('create table t(x)')
    store.commit()
    store.close()
    repository = tmp_path / 'repository'
    repository.write_text(str(repo))
    password = tmp_path / 'password'
    password.write_text('synthetic-restic-password')
    for path in (repository, password):
        path.chmod(0o600)
    common = dict(recorder_conninfo=cluster['as_role']('nexloop_backup_recorder'), staging_root=staging, repository_file=repository,
                  password_file=password, key_ref='restic-repo1', restic=str(write_executable(tmp_path / 'restic')))
    files = fb.run_file_backup(kind='artifact', source_root=source, **common)
    runtime = fb.run_file_backup(kind='runtime', source_root=runs, **common)
    assert files['verify_status'] == runtime['verify_status'] == 'verified'
    rows = db.execute("""select kind,backup_id,verify_status,file_count,bytes,file_manifest_digest,key_ref,verified_at is not null
        from runtime.nexloop_backup_manifests where kind like 'files_%%' order by kind""").fetchall()
    assert rows == [('files_artifact', files['backup_id'], 'verified', 2, files['bytes'], files['file_manifest_digest'], 'restic-repo1', True),
                    ('files_runtime', runtime['backup_id'], 'verified', 1, runtime['bytes'], runtime['file_manifest_digest'], 'restic-repo1', True)]
