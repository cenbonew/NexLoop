"""Durable final-orphan maintenance; never infer DB liveness from file age."""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import re
import secrets
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.local_artifacts import BlobReference,ArtifactAccessDenied
from nexloop_eios.postgres_artifacts import canonical_payload


class FinalOrphanCollector:
    def __init__(self,pool,session,signer,store):
        self.pool,self.session,self.signer,self.store=pool,session,signer,store

    def _arguments(self,verb,payload):
        with self.pool.connection() as c:
            if verify_application_role(c)!='nexloop_domain_worker':
                raise ArtifactAccessDenied('final orphan cleanup requires maintenance role')
        entries=[];target=f'eios:artifact:orphans_{self.session.world}'
        query=self.session.query(resource_id=target,resource_type=ResourceType.ARTIFACT,operation=Operation.DELETE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:
            raise ArtifactAccessDenied('final orphan maintenance authority denied')
        body=canonical_payload(payload)
        claims={'protocol':'nexloop-orphan-sweep-v1','key_id':self.signer.key_id,'verb':verb,
            'tenant_id':self.session.authentication.tenant_id,'principal_id':self.session.authentication.subject_principal_id,
            'credential_id':self.session.authentication.credential_id,'directory_hash':self.session.directory_hash,
            'world':self.session.world,'resource_id':target,'target_resource':target,'operation':'delete',
            'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,('nexloop-orphan-sweep-v1:'+text).encode(),'sha256').hexdigest()
        return (self.session.token_digest,self.session.world,text,signature,body)

    @staticmethod
    def _execute(connection,arguments):
        return connection.execute('select authz.nexloop_orphan_sweep_command(%s,%s,%s,%s,%s)',arguments).fetchone()[0]

    def _command(self,verb,payload):
        args=self._arguments(verb,payload)
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return self._execute(c,args)

    def _namespace(self):
        return BlobReference(self.session.authentication.tenant_id,self.session.world,'0'*32,'0'*64,0,'application/octet-stream')

    def collect(self,*,older_than,limit=25,after=''):
        if (older_than.tzinfo is None or older_than>datetime.now(UTC)-timedelta(seconds=60)
                or type(limit) is not int or not 1<=limit<=50 or type(after) is not str
                or (after and not re.fullmatch(r'[a-f0-9]{32}',after))):
            raise ValueError('invalid final orphan page')
        self._arguments('scan',{'limit':limit,'after':after})  # Real authority before physical discovery.
        page=self.store.orphan_candidate_page(self._namespace(),older_than=older_than,limit=limit,after=after)
        plan={'sweep_id':secrets.token_hex(16),'older_than':older_than.isoformat(),**page}
        self._command('plan',plan)  # Durable plan commits before any unlink.
        return self.resume(plan['sweep_id'])

    def resume(self,sweep_id):
        if type(sweep_id) is not str or not re.fullmatch(r'[a-f0-9]{32}',sweep_id):
            raise ValueError('invalid orphan sweep')
        stored=self._command('load',{'sweep_id':sweep_id});plan=stored['plan']
        if stored['status']=='finished':
            return {'sweep_id':sweep_id,'outcomes':stored['outcomes'],'next_after':plan['next_after'],'exhausted':plan['exhausted']}
        args=self._arguments('lock',{'sweep_id':sweep_id})
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            def guard():return self._execute(c,args)
            state=guard()
            if state['finished']:
                outcomes=state['outcomes']
            else:
                outcomes=self.store.apply_orphan_plan(self._namespace(),plan['candidates'],guard)
                self._execute(c,self._arguments('finish',{'sweep_id':sweep_id,'outcomes':outcomes}))
        return {'sweep_id':sweep_id,'outcomes':outcomes,'next_after':plan['next_after'],'exhausted':plan['exhausted']}
