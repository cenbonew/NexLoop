"""Trusted per-message governed planning and PREPARED-first Run admission.

Requires append-only 0049 protected ports and published MessageAssignment.
This module never supplies business SQL, fake facts, or runtime bearer access.
"""
from datetime import UTC, datetime
import hashlib
import hmac
import re
from eios.ontology.models import ObjectTypeDefinition, PropertyDefinition, PropertyValueType
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.conversation_runtime_bridge import ConversationRuntimeBridge, canonical_event_id
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.run_credential_vault import RunCredentialVault
from nexloop_eios.runtime_activation import RuntimeActivationPort

EFFECT = 'eios:action:nexloop.service.request:1'
PROTOCOL = 'nexloop-message-run-issuance-v1'


class MessageRelayUnavailable(RuntimeError):
    def __init__(self): super().__init__('message_relay_unavailable')


def message_assignment_schema():
    fields = {'message_id':'string','consumer_id':'string','goal_id':'string',
        'goal_revision':'integer','step_id':'string','step_revision':'integer',
        'control_id':'string','control_revision':'integer','consumer_revision':'integer',
        'source_principal':'string','executor_principal':'string','recipe_digest':'string',
        'allowed_actions':'json','state':'string','valid_until':'string'}
    return ObjectTypeDefinition(type_name='MessageAssignment',version=1,only_edit_via_actions=True,
        properties=tuple(PropertyDefinition(property_name=name,value_type=PropertyValueType(kind),required=True) for name,kind in fields.items()))


def validate_recipe(recipe):
    expected={'consumer_id','control_id','control_revision','consumer_revision',
        'valid_until','role_ref','context_manifest_ref','runtime_profile','budget',
        'runtime_owner_epoch','queue'}
    if type(recipe) is not dict or set(recipe)!=expected: raise MessageRelayUnavailable()
    for field in ('consumer_id','control_id'):
        if type(recipe[field]) is not str or not re.fullmatch('[a-f0-9]{64}',recipe[field]): raise MessageRelayUnavailable()
    for field in ('control_revision','consumer_revision','runtime_owner_epoch'):
        if type(recipe[field]) is not int or not 1<=recipe[field]<=9007199254740991: raise MessageRelayUnavailable()
    if type(recipe['valid_until']) is not str or datetime.fromisoformat(recipe['valid_until']).tzinfo is None: raise MessageRelayUnavailable()
    if type(recipe['queue']) is not str or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',recipe['queue']): raise MessageRelayUnavailable()
    # The complete RunCommand validator checks budget and references once PG
    # supplies the immutable first Run expiry. Recipe data is private server data.
    for field in ('role_ref','context_manifest_ref','runtime_profile'):
        if type(recipe[field]) is not str or not 1<=len(recipe[field])<=512: raise MessageRelayUnavailable()
    from datetime import timedelta
    from nexloop_eios.runtime_activation import _command
    # Pure syntax validation does not authenticate this synthetic check object.
    _command({'schema_version':'1.0','run_id':'00000000-0000-4000-8000-000000000001',
        'tenant_id':'00000000-0000-4000-8000-000000000002','trigger_event_id':'00000000-0000-4000-8000-000000000003',
        'world_id':'real','mode':'real','request_id':'message-recipe-validation',
        'role_ref':recipe['role_ref'],'consumer_ref':'consumer:'+recipe['consumer_id'],
        'goal_version_ref':'goal:recipe-validation','context_manifest_ref':recipe['context_manifest_ref'],
        'credential_ref':'run:recipe-validation','runtime_profile':recipe['runtime_profile'],
        'budget':recipe['budget'],'runtime_owner_epoch':recipe['runtime_owner_epoch'],
        'not_after':(datetime.now(UTC)+timedelta(seconds=300)).isoformat()})
    from copy import deepcopy
    return deepcopy(recipe)


class MessageRelayPort:
    """Only real authenticated Backend services; all calls own current RLock."""
    def __init__(self, services):
        self.services=services
        self.backend=services._backend
        self.session=services._session
        if self.session.world!='real' or self.session.run_context is not None or getattr(self.session,'identity_kind',None)=='browser': raise MessageRelayUnavailable()

    def call(self, verb, **parameters):
        try:
            with self.backend._lock:
                self.backend._assert_open()
                reader=PostgresActionDefinitionReader(self.backend._pool,self.session,self.backend._signer)
                definition,capability=reader.get('nexloop.conversation.route',1)
                if definition.preconditions or definition.governance.policy_refs or definition.governance.approval_mode.value!='none': raise ValueError()
                proof=RuntimeActivationPort(self.backend._pool,self.session,self.backend._signer)._proof(self.session,'eios:action:nexloop.conversation.route:1')
                body=canonical_payload({'verb':verb,**parameters})
                if len(body.encode())>262144: raise ValueError()
                text=canonical_payload({'protocol':PROTOCOL,'key_id':self.backend._signer.key_id,**proof,
                    'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),
                    'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json')})
                signature=hmac.new(self.backend._signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
                with self.backend._pool.connection() as db,db.transaction():
                    if verify_application_role(db)!='nexloop_api': raise ValueError()
                    return db.execute('select authz.nexloop_message_run_issuance_command(%s,%s,%s,%s,%s)',
                        (self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]
        except Exception: raise MessageRelayUnavailable() from None

    def source_proofs(self,source):
        if source._backend is not self.backend or source._session.run_context is not None: raise MessageRelayUnavailable()
        reader=PostgresActionDefinitionReader(self.backend._pool,source._session,self.backend._signer)
        definition,capability=reader.get('nexloop.service.request',1)
        proof=RuntimeActivationPort(self.backend._pool,source._session,self.backend._signer)._proof(source._session,EFFECT)
        return source._session.token_digest,[proof],definition.model_dump(mode='json'),capability.model_dump(mode='json')


class MessageRelay:
    def __init__(self,*,route,source,planner,executor_token,vault,recipe):
        self.port=MessageRelayPort(route)
        self.route,self.source,self.planner=route,source,planner
        self.executor_token=executor_token
        self.vault=vault
        self.recipe=validate_recipe(recipe)
        if any(service._backend is not route._backend for service in (source,planner)): raise MessageRelayUnavailable()

    def _create(self,type_name,message_id,properties):
        intent='message-relay-'+hashlib.sha256(canonical_payload([message_id,type_name,self.recipe]).encode()).hexdigest()
        result=self.planner.create_object(action_name=type_name+'.create',action_version=1,
            intent_id=intent,type_name=type_name,properties=properties)
        return result['object_id']

    def run_once(self):
        try:
            item=self.port.call('claim',consumer_id=self.recipe['consumer_id'],lease_seconds=30)
            if item is None: return 'idle'
            message_id=item['message_id']; fence=item['fence']
            bridge=ConversationRuntimeBridge(self.route)
            if item.get('route_fence') is not None:
                recovered=bridge._call('ack',message_id=message_id,fence=item['route_fence'],allow_missing=True)
                if recovered is not None:
                    self.port.call('release',message_id=message_id,fence=fence)
                    return 'queued'
            if datetime.fromisoformat(self.recipe['valid_until'])<=datetime.now(UTC): return 'requires_governed_replan'
            principal=self.source._session.authentication.subject_principal_id
            executor=self.route._backend.authenticate(self.executor_token,world='real')._session.authentication.subject_principal_id
            goal=self._create('Goal',message_id,{'consumer_id':self.recipe['consumer_id'],'state':'active','valid_until':self.recipe['valid_until']})
            step=self._create('PlanStep',message_id,{'consumer_id':self.recipe['consumer_id'],'goal_id':goal,
                'control_id':self.recipe['control_id'],'submitter_principals':[principal],
                'action_name':'nexloop.service.request','state':'ready'})
            assignment={'message_id':message_id,'consumer_id':self.recipe['consumer_id'],
                'goal_id':goal,'goal_revision':1,'step_id':step,'step_revision':1,
                'control_id':self.recipe['control_id'],'control_revision':self.recipe['control_revision'],
                'consumer_revision':self.recipe['consumer_revision'],'source_principal':principal,
                'executor_principal':executor,'recipe_digest':hashlib.sha256(canonical_payload(self.recipe).encode()).hexdigest(),
                'allowed_actions':[EFFECT],'state':'active','valid_until':self.recipe['valid_until']}
            assignment_id=self._create('MessageAssignment',message_id,assignment)
            key=self.vault.message_key(item['tenant_id'],'real',message_id)
            record=self.vault.prepare(message_key=key,assignment_digest=hashlib.sha256(canonical_payload(assignment).encode()).hexdigest())
            if record.state=='requires_governed_replan': return 'requires_governed_replan'
            source_digest,proofs,source_definition,source_capability=self.port.source_proofs(self.source)
            issued=self.port.call('issue',message_id=message_id,fence=fence,
                assignment_id=assignment_id,assignment_revision=1,assignment_digest=record.assignment_digest,assignment_text=canonical_payload(assignment),
                run_id=record.run_id,request_id=record.request_id,issuance_nonce=record.issuance_nonce,
                token_digest=record.token_digest,source_digest=source_digest,source_proofs=proofs,source_definition=source_definition,source_capability=source_capability,ttl_seconds=300)
            if issued['state']=='requires_governed_replan': return 'requires_governed_replan'
            record=self.vault.mark_issued(message_key=key,run_id=issued['run_id'],token_digest=record.token_digest,expires_at=issued['expires_at'])
            self.planner.bind_effect_context(step_id=step,step_revision=1,goal_revision=1,
                consumer_revision=self.recipe['consumer_revision'],control_revision=self.recipe['control_revision'],
                run_id=record.run_id,run_token=record.token,executor_token=self.executor_token)
            command={'schema_version':'1.0','run_id':record.run_id,'tenant_id':item['tenant_id'],
                'world_id':'real','mode':'real','request_id':record.request_id,
                'trigger_event_id':canonical_event_id(item['tenant_id'],'real',item['source_event_id']),
                'role_ref':self.recipe['role_ref'],'consumer_ref':'consumer:'+self.recipe['consumer_id'],
                'goal_version_ref':'goal:'+goal+':revision:1:step:1:control:'+str(self.recipe['control_revision']),
                'context_manifest_ref':self.recipe['context_manifest_ref'],'runtime_profile':self.recipe['runtime_profile'],
                'credential_ref':'run:'+record.run_id,'budget':self.recipe['budget'],'not_after':record.expires_at,
                'runtime_owner_epoch':self.recipe['runtime_owner_epoch']}
            bridge.bind_message(message_id=message_id,run_token=record.token,command=command,queue=self.recipe['queue'])
            owned=self.port.call('own_route',message_id=message_id,fence=fence)
            run=self.route._backend.authenticate_run(record.token,world='real',run_id=record.run_id)._session
            bridge._call('authorize',message_id=message_id,fence=owned['route_fence'],run_proofs=bridge._authority._run_proofs(run))
            self.route.accept_runtime_event(queue=self.recipe['queue'],source_id='webchat',event_id=command['trigger_event_id'],
                run_token=record.token,command=command,input=item['body'])
            bridge._call('ack',message_id=message_id,fence=owned['route_fence'],allow_missing=False)
            self.port.call('release',message_id=message_id,fence=fence)
            return 'queued'
        except Exception: raise MessageRelayUnavailable() from None
