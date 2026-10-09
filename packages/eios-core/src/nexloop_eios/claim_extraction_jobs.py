"""NX-019 background extraction path (docs/04 §3): feed → durable queue → worker.

A governed Message commit writes a feed row in the same transaction (0069 trigger),
so the interactive path never waits for a model. The scheduler (nexloop_api service)
debounces each conversation, accepts one idempotent queue event per window and marks
the feed. The worker (nexloop_domain_worker service) claims leased tasks, extracts,
and finishes succeeded / retry_wait (exponential backoff, dead_lettered when attempts
are exhausted) / failed (denied authority is not retried). Backlog is observable.
"""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac

from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.claim_store import EXTRACT_ACTION,ClaimExtractionDenied,ConversationClaimExtractor
from nexloop_eios.conversation_extraction import ExtractionProviderUnavailable,ExtractionRejected
from nexloop_eios.durable_queue import PostgresDurableQueue
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload

QUEUE='claim-extraction'
SOURCE='nexloop.claim-extraction'


class ClaimFeed:
    """Signed service-only feed/backlog port under Claim-extraction EXECUTE."""
    def __init__(self,pool,session,signer):
        if session.run_context is not None:raise ClaimExtractionDenied()
        self.pool,self.session,self.signer=pool,session,signer

    def _call(self,verb,**parameters):
        try:return self._signed(verb,**parameters)
        except Exception as error:ConversationClaimExtractor._raise(error)

    def _signed(self,verb,**parameters):
        target=resource_id(ResourceType.ACTION,EXTRACT_ACTION,1);entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ClaimExtractionDenied()
        body=canonical_payload({'verb':verb,**parameters});auth=self.session.authentication
        claims={'protocol':'nexloop-claim-feed-v1','key_id':self.signer.key_id,'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,
            'credential_id':auth.credential_id,'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,
            'action_resource':target,'operation':'execute','expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,('nexloop-claim-feed-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_claim_extraction_feed(%s,%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]

    def due(self,*,quiet_seconds,limit,window):return self._call('due',quiet_seconds=quiet_seconds,limit=limit,window=window)
    def mark(self,*,conversation_id,through_sequence,task_id):
        return self._call('mark',conversation_id=conversation_id,through_sequence=through_sequence,task_id=task_id)
    def backlog(self):return self._call('backlog')


class ClaimExtractionScheduler:
    def __init__(self,pool,session,signer,*,quiet_seconds=5,window=50,max_attempts=3):
        self.feed=ClaimFeed(pool,session,signer)
        self.queue=PostgresDurableQueue(pool,session,signer,queue=QUEUE)
        self.quiet_seconds,self.window,self.max_attempts=quiet_seconds,window,max_attempts

    def run_once(self,*,limit=20):
        """Enqueue due windows. Crash between accept and mark is safe: the same
        (conversation, through_sequence) event replays to the same task."""
        enqueued=[]
        for item in self.feed.due(quiet_seconds=self.quiet_seconds,limit=limit,window=self.window):
            payload={'conversation_id':item['conversation_id'],'through_sequence':item['through_sequence'],'message_ids':item['message_ids']}
            accepted=self.queue.accept(source_id=SOURCE,event_id=item['conversation_id']+':'+str(item['through_sequence']),
                payload=payload,max_attempts=self.max_attempts)
            self.feed.mark(conversation_id=item['conversation_id'],through_sequence=item['through_sequence'],task_id=accepted['task_id'])
            enqueued.append(accepted['task_id'])
        return enqueued


class ClaimExtractionWorker:
    def __init__(self,pool,session,signer,provider,*,timezone='UTC',lease_seconds=120,retry_base_seconds=30):
        self.queue=PostgresDurableQueue(pool,session,signer,queue=QUEUE)
        self.extractor=ConversationClaimExtractor(pool,session,signer,provider,timezone=timezone)
        self.lease_seconds,self.retry_base_seconds=lease_seconds,retry_base_seconds

    def run_once(self):
        item=self.queue.claim(lease_seconds=self.lease_seconds)
        if item is None:return 'idle'
        payload=item['payload'];retry=min(3600,self.retry_base_seconds*2**max(0,item['attempts']-1))
        try:
            result=self.extractor.extract(conversation_id=payload['conversation_id'],message_ids=payload['message_ids'])
            status,body,delay='succeeded',{'code':'recorded','input_digest':result['input_digest'],'claims':len(result['claim_ids']),'replay':result['replay']},0
        except (ClaimExtractionDenied,ActionAuthorizationDenied):
            # Missing or revoked authority is a decision, not a transient fault.
            status,body,delay='failed',{'code':'denied'},0
        except ExtractionProviderUnavailable as error:
            status,body,delay='retry_wait',{'code':'provider_'+getattr(error,'code','unavailable')},retry
        except ExtractionRejected:
            status,body,delay='retry_wait',{'code':'output_rejected'},retry
        except Exception:
            status,body,delay='retry_wait',{'code':'unexpected'},retry
        finished=self.queue.finish(task_id=item['task_id'],fence=item['fence'],status=status,result=body,retry_seconds=delay)
        return finished['status']
