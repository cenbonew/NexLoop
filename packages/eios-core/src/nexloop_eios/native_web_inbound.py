"""Native WebChat event admission (v1, and v2 with client-level order/time/reply evidence, NX-051). No third-party channel."""
import hashlib,re,uuid
from nexloop_eios.conversation_messages import ConversationMessagePort,MESSAGE,_id,_key
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.action_definitions import PostgresActionDefinitionReader

NAMESPACE='native.webchat'

class NativeWebMessagePort(ConversationMessagePort):
    def accept_native_message(self,*,conversation_id,idempotency_key,provider_event_id,body,client_sequence=None,client_sent_at=None,reply_to=None):
        try:
            client=_client_fields(client_sequence,client_sent_at,reply_to)
            conversation_id=_id(conversation_id);transport=_key(idempotency_key)
            if type(provider_event_id) is not str or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',provider_event_id) or str(uuid.UUID(provider_event_id))!=provider_event_id:raise ValueError()
            if type(body) is not str or not 1<=len(body)<=8192 or '\0' in body:raise ValueError()
            auth=self.session.authentication
            name=canonical_payload([auth.tenant_id,self.session.world,NAMESPACE,auth.subject_principal_id,provider_event_id])
            parameters=dict(conversation_id=conversation_id,idempotency_key='native-'+hashlib.sha256(name.encode()).hexdigest(),body=body,
                provider_namespace=NAMESPACE,provider_event_id=provider_event_id,transport_key=transport,event_key_text=name)
            with self.pool.connection() as db,db.transaction():
                prepared=self._native_call(db,'prepare_message',**parameters)
                if prepared.get('replay'):result=prepared['result']
                else:
                    result=self._native_call(db,'commit_message',**parameters,create=self._create(db,MESSAGE,prepared['payload']))
                    if client is not None:
                        # Same transaction as the message: client-level evidence only (never order, never a time anchor).
                        envelope,_=self._signed('nexloop-native-client-v1',MESSAGE,{'message_id':result['message']['id'],'provider_event_id':provider_event_id,**client})
                        db.execute('select authz.nexloop_native_client_facts(%s,%s,%s,%s,%s)',
                            (self.session.token_digest,self.session.world,envelope['text'],envelope['signature'],envelope['payload'])).fetchone()
                return {**result,'provider_namespace':NAMESPACE,'provider_event_id':provider_event_id}
        except Exception as error:self._raise(error)

    def _native_call(self,db,verb,**parameters):
        definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(MESSAGE,1)
        if not (set(definition.required_scopes)|set(capability.required_scopes))<=self.session.authentication.requested_scopes:raise ValueError()
        if definition.governance.policy_refs or definition.preconditions or definition.parameters or definition.governance.approval_mode.value!='none':raise ValueError()
        envelope,_=self._signed('nexloop-conversation-v1',MESSAGE,{'verb':verb,**parameters},
            definition=definition.model_dump(mode='json'),capability=capability.model_dump(mode='json'))
        return db.execute('select authz.nexloop_native_web_message(%s,%s,%s,%s,%s)',
            (self.session.token_digest,self.session.world,envelope['text'],envelope['signature'],envelope['payload'])).fetchone()[0]


def _client_fields(client_sequence,client_sent_at,reply_to):
    """v2 client fields (all optional; None when none given). Shape only: they are what the client states."""
    if client_sequence is None and client_sent_at is None and reply_to is None:return None
    if client_sequence is not None and (type(client_sequence) is not int or not 1<=client_sequence<=9007199254740991):raise ValueError()
    if client_sent_at is not None and (type(client_sent_at) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z',client_sent_at)):raise ValueError()
    if reply_to is not None:
        if type(reply_to) is not dict or set(reply_to)!={'kind','ref'}:raise ValueError()
        if reply_to['kind']=='message':_id(reply_to['ref'])
        elif reply_to['kind']=='provider_event':
            if type(reply_to['ref']) is not str or str(uuid.UUID(reply_to['ref']))!=reply_to['ref']:raise ValueError()
        else:raise ValueError()
    return {'client_sequence':client_sequence,'client_sent_at':client_sent_at,'reply_to':reply_to}
