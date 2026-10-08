from contextlib import contextmanager
from datetime import UTC,datetime,timedelta
import os
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.local_artifacts import ArtifactAccessDenied
from nexloop_eios.postgres_artifacts import PostgresArtifactRepository,LocalArtifactService
from authority_fixture import seed_authority,replace_fact
from eios.authz import facts as F
from test_postgres_artifacts import artifact_setup


def leftovers(store,tenant='synthetic-a',world='real'):
    ref=store.put(tenant_id=tenant,world=world,payload=b'final-protected',media_type='text/plain')
    directory=store.root/ref.namespace
    names=['.tmp-'+format(i,'032x') for i in range(1,6)]
    old=(datetime.now(UTC)-timedelta(hours=1)).timestamp()
    for name in names:
        p=directory/name;p.write_bytes(b'interrupted');os.utime(p,(old,old))
    recent=directory/('.tmp-'+'9'*32);recent.write_bytes(b'active')
    symlink=directory/('.tmp-'+'8'*32);symlink.symlink_to(ref.artifact_id)
    return ref,names


def worker(pool,repo,token,signer):
    return PostgresArtifactRepository(pool,authenticate_service(pool,token,world='real'),signer)


def test_bounded_real_pg_cleanup_preserves_final_recent_and_symlink(artifact_setup,pg):
    _,repo,store,token,signer=artifact_setup
    ref,names=leftovers(store)
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as pool:
        service=LocalArtifactService(worker(pool,repo,token,signer),store)
        cutoff=datetime.now(UTC)-timedelta(minutes=5)
        first=service.collect_temporary_files(older_than=cutoff,limit=2)
        assert first=={'removed':2,'examined':2,'next_after':names[1],'exhausted':False}
        second=service.collect_temporary_files(older_than=cutoff,limit=2,after=first['next_after'])
        assert second['removed']==2
        third=service.collect_temporary_files(older_than=cutoff,limit=2,after=second['next_after'])
        assert third['removed']==1 and not third['exhausted']
        last=service.collect_temporary_files(older_than=cutoff,limit=2,after=third['next_after'])
        assert last['removed']==0 and last['exhausted']
        assert service.collect_temporary_files(older_than=cutoff,limit=50)['removed']==0
    assert store.read(ref)==b'final-protected'
    assert (store.root/ref.namespace/('.tmp-'+'9'*32)).read_bytes()==b'active'
    assert (store.root/ref.namespace/('.tmp-'+'8'*32)).is_symlink()


def test_api_role_cannot_invoke_maintenance_cleanup(artifact_setup):
    _,repo,store,_,_=artifact_setup;ref,names=leftovers(store)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        LocalArtifactService(repo,store).collect_temporary_files(older_than=datetime.now(UTC)-timedelta(minutes=5))
    assert all((store.root/ref.namespace/name).exists() for name in names)


def test_read_only_identity_denied_even_on_worker_role(artifact_setup,pg,admin):
    _,repo,store,_,signer=artifact_setup;ref,names=leftovers(store)
    token,_=seed_authority(admin,'synthetic-a','eios:artifact:local_real',operation=Operation.READ,identity_suffix='-temp-reader')
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as pool:
        with pytest.raises((ArtifactAccessDenied,AuthorizationUnavailable)):
            LocalArtifactService(worker(pool,repo,token,signer),store).collect_temporary_files(older_than=datetime.now(UTC)-timedelta(minutes=5))
    assert all((store.root/ref.namespace/name).exists() for name in names)


def test_revoke_after_signed_permit_before_guard_denies_cleanup(artifact_setup,pg,admin,monkeypatch):
    _,repo,store,token,signer=artifact_setup;ref,names=leftovers(store)
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as pool:
        repository=worker(pool,repo,token,signer);original=repository._transaction;count=0
        @contextmanager
        def race():
            nonlocal count
            count+=1
            if count==2:
                replace_fact(admin,'synthetic-a','grants',['synthetic-a-principal','eios:artifact:local_real'],F.GrantFacts,grants=[])
            with original() as c:yield c
        monkeypatch.setattr(repository,'_transaction',race)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            LocalArtifactService(repository,store).collect_temporary_files(older_than=datetime.now(UTC)-timedelta(minutes=5))
    assert count==2 and all((store.root/ref.namespace/name).exists() for name in names)


def test_other_tenant_world_namespace_is_untouched(artifact_setup,pg):
    _,repo,store,token,signer=artifact_setup
    own,names=leftovers(store)
    other,_=leftovers(store,tenant='synthetic-b');test,_=leftovers(store,world='test')
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as pool:
        result=LocalArtifactService(worker(pool,repo,token,signer),store).collect_temporary_files(older_than=datetime.now(UTC)-timedelta(minutes=5))
    assert result['removed']==5
    for ref in [other,test]:assert all((store.root/ref.namespace/name).exists() for name in names)


def test_invalid_page_has_zero_permit_or_file_effects(artifact_setup,admin):
    _,repo,store,_,_=artifact_setup;ref,names=leftovers(store)
    count=admin.execute('select count(*) from authz.nexloop_artifact_permits').fetchone()[0]
    for args in [dict(older_than=datetime.now(UTC)),dict(older_than=datetime.now(UTC)-timedelta(minutes=5),after='../escape'),dict(older_than=datetime.now(UTC)-timedelta(minutes=5),limit=101)]:
        with pytest.raises(ValueError):LocalArtifactService(repo,store).collect_temporary_files(**args)
    assert admin.execute('select count(*) from authz.nexloop_artifact_permits').fetchone()[0]==count
    assert all((store.root/ref.namespace/name).exists() for name in names)


def test_partial_filesystem_failure_still_syncs_and_can_retry(artifact_setup,monkeypatch):
    # Physical primitive test only, separate from the real PG authority cases.
    import nexloop_eios.local_artifacts as files
    _,_,store,_,_=artifact_setup;ref,names=leftovers(store)
    original=files._sync;syncs=[];checks=0
    def sync(fd):syncs.append(fd);original(fd)
    def guard():
        nonlocal checks
        checks+=1
        if checks==3:raise ArtifactAccessDenied('synthetic permit expiry')
    monkeypatch.setattr(files,'_sync',sync)
    with pytest.raises(ArtifactAccessDenied):
        store.collect_temporary_page(ref,older_than=datetime.now(UTC)-timedelta(minutes=5),limit=5,after='',guard=guard)
    assert len(syncs)==1
    assert not (store.root/ref.namespace/names[0]).exists()
    assert all((store.root/ref.namespace/name).exists() for name in names[1:])
    result=store.collect_temporary_page(ref,older_than=datetime.now(UTC)-timedelta(minutes=5),limit=5,after='',guard=lambda:None)
    assert result['removed']==4 and store.read(ref)==b'final-protected'
