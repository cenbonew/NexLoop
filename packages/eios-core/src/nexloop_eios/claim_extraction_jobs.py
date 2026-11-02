"""NX-019 background extraction path (docs/04 §3): feed → durable queue → worker.

A governed Message commit writes a feed row in the same transaction (0069 trigger),
so the interactive path never waits for a model. The scheduler (nexloop_api service)
debounces each conversation, accepts one idempotent queue event per window and marks
the feed. The worker (nexloop_domain_worker service) claims leased tasks, extracts,
and finishes succeeded / retry_wait (exponential backoff, dead_lettered when attempts
are exhausted) / failed (denied authority is not retried). Backlog is observable.
"""
from datetime import UTC,datetime,timedelta
from nexloop_eios.signing import SIGNED_COMMAND_TTL
import hashlib
import hmac

from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import PostgresAuthorityProvider,resolve_authority,authority_request_scoped
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
        decision=AuthorizationDecisionService().decide_resolved(resolve_authority(self.pool,self.session,query,entries))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ClaimExtractionDenied()
        body=canonical_payload({'verb':verb,**parameters});auth=self.session.authentication
        claims={'protocol':'nexloop-claim-feed-v1','key_id':self.signer.key_id,'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,
            'credential_id':auth.credential_id,'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,
            'action_resource':target,'operation':'execute','expires_at':min(decision.expires_at,datetime.now(UTC)+SIGNED_COMMAND_TTL).isoformat(),
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

    @authority_request_scoped
    def run_once(self,*,limit=20):
        """Enqueue due windows. Crash between accept and mark is safe: the same
        (conversation, through_sequence) event replays to the same task."""
        enqueued=[]
        for item in self.feed.due(quiet_seconds=self.quiet_seconds,limit=limit,window=self.window):
            payload={'conversation_id':item['conversation_id'],'through_sequence':item['through_sequence'],'message_ids':item['message_ids']}
            # S-DUP: context only when there is some (the payload of a first window stays the shape it always had).
            if item.get('context_message_ids'):payload['context_message_ids']=item['context_message_ids']
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

    @authority_request_scoped
    def run_once(self):
        item=self.queue.claim(lease_seconds=self.lease_seconds)
        if item is None:return 'idle'
        payload=item['payload'];retry=min(3600,self.retry_base_seconds*2**max(0,item['attempts']-1))
        try:
            # S-DUP: a task from before the split has no context_message_ids (all targets).
            result=self.extractor.extract(conversation_id=payload['conversation_id'],message_ids=payload['message_ids'],
                context_message_ids=payload.get('context_message_ids') or [])
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


REEXTRACT_ACTION='eios:action:nexloop.claim.reextract_contact:1'


def _signed_port():
    from nexloop_eios.plan_reevaluation import _SignedPort

    class ContactChannelReextractPort(_SignedPort):
        PROTOCOL='nexloop-contact-channel-reextract-v1';ACTION=REEXTRACT_ACTION;FUNCTION='nexloop_contact_channel_reextract'

        def windows(self,extractor_version):return self._signed({'extractor_version':extractor_version})
    return ContactChannelReextractPort


@authority_request_scoped
def reextract_contact_channels(pool,session,signer,*,apply=False,max_attempts=3):
    """Plan claim-to-consumer-property §8.4: enqueue one extraction window per Conversation whose consumer channel statements the 0228
    vocabulary cannot read, under the current extractor version (the scheduler's own queue; event ids make it repeatable). Counts only."""
    from nexloop_eios.conversation_extraction import EXTRACTOR_VERSION
    found=_signed_port()(pool,session,signer).windows(EXTRACTOR_VERSION)
    counts={**found['counts'],'windows':len(found['windows']),'accepted':0}
    if apply:
        queue=PostgresDurableQueue(pool,session,signer,queue=QUEUE)
        for window in found['windows']:
            queue.accept(source_id=SOURCE,event_id=f"reextract:{EXTRACTOR_VERSION}:{window['conversation_id']}:{window['through_sequence']}",
                payload={'conversation_id':window['conversation_id'],'through_sequence':window['through_sequence'],'message_ids':window['message_ids']},
                max_attempts=max_attempts)
            counts['accepted']+=1
    return {'extractor_version':EXTRACTOR_VERSION,'apply':apply,'counts':counts}


def reextract_main(argv=None):
    """contact_channel_reextract.py --database-url-file F --signing-key-file F --service-credential-file F --artifact-root D [--apply]
    as the claim extraction scheduler (nexloop_api); prints counts only."""
    import argparse
    import json
    from contextlib import ExitStack
    from pathlib import Path
    from nexloop_eios.backend import open_backend
    from nexloop_eios.private_configuration import read_private_text

    class _Parser(argparse.ArgumentParser):
        def error(self,message):self.exit(2,'contact channel reextract configuration unavailable\n')

    p=_Parser(description='Contact channel re-extraction (signed port; counts only)')
    for name in ('database-url-file','signing-key-file','service-credential-file','artifact-root'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--signing-key-id',default='active')
    p.add_argument('--apply',action='store_true',help='enqueue; without it a dry run that writes nothing')
    a=p.parse_args(argv)
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=read_private_text(a.database_url_file,maximum=16384),artifact_root=a.artifact_root,
            signing_key_file=a.signing_key_file,signing_key_id=a.signing_key_id,pool_max_size=1))
        with backend._pool.connection() as connection:
            if verify_application_role(connection)!='nexloop_api':raise SystemExit('restricted service role required')
        current=backend.authenticate(read_private_text(a.service_credential_file,maximum=16384),world='real')
        result=reextract_contact_channels(current._backend._pool,current._session,current._backend._signer,apply=a.apply)
        print(json.dumps(result,sort_keys=True,separators=(',',':')),flush=True)
    return 0
