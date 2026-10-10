"""Human-owned conversations and durable Messages, only real governed Actions.

This private port accepts actual authenticated server sessions. It never accepts
browser-provided principal/tenant/Consumer selection or an authority callback.
"""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import re

import psycopg
from eios.actions import models as M
from eios.actions.governance import ActionGovernor
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from eios.identity.models import SubjectKind
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType,validate_object_properties
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.action_governor import UnavailableApprovalAuthority,UnavailableApprovalPort
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.postgres_artifacts import canonical_payload

OWNERSHIP='ConsumerOwnership.create'
CREATE='Conversation.create'
MESSAGE='Message.create'
READ='nexloop.conversation.read'


class ConversationUnavailable(RuntimeError):
    def __init__(self):super().__init__('conversation_unavailable')


class ConversationDenied(ConversationUnavailable,PermissionError):
    pass


class ConversationConflict(RuntimeError):
    code='conversation_payload_conflict'
    http_status=409
    def __init__(self):super().__init__(self.code)


def conversation_schemas():
    fields={'ConsumerOwnership':{'consumer_id':'string','principal_id':'string'},
        'Conversation':{'consumer_id':'string','owner_principal':'string'},
        'Message':{'conversation_id':'string','sequence':'integer','actor':'string','body':'string','accepted_at':'string'}}
    return tuple(ObjectTypeDefinition(type_name=name,version=1,only_edit_via_actions=True,
        properties=tuple(PropertyDefinition(property_name=key,value_type=PropertyValueType(kind)) for key,kind in values.items()))
        for name,values in fields.items())


def _key(value):
    if type(value) is not str or not 16<=len(value)<=200 or re.fullmatch('[A-Za-z0-9._~-]+',value) is None:raise ValueError()
    return value


def _id(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:raise ValueError()
    return value


class _AtomicReserve:
    def __init__(self,execute):self.execute=execute
    def reserve(self,command):return self.execute(command)


class _ConversationPort:
    def __init__(self,pool,session,signer):
        self.pool,self.session,self.signer=pool,session,signer
        if session.world!='real' or session.run_context is not None:raise ConversationDenied()
        with pool.connection() as db:
            if verify_application_role(db)!='nexloop_api':raise ConversationUnavailable()

    def _proof(self,action):
        target='eios:action:'+action+':1';entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(
            PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ConversationDenied()
        auth=self.session.authentication
        return {'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,'credential_id':auth.credential_id,
            'world':self.session.world,'directory_hash':self.session.directory_hash,'resource_id':target,'action_resource':target,
            'operation':'execute','expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))},decision

    def _signed(self,protocol,action,payload,**extra):
        proof,decision=self._proof(action)
        body=canonical_payload(payload)
        claims={'protocol':protocol,'key_id':self.signer.key_id,**proof,
            'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),**extra}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,(protocol+':'+text).encode(),'sha256').hexdigest()
        return {'text':text,'signature':signature,'payload':body},decision

    def _call(self,db,verb,action,_function='authz.nexloop_conversation_command',**arguments):
        definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(action,1)
        if not (set(definition.required_scopes)|set(capability.required_scopes))<=self.session.authentication.requested_scopes:raise ConversationUnavailable()
        if definition.governance.policy_refs or definition.preconditions or definition.parameters or definition.governance.approval_mode.value!='none':raise ConversationUnavailable()
        envelope,_=self._signed('nexloop-conversation-v1',action,{'verb':verb,**arguments},
            definition=definition.model_dump(mode='json'),capability=capability.model_dump(mode='json'))
        if _function not in ('authz.nexloop_conversation_command','authz.nexloop_conversation_messages_projection'):raise ConversationUnavailable()
        return db.execute('select '+_function+'(%s,%s,%s,%s,%s)',
            (self.session.token_digest,self.session.world,envelope['text'],envelope['signature'],envelope['payload'])).fetchone()[0]

    def _create(self,db,action,payload):
        """Reserve/Governor/actual object+terminal commit in caller transaction."""
        definition,capability,schemas=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get_with_schemas(action,1)
        schema=next(schema for schema in schemas if schema.type_name==payload['type_name'])
        payload={**payload,'properties':validate_object_properties(schema,payload['properties'])}
        now=db.execute('select clock_timestamp()').fetchone()[0].astimezone(UTC)
        command=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,
            action_stable_name=action,idempotency_key=payload['request_id']),
            binding=M.ClaimBindingPayload(invocation_id=payload['request_id'],action_reference=definition.reference(),
                request_digest=M.canonical_request_digest(payload),capability_binding=capability.binding(),
                adapter_id='nexloop.core',target_system='postgres'),requested_at=now,lease_expires_at=now+timedelta(seconds=20))
        def reserve(actual):
            envelope,decision=self._signed('nexloop-action-command-v1',action,actual.model_dump(mode='json'),verb='reserve')
            # Existing claim definer requires a genuine current decision ID.
            claims=__import__('json').loads(envelope['text']);claims['permit_id']=decision.decision_id
            text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-action-command-v1:'+text).encode(),'sha256').hexdigest()
            result=db.execute('select authz.nexloop_action_claim_command(%s,%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,text,signature,envelope['payload'])).fetchone()[0]
            return M.ActionClaimResult.model_validate_json(canonical_payload(result))
        def clock():return db.execute('select clock_timestamp()').fetchone()[0].astimezone(UTC)
        evidence=M.PolicyEvidenceSet(tenant_id=self.session.authentication.tenant_id,invocation_id=command.binding.invocation_id,
            action_reference=definition.reference(),required_policy_references=(),evidence=(),evaluated_at=clock())
        governor=ActionGovernor(claim_port=_AtomicReserve(reserve),approval_port=UnavailableApprovalPort(),
            authority_verifier=UnavailableApprovalAuthority(),clock=clock)
        permit=governor.govern(tenant_id=self.session.authentication.tenant_id,invocation_id=command.binding.invocation_id,
            action_reference=definition.reference(),action_definition=definition,capability_snapshot=capability,request=payload,
            claim_request=command,granted_scopes=self.session.authentication.requested_scopes,policy_evidence=evidence,approval_evidence=None)
        if type(permit) is not M.ActionExecutionPermit:raise ConversationUnavailable()
        envelope,_=self._signed('nexloop-object-create-v1',action,payload,permit=permit.model_dump(mode='json'),
            definition=definition.model_dump(mode='json'),schema=schema.model_dump(mode='json'))
        return envelope

    @staticmethod
    def _raise(error):
        if isinstance(error,psycopg.Error) and error.diag.message_primary=='conversation_payload_conflict':raise ConversationConflict() from None
        if isinstance(error,PermissionError) or isinstance(error,psycopg.errors.InsufficientPrivilege):raise ConversationDenied() from None
        raise ConversationUnavailable() from None


class ConversationOwnershipRegistrar(_ConversationPort):
    """Registered service Action assigns an actual Human to an actual Consumer."""
    def __init__(self,pool,session,signer):
        super().__init__(pool,session,signer)
        if session.authentication.subject_kind is not SubjectKind.SERVICE:raise ConversationDenied()
    def register_consumer_owner(self,*,consumer_id,principal_id,idempotency_key):
        try:
            consumer_id=_id(consumer_id);key=_key(idempotency_key)
            if type(principal_id) is not str or not 1<=len(principal_id)<=320:raise ValueError()
            with self.pool.connection() as db,db.transaction():
                payload=self._call(db,'prepare_owner',OWNERSHIP,consumer_id=consumer_id,principal_id=principal_id,idempotency_key=key)
                if payload.get('replay'):return payload['result']
                return self._call(db,'commit_owner',OWNERSHIP,consumer_id=consumer_id,principal_id=principal_id,
                    idempotency_key=key,create=self._create(db,OWNERSHIP,payload['payload']))
        except Exception as error:self._raise(error)


class ConversationMessagePort(_ConversationPort):
    def __init__(self,pool,session,signer):
        super().__init__(pool,session,signer)
        if session.authentication.subject_kind is not SubjectKind.HUMAN or getattr(session,'identity_kind',None)!='browser':raise ConversationDenied()
    def create_conversation(self,*,idempotency_key):
        try:
            key=_key(idempotency_key)
            with self.pool.connection() as db,db.transaction():
                payload=self._call(db,'prepare_conversation',CREATE,idempotency_key=key)
                if payload.get('replay'):return payload['result']
                return self._call(db,'commit_conversation',CREATE,idempotency_key=key,
                    create=self._create(db,CREATE,payload['payload']))
        except Exception as error:self._raise(error)
    def accept_message(self,*,conversation_id,idempotency_key,body):
        try:
            conversation_id=_id(conversation_id);key=_key(idempotency_key)
            if type(body) is not str or not 1<=len(body)<=8192 or '\0' in body:raise ValueError()
            with self.pool.connection() as db,db.transaction():
                prepared=self._call(db,'prepare_message',MESSAGE,conversation_id=conversation_id,idempotency_key=key,body=body)
                if prepared.get('replay'):return prepared['result']
                return self._call(db,'commit_message',MESSAGE,conversation_id=conversation_id,idempotency_key=key,body=body,
                    create=self._create(db,MESSAGE,prepared['payload']))
        except Exception as error:self._raise(error)
    def list_conversations(self,*,after='',limit=50):
        return self._read('conversations',after=after,limit=limit)
    def read_messages(self,*,conversation_id,after_sequence=0,limit=50):
        return self._read('messages',conversation_id=_id(conversation_id),after_sequence=after_sequence,limit=limit)
    def read_events(self,*,conversation_id,after_sequence=0,limit=50):
        return self._read('events',conversation_id=_id(conversation_id),after_sequence=after_sequence,limit=limit)
    def _read(self,verb,**arguments):
        try:
            if type(arguments['limit']) is not int or not 1<=arguments['limit']<=100:raise ValueError()
            if 'after_sequence' in arguments and (type(arguments['after_sequence']) is not int or not 0<=arguments['after_sequence']<=2**63-1):raise ValueError()
            if 'after' in arguments and arguments['after']!='':_id(arguments['after'])
            # NX-051: the messages read is the 0046 owner read plus provider/reply projection fields (0120).
            function='authz.nexloop_conversation_messages_projection' if verb=='messages' else 'authz.nexloop_conversation_command'
            with self.pool.connection() as db,db.transaction():return self._call(db,verb,READ,_function=function,**arguments)
        except Exception as error:self._raise(error)
