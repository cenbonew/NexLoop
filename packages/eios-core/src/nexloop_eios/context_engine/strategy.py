"""Versioned Context strategies: validation, governed human publication, current read.

A strategy only chooses budgets, quotas and the trim order of non-mandatory evidence.
It can never unpin mandatory sections, admit Claims into the formal zone, or turn on
hypotheses in the real world (ADR-019 decisions 2/5; docs/05 §2, §9).
"""
from dataclasses import dataclass
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re

from eios.actions import models as M
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.action_governor import govern_published_action
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.context_engine.authority import action_claims,run_assemble_claims,signed_read
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload

PROTOCOL='nexloop.context-pack.v6'
PUBLISH_ACTION='nexloop.context.strategy.publish'
PUBLISH_CAPABILITY='context.strategy.publish'
ASSEMBLE_ACTION='nexloop.context.assemble'
SECTIONS=('consumer_state','open_work','evidence','semantics','experience')
TRIM_STEPS=('duplicate_evidence','experience','hypotheses','semantics_low_relevance','older_conversation','older_claim_evidence','low_score_recall')
KEYS={'strategy_id','version','protocol','input_token_budget','output_reserve','framing_reserve','sections','trim_order',
      'semantic','include_hypotheses','include_rejected_definitions'}


class StrategyRejected(ValueError):
    pass


def context_strategy_object_type():
    """Published object type the strategy-publication Action declares as its change scope.

    Strategy rows live in control.nexloop_context_strategies; this metadata type only gives
    the governed Action a typed, digest-bound scope (EIOS requires one per Action).
    """
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    return ObjectTypeDefinition(type_name='ContextStrategy',version=1,only_edit_via_actions=True,display_name='Context 策略',
        description='Context 组装策略的不可变版本（预算、配额、裁剪顺序）',
        properties=tuple(PropertyDefinition(property_name=name,value_type=kind,required=True) for name,kind in
            (('strategy_id',PropertyValueType.STRING),('version',PropertyValueType.INTEGER),('definition_digest',PropertyValueType.STRING))))


def _int(value,low,high,name):
    if type(value) is not int or not low<=value<=high:raise StrategyRejected(name)
    return value


def validate(definition,*,world):
    d=definition
    if type(d) is not dict or set(d)!=KEYS:raise StrategyRejected('strategy keys')
    if type(d['strategy_id']) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}',d['strategy_id']):raise StrategyRejected('strategy_id')
    _int(d['version'],1,1000000,'version')
    if d['protocol']!=PROTOCOL:raise StrategyRejected('protocol')
    budget=_int(d['input_token_budget'],1024,1000000,'input_token_budget');_int(d['output_reserve'],128,200000,'output_reserve')
    if _int(d['framing_reserve'],0,budget,'framing_reserve')>budget-512:raise StrategyRejected('framing_reserve leaves no room for content')
    if type(d['sections']) is not dict or set(d['sections'])!=set(SECTIONS):raise StrategyRejected('sections')
    for name,quota in d['sections'].items():
        allowed={'max_items','recent_turns'} if name=='evidence' else {'max_items'}
        if type(quota) is not dict or set(quota)!=allowed:raise StrategyRejected('section '+name)
        _int(quota['max_items'],0,256,'max_items')
        if name=='evidence':_int(quota['recent_turns'],0,64,'recent_turns')
    if type(d['trim_order']) is not list or sorted(d['trim_order'])!=sorted(TRIM_STEPS):raise StrategyRejected('trim_order must order every step exactly once')
    sem=d['semantic']
    if type(sem) is not dict or set(sem)!={'enabled','top_k','ambiguity_margin'} or type(sem['enabled']) is not bool:raise StrategyRejected('semantic')
    _int(sem['top_k'],1,20,'top_k')
    if type(sem['ambiguity_margin']) not in (int,float) or isinstance(sem['ambiguity_margin'],bool) or not 0<=sem['ambiguity_margin']<=1:raise StrategyRejected('ambiguity_margin')
    for flag in ('include_hypotheses','include_rejected_definitions'):
        if type(d[flag]) is not bool:raise StrategyRejected(flag)
    # Hypotheses never reach a real-world Context (research/shadow worlds only).
    if d['include_hypotheses'] and world=='real':raise StrategyRejected('hypotheses are not admitted in the real world')
    if d['include_rejected_definitions']:raise StrategyRejected('rejected-definition Claims are never Context evidence')
    return d


def strategy_ref(definition):return f"context-strategy:{definition['strategy_id']}@{definition['version']}"


def load_builtins(path,*,world='real'):
    body=json.loads(Path(path).read_text())
    if type(body) is not dict or set(body)!={'schema_version','manifest_version','decision','strategies'} or body['schema_version']!='nexloop-context-strategies/1':
        raise StrategyRejected('strategy manifest shape')
    return [validate(item,world=world) for item in body['strategies']]


@dataclass(frozen=True)
class Strategy:
    ref:str
    definition:dict
    definition_digest:str


class StrategyRegistry:
    """Current strategy for the Context assembler (EXECUTE nexloop.context.assemble:1).

    With ``run`` (the Run credential this Source issued) the assemble authority is the
    one issued with that Run (0104); without it, the Source's own standing grant.
    """
    def __init__(self,pool,session,signer,run=None):self.pool,self.session,self.signer,self.run=pool,session,signer,run
    def get(self,strategy_id,version=None):
        payload={'strategy_id':strategy_id}|({} if version is None else {'version':version})
        claims=action_claims(self.pool,self.session,ASSEMBLE_ACTION) if self.run is None else run_assemble_claims(self.session,self.run)
        row=signed_read(self.pool,self.session,self.signer,'strategy',payload,claims=claims)
        if row is None:return None
        validate(row['definition'],world=self.session.world)
        # SQL computed the digest over its jsonb text rendering at publication.
        if hashlib.sha256(_pg_jsonb_text(row['definition']).encode()).hexdigest()!=row['definition_digest']:
            raise StrategyRejected('strategy digest mismatch')
        return Strategy(row['strategy_ref'],row['definition'],row['definition_digest'])


def _pg_jsonb_text(value):
    """PostgreSQL jsonb::text rendering (keys by length then bytes, ', ' and ': ')."""
    if isinstance(value,dict):
        keys=sorted(value,key=lambda k:(len(k.encode()),k.encode()))
        return '{'+', '.join(json.dumps(k,ensure_ascii=False)+': '+_pg_jsonb_text(value[k]) for k in keys)+'}'
    if isinstance(value,list):return '['+', '.join(_pg_jsonb_text(v) for v in value)+']'
    return json.dumps(value,ensure_ascii=False)


class StrategyPublisher:
    """Governed human Action; services and Agents are refused by SQL regardless of grants."""
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer
    def publish(self,definition,*,request_id,action_version=1):
        validate(definition,world=self.session.world)
        definition_reader=PostgresActionDefinitionReader(self.pool,self.session,self.signer)
        action,capability=definition_reader.get(PUBLISH_ACTION,action_version)
        if capability.capability_name!=PUBLISH_CAPABILITY:raise ActionAuthorizationDenied('strategy publication capability required')
        payload={'request_id':request_id,'strategy_id':definition['strategy_id'],'version':definition['version'],'definition':definition}
        now=datetime.now(UTC)
        command=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,action_stable_name=PUBLISH_ACTION,idempotency_key=request_id),
            binding=M.ClaimBindingPayload(invocation_id=request_id,action_reference=action.reference(),request_digest=M.canonical_request_digest(payload),
                capability_binding=capability.binding(),adapter_id='nexloop.core',target_system='postgres'),requested_at=now,lease_expires_at=now+timedelta(seconds=20))
        permit=govern_published_action(self.pool,self.session,self.signer,claim_request=command,request=payload)
        if type(permit) is M.TerminalOutcomeReference:
            if permit.status is not M.TerminalOutcomeStatus.SUCCEEDED:raise ActionAuthorizationDenied('strategy publication has a non-success outcome')
            return {'outcome_id':permit.outcome_id,'strategy_ref':strategy_ref(definition),'replayed':True}
        claims=action_claims(self.pool,self.session,PUBLISH_ACTION,action_version)
        claims.update(protocol='nexloop-context-strategy-v1',key_id=self.signer.key_id,permit=permit.model_dump(mode='json'),definition=action.model_dump(mode='json'))
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-context-strategy-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_context_strategy_publish(%s,%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,text,signature,canonical_payload(payload))).fetchone()[0]
