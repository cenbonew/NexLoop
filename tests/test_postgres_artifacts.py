from datetime import UTC,datetime,timedelta
import secrets
import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from eios.authz import facts as F
from eios.authz.operations import Operation
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.local_artifacts import LocalBlobStore,ArtifactAccessDenied
from nexloop_eios.postgres_artifacts import AuthoritySigner,PostgresArtifactRepository,LocalArtifactService,canonical_payload
from authority_fixture import seed_authority,replace_fact

@pytest.fixture
def artifact_setup(admin,pg,tmp_path):
    bootstrap(admin)
    token,_=seed_authority(admin,'synthetic-a','eios:artifact:local_real',operations=(Operation.CREATE,Operation.READ,Operation.DELETE))
    signer=AuthoritySigner('synthetic-key',secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys(key_id,key_material,active) values(%s,%s,true)',(signer.key_id,signer.material))
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool,LocalBlobStore(tmp_path/'blobs') as store:
        session=authenticate_service(pool,token,world='real')
        repository=PostgresArtifactRepository(pool,session,signer)
        yield pool,repository,store,token,signer


def params(artifact_id=None):
    import hashlib
    return dict(artifact_id=artifact_id or secrets.token_hex(16),sha256=hashlib.sha256(b'hello').hexdigest(),size_bytes=5,
                media_type='text/plain',retention_until=(datetime.now(UTC)+timedelta(days=30)).isoformat())


@pytest.mark.parametrize('physical_file',[False,True])
def test_expired_pending_upload_retires_after_lease_without_finalize(artifact_setup,admin,physical_file):
    import time
    _,repo,store,_,_=artifact_setup
    p=params();p['retention_until']=(datetime.now(UTC)-timedelta(days=1)).isoformat()
    upload=repo.reserve(p,lease_seconds=1)
    if physical_file:
        store.put(tenant_id=upload.reference.tenant_id,world=upload.reference.world,
            artifact_id=upload.reference.artifact_id,payload=b'hello',media_type='text/plain')
    with pytest.raises(psycopg.errors.SerializationFailure):repo.claim_deletion(upload.reference.artifact_id)
    time.sleep(1.05)  # Actual lease expiry; no administrative timestamp mutation.
    service=LocalArtifactService(repo,store)
    service.delete_expired(upload.reference.artifact_id)
    assert admin.execute('select status from runtime.nexloop_local_artifacts').fetchone()[0]=='deleted'
    with pytest.raises(FileNotFoundError):store.read(upload.reference)
    with pytest.raises(psycopg.errors.SerializationFailure):repo.finalize(upload)
    with pytest.raises(psycopg.errors.SerializationFailure):
        with repo.upload_transaction(upload):
            pytest.fail('retired upload must not reach physical file writes')
    service.delete_expired(upload.reference.artifact_id)


def test_pending_upload_with_future_retention_is_not_an_orphan(artifact_setup):
    import time
    _,repo,store,_,_=artifact_setup
    upload=repo.reserve(params(),lease_seconds=1)
    time.sleep(1.05)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        LocalArtifactService(repo,store).delete_expired(upload.reference.artifact_id)


def test_real_pg_metadata_roundtrip_and_reopen(artifact_setup):
    pool,repo,store,_,_=artifact_setup
    service=LocalArtifactService(repo,store);deadline=datetime.now(UTC)+timedelta(days=30)
    ref=service.put(request_id='synthetic-upload-0001',payload=b'hello',media_type='text/plain',retention_until=deadline)
    assert service.read(ref.artifact_id,start=1,stop=4)==b'ell'
    assert service.put(request_id='synthetic-upload-0001',payload=b'hello',media_type='text/plain',retention_until=deadline)==ref
    with LocalBlobStore(store.root) as reopened:
        assert LocalArtifactService(repo,reopened).read(ref.artifact_id)==b'hello'
    with pool.connection() as c,c.transaction():
        for table in ['runtime.nexloop_local_artifacts','authz.nexloop_artifact_permits','authz.nexloop_authority_signing_keys']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with c.transaction():c.execute(f'select * from {table}')


def test_same_request_different_payload_conflicts_without_overwrite(artifact_setup):
    _,repo,store,_,_=artifact_setup;service=LocalArtifactService(repo,store);deadline=datetime.now(UTC)+timedelta(days=30)
    ref=service.put(request_id='synthetic-upload-0001',payload=b'hello',media_type='text/plain',retention_until=deadline)
    with pytest.raises(psycopg.errors.DataException):
        service.put(request_id='synthetic-upload-0001',payload=b'other',media_type='text/plain',retention_until=deadline)
    assert service.read(ref.artifact_id)==b'hello'


def test_signature_cannot_be_forged_by_application_role(artifact_setup):
    pool,repo,store,_,signer=artifact_setup
    forged=PostgresArtifactRepository(pool,repo.session,AuthoritySigner(signer.key_id,secrets.token_bytes(32)))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):forged.reserve(params())
    assert not list(store.root.rglob('*'))


def test_revoke_after_permit_prevents_metadata_reservation(artifact_setup,admin):
    pool,repo,_,_,_=artifact_setup;p=params();permit=repo.issue_permit(Operation.CREATE,p)
    principal='synthetic-a-principal';resource='eios:artifact:local_real'
    replace_fact(admin,'synthetic-a','grants',[principal,resource],F.GrantFacts,grants=[])
    with pool.connection() as c,c.transaction():
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('select authz.nexloop_reserve_local_artifact(%s,%s,%s,%s,%s,%s)',
                (repo.session.token_digest,'real',permit,canonical_payload(p),secrets.token_hex(16),1))


def test_permit_is_single_use_and_bound_to_payload(artifact_setup):
    pool,repo,_,_,_=artifact_setup;p=params();permit=repo.issue_permit(Operation.CREATE,p);worker=secrets.token_hex(16)
    with pool.connection() as c,c.transaction():
        row=c.execute('select authz.nexloop_reserve_local_artifact(%s,%s,%s,%s,%s,%s)',
            (repo.session.token_digest,'real',permit,canonical_payload(p),worker,1)).fetchone()[0]
        assert row['status']=='pending'
    with pool.connection() as c,c.transaction():
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('select authz.nexloop_reserve_local_artifact(%s,%s,%s,%s,%s,%s)',
                (repo.session.token_digest,'real',permit,canonical_payload(p),worker,1))
    other=repo.issue_permit(Operation.CREATE,p);changed=dict(p,sha256='a'*64)
    with pool.connection() as c,c.transaction():
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('select authz.nexloop_reserve_local_artifact(%s,%s,%s,%s,%s,%s)',
                (repo.session.token_digest,'real',other,canonical_payload(changed),worker,1))


def test_stale_upload_fence_rejected_after_lease_takeover(artifact_setup):
    pool,repo,store,_,_=artifact_setup;p=params();old=repo.reserve(p,lease_seconds=1)
    store.put(tenant_id=old.reference.tenant_id,world='real',artifact_id=old.reference.artifact_id,payload=b'hello',media_type='text/plain')
    with pool.connection() as c:c.execute('select pg_sleep(1.1)')
    new=repo.reserve(p,lease_seconds=5)
    assert new.fence==old.fence+1
    with pytest.raises(psycopg.errors.SerializationFailure):repo.finalize(old)
    assert repo.finalize(new)==old.reference
    assert LocalArtifactService(repo,store).read(new.reference.artifact_id)==b'hello'


def test_revoke_after_file_write_blocks_publish_and_read(artifact_setup,admin):
    _,repo,store,_,_=artifact_setup;p=params();claim=repo.reserve(p)
    store.put(tenant_id=claim.reference.tenant_id,world='real',artifact_id=claim.reference.artifact_id,payload=b'hello',media_type='text/plain')
    admin.execute("update authz.nexloop_service_credentials set status='revoked' where tenant_id='synthetic-a'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):repo.finalize(claim)
    assert store.read(claim.reference)==b'hello'


def test_other_tenant_cannot_read_artifact_metadata_or_bytes(artifact_setup,admin):
    pool,repo,store,_,signer=artifact_setup
    service=LocalArtifactService(repo,store)
    ref=service.put(request_id='synthetic-upload-0001',payload=b'hello',media_type='text/plain',retention_until=datetime.now(UTC)+timedelta(days=30))
    other,_=seed_authority(admin,'synthetic-b','eios:artifact:local_real',operations=(Operation.CREATE,Operation.READ,Operation.DELETE))
    session=authenticate_service(pool,other,world='real')
    other_service=LocalArtifactService(PostgresArtifactRepository(pool,session,signer),store)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):other_service.read(ref.artifact_id)
    assert service.read(ref.artifact_id)==b'hello'


def test_actual_process_exit_after_fsync_recovers_same_artifact(artifact_setup,pg,tmp_path):
    import json,os,subprocess,sys
    pool,repo,store,token,signer=artifact_setup
    deadline=datetime.now(UTC)+timedelta(days=30)
    credential=tmp_path/'service-credential';credential.write_text(token);credential.chmod(0o600)
    key=tmp_path/'authority-signing-key';key.write_bytes(signer.material);key.chmod(0o600)
    config=tmp_path/'private-child-config.json'
    config.write_text(json.dumps(dict(dsn=make_conninfo(pg,user='nexloop_api'),credential=str(credential),signer=str(key),key_id=signer.key_id,root=str(store.root),deadline=deadline.isoformat())))
    config.chmod(0o600)
    script='''
import json,os,sys
from pathlib import Path
from datetime import datetime
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.local_artifacts import LocalBlobStore
from nexloop_eios.postgres_artifacts import AuthoritySigner,PostgresArtifactRepository,LocalArtifactService
c=json.loads(Path(sys.argv[1]).read_text())
with open_core(c['dsn']) as pool,LocalBlobStore(Path(c['root'])) as store:
 session=authenticate_service(pool,Path(c['credential']).read_text(),world='real')
 repository=PostgresArtifactRepository(pool,session,AuthoritySigner.from_file(Path(c['signer']),key_id=c['key_id']))
 reserve=repository.reserve
 repository.reserve=lambda parameters:reserve(parameters,lease_seconds=1)
 original_put=store.put
 def crash_after_fsync(**kwargs):
  original_put(**kwargs)
  os._exit(23)
 store.put=crash_after_fsync
 LocalArtifactService(repository,store).put(request_id='synthetic-crash-upload',payload=b'hello',media_type='text/plain',retention_until=datetime.fromisoformat(c['deadline']))
'''
    child=subprocess.run([sys.executable,'-c',script,str(config)],capture_output=True,text=True,timeout=15)
    assert child.returncode==23
    assert child.stdout=='' and child.stderr==''
    physical_before={p.relative_to(store.root).as_posix() for p in store.root.rglob('*') if p.is_file()}
    assert len(physical_before)==1
    with pool.connection() as c:c.execute('select pg_sleep(1.1)')
    recovered=LocalArtifactService(repo,store).put(request_id='synthetic-crash-upload',payload=b'hello',media_type='text/plain',retention_until=deadline)
    assert LocalArtifactService(repo,store).read(recovered.artifact_id)==b'hello'
    physical_after={p.relative_to(store.root).as_posix() for p in store.root.rglob('*') if p.is_file()}
    assert physical_before==physical_after
    # Real process exit/reopen evidence for Artifact; not Pi Run recovery.


def test_signing_key_loader_rejects_public_and_symlink_files(tmp_path):
    private=tmp_path/'authority-key';private.write_bytes(secrets.token_bytes(32));private.chmod(0o600)
    signer=AuthoritySigner.from_file(private)
    assert 'material' not in repr(signer)
    private.chmod(0o644)
    with pytest.raises(PermissionError):AuthoritySigner.from_file(private)
    private.chmod(0o600);link=tmp_path/'key-link';link.symlink_to(private)
    with pytest.raises(OSError):AuthoritySigner.from_file(link)


def test_administrative_repository_dsn_rejected_before_any_business_operation(artifact_setup,pg):
    from psycopg_pool import ConnectionPool
    from eios.adapters.postgres.database import StorageUnavailable
    _,repo,_,_,signer=artifact_setup
    with ConnectionPool(pg) as administrative:
        with pytest.raises(StorageUnavailable):PostgresArtifactRepository(administrative,repo.session,signer)


def test_revoke_restore_cycle_never_resurrects_old_permit(artifact_setup,admin):
    from psycopg.types.json import Jsonb
    pool,repo,_,_,_=artifact_setup;p=params();permit=repo.issue_permit(Operation.CREATE,p)
    principal='synthetic-a-principal';resource='eios:artifact:local_real'
    old=admin.execute("select payload from authz.nexloop_authority_facts where tenant_id='synthetic-a' and fact_kind='grants' and entity_key=%s",([principal,resource],)).fetchone()[0]
    before=admin.execute("select authority_revision from control.nexloop_tenants where tenant_id='synthetic-a'").fetchone()[0]
    replace_fact(admin,'synthetic-a','grants',[principal,resource],F.GrantFacts,grants=[])
    admin.execute("update authz.nexloop_authority_facts set payload=%s where tenant_id='synthetic-a' and fact_kind='grants' and entity_key=%s",(Jsonb(old),[principal,resource]))
    after=admin.execute("select authority_revision from control.nexloop_tenants where tenant_id='synthetic-a'").fetchone()[0]
    assert after>before
    with pool.connection() as c,c.transaction():
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('select authz.nexloop_reserve_local_artifact(%s,%s,%s,%s,%s,%s)',
                (repo.session.token_digest,'real',permit,canonical_payload(p),secrets.token_hex(16),1))


def expired_artifact(repo,store):
    p=params();p['retention_until']=(datetime.now(UTC)-timedelta(days=1)).isoformat()
    claim=repo.reserve(p)
    store.put(tenant_id=claim.reference.tenant_id,world='real',artifact_id=claim.reference.artifact_id,payload=b'hello',media_type='text/plain')
    return repo.finalize(claim)


def test_cleanup_discovers_bounded_pages_and_preserves_future_or_live_uploads(artifact_setup):
    _,repo,store,_,_=artifact_setup
    refs=[expired_artifact(repo,store) for _ in range(3)]
    future=LocalArtifactService(repo,store).put(request_id='synthetic-future-cleanup-001',payload=b'hello',
        media_type='text/plain',retention_until=datetime.now(UTC)+timedelta(days=1))
    p=params();p['retention_until']=(datetime.now(UTC)-timedelta(days=1)).isoformat()
    pending=repo.reserve(p,lease_seconds=30)
    expected=sorted(r.artifact_id for r in refs)
    assert repo.cleanup_candidates(limit=2)==tuple(expected[:2])
    assert repo.cleanup_candidates(limit=2,after=expected[1])==tuple(expected[2:])
    service=LocalArtifactService(repo,store)
    first=service.collect_expired(limit=2)
    assert first=={'deleted':expected[:2],'next_after':expected[1],'exhausted':False}
    second=service.collect_expired(limit=2,after=first['next_after'])
    assert second=={'deleted':expected[2:],'next_after':expected[2],'exhausted':True}
    assert repo.cleanup_candidates()==()
    assert service.read(future.artifact_id)==b'hello'
    for ref in refs:
        with pytest.raises(FileNotFoundError):store.read(ref)
    with pytest.raises(psycopg.errors.SerializationFailure):repo.claim_deletion(pending.reference.artifact_id)


def test_cleanup_candidates_are_tenant_and_principal_bound(artifact_setup,admin):
    pool,repo,store,token,signer=artifact_setup
    owned=expired_artifact(repo,store)
    other_token,_=seed_authority(admin,'synthetic-a','eios:artifact:local_real',
        operations=(Operation.CREATE,Operation.READ,Operation.DELETE),identity_suffix='-other')
    other=PostgresArtifactRepository(pool,authenticate_service(pool,other_token,world='real'),signer)
    other_ref=expired_artifact(other,store)
    tenant_token,_=seed_authority(admin,'synthetic-b','eios:artifact:local_real',
        operations=(Operation.CREATE,Operation.READ,Operation.DELETE))
    tenant=PostgresArtifactRepository(pool,authenticate_service(pool,tenant_token,world='real'),signer)
    tenant_ref=expired_artifact(tenant,store)
    refreshed=PostgresArtifactRepository(pool,authenticate_service(pool,token,world='real'),signer)
    assert refreshed.cleanup_candidates()==(owned.artifact_id,)
    other=PostgresArtifactRepository(pool,authenticate_service(pool,other_token,world='real'),signer)
    assert other.cleanup_candidates()==(other_ref.artifact_id,)
    assert tenant.cleanup_candidates()==(tenant_ref.artifact_id,)


def test_cleanup_discovery_does_not_grant_delete_authority(artifact_setup,admin):
    import json
    pool,repo,store,token,signer=artifact_setup
    ref=expired_artifact(repo,store)
    key=['synthetic-a-principal','eios:artifact:local_real']
    payload=admin.execute("select payload from authz.nexloop_authority_facts where tenant_id='synthetic-a' and fact_kind='grants' and entity_key=%s",(key,)).fetchone()[0]
    facts=F.GrantFacts.model_validate_json(json.dumps(payload))
    readonly=facts.grants[0].model_copy(update={'operations':frozenset({Operation.READ})})
    replace_fact(admin,'synthetic-a','grants',key,F.GrantFacts,grants=[readonly.model_dump(mode='json')])
    repo=PostgresArtifactRepository(pool,authenticate_service(pool,token,world='real'),signer)
    assert repo.cleanup_candidates()==(ref.artifact_id,)
    with pytest.raises(ArtifactAccessDenied):LocalArtifactService(repo,store).collect_expired()
    assert store.read(ref)==b'hello'
    assert admin.execute('select status from runtime.nexloop_local_artifacts').fetchone()[0]=='available'


@pytest.mark.parametrize('arguments',[{'limit':True},{'limit':101},{'limit':0},{'after':'../escape'}])
def test_invalid_cleanup_cursor_rejected_before_permit(artifact_setup,admin,arguments):
    _,repo,_,_,_=artifact_setup
    with pytest.raises(ValueError):repo.cleanup_candidates(**arguments)
    assert admin.execute('select count(*) from authz.nexloop_artifact_permits').fetchone()[0]==0


def test_expired_completed_upload_deletes_and_tombstones(artifact_setup):
    _,repo,store,_,_=artifact_setup;ref=expired_artifact(repo,store);service=LocalArtifactService(repo,store)
    service.delete_expired(ref.artifact_id)
    with pytest.raises(FileNotFoundError):store.read(ref)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):repo.get(ref.artifact_id)
    service.delete_expired(ref.artifact_id)


def test_future_retention_blocks_deletion_without_touching_bytes(artifact_setup):
    _,repo,store,_,_=artifact_setup;service=LocalArtifactService(repo,store)
    ref=service.put(request_id='synthetic-retained-001',payload=b'hello',media_type='text/plain',retention_until=datetime.now(UTC)+timedelta(days=1))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):service.delete_expired(ref.artifact_id)
    assert service.read(ref.artifact_id)==b'hello'


def test_revocation_between_delete_claim_and_unlink_protects_file(artifact_setup,admin):
    _,repo,store,_,_=artifact_setup;ref=expired_artifact(repo,store);claim=repo.claim_deletion(ref.artifact_id)
    replace_fact(admin,'synthetic-a','grants',['synthetic-a-principal','eios:artifact:local_real'],F.GrantFacts,grants=[])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with repo.deletion_transaction(claim) as bound:store.remove(bound)
    assert store.read(ref)==b'hello'


def test_unlink_then_transaction_failure_recovers_after_lease(artifact_setup):
    pool,repo,store,_,_=artifact_setup;ref=expired_artifact(repo,store);claim=repo.claim_deletion(ref.artifact_id,lease_seconds=1)
    with pytest.raises(RuntimeError):
        with repo.deletion_transaction(claim) as bound:
            store.remove(bound)
            raise RuntimeError('synthetic failure after fsync')
    with pytest.raises(FileNotFoundError):store.read(ref)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):repo.get(ref.artifact_id)
    with pool.connection() as c:c.execute('select pg_sleep(1.1)')
    new=repo.claim_deletion(ref.artifact_id,lease_seconds=5)
    assert new.fence==claim.fence+1
    with pytest.raises(psycopg.errors.SerializationFailure):
        with repo.deletion_transaction(claim):pass
    with repo.deletion_transaction(new) as bound:store.remove(bound)
    LocalArtifactService(repo,store).delete_expired(ref.artifact_id)


def test_unfinished_upload_cannot_be_collected(artifact_setup):
    _,repo,store,_,_=artifact_setup;p=params();p['retention_until']=(datetime.now(UTC)-timedelta(days=1)).isoformat()
    claim=repo.reserve(p)
    store.put(tenant_id=claim.reference.tenant_id,world='real',artifact_id=claim.reference.artifact_id,payload=b'hello',media_type='text/plain')
    with pytest.raises(psycopg.errors.SerializationFailure):repo.claim_deletion(claim.reference.artifact_id)
    assert store.read(claim.reference)==b'hello'


def test_deletion_transaction_blocks_runtime_reference_writers(artifact_setup,pg):
    _,repo,store,_,_=artifact_setup;ref=expired_artifact(repo,store);claim=repo.claim_deletion(ref.artifact_id)
    with repo.deletion_transaction(claim) as bound:
        # Probe the actual INSERT/UPDATE table-lock mode without writing business data.
        with psycopg.connect(pg) as writer:
            writer.execute("set local lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                writer.execute('lock table runtime.invocations in row exclusive mode')
            writer.rollback()
        store.remove(bound)
