"""ADR-023 §2.7: the fallback reply Run (dispatcher decision A, message path, migration 0110).

``FallbackReplyLauncher.start`` runs once per due, unsettled inbound message, from the reply worker:

1. governed per-message planning objects for the fallback (Goal, PlanStep, MessageAssignment) by the Planner, with
   intent keys distinct from the original message Run's (a separate step slot, never the original one);
2. issuance of the fallback Run through ``authz.nexloop_reply_fallback_command`` (at most one per message; replay of the
   same issuance only; refused once the pending reply is settled);
3. the Planner's effect-context bind, the fallback Context v6 (this message, recent conversation, pinned constraints),
   and acceptance into the durable runtime queue with the fixed, configured budget.

The Run's only effect is a reply bound to this message (enforced in SQL, 0110); the model writes the reply (an LLM in
production, a deterministic provider in tests). Nothing here sends anything itself.
"""
import hashlib
import json
import secrets
import uuid

from nexloop_eios.plan_reevaluation import _SignedPort
from nexloop_eios.postgres_artifacts import canonical_payload

FALLBACK_ACTION='eios:action:nexloop.reply.fallback:1'
EFFECT='eios:action:nexloop.service.request:1'


class FallbackUnavailable(RuntimeError):
    pass


class FallbackIssuePort(_SignedPort):
    PROTOCOL='nexloop-reply-fallback-v1';ACTION=FALLBACK_ACTION;FUNCTION='nexloop_reply_fallback_command'

    def issue(self,**parameters):return self._signed({'verb':'issue',**parameters})
    def lookup(self,message_id):return self._signed({'verb':'lookup','message_id':message_id})


def request_id(tenant,message_id):
    return 'reply-fallback-'+hashlib.sha256(json.dumps([tenant,'real',message_id],separators=(',',':')).encode()).hexdigest()


class FallbackReplyLauncher:
    """Launches the fallback reply Run of one message as the consumer's message relay identities would.

    ``route``, ``source`` and ``planner`` may be callables returning current sessions (re-authenticated per use);
    ``recipe`` is the consumer's message relay recipe; ``policy`` the reply-guarantee configuration.
    """

    def __init__(self,*,route,source,planner,executor_token,recipe,policy):
        from nexloop_eios.message_relay import validate_recipe
        self._route,self._source,self._planner,self._executor=route,source,planner,executor_token
        self.recipe=validate_recipe(recipe);self.policy=policy

    @staticmethod
    def _current(value):return value() if callable(value) else value

    def _create(self,planner,type_name,message_id,properties):
        intent='reply-fallback-'+hashlib.sha256(canonical_payload([message_id,type_name,self.recipe]).encode()).hexdigest()
        return planner.create_object(action_name=type_name+'.create',action_version=1,intent_id=intent,type_name=type_name,properties=properties)['object_id']

    def start(self,*,message_id,state):
        from nexloop_eios.context_artifacts import FallbackContextV6Producer
        from nexloop_eios.message_relay import MessageRelayPort
        route,source,planner=(self._current(x) for x in (self._route,self._source,self._planner))
        executor_token=self._current(self._executor)
        recipe=self.recipe;fallback=self.policy['fallback']
        if state.get('consumer_id')!=recipe['consumer_id']:raise FallbackUnavailable('recipe is for another consumer')
        tenant=source._session.authentication.tenant_id
        principal=source._session.authentication.subject_principal_id
        executor=route._backend.authenticate(executor_token,world='real')._session.authentication.subject_principal_id
        goal=self._create(planner,'Goal',message_id,{'consumer_id':recipe['consumer_id'],'state':'active','valid_until':recipe['valid_until']})
        step=self._create(planner,'PlanStep',message_id,{'consumer_id':recipe['consumer_id'],'goal_id':goal,'control_id':recipe['control_id'],
            'submitter_principals':[principal],'action_name':'nexloop.service.request','state':'ready'})
        assignment={'message_id':message_id,'consumer_id':recipe['consumer_id'],'goal_id':goal,'goal_revision':1,'step_id':step,'step_revision':1,
            'control_id':recipe['control_id'],'control_revision':recipe['control_revision'],'consumer_revision':recipe['consumer_revision'],
            'source_principal':principal,'executor_principal':executor,'recipe_digest':hashlib.sha256(canonical_payload(recipe).encode()).hexdigest(),
            'allowed_actions':[EFFECT],'state':'active','valid_until':recipe['valid_until']}
        assignment_id=self._create(planner,'MessageAssignment',message_id,assignment)
        assignment_text=canonical_payload(assignment)
        run_id=str(uuid.uuid4());token=secrets.token_hex(48);token_digest=hashlib.sha256(token.encode()).hexdigest()
        source_digest,proofs,definition,capability=MessageRelayPort(route).source_proofs(source)
        port=FallbackIssuePort(route._backend._pool,route._session,route._backend._signer)
        issued=port.issue(message_id=message_id,run_id=run_id,request_id=request_id(tenant,message_id),issuance_nonce=secrets.token_hex(32),
            assignment_id=assignment_id,assignment_revision=1,assignment_digest=hashlib.sha256(assignment_text.encode()).hexdigest(),assignment_text=assignment_text,
            token_digest=token_digest,source_digest=source_digest,source_proofs=proofs,source_definition=definition,source_capability=capability,
            ttl_seconds=fallback['run_ttl_seconds'])
        if issued['run_id']!=run_id:raise FallbackUnavailable('fallback already issued')
        planner.bind_effect_context(step_id=step,step_revision=1,goal_revision=1,consumer_revision=recipe['consumer_revision'],
            control_revision=recipe['control_revision'],run_id=run_id,run_token=token,executor_token=executor_token)
        command={'schema_version':'1.0','run_id':run_id,'tenant_id':tenant,'world_id':'real','mode':'real','request_id':issued['request_id'],
            'trigger_event_id':str(uuid.uuid5(uuid.NAMESPACE_URL,'nexloop:reply-fallback:'+tenant+':'+message_id)),
            'role_ref':recipe['role_ref'],'consumer_ref':'consumer:'+recipe['consumer_id'],
            'goal_version_ref':'goal:'+goal+':revision:1:step:1:control:'+str(recipe['control_revision']),
            'context_manifest_ref':'artifact:context-bind-pending','runtime_profile':fallback['runtime_profile'],'credential_ref':'run:'+run_id,
            'budget':dict(fallback['run_budget']),'not_after':issued['expires_at'],'runtime_owner_epoch':recipe['runtime_owner_epoch']}
        context=FallbackContextV6Producer(source,fallback['context_strategy']).prepare(message_id=message_id,run_token=token,command=command,
            offering_id=recipe['offering_id'],binding_id=recipe['offering_binding_id'])
        command['context_manifest_ref']=context['artifact_ref']
        route.accept_runtime_event(queue=recipe['queue'],source_id='reply-fallback',event_id=command['trigger_event_id'],run_token=token,command=command,input=context['input'])
        self.last={'run_id':run_id,'command':command,'input':context['input']}
        return 'started'
