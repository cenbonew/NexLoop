"""NX-022 Goal/KR versions and owner control revisions.

Writes go only through one restricted SQL function behind a published, governed
Action and a current EIOS EXECUTE decision; owner capabilities additionally
require a human subject. Read/check helpers derive tenant and world from the
authenticated session, never from a caller payload. KR values come from an
approved MetricDefinition's fixed aggregation; no caller SQL is executed.
"""
from datetime import UTC,datetime,timedelta
from decimal import Decimal,InvalidOperation
import hmac
import re
import uuid

import psycopg

from eios.actions import models as M
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.action_governor import govern_published_action
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload

PROTOCOL='nexloop-goal-governed-v1'
CAPABILITIES={'approve_metric':'goals.metric.approve','publish_goal':'goals.version.publish',
    'propose_agent_goal':'goals.agent.propose','set_control':'goals.control.set','set_budget':'goals.budget.set',
    'release_contact_restriction':'goals.contact.release',
    # NX-026: human-only commitment Actions (SQL refuses services and Agents even with a grant).
    'cancel_commitment':'commitment.cancel','extend_commitment':'commitment.extend','attest_commitment':'commitment.attest',
    'commitment_condition_met':'commitment.condition_met','mark_commitment_communication':'commitment.mark_communication',
    # NX-028 slice 2 (0150 registry): human requests.
    'request_plan_reevaluation':'plan.request_reevaluation','request_effect_query':'service.query_request',
    # NX-028 slice 3 (0152): human takeover and hand-back.
    'take_over_conversation':'conversation.takeover','hand_back_conversation':'conversation.handback',
    # NX-028 ruling B (0153): a staff reply written during the author's own takeover.
    'send_staff_reply':'message.staff_send',
    # NX-027 on the registry (0140): commitment commercial binding and operator-entered costs, human only.
    'bind_commitment_commercial':'commitment.bind_commercial','record_cost':'cost.record',
    # NX-029 slice 2 (0142): erasure and retention holds, human owner only.
    'erase_consumer':'consumer.erase','erase_message':'message.erase','hold_retention':'retention.hold','release_retention_hold':'retention.release_hold',
    # NX-030: the owner silences one alert rule (optionally one selector) for at most 7 days.
    'silence_alert':'alert.silence'}
_ID=re.compile(r'[a-z0-9][a-z0-9._-]{0,127}')
_KR=re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
_REF=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}')
_DECIMAL=re.compile(r'-?\d{1,18}(\.\d{1,12})?')

# Stable reasons the dispatch/budget checks report. A stale control is not a
# failure to retry blindly: the queued work must be re-evaluated.
REASONS={'NXC01':'control_paused','NXC02':'control_revision_stale','NXC03':'goal_version_stale',
    'NXC04':'object_revision_stale','NXC05':'contact_restricted','NXB01':'budget_exhausted','NXB02':'budget_consumption_conflict',
    'NXB03':'budget_unconfigured','NXM01':'observation_conflict'}


def goal_version_ref(goal_id,version):
    """Contract `goal_version_ref` (run-command/action-intent/context-manifest)."""
    if not _ID.fullmatch(str(goal_id)) or type(version) is not int or version<1:raise ValueError('goal id/version required')
    return f'goal:{goal_id}@{version}'


def parse_goal_version_ref(ref):
    match=re.fullmatch(r'goal:([a-z0-9][a-z0-9._-]{0,127})@([1-9][0-9]{0,8})',ref) if type(ref) is str else None
    if match is None:raise ValueError('goal version reference required')
    return match.group(1),int(match.group(2))


def _nx022_goal(ref):
    try:return parse_goal_version_ref(ref)
    except ValueError:return None  # not an NX-022 managed goal reference


class ControlDenied(Exception):
    def __init__(self,reason):
        super().__init__(reason)
        self.reason=reason


def _utc(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset()!=timedelta(0):
        raise ValueError('aware UTC datetime required')
    return value.astimezone(UTC).isoformat()


def _decimal(value):
    if type(value) is int:value=str(value)
    if type(value) is Decimal:value=format(value,'f')
    if type(value) is not str or not _DECIMAL.fullmatch(value):
        raise ValueError('decimal amount as string/Decimal/int required; binary floats are rejected')
    try:Decimal(value)
    except InvalidOperation:raise ValueError('invalid decimal') from None
    return value


def _key_results(items):
    if type(items) not in (list,tuple) or not 1<=len(items)<=16:raise ValueError('1..16 key results required')
    out=[];seen=set()
    for kr in items:
        if type(kr) is not dict or set(kr)!={'kr_key','metric_id','metric_version','target','direction','window_start','window_end'}:
            raise ValueError('complete key result required')
        if not _KR.fullmatch(str(kr['kr_key'])) or kr['kr_key'] in seen:raise ValueError('unique key result key required')
        if not _ID.fullmatch(str(kr['metric_id'])) or type(kr['metric_version']) is not int or kr['metric_version']<1:
            raise ValueError('approved metric reference required')
        if kr['direction'] not in ('at_least','at_most'):raise ValueError('direction must be at_least or at_most')
        seen.add(kr['kr_key'])
        out.append({'kr_key':kr['kr_key'],'metric_id':kr['metric_id'],'metric_version':kr['metric_version'],'target':_decimal(kr['target']),
            'direction':kr['direction'],'window_start':_utc(kr['window_start']),'window_end':_utc(kr['window_end'])})
    return out


def goal_payload(*,request_id,operation,goal_id,goal_kind,expected_current_version,objective,period_start,period_end,
                 priority,key_results,change_summary,parent=None,budget=None,constraints=(),role_ref=None):
    if operation not in ('publish_goal','propose_agent_goal'):raise ValueError('goal operation required')
    if not _ID.fullmatch(str(goal_id)):raise ValueError('invalid goal id')
    if type(expected_current_version) is not int or expected_current_version<0:raise ValueError('expected version required')
    if type(priority) is not int or not 1<=priority<=5:raise ValueError('priority 1..5 required')
    if type(objective) is not str or not 1<=len(objective)<=4000 or type(change_summary) is not str or not 1<=len(change_summary)<=2000:
        raise ValueError('objective and change summary required')
    if parent is not None and (type(parent) is not dict or set(parent)!={'goal_id','version'} or type(parent['version']) is not int):
        raise ValueError('parent must name goal_id and version')
    budget={} if budget is None else budget
    if type(budget) is not dict or any(type(k) is not str for k in budget):raise ValueError('budget object required')
    budget={k:_decimal(v) for k,v in budget.items()}
    if any(type(c) is not str or not 1<=len(c)<=500 for c in constraints) or len(constraints)>32:raise ValueError('constraints must be short strings')
    if role_ref is not None and not _REF.fullmatch(role_ref):raise ValueError('invalid role reference')
    payload={'request_id':request_id,'operation':operation,'goal_id':goal_id,'goal_kind':goal_kind,
        'expected_current_version':expected_current_version,'parent':parent,'objective':objective,
        'period_start':_utc(period_start),'period_end':_utc(period_end),'priority':priority,'budget':budget,
        'constraints':list(constraints),'change_summary':change_summary,'key_results':_key_results(key_results)}
    if operation=='propose_agent_goal':payload['role_ref']=role_ref
    return payload


class GoalGovernedActions:
    """Submits goal/metric/control/budget changes as governed published Actions."""

    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    def approve_metric(self,*,action_name,action_version,request_id,metric_id,version,name,aggregation,unit,
                       maturity_seconds,refund_rule,cohort_rule,currency=None):
        if not _ID.fullmatch(str(metric_id)) or type(version) is not int or version<1:raise ValueError('metric id/version required')
        if aggregation not in ('count','sum','ratio_of_sums') or refund_rule not in ('net_of_refunds','gross','not_applicable'):
            raise ValueError('aggregation/refund rule must be registered values')
        if type(maturity_seconds) is not int or not 0<=maturity_seconds<=31622400:raise ValueError('maturity window required')
        if currency is not None and not re.fullmatch('[A-Z]{3}',currency):raise ValueError('ISO currency required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'approve_metric','metric_id':metric_id,
            'version':version,'name':name,'aggregation':aggregation,'unit':unit,'currency':currency,
            'maturity_seconds':maturity_seconds,'refund_rule':refund_rule,'cohort_rule':cohort_rule})

    def publish_goal(self,*,action_name,action_version,**fields):
        return self._submit(action_name,action_version,goal_payload(operation='publish_goal',**fields))

    def propose_agent_goal(self,*,action_name,action_version,**fields):
        return self._submit(action_name,action_version,goal_payload(operation='propose_agent_goal',goal_kind='agent',**fields))

    def set_control(self,*,action_name,action_version,request_id,scope_kind,scope_ref,paused,reason):
        if scope_kind not in ('tenant','role','consumer','strategy','action_type') or type(paused) is not bool:
            raise ValueError('control scope and boolean state required')
        if (scope_kind=='tenant')!=(scope_ref=='*') or (scope_ref!='*' and not _REF.fullmatch(str(scope_ref))):
            raise ValueError('invalid control scope reference')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'set_control','scope_kind':scope_kind,
            'scope_ref':scope_ref,'paused':paused,'reason':reason})

    def set_budget(self,*,action_name,action_version,request_id,budget_kind,unit,limit_amount,period_start,period_end):
        if budget_kind not in ('model','incentive'):raise ValueError('budget kind required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'set_budget','budget_kind':budget_kind,
            'unit':unit,'limit_amount':_decimal(limit_amount),'period_start':_utc(period_start),'period_end':_utc(period_end)})

    def release_contact_restriction(self,*,action_name,action_version,request_id,consumer_id,reason):
        # ADR-023 §2.4: only a human owner's governed Action releases a contact restriction (SQL enforces human).
        if not re.fullmatch(r'[a-f0-9]{64}',str(consumer_id)) or type(reason) is not str or not 1<=len(reason)<=500:
            raise ValueError('consumer id and reason required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'release_contact_restriction',
            'consumer_id':consumer_id,'reason':reason})

    def _commitment(self,action_name,action_version,request_id,operation,commitment_id,reason,**extra):
        if not re.fullmatch(r'[0-9a-f]{64}',str(commitment_id)) or type(reason) is not str or not 1<=len(reason)<=500:
            raise ValueError('commitment id and reason required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':operation,'commitment_id':commitment_id,'reason':reason,**extra})

    # NX-026 (D4, D8): commitment cancel / extend / attest / condition met / communication marking are human Actions only.
    def cancel_commitment(self,*,action_name,action_version,request_id,commitment_id,reason):
        return self._commitment(action_name,action_version,request_id,'cancel_commitment',commitment_id,reason)

    def extend_commitment(self,*,action_name,action_version,request_id,commitment_id,due_at,reason):
        return self._commitment(action_name,action_version,request_id,'extend_commitment',commitment_id,reason,due_at=_utc(due_at))

    def attest_commitment(self,*,action_name,action_version,request_id,commitment_id,occurred_at,reason):
        return self._commitment(action_name,action_version,request_id,'attest_commitment',commitment_id,reason,occurred_at=_utc(occurred_at))

    def commitment_condition_met(self,*,action_name,action_version,request_id,commitment_id,reason):
        return self._commitment(action_name,action_version,request_id,'commitment_condition_met',commitment_id,reason)

    def mark_commitment_communication(self,*,action_name,action_version,request_id,commitment_id,reason):
        return self._commitment(action_name,action_version,request_id,'mark_commitment_communication',commitment_id,reason)

    # NX-028 slice 2: a human asks for one plan reevaluation now, or for the result of an unknown effect (D7: the executor queries).
    def request_plan_reevaluation(self,*,action_name,action_version,request_id,plan_id,reason):
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',str(plan_id)) or type(reason) is not str or not 1<=len(reason)<=500:
            raise ValueError('plan id and reason required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'request_plan_reevaluation','plan_id':str(plan_id),'reason':reason})

    def request_effect_query(self,*,action_name,action_version,request_id,intent_id,reason):
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',str(intent_id)) or type(reason) is not str or not 1<=len(reason)<=500:
            raise ValueError('intent id and reason required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'request_effect_query','intent_id':str(intent_id),'reason':reason})

    # NX-028 slice 3 (D3): a person takes over a conversation or a consumer; hand-back ends it (D5).
    def take_over_conversation(self,*,action_name,action_version,request_id,scope_kind,scope_ref,reason,duration_seconds=None):
        if scope_kind not in ('conversation','consumer') or not re.fullmatch(r'[a-f0-9]{64}',str(scope_ref)) or type(reason) is not str or not 1<=len(reason)<=500:
            raise ValueError('takeover scope and reason required')
        payload={'request_id':request_id,'operation':'take_over_conversation','scope_kind':scope_kind,'scope_ref':scope_ref,'reason':reason}
        if duration_seconds is not None:
            if type(duration_seconds) is not int or duration_seconds<60:raise ValueError('duration_seconds')
            payload['duration_seconds']=duration_seconds
        return self._submit(action_name,action_version,payload)

    def hand_back_conversation(self,*,action_name,action_version,request_id,takeover_id,reason):
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',str(takeover_id)) or type(reason) is not str or not 1<=len(reason)<=500:
            raise ValueError('takeover id and reason required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'hand_back_conversation','takeover_id':str(takeover_id),'reason':reason})

    # NX-030 (0150): owner-only alert silence; alerts are still evaluated and recorded, only marked as silenced.
    def silence_alert(self,*,action_name,action_version,request_id,rule_id,selector,until,reason):
        if (not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',str(rule_id)) or (selector is not None and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',str(selector)))
                or type(reason) is not str or not 1<=len(reason)<=500):
            raise ValueError('rule, selector and reason required')
        until=until.isoformat() if hasattr(until,'isoformat') else str(until)
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'silence_alert','rule_id':rule_id,'selector':selector,
            'until':until,'reason':reason})

    def send_staff_reply(self,*,action_name,action_version,request_id,conversation_id,reply_to,text):
        if not re.fullmatch(r'[a-f0-9]{64}',str(conversation_id)) or not re.fullmatch(r'[a-f0-9]{64}',str(reply_to)) or type(text) is not str or not 1<=len(text)<=8192:
            raise ValueError('conversation, bound inbound message and text required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'send_staff_reply','conversation_id':conversation_id,
            'reply_to':reply_to,'text':text})

    # NX-027 (0121 handler): only verified signed events of this reference become the commitment's commercial evidence.
    def bind_commitment_commercial(self,*,action_name,action_version,request_id,commitment_id,connector_id,record_kind,external_id,statuses=None):
        if (not re.fullmatch(r'[a-f0-9]{64}',str(commitment_id)) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}',str(connector_id))
                or record_kind not in ('order','payment','renewal','refund') or type(external_id) is not str or not 1<=len(external_id)<=200):
            raise ValueError('commitment and commercial reference required')
        payload={'request_id':request_id,'operation':'bind_commitment_commercial','commitment_id':commitment_id,'connector_id':connector_id,
            'record_kind':record_kind,'external_id':external_id}
        if statuses is not None:
            if type(statuses) not in (list,tuple) or not statuses or any(type(x) is not str for x in statuses):raise ValueError('statuses')
            payload['statuses']=list(statuses)
        return self._submit(action_name,action_version,payload)

    # NX-027 (0120 handler): a service or labour cost entered by a person; a correction supersedes, never rewrites.
    def record_cost(self,*,action_name,action_version,request_id,cost_kind,amount,currency,amount_unit,occurred_at,units=None,consumer_id=None,
                    corrects_entry_id=None):
        if (cost_kind not in ('service','labour') or not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,8})?',str(amount))
                or not re.fullmatch(r'[A-Z]{3}',str(currency)) or amount_unit not in ('major','minor')):
            raise ValueError('cost kind, amount, currency and unit required')
        payload={'request_id':request_id,'operation':'record_cost','cost_kind':cost_kind,'amount':str(amount),'currency':currency,
            'amount_unit':amount_unit,'occurred_at':_utc(occurred_at)}
        if units is not None:payload['units']=str(units)
        if consumer_id is not None:payload['consumer_id']=str(consumer_id)
        if corrects_entry_id is not None:payload['corrects_entry_id']=str(corrects_entry_id)
        return self._submit(action_name,action_version,payload)

    # NX-029 (0142): erase a Consumer (blocking at once, purge by the keeper) or one Message; register or release a hold.
    def erase_consumer(self,*,action_name,action_version,request_id,consumer_id,reason):
        if not re.fullmatch(r'[a-f0-9]{64}',str(consumer_id)) or type(reason) is not str or not 1<=len(reason)<=500:
            raise ValueError('consumer and reason required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'erase_consumer','consumer_id':consumer_id,'reason':reason})

    def erase_message(self,*,action_name,action_version,request_id,message_id,reason):
        if not re.fullmatch(r'[a-f0-9]{64}',str(message_id)) or type(reason) is not str or not 1<=len(reason)<=500:
            raise ValueError('message and reason required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'erase_message','message_id':message_id,'reason':reason})

    def hold_retention(self,*,action_name,action_version,request_id,scope_kind,scope_ref,basis,valid_until=None):
        if scope_kind not in ('consumer','item_class') or type(scope_ref) is not str or type(basis) is not str or not 1<=len(basis)<=1000:
            raise ValueError('hold scope and basis required')
        payload={'request_id':request_id,'operation':'hold_retention','scope_kind':scope_kind,'scope_ref':scope_ref,'basis':basis}
        if valid_until is not None:payload['valid_until']=_utc(valid_until)
        return self._submit(action_name,action_version,payload)

    def release_retention_hold(self,*,action_name,action_version,request_id,hold_id,reason):
        if type(hold_id) is not str or not hold_id or type(reason) is not str or not 1<=len(reason)<=500:raise ValueError('hold and reason required')
        return self._submit(action_name,action_version,{'request_id':request_id,'operation':'release_retention_hold','hold_id':hold_id,'reason':reason})

    def _submit(self,action_name,action_version,payload):
        definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(action_name,action_version)
        if capability.capability_name!=CAPABILITIES[payload['operation']]:
            raise ActionAuthorizationDenied('Action capability does not match goal operation')
        now=datetime.now(UTC);ref=definition.reference();intent=payload['request_id']
        command=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,action_stable_name=action_name,idempotency_key=intent),
          binding=M.ClaimBindingPayload(invocation_id=intent,action_reference=ref,request_digest=M.canonical_request_digest(payload),
            capability_binding=capability.binding(),adapter_id='nexloop.core',target_system='postgres'),requested_at=now,lease_expires_at=now+timedelta(seconds=20))
        permit=govern_published_action(self.pool,self.session,self.signer,claim_request=command,request=payload)
        if type(permit) is M.TerminalOutcomeReference:
            if permit.status is not M.TerminalOutcomeStatus.SUCCEEDED:raise ActionAuthorizationDenied('Action has a non-success terminal outcome')
            return {'outcome_id':permit.outcome_id,'operation':payload['operation'],'replayed':True}
        target=resource_id(ResourceType.ACTION,action_name,action_version);entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ActionAuthorizationDenied('goal Action authorization denied')
        claims={'protocol':PROTOCOL,'key_id':self.signer.key_id,'tenant_id':self.session.authentication.tenant_id,
          'principal_id':self.session.authentication.subject_principal_id,'credential_id':self.session.authentication.credential_id,
          'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,'action_resource':target,'operation':'execute',
          'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'facts':sorted(entries,key=lambda row:(row['kind'],row['key'])),
          'permit':permit.model_dump(mode='json'),'definition':definition.model_dump(mode='json')}
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_goal_governed_action(%s,%s,%s,%s,%s)',
              (self.session.token_digest,self.session.world,text,signature,canonical_payload(payload))).fetchone()[0]


class ControlPlane:
    """Session-bound control reads and dispatch/budget checks.

    `assert_dispatch` and `reserve_budget` accept an optional open connection so
    a dispatcher can run the check inside its own dispatch transaction; the SQL
    share-locks the control head, ordering it against concurrent owner changes.
    """

    def __init__(self,pool,session):self.pool,self.session=pool,session

    def _call(self,sql,args,connection=None):
        def run(c):
            try:return c.execute(sql,(self.session.token_digest,self.session.world,*args)).fetchone()[0]
            except psycopg.Error as error:
                reason=REASONS.get(getattr(error,'sqlstate',None))
                if reason is None:raise
                raise ControlDenied(reason) from None
        if connection is not None:
            verify_application_role(connection)
            return run(connection)
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return run(c)

    def snapshot(self,*,scopes=(),goals=(),objects=(),budgets=()):
        request={'scopes':[{'kind':k,'ref':r} for k,r in scopes],'goals':list(goals),
            'objects':[{'type_name':t,'object_id':o} for t,o in objects],'budgets':list(budgets)}
        return self._call('select authz.nexloop_control_snapshot(%s,%s,%s::jsonb)',(canonical_payload(request),))

    def assert_dispatch(self,snapshot,*,connection=None):
        return self._call('select authz.nexloop_assert_dispatch_controls(%s,%s,%s::jsonb)',(canonical_payload(snapshot),),connection)

    def reserve_budget(self,*,budget_kind,consumption_id,amount,unit,source_ref,connection=None):
        return self._call('select authz.nexloop_reserve_budget(%s,%s,%s,%s,%s,%s,%s)',
            (budget_kind,consumption_id,_decimal(amount),unit,source_ref),connection)

    def budget_status(self,budget_kind):
        return self._call('select authz.nexloop_budget_status(%s,%s,%s)',(budget_kind,))

    def record_observation(self,*,metric_id,metric_version,observation_id,numerator,occurred_at,data_mode,source_ref,evidence_refs=(),denominator=None):
        if data_mode not in ('real','test','simulation'):raise ValueError('data mode required')
        body={'metric_id':metric_id,'metric_version':metric_version,'observation_id':observation_id,'numerator':_decimal(numerator),
            'denominator':None if denominator is None else _decimal(denominator),'occurred_at':_utc(occurred_at),'data_mode':data_mode,
            'source_ref':source_ref,'evidence_refs':list(evidence_refs)}
        return self._call('select authz.nexloop_record_metric_observation(%s,%s,%s::jsonb)',(canonical_payload(body),))

    def compute_key_result(self,*,goal_id,goal_version,kr_key,as_of=None):
        return self._call('select authz.nexloop_compute_key_result(%s,%s,%s,%s,%s,%s)',(goal_id,goal_version,kr_key,as_of))

    def bind_run(self,*,run_id,goal_id=None,goal_version=None,goal_ref=None):
        if goal_ref is not None:goal_id,goal_version=parse_goal_version_ref(goal_ref)
        return self._call('select authz.nexloop_bind_run_goal(%s,%s,%s,%s,%s)',(uuid.UUID(str(run_id)),goal_id,goal_version))

    def assert_intent_dispatch(self,intent_id,*,connection=None):
        """Effect admission: the latest submission snapshot of this intent (0097)."""
        return self._call('select authz.nexloop_assert_intent_dispatch_controls(%s,%s,%s)',(uuid.UUID(str(intent_id)),),connection)

    def assert_task_dispatch(self,task_id,*,connection=None):
        """Runtime task dispatch: control head at enqueue + scopes/goal of the stored Run command (0097)."""
        return self._call('select authz.nexloop_assert_task_dispatch_controls(%s,%s,%s)',(str(task_id),),connection)

    def prepare_run_dispatch(self,*,task_id,command):
        """Dispatch-time owner controls for one queued Run (NX-022).

        1. A Run whose goal_version_ref names an NX-022 goal (goal:<id>@<v>) is bound
           to that version (bind_run is idempotent; a superseded version is refused).
        2. The stored task snapshot is asserted (pause, stale goal chain, later
           relevant control events including model budget changes).
        3. When the tenant has a model budget configured, the Run's declared maximum
           cost is reserved once per Run (consumption id run:<run_id>); exhausted or
           unit mismatch refuses dispatch. No configured budget means no budget gate.
        Raises ControlDenied(reason) for control decisions.
        """
        goal=_nx022_goal(command.get('goal_version_ref'))
        if goal is not None:self.bind_run(run_id=command['run_id'],goal_id=goal[0],goal_version=goal[1])
        result=self.assert_task_dispatch(task_id)
        status=self.budget_status('model')
        if status.get('configured'):
            budget=command.get('budget') or {}
            try:
                self.reserve_budget(budget_kind='model',consumption_id='run:'+str(command['run_id']),amount=str(budget.get('maximum_cost')),
                    unit=str(budget.get('currency')),source_ref='run:'+str(command['run_id']))
            except ControlDenied:raise
            except (ValueError,psycopg.Error):raise ControlDenied('budget_unit_mismatch') from None
        return result

    def read_goal(self,goal_id,version=None):
        return self._call('select authz.nexloop_read_goal(%s,%s,%s,%s)',(goal_id,version))
