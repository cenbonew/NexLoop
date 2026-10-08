from contextlib import contextmanager
from datetime import UTC,datetime,timedelta
import os
import secrets
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.artifact_orphans import FinalOrphanCollector
from nexloop_eios.local_artifacts import ArtifactAccessDenied
from nexloop_eios.postgres_artifacts import LocalArtifactService,canonical_payload
from authority_fixture import seed_authority,replace_fact
from test_postgres_artifacts import artifact_setup


def orphan_file(store,*,tenant='synthetic-a',world='real',payload=b'orphan'):
    ref=store.put(tenant_id=tenant,world=world,payload=payload,media_type='text/plain')
    path=store.root/ref.namespace/ref.artifact_id
    old=(datetime.now(UTC)-timedelta(hours=1)).timestamp();os.utime(path,(old,old))
    return ref,path


@pytest.fixture
def orphan_collector(artifact_setup,pg,admin):
    _,repo,store,_,signer=artifact_setup
    # Formal metadata/file is created only through the actual restricted Artifact service.
    formal=LocalArtifactService(repo,store).put(request_id='synthetic-orphan-protected-001',payload=b'formal',media_type='text/plain',retention_until=datetime.now(UTC)+timedelta(days=1))
    path=store.root/formal.namespace/formal.artifact_id
    old=(datetime.now(UTC)-timedelta(hours=1)).timestamp();os.utime(path,(old,old))
    token,_=seed_authority(admin,'synthetic-a','eios:artifact:orphans_real',operation=Operation.DELETE,identity_suffix='-orphan-maintenance')
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as pool:
        collector=FinalOrphanCollector(pool,authenticate_service(pool,token,world='real'),signer,store)
        yield collector,token,formal


def cutoff():return datetime.now(UTC)-timedelta(minutes=5)


def persist_plan(collector):
    page=collector.store.orphan_candidate_page(collector._namespace(),older_than=cutoff(),limit=50,after='')
    plan={'sweep_id':secrets.token_hex(16),'older_than':cutoff().isoformat(),**page}
    collector._command('plan',plan)
    return plan


def test_final_orphans_cleanup_preserves_registered_and_other_namespaces(orphan_collector,admin):
    collector,_,formal=orphan_collector
    orphan,path=orphan_file(collector.store)
    other,other_path=orphan_file(collector.store,tenant='synthetic-b')
    test,test_path=orphan_file(collector.store,world='test')
    result=collector.collect(older_than=cutoff(),limit=50)
    dispositions={r['artifact_id']:r['disposition'] for r in result['outcomes']}
    assert dispositions=={formal.artifact_id:'protected',orphan.artifact_id:'removed'}
    assert not path.exists() and other_path.exists() and test_path.exists()
    assert collector.store.read(formal)==b'formal'
    assert collector.resume(result['sweep_id'])==result
    row=admin.execute('select status,outcomes from runtime.nexloop_orphan_sweeps').fetchone()
    assert row==('finished',result['outcomes'])
    with collector.pool.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select * from runtime.nexloop_orphan_sweeps')


def test_durable_plan_and_missing_file_resume(orphan_collector,admin):
    collector,_,_=orphan_collector;orphan,path=orphan_file(collector.store)
    plan=persist_plan(collector)
    assert admin.execute('select status from runtime.nexloop_orphan_sweeps').fetchone()[0]=='planned'
    assert path.exists()
    # Models already-fsynced unlink with missing result; live service must reconcile absence.
    collector.store.remove(orphan)
    result=collector.resume(plan['sweep_id'])
    assert next(r['disposition'] for r in result['outcomes'] if r['artifact_id']==orphan.artifact_id)=='absent'
    assert admin.execute('select status from runtime.nexloop_orphan_sweeps').fetchone()[0]=='finished'


def test_changed_file_is_preserved(orphan_collector):
    collector,_,_=orphan_collector;orphan,path=orphan_file(collector.store)
    plan=persist_plan(collector)
    path.write_bytes(b'changed-after-plan')
    result=collector.resume(plan['sweep_id'])
    assert next(r['disposition'] for r in result['outcomes'] if r['artifact_id']==orphan.artifact_id)=='changed'
    assert path.read_bytes()==b'changed-after-plan'


def test_new_metadata_reservation_after_plan_protects_file(orphan_collector,admin):
    collector,_,_=orphan_collector;orphan,path=orphan_file(collector.store)
    plan=persist_plan(collector)
    # Configure distinct normal uploader; business infrastructure reservation still uses its permit chain.
    from nexloop_eios.postgres_artifacts import PostgresArtifactRepository
    token,_=seed_authority(admin,'synthetic-a','eios:artifact:local_real',operation=Operation.CREATE,identity_suffix='-orphan-uploader')
    repo=PostgresArtifactRepository(collector.pool,authenticate_service(collector.pool,token,world='real'),collector.signer)
    repo.reserve(dict(artifact_id=orphan.artifact_id,sha256=orphan.sha256,size_bytes=orphan.size_bytes,media_type=orphan.media_type,retention_until=(datetime.now(UTC)+timedelta(days=1)).isoformat()))
    # Refresh maintenance identity after authority directory changes, without broadening it.
    # Fixture's token is retrieved explicitly from its private return, never from logs.
    return_token=orphan_collector[1]
    collector.session=authenticate_service(collector.pool,return_token,world='real')
    result=collector.resume(plan['sweep_id'])
    assert next(r['disposition'] for r in result['outcomes'] if r['artifact_id']==orphan.artifact_id)=='protected'
    assert path.exists()


def test_normal_artifact_delete_cannot_clean_final_orphans(artifact_setup,pg):
    _,repo,store,token,signer=artifact_setup;_,path=orphan_file(store)
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as pool:
        collector=FinalOrphanCollector(pool,authenticate_service(pool,token,world='real'),signer,store)
        with pytest.raises((AuthorizationUnavailable,ArtifactAccessDenied)):collector.collect(older_than=cutoff())
    assert path.exists()


def test_api_role_cannot_scan_or_delete_even_with_orphan_grant(orphan_collector,pg):
    collector,token,_=orphan_collector;_,path=orphan_file(collector.store)
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        denied=FinalOrphanCollector(pool,authenticate_service(pool,token,world='real'),collector.signer,collector.store)
        with pytest.raises(ArtifactAccessDenied):denied.collect(older_than=cutoff())
    assert path.exists()


def test_revoke_between_signed_lock_and_sql_dispatch_preserves_file(orphan_collector,admin,monkeypatch):
    collector,_,_=orphan_collector;_,path=orphan_file(collector.store);plan=persist_plan(collector)
    original=collector._arguments
    def race(verb,payload):
        args=original(verb,payload)
        if verb=='lock':replace_fact(admin,'synthetic-a','grants',['synthetic-a-orphan-maintenance-principal','eios:artifact:orphans_real'],F.GrantFacts,grants=[])
        return args
    monkeypatch.setattr(collector,'_arguments',race)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):collector.resume(plan['sweep_id'])
    assert path.exists() and admin.execute('select status from runtime.nexloop_orphan_sweeps').fetchone()[0]=='planned'


def test_sigkill_after_unlink_fsync_recovers_durable_sweep(orphan_collector,pg,tmp_path):
    import subprocess
    import sys
    import signal
    collector,token,_=orphan_collector;orphan,path=orphan_file(collector.store)
    plan=persist_plan(collector)
    config=tmp_path/'owned-orphan-crash';config.mkdir(mode=0o700)
    for name,payload in [('dsn',make_conninfo(pg,user='nexloop_domain_worker').encode()),('token',token.encode()),('key',collector.signer.material)]:
        f=config/name;f.write_bytes(payload);f.chmod(0o600)
    script='''
import os,signal,sys
from pathlib import Path
from nexloop_eios.backend import open_backend
from nexloop_eios.private_configuration import read_private_text
config=Path(sys.argv[1])
with open_backend(database_url=read_private_text(config/'dsn',maximum=8192),artifact_root=sys.argv[2],signing_key_file=config/'key',signing_key_id=sys.argv[4]) as backend:
    original=backend._store.apply_orphan_plan
    def crash(*args,**kwargs):
        outcomes=original(*args,**kwargs)
        os.kill(os.getpid(),signal.SIGKILL)
        return outcomes
    backend._store.apply_orphan_plan=crash
    backend.authenticate(read_private_text(config/'token',maximum=512),world='real').resume_final_artifact_orphan_sweep(sys.argv[3])
'''
    child=subprocess.run([sys.executable,'-I','-c',script,str(config),str(collector.store.root),plan['sweep_id'],collector.signer.key_id],capture_output=True,text=True,timeout=30)
    assert child.returncode==-signal.SIGKILL,'owned cleanup subprocess did not reach the physical crash boundary'
    assert token not in child.stdout+child.stderr
    assert not path.exists()
    result=collector.resume(plan['sweep_id'])
    assert next(r['disposition'] for r in result['outcomes'] if r['artifact_id']==orphan.artifact_id)=='absent'


def test_page_input_invalid_before_any_plan_or_file_effect(orphan_collector,admin):
    collector,_,_=orphan_collector;_,path=orphan_file(collector.store)
    for arguments in [dict(older_than=datetime.now(UTC)),dict(older_than=cutoff(),limit=51),dict(older_than=cutoff(),after='../escape')]:
        with pytest.raises(ValueError):collector.collect(**arguments)
    assert admin.execute('select count(*) from runtime.nexloop_orphan_sweeps').fetchone()[0]==0
    assert path.exists()
