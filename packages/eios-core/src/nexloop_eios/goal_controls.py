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
    'propose_agent_goal':'goals.agent.propose','set_control':'goals.control.set','set_budget':'goals.budget.set'}
_ID=re.compile(r'[a-z0-9][a-z0-9._-]{0,127}')
_KR=re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
_REF=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}')
_DECIMAL=re.compile(r'-?\d{1,18}(\.\d{1,12})?')

# Stable reasons the dispatch/budget checks report. A stale control is not a
# failure to retry blindly: the queued work must be re-evaluated.
REASONS={'NXC01':'control_paused','NXC02':'control_revision_stale','NXC03':'goal_version_stale',
    'NXC04':'object_revision_stale','NXB01':'budget_exhausted','NXB02':'budget_consumption_conflict',
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
