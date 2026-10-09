"""Restricted EIOS technical queue port; PG commit precedes acceptance.

No channel/provider I/O or formal ontology writes occur here. Task leases do not
confer Action authority. Callers never supply tenant/actor/world/worker identity.
"""
from datetime import UTC,datetime,timedelta
import hashlib,hmac,re
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import PostgresAuthorityProvider,resolve_authority
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied

class QueueConflict(RuntimeError):pass
class StaleQueueLease(RuntimeError):pass

class PostgresDurableQueue:
    def __init__(self,pool,session,signer,*,queue):
        if not isinstance(queue,str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',queue) is None:raise ValueError('invalid queue')
        if session.run_context is not None:raise ActionAuthorizationDenied('Run cannot manage scheduler authority')
        self.pool,self.session,self.signer,self.queue=pool,session,signer,queue

    def _call(self,verb,**parameters):
        target=f'eios:action:NexLoop.queue.{self.queue}:1';entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        d=AuthorizationDecisionService().decide_resolved(resolve_authority(self.pool,self.session,query,entries))
        if not d.allowed or not d.authoritative or d.obligations:raise ActionAuthorizationDenied('queue authority denied')
        payload=canonical_payload({'queue':self.queue,'verb':verb,**parameters})
        if len(payload.encode())>262144:raise ValueError('queue payload budget exceeded')
        s=self.session;c={'protocol':'nexloop-queue-command-v1','key_id':self.signer.key_id,'tenant_id':s.authentication.tenant_id,
          'credential_id':s.authentication.credential_id,'principal_id':s.authentication.subject_principal_id,
          'directory_hash':s.directory_hash,'world':s.world,'resource_id':target,'action_resource':target,'operation':'execute',
          'expires_at':min(d.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'parameters_digest':hashlib.sha256(payload.encode()).hexdigest(),'facts':sorted(entries,key=lambda r:(r['kind'],r['key']))}
        text=canonical_payload(c);sig=hmac.new(self.signer.material,('nexloop-queue-command-v1:'+text).encode(),'sha256').hexdigest()
        import psycopg
        try:
            with self.pool.connection() as db,db.transaction():
                verify_application_role(db)
                return db.execute('select authz.nexloop_queue_command(%s,%s,%s,%s,%s)',(s.token_digest,s.world,text,sig,payload)).fetchone()[0]
        except psycopg.errors.RaiseException as error:
            code=error.diag.message_primary
            if code=='queue_payload_conflict':raise QueueConflict(code) from None
            if code=='queue_stale_lease':raise StaleQueueLease(code) from None
            raise RuntimeError('queue command rejected') from None

    def accept(self,*,source_id,event_id,payload,max_attempts=3):
        return self._call('accept',source_id=source_id,event_id=event_id,payload=payload,max_attempts=max_attempts)
    def claim(self,*,lease_seconds=30):return self._call('claim',lease_seconds=lease_seconds)
    def finish(self,*,task_id,fence,status,result=None,retry_seconds=0):
        return self._call('finish',task_id=task_id,fence=fence,status=status,result=result,retry_seconds=retry_seconds)
    def renew(self,*,task_id,fence,lease_seconds=30):return self._call('renew',task_id=task_id,fence=fence,lease_seconds=lease_seconds)
    def claim_outbox(self,*,lease_seconds=30):return self._call('outbox_claim',lease_seconds=lease_seconds)
    def acknowledge_outbox(self,*,outbox_id,fence):return self._call('outbox_ack',outbox_id=outbox_id,fence=fence)

    def inspect(self,*,task_id):return self._call('inspect',task_id=task_id)

    def assert_lease(self,*,task_id,fence):return self._call('assert_lease',task_id=task_id,fence=fence)
