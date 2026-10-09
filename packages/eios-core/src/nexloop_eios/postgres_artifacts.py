"""Artifact metadata/permit adapter; all mutations go through narrow definers."""
from contextlib import contextmanager
from dataclasses import dataclass,field
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import stat

from eios.authz import facts as F
from eios.authz.service import AuthorizationDecisionService
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.local_artifacts import BlobReference,ArtifactAccessDenied,ArtifactIntegrityError


def canonical_payload(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


@dataclass(frozen=True)
class AuthoritySigner:
    key_id: str
    material: bytes=field(repr=False)

    def __post_init__(self):
        if type(self.material) is not bytes or len(self.material)!=32:
            raise ValueError('authority key must have 32 immutable bytes')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',self.key_id):
            raise ValueError('invalid authority signer ID')

    @classmethod
    def from_file(cls,path: Path,*,key_id='active'):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.geteuid():
                raise PermissionError('authority key must be private and service-owned')
            material=stream.read(33)
        return cls(key_id,material)

    def signature(self,text):
        return hmac.new(self.material,('nexloop-artifact-permit-v1:'+text).encode(),'sha256').hexdigest()


@dataclass(frozen=True)
class ArtifactUploadClaim:
    reference: BlobReference
    worker: str
    token: str=field(repr=False)
    fence: int
    available: bool=False


def reference(row):
    return BlobReference(row['tenant_id'],row['world'],row['artifact_id'],row['sha256'],
                         row['size_bytes'],row['media_type'],row['encryption'])


class PostgresArtifactRepository:
    def __init__(self,pool,session,signer: AuthoritySigner):
        self.pool,self.session,self.signer=pool,session,signer
        with self.pool.connection() as connection:
            verify_application_role(connection)

    @contextmanager
    def _transaction(self):
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            yield c

    def issue_permit(self,operation,parameters):
        entries=[]
        query=self.session.query(resource_id=f'eios:artifact:local_{self.session.world}',
                resource_type=ResourceType.ARTIFACT,operation=operation)
        resolved=F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query)
        decision=AuthorizationDecisionService().decide_resolved(resolved)
        if not decision.allowed or not decision.authoritative or decision.obligations:
            raise ArtifactAccessDenied('artifact authorization denied')
        expiry=min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25))
        claims={'protocol':'nexloop-artifact-permit-v1','key_id':self.signer.key_id,
                'permit_id':decision.decision_id,'tenant_id':query.tenant_id,
                'principal_id':query.authentication.subject_principal_id,
                'credential_id':query.authentication.credential_id,'world':self.session.world,
                'directory_hash':self.session.directory_hash,'resource_id':query.target.resource_id,
                'operation':operation.value,'expires_at':expiry.isoformat(),
                'parameters_digest':hashlib.sha256(canonical_payload(parameters).encode()).hexdigest(),
                'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}
        text=canonical_payload(claims)
        with self._transaction() as c:
            row=c.execute('select authz.nexloop_issue_artifact_permit(%s,%s,%s,%s)',
                    (self.session.token_digest,self.session.world,text,self.signer.signature(text))).fetchone()
            return row[0]

    def reserve(self,parameters,*,worker=None,lease_seconds=20):
        permit=self.issue_permit(Operation.CREATE,parameters)
        worker=worker or secrets.token_hex(16)
        with self._transaction() as c:
            row=c.execute('select authz.nexloop_reserve_local_artifact(%s,%s,%s,%s,%s,%s)',
                    (self.session.token_digest,self.session.world,permit,canonical_payload(parameters),worker,lease_seconds)).fetchone()[0]
        return ArtifactUploadClaim(reference(row),worker,row.get('upload_token',''),row['upload_fence'],row['status']=='available')

    def finalize(self,claim):
        with self._transaction() as c:
            row=c.execute('select authz.nexloop_finalize_local_artifact(%s,%s,%s,%s,%s,%s)',
                    (self.session.token_digest,self.session.world,claim.reference.artifact_id,claim.worker,claim.token,claim.fence)).fetchone()[0]
        return reference(row)

    @contextmanager
    def upload_transaction(self,claim):
        arguments=(self.session.token_digest,self.session.world,claim.reference.artifact_id,claim.worker,claim.token,claim.fence)
        with self._transaction() as c:
            row=c.execute('select authz.nexloop_lock_artifact_upload(%s,%s,%s,%s,%s,%s)',arguments).fetchone()[0]
            yield reference(row)
            c.execute('select authz.nexloop_finalize_local_artifact(%s,%s,%s,%s,%s,%s)',arguments)

    def claim_deletion(self,artifact_id,*,worker=None,lease_seconds=20):
        params={'artifact_id':artifact_id};permit=self.issue_permit(Operation.DELETE,params)
        worker=worker or secrets.token_hex(16)
        with self._transaction() as c:
            row=c.execute('select authz.nexloop_claim_artifact_deletion(%s,%s,%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,permit,canonical_payload(params),worker,lease_seconds)).fetchone()[0]
        return ArtifactUploadClaim(reference(row),worker,row.get('upload_token',''),row['upload_fence'],row['status']=='deleted')

    @contextmanager
    def deletion_transaction(self,claim):
        arguments=(self.session.token_digest,self.session.world,claim.reference.artifact_id,claim.worker,claim.token,claim.fence)
        with self._transaction() as c:
            row=c.execute('select authz.nexloop_lock_artifact_deletion(%s,%s,%s,%s,%s,%s)',arguments).fetchone()[0]
            yield reference(row)
            c.execute('select authz.nexloop_finish_artifact_deletion(%s,%s,%s,%s,%s,%s)',arguments)

    def get(self,artifact_id):
        params={'artifact_id':artifact_id};permit=self.issue_permit(Operation.READ,params)
        with self._transaction() as c:
            dependency=c.execute('select authz.nexloop_context_artifact_read_dependency_v2(%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,permit,canonical_payload(params))).fetchone()[0]
        if dependency is not None and dependency['kind']=='v6':
            # v6 copy: the reader's own current READ on every source object SQL names (0095).
            from nexloop_eios.service_offerings import _read_envelope
            from types import SimpleNamespace
            holder=SimpleNamespace(_session=self.session,_backend=SimpleNamespace(_pool=self.pool,_signer=self.signer))
            params['context_dependency']={'kind':'v6','run_id':dependency['run_id'],
              'reads':{name:_read_envelope(holder,row['type_name'],row['object_id'],tuple(row['fields'])) for name,row in dependency['reads'].items()}}
        elif dependency is not None and dependency['kind']=='role':
            from nexloop_eios.service_offerings import _read_envelope
            from types import SimpleNamespace
            holder=SimpleNamespace(_session=self.session,_backend=SimpleNamespace(_pool=self.pool,_signer=self.signer))
            params['context_dependency']={'kind':'role','run_id':dependency['run_id'],'event_id':dependency['event_id'],
              'reads':{name:_read_envelope(holder,row['type_name'],row['object_id'],row['fields']) for name,row in dependency['reads'].items()}}
        elif dependency is not None and dependency['kind']=='relationship_context':
            from nexloop_eios.relationship_context import RelationshipContextReader,RelationshipContextRecipe
            reader=RelationshipContextReader(self.pool,self.session,self.signer,RelationshipContextRecipe(tuple(dependency['assessment_ids'])))
            from types import SimpleNamespace
            from nexloop_eios.service_offerings import _read_envelope
            refs=dependency.get('formal_refs')
            if type(refs) is not dict or set(refs)!={'Consumer','Goal','PlanStep','EffectControl'}:raise ArtifactAccessDenied('context formal dependency unavailable')
            # Actual caller's authenticated session; no Source credential lookup.
            holder=SimpleNamespace(_backend=SimpleNamespace(_pool=self.pool,_signer=self.signer),_session=self.session)
            formal_reads={kind:_read_envelope(holder,kind,ref,('allow_effect','budget_units','executor_principal','valid_until') if kind=='EffectControl' else ()) for kind,ref in refs.items()}
            params['context_dependency']={'kind':'relationship_context','message':reader.message_envelope(dependency['message_id']),'relationships':reader.envelopes(),'formal_reads':formal_reads}
        elif dependency is not None:
            params['context_dependency']=context_message_read_envelope(self.pool,self.session,self.signer,dependency['message_id'])
        permit=self.issue_permit(Operation.READ,params)
        with self._transaction() as c:
            row=c.execute('select authz.nexloop_read_local_artifact(%s,%s,%s,%s)',
                    (self.session.token_digest,self.session.world,permit,canonical_payload(params))).fetchone()[0]
        return reference(row)

    def cleanup_candidates(self,*,limit=50,after=''):
        if type(limit) is not int or not 1<=limit<=100 or type(after) is not str or (after and not re.fullmatch(r'[a-f0-9]{32}',after)):
            raise ValueError('invalid artifact cleanup cursor')
        params={'limit':limit,'after':after};permit=self.issue_permit(Operation.READ,params)
        with self._transaction() as c:
            ids=c.execute('select authz.nexloop_artifact_cleanup_candidates(%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,permit,canonical_payload(params))).fetchone()[0]
        if (type(ids) is not list or len(ids)>limit
            or any(type(i) is not str or not re.fullmatch(r'[a-f0-9]{32}',i) or i<=after for i in ids)
            or ids!=sorted(set(ids))):
            raise ArtifactIntegrityError('invalid artifact cleanup page')
        return tuple(ids)

    @contextmanager
    def temporary_cleanup_transaction(self,parameters):
        permit=self.issue_permit(Operation.DELETE,parameters)
        arguments=(self.session.token_digest,self.session.world,permit,canonical_payload(parameters))
        with self._transaction() as c:
            def guard():
                binding=c.execute('select authz.nexloop_lock_temporary_artifact_cleanup(%s,%s,%s,%s)',arguments).fetchone()[0]
                if binding!={'tenant_id':self.session.authentication.tenant_id,'world':self.session.world}:
                    raise ArtifactAccessDenied('temporary cleanup namespace mismatch')
            guard()
            yield guard


class LocalArtifactService:
    def __init__(self,repository: PostgresArtifactRepository,store):
        self.repository,self.store=repository,store

    def put(self,*,request_id,payload,media_type,retention_until):
        if (type(request_id) is not str or not 16<=len(request_id)<=200
                or not isinstance(payload,bytes) or retention_until.tzinfo is None):
            raise ValueError('invalid artifact upload')
        session=self.repository.session
        identity=canonical_payload([session.authentication.tenant_id,session.world,session.authentication.subject_principal_id,request_id])
        params={'artifact_id':hashlib.sha256(identity.encode()).hexdigest()[:32],
                'sha256':hashlib.sha256(payload).hexdigest(),'size_bytes':len(payload),
                'media_type':media_type,'retention_until':retention_until.isoformat()}
        # Validate before issuing a permit or touching the filesystem.
        BlobReference(session.authentication.tenant_id,session.world,params['artifact_id'],params['sha256'],len(payload),media_type)
        claim=self.repository.reserve(params)
        if claim.available:
            self.store.read(claim.reference)
            return claim.reference
        with self.repository.upload_transaction(claim) as ref:
            self.store.put(tenant_id=ref.tenant_id,world=ref.world,
                artifact_id=ref.artifact_id,payload=payload,media_type=media_type)
            # Trusted upload integrity readback remains inside CREATE/finalize transaction;
            # it does not expose unbound Context bytes through a public READ API.
            if self.store.read(ref)!=payload:raise ArtifactIntegrityError('upload integrity mismatch')
        return ref

    def read(self,artifact_id,*,start=0,stop=None):
        ref=self.repository.get(artifact_id)
        stop=ref.size_bytes if stop is None else stop
        if type(start) is not int or type(stop) is not int or not 0<=start<=stop<=ref.size_bytes:
            raise ValueError('unsatisfiable byte range')
        return self.store.read(ref)[start:stop]

    def delete_expired(self,artifact_id):
        claim=self.repository.claim_deletion(artifact_id)
        if claim.available:
            return
        with self.repository.deletion_transaction(claim) as ref:
            self.store.remove(ref)

    def collect_expired(self,*,limit=50,after=''):
        ids=self.repository.cleanup_candidates(limit=limit,after=after)
        for artifact_id in ids:
            self.delete_expired(artifact_id)
        return {'deleted':list(ids),'next_after':ids[-1] if ids else after,'exhausted':len(ids)<limit}

    def collect_temporary_files(self,*,older_than,limit=50,after=''):
        if (older_than.tzinfo is None or older_than>datetime.now(UTC)-timedelta(seconds=60)
                or type(limit) is not int or not 1<=limit<=100
                or type(after) is not str or (after and not re.fullmatch(r'\.tmp-[a-f0-9]{32}',after))):
            raise ValueError('invalid temporary cleanup page')
        params={'older_than':older_than.isoformat(),'limit':limit,'after':after}
        session=self.repository.session
        namespace=BlobReference(session.authentication.tenant_id,session.world,'0'*32,'0'*64,0,'application/octet-stream')
        with self.repository.temporary_cleanup_transaction(params) as guard:
            return self.store.collect_temporary_page(namespace,older_than=older_than,limit=limit,after=after,guard=guard)

def context_message_read_envelope(pool,session,signer,message_id):
    """Internal typed current-reader proof; carries no permission from a binding owner.

    SQL selects the path: configured Message authority, or READ derived from the
    caller's explicit message_read_rule plus current Consumer READ (message_read).
    """
    from nexloop_eios.message_read import message_read_basis,derived_message_read_envelope
    basis=message_read_basis(pool,session,message_id)
    if basis.get('mode')=='derived':return derived_message_read_envelope(pool,session,signer,message_id,basis)
    from nexloop_eios.object_reads import AuthorizedObjectReader
    reader=AuthorizedObjectReader(pool,session,signer)
    claims=reader._authority(ResourceType.OBJECT,'Message/'+message_id)
    claims.update(protocol='nexloop-object-read-v1',key_id=signer.key_id,type_name='Message',object_id=message_id,
        fields=['actor','body'],property_authorities=[reader._authority(ResourceType.PROPERTY,'Message/'+message_id+'/'+field) for field in ('actor','body')])
    text=canonical_payload(claims)
    return {'text':text,'signature':hmac.new(signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()}

