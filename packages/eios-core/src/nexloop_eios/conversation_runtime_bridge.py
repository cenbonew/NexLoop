"""Trusted message Outbox→Run admission. No browser-selected execution identity.

The two commits are intentional: actual queue+Run enrollment first, then a
fenced technical ACK. Recovery checks that committed enrollment before doing
anything that could authorize a new Run. Only digests enter route storage.
"""
import hashlib,hmac,re,uuid

from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.run_credentials import AUDIENCE
from nexloop_eios.runtime_activation import RuntimeActivationPort,_command,_input_digest

ROUTE='nexloop.conversation.route'
READ='nexloop.conversation.read'
_NAMESPACE=uuid.UUID('e0963cdf-ea02-587c-a8bc-c04f7c2c4029')


class MessageRuntimeUnavailable(RuntimeError):
    def __init__(self):super().__init__('message_runtime_unavailable')


class MessageRuntimeConflict(RuntimeError):
    code='message_runtime_conflict'
    http_status=409
    def __init__(self):super().__init__(self.code)


def canonical_event_id(tenant_id,world_id,source_event_id):
    return str(uuid.uuid5(_NAMESPACE,canonical_payload([tenant_id,world_id,'webchat',source_event_id])))


def _message_id(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:raise ValueError()
    return value


class _SignedMessagePort:
    def __init__(self,pool,session,signer):
        self.pool,self.session,self.signer=pool,session,signer
        if session.world!='real' or session.run_context is not None:raise MessageRuntimeUnavailable()
        with pool.connection() as db:
            if verify_application_role(db)!='nexloop_api':raise MessageRuntimeUnavailable()
        self._authority=RuntimeActivationPort(pool,session,signer)

    def _call(self,verb,**parameters):
        backend=getattr(self,"_backend",None)
        if backend is not None:
            with backend._lock:
                backend._assert_open()
                return self._call_open(verb,**parameters)
        return self._call_open(verb,**parameters)

    def _call_open(self,verb,**parameters):
        action=READ if verb=='read' else ROUTE
        definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(action,1)
        if definition.preconditions or definition.governance.policy_refs or definition.governance.approval_mode.value!='none':raise MessageRuntimeUnavailable()
        proof=self._authority._proof(self.session,'eios:action:'+action+':1')
        payload=canonical_payload({'verb':verb,**parameters})
        if len(payload.encode())>262144:raise MessageRuntimeUnavailable()
        claims={'protocol':'nexloop-message-runtime-v1','key_id':self.signer.key_id,**proof,
            'parameters_digest':hashlib.sha256(payload.encode()).hexdigest(),
            'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json')}
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-message-runtime-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_message_runtime_command(%s,%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,text,signature,payload)).fetchone()[0]

    @staticmethod
    def _raise(error):
        import psycopg
        if isinstance(error,psycopg.Error) and error.diag.message_primary=='message_runtime_conflict':raise MessageRuntimeConflict() from None
        raise MessageRuntimeUnavailable() from None


class ConversationRuntimeBridge(_SignedMessagePort):
    """Server-only bridge using an actual API service and actual Source Run.

    ``services`` is the authenticated Backend API service; it is deliberately
    not a success callback, provider, or runtime-controlled configuration.
    """
    def __init__(self,services):
        backend=services._backend
        super().__init__(backend._pool,services._session,backend._signer)
        self.services=services
        self._backend=backend
        if getattr(self.session,'identity_kind',None)=='browser':raise MessageRuntimeUnavailable()

    def bind_message(self,*,message_id,run_token,command,queue='operations'):
        try:
            _message_id(message_id);text,digest=_command(command)
            message=self._call('message',message_id=message_id)
            run=authenticate_service(self.pool,run_token,world='real',run_id=command['run_id'],audience=AUDIENCE)
            if run_token in text or run_token in message['record']['body']:raise ValueError()
            event_id=canonical_event_id(command['tenant_id'],'real',message['source_event_id'])
            if command['trigger_event_id']!=event_id:raise ValueError()
            record=message['record'];event={'schema_version':'1.0','event_id':event_id,'tenant_id':command['tenant_id'],
                'world_id':'real','mode':'real','event_type':'message.accepted','source':'webchat',
                'source_event_id':message['source_event_id'],'occurred_at':record['accepted_at'],'recorded_at':record['accepted_at'],
                'subject_ref':'message:'+message_id,'correlation_id':command['run_id'],'causation_id':None,
                'payload':{'message_ref':'message:'+message_id,'conversation_ref':'conversation:'+record['conversation_id']}}
            return self._call('bind',message_id=message_id,queue=queue,run_digest=run.token_digest,command_text=text,
                command_digest=digest,input_digest=_input_digest(record['body']),event_text=canonical_payload(event),
                event_name_text=canonical_payload([command['tenant_id'],'real','webchat',message['source_event_id']]),
                run_proofs=self._authority._run_proofs(run))
        except Exception as error:self._raise(error)

    def deliver_one(self,*,lease_seconds=30,run_token=None):
        try:
            if type(lease_seconds) is not int or not 3<=lease_seconds<=300:raise ValueError()
            item=self._call('claim',lease_seconds=lease_seconds)
            if item is None:return None
            # This reads committed queue enrollment, never grants execution.
            recovered=self._call('ack',message_id=item['message_id'],fence=item['fence'],allow_missing=True)
            if recovered is not None:return recovered
            if type(run_token) is not str or not run_token:raise ValueError()
            run=authenticate_service(self.pool,run_token,world='real',run_id=item['command']['run_id'],audience=AUDIENCE)
            if run.token_digest!=item['_run_digest']:raise ValueError()
            self._call('authorize',message_id=item['message_id'],fence=item['fence'],run_proofs=self._authority._run_proofs(run))
            self.services.accept_runtime_event(queue=item['queue'],source_id='webchat',event_id=item['event_id'],
                run_token=run_token,command=item['command'],input=item['input'])
            # Queue accepts after commit. A failure here leaves a replayable
            # message lease; the next bridge only ACKs that exact existing job.
            return self._call('ack',message_id=item['message_id'],fence=item['fence'],allow_missing=False)
        except Exception as error:self._raise(error)


class ConversationRunReader(_SignedMessagePort):
    def __init__(self,pool,session,signer):
        super().__init__(pool,session,signer)
        if getattr(session,'identity_kind',None)!='browser':raise MessageRuntimeUnavailable()

    def read_message_run(self,*,message_id):
        try:return self._call('read',message_id=_message_id(message_id))
        except Exception as error:self._raise(error)
