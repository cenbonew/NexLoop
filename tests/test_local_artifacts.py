from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from eios.authz.facts import AuthorizationFactQuery, AuthorizationTarget, CredentialAuthenticationBinding
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from eios.authz.errors import AuthorizationUnavailable
from eios.identity.models import FrozenJsonMap, SubjectKind
from nexloop_eios.local_artifacts import (
    LocalBlobStore, AuthorizedArtifactAccess, ArtifactAccessDenied,
    ArtifactIntegrityError, BlobReference,
)

@pytest.fixture
def store(tmp_path):
    with LocalBlobStore(tmp_path/'blobs') as blobs:
        yield blobs


def query(ref, *, tenant=None, world=None, operation=Operation.READ):
    tenant = tenant or ref.tenant_id
    return AuthorizationFactQuery(
        tenant_id=tenant,
        authentication=CredentialAuthenticationBinding(
            tenant_id=tenant, credential_tenant_id=tenant, credential_id='synthetic-credential',
            credential_revision=1, credential_epoch=1, subject_id='synthetic-service',
            subject_principal_id='synthetic-service',subject_kind=SubjectKind.SERVICE,
            subject_revision=1,membership_revision=1,credential_kind='api_key',
            caller_application_id='eios:application:synthetic',caller_application_version='1',
            caller_application_digest='a'*64,requested_scopes=frozenset({'artifact.read'}),
        ),
        agent_invocation=None,
        target=AuthorizationTarget(tenant_id=tenant, resource_type=ResourceType.ARTIFACT,
                                   resource_id=ref.resource_id, operation=operation),
        request_attributes=FrozenJsonMap({'world': world or ref.world}),
        request_id='synthetic-request', trace_id='synthetic-trace',
    )


class DecisionProbe:
    # Unit double only; no production resolver/authentication claim.
    def __init__(self, **kw):
        self.kw = kw
        self.calls = 0
    def decide(self, q):
        self.calls += 1
        fields=dict(authoritative=True,allowed=True,obligations=(),
                    expires_at=datetime.now(UTC)+timedelta(seconds=2))
        fields.update(self.kw)
        return SimpleNamespace(**fields)


def test_roundtrip_reopen_and_tenant_world_separation(store):
    refs=[store.put(tenant_id=t,world=w,payload=b'synthetic evidence',media_type='text/plain')
          for t,w in [('t1','real'),('t2','real'),('t1','shadow')]]
    assert len({r.resource_id for r in refs})==3
    assert all(store.read(r)==b'synthetic evidence' for r in refs)
    with LocalBlobStore(store.root) as reopened:
        assert reopened.read(refs[0])==b'synthetic evidence'
    assert refs[0].encryption=='none'


def test_immutable_retry_and_corruption_fail_closed(store):
    ref=store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    assert store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain',artifact_id=ref.artifact_id)==ref
    (store.root/ref.namespace/ref.artifact_id).write_bytes(b'XXXXX')
    with pytest.raises(ArtifactIntegrityError): store.read(ref)
    with pytest.raises(ArtifactIntegrityError):
        store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain',artifact_id=ref.artifact_id)
    assert not list((store.root/ref.namespace).glob('.tmp-*'))


@pytest.mark.parametrize('kind',['root','namespace','blob'])
def test_symlink_escape_rejected(store,tmp_path,kind):
    ref=store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    outside=tmp_path/'outside'; outside.write_bytes(b'secret')
    if kind=='root':
        link=tmp_path/'root-link';link.symlink_to(store.root,target_is_directory=True)
        with pytest.raises(ValueError): LocalBlobStore(link)
    elif kind=='namespace':
        other=replace(ref,world='shadow')
        (store.root/other.namespace).symlink_to(tmp_path,target_is_directory=True)
        with pytest.raises(OSError): store.read(other)
    else:
        path=store.root/ref.namespace/ref.artifact_id
        path.unlink();path.symlink_to(outside)
        with pytest.raises(OSError):store.read(ref)
    assert outside.read_bytes()==b'secret'


def test_file_and_directory_sync_precede_receipt(store,monkeypatch):
    from nexloop_eios import local_artifacts
    synced=[]
    real_sync=local_artifacts._sync
    def sync(fd):
        synced.append(os.fstat(fd).st_mode)
        real_sync(fd)
    monkeypatch.setattr(local_artifacts,'_sync',sync)
    ref=store.put(tenant_id='t1',world='real',payload=b'x',media_type='text/plain')
    import stat
    assert stat.S_ISDIR(synced[0]) and stat.S_ISREG(synced[1]) and stat.S_ISDIR(synced[2])
    assert store.read(ref)==b'x'


def test_sync_failure_returns_no_receipt_and_preserves_original(store,monkeypatch):
    from nexloop_eios import local_artifacts
    ref=store.put(tenant_id='t1',world='real',payload=b'x',media_type='text/plain')
    monkeypatch.setattr(local_artifacts,'_sync',lambda fd: (_ for _ in ()).throw(OSError('synthetic sync failure')))
    with pytest.raises(OSError):store.put(tenant_id='t1',world='real',payload=b'x',media_type='text/plain',artifact_id=ref.artifact_id)
    assert store.read(ref)==b'x'
    assert not list((store.root/ref.namespace).glob('.tmp-*'))


def test_process_crash_orphan_cleanup_never_deletes_final_blobs(store):
    ref=store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    script='''
import os,sys
from pathlib import Path
from nexloop_eios.local_artifacts import LocalBlobStore
with LocalBlobStore(Path(sys.argv[1])) as s:
 r=s.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain',artifact_id=sys.argv[2])
 with s._namespace(r) as d:
  f=os.open('.tmp-'+'a'*32,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=d)
  os.write(f,b'partial');os.fsync(f)
  os._exit(23)
'''
    process=subprocess.run([sys.executable,'-c',script,str(store.root),ref.artifact_id])
    assert process.returncode==23
    cutoff=datetime.now(UTC)+timedelta(seconds=-0.001)
    os.utime(store.root/ref.namespace/('.tmp-'+'a'*32),(cutoff.timestamp()-2,cutoff.timestamp()-2))
    assert store.collect_abandoned_temporary_files(ref,older_than=cutoff)==1
    assert store.read(ref)==b'hello'
    assert store.collect_abandoned_temporary_files(ref,older_than=cutoff)==0


def test_multiprocess_same_content_creation(store):
    script='''
import sys
from pathlib import Path
from nexloop_eios.local_artifacts import LocalBlobStore
with LocalBlobStore(Path(sys.argv[1])) as s:
 r=s.put(tenant_id='t1',world='real',payload=b'concurrent',media_type='text/plain',artifact_id='b'*32)
 assert s.read(r)==b'concurrent'
'''
    processes=[subprocess.Popen([sys.executable,'-c',script,str(store.root)]) for _ in range(3)]
    assert [p.wait(timeout=15) for p in processes]==[0,0,0]
    ref=store.put(tenant_id='t1',world='real',payload=b'concurrent',media_type='text/plain',artifact_id='b'*32)
    assert store.read(ref)==b'concurrent'


def test_binding_checks_precede_authority_and_range_read(store):
    ref=store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    authority=DecisionProbe();reader=AuthorizedArtifactAccess(store,authority)
    for q in [query(ref,tenant='t2'),query(ref,world='shadow'),query(ref,operation=Operation.EXPORT)]:
        with pytest.raises(ArtifactAccessDenied):reader.read(q,ref)
    assert authority.calls==0
    assert reader.read(query(ref),ref,start=1,stop=4)==b'ell'
    with pytest.raises(ValueError):reader.read(query(ref),ref,start=-1)
    with pytest.raises(ValueError):reader.read(query(ref),ref,stop=99)


def test_missing_live_resolver_cannot_fall_back_to_allow(store):
    ref=store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    with pytest.raises(AuthorizationUnavailable):
        AuthorizedArtifactAccess(store,AuthorizationDecisionService()).read(query(ref),ref)


def test_closed_and_nonprivate_configuration_rejected(store,tmp_path):
    root=tmp_path/'public';root.mkdir(mode=0o755)
    with pytest.raises(PermissionError):LocalBlobStore(root)
    store.close()
    with pytest.raises(RuntimeError):store.put(tenant_id='t1',world='real',payload=b'x',media_type='text/plain')


@pytest.mark.parametrize('tenant,world',[('../escape','real'),('t1','../shadow'),('a\0b','c')])
def test_namespace_identifiers_are_not_paths(store,tenant,world):
    with pytest.raises(ValueError):store.put(tenant_id=tenant,world=world,payload=b'x',media_type='text/plain')


@pytest.mark.parametrize('decision',[{'allowed':False},{'authoritative':False},{'obligations':('approval',)},{'expires_at':datetime(2020,1,1,tzinfo=UTC)}])
def test_denial_untrusted_obligations_and_expiry_fail_closed(store,decision):
    ref=store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    reader=AuthorizedArtifactAccess(store,DecisionProbe(**decision))
    with pytest.raises(ArtifactAccessDenied):reader.read(query(ref),ref)


@pytest.mark.parametrize('change',[{'tenant_id':'t2'},{'artifact_id':'wrong'},{'invocation_status':'running'},{'job_status':'unknown'},{'retention_until':datetime(2100,1,1,tzinfo=UTC)}])
def test_retention_requires_exact_terminal_database_proof(store,change):
    from eios.runtime.artifacts import ArtifactRetentionTerminalProof
    ref=store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    data=dict(tenant_id='t1',artifact_id=ref.artifact_id,database_time=datetime.now(UTC),
              retention_until=datetime(2020,1,1,tzinfo=UTC),origin_invocation_id='synthetic-invocation',
              invocation_status='succeeded',origin_job_id='synthetic-job',job_status='succeeded')
    data.update(change)
    access=AuthorizedArtifactAccess(store,DecisionProbe())
    with pytest.raises(ArtifactAccessDenied):
        access.delete_expired(query(ref,operation=Operation.DELETE),ref,ArtifactRetentionTerminalProof(**data))
    assert store.read(ref)==b'hello'


def test_authorized_expired_delete_is_idempotent(store):
    from eios.runtime.artifacts import ArtifactRetentionTerminalProof
    ref=store.put(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    proof=ArtifactRetentionTerminalProof(tenant_id='t1',artifact_id=ref.artifact_id,database_time=datetime.now(UTC),
              retention_until=datetime(2020,1,1,tzinfo=UTC),origin_invocation_id='synthetic-invocation',
              invocation_status='succeeded',origin_job_id='synthetic-job',job_status='succeeded')
    access=AuthorizedArtifactAccess(store,DecisionProbe())
    access.delete_expired(query(ref,operation=Operation.DELETE),ref,proof)
    access.delete_expired(query(ref,operation=Operation.DELETE),ref,proof)
    with pytest.raises(FileNotFoundError):store.read(ref)


def test_identical_payloads_have_independent_retention_identity(store):
    a=store.put(tenant_id='t1',world='real',payload=b'identical',media_type='text/plain')
    b=store.put(tenant_id='t1',world='real',payload=b'identical',media_type='text/plain')
    assert a.sha256==b.sha256 and a.artifact_id!=b.artifact_id and a.resource_id!=b.resource_id
    store.remove(a)
    assert store.read(b)==b'identical'


def test_same_artifact_identity_cannot_overwrite_different_payload(store):
    ref=store.put(tenant_id='t1',world='real',payload=b'first',media_type='text/plain')
    with pytest.raises(ArtifactIntegrityError):
        store.put(tenant_id='t1',world='real',payload=b'other',media_type='text/plain',artifact_id=ref.artifact_id)
    assert store.read(ref)==b'first'


@pytest.mark.parametrize('fields',[{'artifact_id':''},{'artifact_id':'../outside'},{'media_type':'text/plain\r\nX-Forged: yes'}])
def test_invalid_identity_and_header_metadata_rejected(store,fields):
    parameters=dict(tenant_id='t1',world='real',payload=b'hello',media_type='text/plain')
    parameters.update(fields)
    with pytest.raises(ValueError):store.put(**parameters)
