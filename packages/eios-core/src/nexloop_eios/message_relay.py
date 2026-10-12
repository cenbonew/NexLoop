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
        'runtime_owner_epoch','queue','offering_id','offering_binding_id'}
    if type(recipe) is not dict or set(recipe)!=expected: raise MessageRelayUnavailable()
    for field in ('consumer_id','control_id','offering_id','offering_binding_id'):
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


RUN_PROFILE_KEYS=frozenset({'role_ref','context_manifest_ref','runtime_profile','budget','runtime_owner_epoch','queue'})
_PLACEHOLDER='0'*64


def validate_run_profile(profile):
    """Relay multi-consumer (0160): the deployment-level Run settings, the same for every consumer."""
    if type(profile) is not dict or set(profile)!=RUN_PROFILE_KEYS: raise MessageRelayUnavailable()
    validate_recipe({**profile,'consumer_id':_PLACEHOLDER,'control_id':_PLACEHOLDER,'offering_id':_PLACEHOLDER,'offering_binding_id':_PLACEHOLDER,
        'control_revision':1,'consumer_revision':1,'valid_until':'2999-01-01T00:00:00+00:00'})
    from copy import deepcopy
    return deepcopy(profile)


def recipe_from_profile(run_profile,profile):
    """The per-message recipe: the consumer's enrolled profile (current revisions, read in SQL at claim) + the Run settings."""
    if type(profile) is not dict: raise MessageRelayUnavailable()
    return validate_recipe({**run_profile,'consumer_id':profile.get('consumer_id'),'control_id':profile.get('control_id'),
        'offering_id':profile.get('offering_id'),'offering_binding_id':profile.get('binding_id'),'control_revision':profile.get('control_revision'),
        'consumer_revision':profile.get('consumer_revision'),'valid_until':profile.get('valid_until')})


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
        except Exception as error:
            self._diagnostic_failure=(getattr(self,'_diagnostic_stage','before_context'),type(error).__name__)
            raise MessageRelayUnavailable() from None

    def source_proofs(self,source):
        if source._backend is not self.backend or source._session.run_context is not None: raise MessageRelayUnavailable()
        reader=PostgresActionDefinitionReader(self.backend._pool,source._session,self.backend._signer)
        definition,capability=reader.get('nexloop.service.request',1)
        proof=RuntimeActivationPort(self.backend._pool,source._session,self.backend._signer)._proof(source._session,EFFECT)
        return source._session.token_digest,[proof],definition.model_dump(mode='json'),capability.model_dump(mode='json')


class MessageRelay:
    def __init__(self,*,route,source,planner,executor_token,vault,recipe=None,run_profile=None,relationship_recipe=None,context_strategy=None,max_backlog=None):
        from nexloop_eios.relationship_context import RelationshipContextRecipe
        if relationship_recipe is not None and type(relationship_recipe) is not RelationshipContextRecipe:raise MessageRelayUnavailable()
        # NX-023: an explicit strategy selects Context v6 (v2 core, or the v4 core when a
        # relationship recipe is also given, + Engine sections).
        if context_strategy is not None and (type(context_strategy) is not str
            or re.fullmatch(r'[a-z][a-z0-9_]{0,63}',context_strategy) is None):raise MessageRelayUnavailable()
        self.relationship_recipe=relationship_recipe
        self.context_strategy=context_strategy
        self.port=MessageRelayPort(route)
        self.route,self.source,self.planner=route,source,planner
        self.executor_token=executor_token
        self.vault=vault
        # Exactly one mode (D4): a single consumer's recipe (claim by consumer) or the deployment Run profile (claim_next over
        # the consumers enrolled for this relay, 0160).
        if (recipe is None)==(run_profile is None): raise MessageRelayUnavailable()
        self.recipe=None if recipe is None else validate_recipe(recipe)
        self.run_profile=None if run_profile is None else validate_run_profile(run_profile)
        if any(service._backend is not route._backend for service in (source,planner)): raise MessageRelayUnavailable()
        # NX-031 ladder: admission bound on the tenant's open message Run tasks; None keeps claiming as before.
        if max_backlog is not None and (type(max_backlog) is not int or not 1<=max_backlog<=10000): raise MessageRelayUnavailable()
        self.max_backlog=max_backlog

    def _backlog(self):
        session=self.route._session
        with self.route._backend._pool.connection() as db:
            return db.execute('select authz.nexloop_relay_backlog(%s,%s)',(session.token_digest,session.world)).fetchone()[0]

    def _create(self,type_name,message_id,properties,recipe=None):
        recipe=self.recipe if recipe is None else recipe
        intent='message-relay-'+hashlib.sha256(canonical_payload([message_id,type_name,recipe]).encode()).hexdigest()
        result=self.planner.create_object(action_name=type_name+'.create',action_version=1,
            intent_id=intent,type_name=type_name,properties=properties)
        return result['object_id']

    def _handoff(self,message_id,fence,reason):
        """End this message on the relay path (0150): the reply guarantee answers it, the consumer's later messages proceed."""
        self.last_handoff=reason
        self.port.call('handoff',message_id=message_id,fence=fence,reason=reason)
        return 'handed_to_fallback'

    def run_once(self):
        claimed={}
        try:
            return self._run_once(claimed)
        except Exception:
            # Root cause of the stuck messages: a failed attempt kept its claim until the lease ran out and was then retried with
            # a changed command. A failed attempt now gives the message back with a bounded count; when the count is used up
            # the message is handed to the reply guarantee instead of blocking the consumer (0150).
            if claimed:
                try:
                    yielded=self.port.call('yield',message_id=claimed['message_id'],fence=claimed['fence'])
                    if yielded and yielded.get('handoff_due'):
                        return self._handoff(claimed['message_id'],claimed['fence'],'attempts_exhausted')
                except MessageRelayUnavailable:
                    pass
            raise MessageRelayUnavailable() from None

    def _run_once(self,claimed):
        # A Run credential lives 300 s from issuance: while the queue already holds max_backlog open Run tasks, issue no new
        # Run (the message stays unrouted and is claimed later), so a credential is never issued only to expire queued.
        if self.max_backlog is not None and self._backlog()>=self.max_backlog: return 'idle'
        try:
            if self.recipe is not None:
                recipe=self.recipe
                item=self.port.call('claim',consumer_id=recipe['consumer_id'],lease_seconds=30)
                if item is None: return 'idle'
            else:
                item=self.port.call('claim_next',lease_seconds=30)
                if item is None: return 'idle'
                recipe=recipe_from_profile(self.run_profile,item.get('profile'))
                if recipe['consumer_id']!=item['consumer_id']: raise MessageRelayUnavailable()
            self.last_consumer=recipe['consumer_id']
            message_id=item['message_id']; fence=item['fence']
            claimed.update(message_id=message_id,fence=fence)
            from nexloop_eios.context_runtime_bridge import ContextConversationRuntimeBridge
            bridge=ContextConversationRuntimeBridge(self.route)
            if item.get('route_fence') is not None:
                recovered=bridge._call('ack',message_id=message_id,fence=item['route_fence'],allow_missing=True)
                if recovered is not None:
                    self.port.call('release',message_id=message_id,fence=fence)
                    return 'queued'
            if datetime.fromisoformat(recipe['valid_until'])<=datetime.now(UTC): return self._handoff(message_id,fence,'requires_governed_replan')
            # A Run issued by an earlier attempt pins its command; once its credential expired it can never run (no reuse of it).
            pinned=self.port.call('lookup',message_id=message_id,fence=fence)
            if pinned is not None and pinned.get('state')=='requires_governed_replan': return self._handoff(message_id,fence,'credential_expired')
            principal=self.source._session.authentication.subject_principal_id
            executor=self.route._backend.authenticate(self.executor_token,world='real')._session.authentication.subject_principal_id
            goal=self._create('Goal',message_id,{'consumer_id':recipe['consumer_id'],'state':'active','valid_until':recipe['valid_until']},recipe)
            step=self._create('PlanStep',message_id,{'consumer_id':recipe['consumer_id'],'goal_id':goal,
                'control_id':recipe['control_id'],'submitter_principals':[principal],
                'action_name':'nexloop.service.request','state':'ready'},recipe)
            assignment={'message_id':message_id,'consumer_id':recipe['consumer_id'],
                'goal_id':goal,'goal_revision':1,'step_id':step,'step_revision':1,
                'control_id':recipe['control_id'],'control_revision':recipe['control_revision'],
                'consumer_revision':recipe['consumer_revision'],'source_principal':principal,
                'executor_principal':executor,'recipe_digest':hashlib.sha256(canonical_payload(recipe).encode()).hexdigest(),
                'allowed_actions':[EFFECT],'state':'active','valid_until':recipe['valid_until']}
            assignment_id=self._create('MessageAssignment',message_id,assignment,recipe)
            key=self.vault.message_key(item['tenant_id'],'real',message_id)
            digest=hashlib.sha256(canonical_payload(assignment).encode()).hexdigest()
            # The current command differs from the one an earlier attempt issued (for example the consumer's control revision
            # changed): dispatching under the old command would ignore the current controls, so the message is ended instead.
            if pinned is not None and pinned.get('assignment_digest') not in (None,digest): return self._handoff(message_id,fence,'binding_conflict')
            if self.vault.has(key) and self.vault.read(key).assignment_digest!=digest: return self._handoff(message_id,fence,'binding_conflict')
            record=self.vault.prepare(message_key=key,assignment_digest=digest)
            if record.state=='requires_governed_replan': return self._handoff(message_id,fence,'requires_governed_replan')
            source_digest,proofs,source_definition,source_capability=self.port.source_proofs(self.source)
            issued=self.port.call('issue',message_id=message_id,fence=fence,
                assignment_id=assignment_id,assignment_revision=1,assignment_digest=record.assignment_digest,assignment_text=canonical_payload(assignment),
                run_id=record.run_id,request_id=record.request_id,issuance_nonce=record.issuance_nonce,
                token_digest=record.token_digest,source_digest=source_digest,source_proofs=proofs,source_definition=source_definition,source_capability=source_capability,ttl_seconds=300)
            if issued['state']=='requires_governed_replan': return self._handoff(message_id,fence,'credential_expired')
            record=self.vault.mark_issued(message_key=key,run_id=issued['run_id'],token_digest=record.token_digest,expires_at=issued['expires_at'])
            self.planner.bind_effect_context(step_id=step,step_revision=1,goal_revision=1,
                consumer_revision=recipe['consumer_revision'],control_revision=recipe['control_revision'],
                run_id=record.run_id,run_token=record.token,executor_token=self.executor_token)
            command={'schema_version':'1.0','run_id':record.run_id,'tenant_id':item['tenant_id'],
                'world_id':'real','mode':'real','request_id':record.request_id,
                'trigger_event_id':canonical_event_id(item['tenant_id'],'real',item['source_event_id']),
                'role_ref':recipe['role_ref'],'consumer_ref':'consumer:'+recipe['consumer_id'],
                'goal_version_ref':'goal:'+goal+':revision:1:step:1:control:'+str(recipe['control_revision']),
                'context_manifest_ref':'artifact:context-bind-pending','runtime_profile':recipe['runtime_profile'],
                'credential_ref':'run:'+record.run_id,'budget':recipe['budget'],'not_after':record.expires_at,
                'runtime_owner_epoch':recipe['runtime_owner_epoch']}
            self._diagnostic_stage='context_prepare'
            if self.context_strategy is not None:
                from nexloop_eios.context_artifacts import ContextV6ArtifactProducer
                formal=None if self.relationship_recipe is None else {'Consumer':recipe['consumer_id'],'Goal':goal,'PlanStep':step,'EffectControl':recipe['control_id']}
                producer=ContextV6ArtifactProducer(self.source,self.context_strategy,self.relationship_recipe,formal)
                try:context=producer.prepare(message_id=message_id,run_token=record.token,command=command,offering_id=recipe['offering_id'],binding_id=recipe['offering_binding_id'])
                finally:self._context_diagnostic=producer.last_diagnostic
            elif self.relationship_recipe is None:
                context=self.source.prepare_message_context(message_id=message_id,run_token=record.token,command=command,offering_id=recipe['offering_id'],binding_id=recipe['offering_binding_id'])
            else:
                from nexloop_eios.relationship_context_artifacts import RelationshipContextArtifactProducer
                context=RelationshipContextArtifactProducer(self.source,self.relationship_recipe,{'Consumer':recipe['consumer_id'],'Goal':goal,'PlanStep':step,'EffectControl':recipe['control_id']}).prepare(message_id=message_id,run_token=record.token,command=command,offering_id=recipe['offering_id'],binding_id=recipe['offering_binding_id'])
            command['context_manifest_ref']=context['artifact_ref']
            bridge.bind_message(message_id=message_id,run_token=record.token,command=command,input=context['input'],queue=recipe['queue'])
            owned=self.port.call('own_route',message_id=message_id,fence=fence)
            run=self.route._backend.authenticate_run(record.token,world='real',run_id=record.run_id)._session
            bridge._call('authorize',message_id=message_id,fence=owned['route_fence'],run_proofs=bridge._authority._run_proofs(run))
            self._diagnostic_stage='runtime_accept'
            self.route.accept_runtime_event(queue=recipe['queue'],source_id='webchat',event_id=command['trigger_event_id'],
                run_token=record.token,command=command,input=context['input'])
            bridge._call('ack',message_id=message_id,fence=owned['route_fence'],allow_missing=False)
            self.port.call('release',message_id=message_id,fence=fence)
            return 'queued'
        except Exception as error:
            self._diagnostic_failure=(getattr(self,'_diagnostic_stage','before_context'),type(error).__name__)
            raise MessageRelayUnavailable() from None
