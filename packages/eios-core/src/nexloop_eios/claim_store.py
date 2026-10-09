"""NX-019 governed Claim extraction port over persisted Messages.

Messages are read only through the existing per-object/per-property EIOS READ
projection. Claims are recorded through one signed definer that re-proves the
service's current EXECUTE and every source READ and re-checks every evidence
span against the stored Message body. Claims never become formal objects here.
"""
from datetime import UTC,datetime,timedelta
import hmac
import json
import re

import psycopg
from eios.authz import facts as F
from eios.authz.errors import AuthorizationError
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from eios.identity.models import SubjectKind
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.conversation_extraction import (EXTRACTOR_VERSION,PROMPT_VERSION,ExtractionContext,SourceMessage,
    build_user_payload,input_digest,normalize,SYSTEM_PROMPT)
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload

EXTRACT_ACTION='nexloop.claim.extract'
MESSAGE_FIELDS=('accepted_at','actor','body','conversation_id','sequence')
CONVERSATION_FIELDS=('consumer_id','owner_principal')


class ClaimExtractionUnavailable(RuntimeError):
    def __init__(self):super().__init__('claim_extraction_unavailable')


class ClaimExtractionDenied(ClaimExtractionUnavailable,PermissionError):
    pass


def _id(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:raise ValueError('object id')
    return value


def _timestamp(value):
    stamp=datetime.fromisoformat(value.replace(' ','T',1)) if type(value) is str else None
    if stamp is None or stamp.tzinfo is None:raise ValueError('timezone required')
    return stamp


class ConversationClaimExtractor:
    """Background service port; no browser/Run identity, no formal object write."""
    def __init__(self,pool,session,signer,provider,*,timezone='UTC'):
        if session.authentication.subject_kind is not SubjectKind.SERVICE or session.run_context is not None:raise ClaimExtractionDenied()
        self.pool,self.session,self.signer,self.provider,self.timezone=pool,session,signer,provider,timezone
        self.reader=AuthorizedObjectReader(pool,session,signer)

    def _action_claims(self):
        target=resource_id(ResourceType.ACTION,EXTRACT_ACTION,1);entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ClaimExtractionDenied()
        auth=self.session.authentication
        return {'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,'credential_id':auth.credential_id,
            'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,'action_resource':target,
            'operation':'execute','expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}

    def _source_proofs(self,conversation_id,message_ids):
        proofs=[self.reader._authority(ResourceType.OBJECT,'Conversation/'+conversation_id)]
        proofs+=[self.reader._authority(ResourceType.PROPERTY,'Conversation/'+conversation_id+'/'+f) for f in CONVERSATION_FIELDS]
        for message_id in message_ids:
            proofs.append(self.reader._authority(ResourceType.OBJECT,'Message/'+message_id))
            proofs+=[self.reader._authority(ResourceType.PROPERTY,'Message/'+message_id+'/'+f) for f in MESSAGE_FIELDS]
        return proofs

    def load_window(self,conversation_id,message_ids):
        """Current authorized Message window in receipt (sequence) order."""
        conversation_id=_id(conversation_id)
        if type(message_ids) not in (list,tuple) or not 1<=len(message_ids)<=200 or len(set(message_ids))!=len(message_ids):
            raise ValueError('bounded distinct message window required')
        conversation=self.reader.get('Conversation',conversation_id,fields=CONVERSATION_FIELDS)['properties']
        messages=[]
        for message_id in message_ids:
            values=self.reader.get('Message',_id(message_id),fields=MESSAGE_FIELDS)['properties']
            if values['conversation_id']!=conversation_id:raise ClaimExtractionDenied()
            messages.append(SourceMessage(message_id=message_id,sequence=values['sequence'],
                speaker='consumer' if values['actor']==conversation['owner_principal'] else 'agent',
                body=values['body'],accepted_at=_timestamp(values['accepted_at'])))
        messages.sort(key=lambda m:m.sequence)
        context=ExtractionContext(tenant_id=self.session.authentication.tenant_id,world=self.session.world,
            conversation_id=conversation_id,consumer_id=conversation['consumer_id'],timezone=self.timezone)
        return context,messages

    def extract(self,*,conversation_id,message_ids):
        try:
            context,messages=self.load_window(conversation_id,message_ids)
            digest=input_digest(messages,context,self.provider)
            existing=self.read(conversation_id=conversation_id,input_digest=digest)
            if existing['runs']:
                # Same input version already recorded: no second model call, no duplicates.
                ids=sorted(item['claim_id'] for item in existing['statements']+existing['hypotheses'])
                return {'replay':True,'input_digest':digest,'claim_ids':ids}
            raw=self.provider.complete(SYSTEM_PROMPT,build_user_payload(messages,context))
            topics,claims,rejected=normalize(raw,messages,context)
            payload={'verb':'record','conversation_id':context.conversation_id,'input_digest':digest,
                'extractor_version':EXTRACTOR_VERSION,'prompt_version':PROMPT_VERSION,'provider':self.provider.provider,
                'model_id':self.provider.model_id,'timezone':context.timezone,
                'messages':[{'message_id':m.message_id,'sequence':m.sequence,'content_hash':m.content_hash,'speaker':m.speaker} for m in messages],
                'topics':topics,'claims':claims,'rejected':rejected}
            body=canonical_payload(payload)
            claims_text=self._action_claims()
            claims_text.update(protocol='nexloop-claim-extraction-v1',key_id=self.signer.key_id,
                parameters_digest=__import__('hashlib').sha256(body.encode()).hexdigest(),
                source_authorities=self._source_proofs(context.conversation_id,[m.message_id for m in messages]))
            text=canonical_payload(claims_text)
            signature=hmac.new(self.signer.material,('nexloop-claim-extraction-v1:'+text).encode(),'sha256').hexdigest()
            with self.pool.connection() as db,db.transaction():
                verify_application_role(db)
                return db.execute('select authz.nexloop_record_claim_extraction(%s,%s,%s,%s,%s)',
                    (self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]
        except Exception as error:self._raise(error)

    def read(self,*,conversation_id,input_digest=None):
        """Evidence view: statements and hypotheses separated; never formal properties."""
        conversation_id=_id(conversation_id)
        if input_digest is not None:_id(input_digest)
        claims=self.reader._authority(ResourceType.OBJECT,'Conversation/'+conversation_id)
        claims.update(protocol='nexloop-claim-read-v1',key_id=self.signer.key_id,conversation_id=conversation_id)
        if input_digest is not None:claims['input_digest']=input_digest
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,('nexloop-claim-read-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_read_conversation_claims(%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,text,signature)).fetchone()[0]

    @staticmethod
    def _raise(error):
        # Authority that is missing, stale or revoked is a denial, never a retryable success.
        if isinstance(error,(PermissionError,AuthorizationError,ActionAuthorizationDenied,psycopg.errors.InsufficientPrivilege)):raise ClaimExtractionDenied() from None
        raise error
