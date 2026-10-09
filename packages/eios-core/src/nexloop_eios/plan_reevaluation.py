"""NX-024 strategy / plan / reevaluation scheduling (docs/implementation/NX-024-design.md).

Plans are the Agent's working state (runtime schema, append-only, 0106), never formal business facts.
A change that may invalidate a plan (goal version, pause / resume, control revision, dispatch denial,
strategy change, reassess_at, external result) marks the plan in the 'plan-reevaluate' work feed in the
same transaction. ``PlanReevaluationWorker`` claims due plans and first runs a deterministic SQL precheck
(no model call): closed / paused / invalidated plans end there. Only a plan that really needs judgement
gets one bounded reevaluation Run (a governed Role Run with a v6 Context strategy) through the injected
launcher; its Host records the run-outcome contract back through the guard (``PlanOutcomePort``). The
self-trigger window and cap, the minimum reassess interval and the reevaluation Run budget come from the
deployment configuration (deploy/configuration/plan-reevaluation.v1.json), never from code.
"""
from dataclasses import dataclass
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re
import uuid

import psycopg

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import authority_request_scoped,resolve_authority
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied,retry_delay

FEED='plan-reevaluate'
SETTINGS_SCHEMA='nexloop-plan-reevaluation/1'
REEVALUATE_ACTION='eios:action:nexloop.plan.reevaluate:1'
OUTCOME_ACTION='eios:action:nexloop.plan.outcome:1'
_SETTINGS_KEYS={'schema_version','decision','self_window_seconds','self_trigger_cap','min_reassess_seconds','run_budget','context_strategy',
    'runtime_profile','run_ttl_seconds','batch','lease_seconds','max_attempts','retry_base_seconds'}
_BUDGET_KEYS={'maximum_model_turns','maximum_tool_calls','active_timeout_seconds','maximum_cost','currency'}
_LIMITS={'maximum_model_turns':64,'maximum_tool_calls':128,'active_timeout_seconds':3600}


class PlanUnavailable(PermissionError):
    def __init__(self,message='plan port unavailable'):super().__init__(message)


def load_settings(path):
    """Deployment configuration; every value is bounded, nothing defaults silently."""
    value=json.loads(Path(path).read_text())
    if type(value) is not dict or set(value)!=_SETTINGS_KEYS or value['schema_version']!=SETTINGS_SCHEMA:raise ValueError('plan reevaluation settings shape')
    for key,low,high in (('self_window_seconds',1,86400),('self_trigger_cap',1,100),('min_reassess_seconds',0,86400),('run_ttl_seconds',30,300),
            ('batch',1,50),('lease_seconds',10,600),('max_attempts',1,20),('retry_base_seconds',1,3600)):
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError('plan reevaluation setting '+key)
    budget=value['run_budget']
    if type(budget) is not dict or set(budget)!=_BUDGET_KEYS:raise ValueError('plan reevaluation run budget')
    for key,high in _LIMITS.items():
        if type(budget[key]) is not int or not 1<=budget[key]<=high:raise ValueError('plan reevaluation run budget '+key)
    if type(budget['maximum_cost']) is not str or re.fullmatch(r'\d+(?:\.\d{1,8})?',budget['maximum_cost']) is None or re.fullmatch('[A-Z]{3}',str(budget['currency'])) is None:
        raise ValueError('plan reevaluation run budget cost')
    if type(value['context_strategy']) is not str or re.fullmatch(r'[a-z][a-z0-9_]{0,63}',value['context_strategy']) is None:raise ValueError('context strategy')
    if value['runtime_profile'] not in ('deterministic-test','deepseek-flash'):raise ValueError('runtime profile')
    if type(value['decision']) is not str or not value['decision'].strip():raise ValueError('decision')
    return value


def plan_settings(settings):
    """The part of the configuration a plan carries (SQL enforces it on later triggers)."""
    return {k:settings[k] for k in ('self_window_seconds','self_trigger_cap','min_reassess_seconds')}


def run_budget(settings,steps):
    """Reevaluation Run budget: the smaller of the configured budget and the plan's largest step budget."""
    budget=dict(settings['run_budget'])
    for key in _LIMITS:
        largest=max((s['budget'][key] for s in steps),default=budget[key])
        budget[key]=min(budget[key],largest)
    return budget


class _SignedPort:
    PROTOCOL=None;ACTION=None;FUNCTION=None

    def __init__(self,pool,session,signer):
        if session.run_context is not None:raise PlanUnavailable()
        self.pool,self.session,self.signer=pool,session,signer

    def _signed(self,payload):
        entries=[]
        query=self.session.query(resource_id=self.ACTION,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        try:decision=AuthorizationDecisionService().decide_resolved(resolve_authority(self.pool,self.session,query,entries))
        except Exception:raise PlanUnavailable() from None
        if not decision.allowed or not decision.authoritative or decision.obligations:raise PlanUnavailable()
        body=canonical_payload(payload);auth=self.session.authentication
        claims={'protocol':self.PROTOCOL,'key_id':self.signer.key_id,'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,
            'credential_id':auth.credential_id,'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':self.ACTION,
            'action_resource':self.ACTION,'operation':'execute','expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,(self.PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        try:
            with self.pool.connection() as db,db.transaction():
                verify_application_role(db)
                return db.execute(f'select authz.{self.FUNCTION}(%s,%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]
        except psycopg.errors.InsufficientPrivilege:raise PlanUnavailable() from None


class PlanPort(_SignedPort):
    """Reevaluator service port (eios:action:nexloop.plan.reevaluate:1)."""
    PROTOCOL='nexloop-plan-command-v1';ACTION=REEVALUATE_ACTION;FUNCTION='nexloop_plan_command'

    def establish(self,plan):return self._signed({'verb':'establish','plan':plan})
    def read(self,plan_id):return self._signed({'verb':'read','plan_id':str(plan_id)})
    def precheck(self,plan_id,triggers=()):return self._signed({'verb':'precheck','plan_id':str(plan_id),'triggers':list(triggers)})
    def mark(self,*,plan_id,version,status,reason):return self._signed({'verb':'mark','plan_id':str(plan_id),'version':version,'status':status,'reason':reason})
    def link_run(self,*,plan_id,version,run_id,triggers):
        return self._signed({'verb':'link_run','plan_id':str(plan_id),'version':version,'run_id':str(run_id),'triggers':list(triggers)[-16:]})


class PlanOutcomePort(_SignedPort):
    """Runtime worker side of the Host's run-outcome tool (eios:action:nexloop.plan.outcome:1)."""
    PROTOCOL='nexloop-plan-outcome-v1';ACTION=OUTCOME_ACTION;FUNCTION='nexloop_record_plan_outcome'

    def record(self,*,activation_ref,command,outcome):
        from nexloop_eios.contracts import RunOutcome
        if not isinstance(activation_ref,str) or re.fullmatch(r'activation_[a-f0-9-]{36}',activation_ref) is None:raise ValueError('activation reference')
        RunOutcome.model_validate(outcome)  # exact run-outcome 1.0 shape before anything is signed
        return self._signed({'activation_ref':activation_ref,'command_text':canonical_payload(command),'outcome':outcome})


@dataclass
class LaunchedRun:
    run_id:str
    token:object
    expires_at:datetime


class PlanReevaluationWorker:
    """Leases due plans; deterministic precheck first; at most one bounded Run per needed reevaluation."""

    def __init__(self,pool,session,signer,*,settings,launcher):
        self.feed=WorkFeed(pool,session,signer,feed=FEED)
        self.port=PlanPort(pool,session,signer)
        self.settings,self.launcher=settings,launcher

    def _retry(self,item,code,delay=None,max_attempts=None):
        return self.feed.retry(item_key=item['item_key'],fence=item['fence'],code=code,
            delay_seconds=retry_delay(item['attempts'],self.settings['retry_base_seconds']) if delay is None else delay,
            max_attempts=max_attempts or self.settings['max_attempts'])

    @authority_request_scoped
    def run_once(self):
        summary={'closed':0,'paused':0,'invalidated':0,'throttled':0,'launched':0,'retry':0,'dead_lettered':0,'changed':0,'lease_lost':0}
        for item in self.feed.claim(limit=self.settings['batch'],lease_seconds=self.settings['lease_seconds']):
            plan_id=item['payload']['plan_id'];triggers=item['payload'].get('triggers',[])
            try:
                decision=self.port.precheck(plan_id,triggers)
                kind=decision['decision']
                if kind=='throttled':
                    # Self-caused triggers only: wait for the window instead of counting it as a failure.
                    status=self._retry(item,'self_trigger_throttled',delay=min(3600,decision['retry_after_seconds']),max_attempts=20)
                    summary['throttled' if status=='pending' else status]+=1;continue
                if kind=='invalidated':
                    self.port.mark(plan_id=plan_id,version=decision['version'],status='invalidated',reason=', '.join(decision['reasons'])[:500])
                elif kind=='reevaluate':
                    budget=run_budget(self.settings,decision['plan_steps']) if 'plan_steps' in decision else self.settings['run_budget']
                    run=self.launcher.issue(decision,budget)
                    self.port.link_run(plan_id=plan_id,version=decision['version'],run_id=run.run_id,triggers=triggers)
                    self.launcher.activate(run,decision,budget,triggers)
                outcome='launched' if kind=='reevaluate' else kind
            except WorkFeedDenied:raise
            except Exception as error:
                code='denied' if isinstance(error,PermissionError) else 'launch_failed' if 'launch' in type(error).__name__.lower() else 'reevaluation_failed'
                status=self._retry(item,code)
                summary['retry' if status=='pending' else status]+=1;continue
            status=self.feed.complete(item_key=item['item_key'],fence=item['fence'])
            summary[outcome if status=='completed' else status]+=1
        return summary


class RoleRunLauncher:
    """Bounded reevaluation Run as a governed Role Run on the plan's PlanStep, Context v6.

    ``issue`` is the production Role issuance (role_policies.issue_role_run): the Source atomically issues
    the Run credential and binds it to the Role's current execution ceiling and assignment scope, both read
    by the Source itself, with the reevaluation budget as the Run's policy budget (the ceiling must allow it).
    The plan links the Run before anything runs. ``activate`` binds the Planner effect context at the current
    object revisions and activates the Role plan with the configured Context strategy and the same budget.
    ``source``, ``queue_service``, ``planner`` and ``executor_token`` may be callables returning the current
    session / credential (deployed processes re-read and re-authenticate on every use).
    """

    def __init__(self,*,source,queue_service,planner,executor_token,settings,effect_action,queue='operations'):
        self._source,self._queue_service,self._planner,self.executor_token=source,queue_service,planner,executor_token
        self.settings,self.effect_action,self.queue=settings,effect_action,queue

    @staticmethod
    def _current(value):return value() if callable(value) else value

    def issue(self,decision,budget):
        from nexloop_eios.role_policies import issue_role_run
        source=self._current(self._source);recipe=decision['plan']['recipe']
        role=source.read_object(type_name='RoleDefinition',object_id=recipe['role_id'],fields=('ceiling_ref',))
        link=source.read_object(type_name='ConsumerRoleLink',object_id=recipe['link_id'],fields=('scope',))
        selected=dict(role_id=recipe['role_id'],link_id=recipe['link_id'],consumer_id=decision['plan']['consumer_id'],step_id=recipe['step_id'])
        return issue_role_run(source,action_resources=[self.effect_action],role_parameters=selected,
            policy_parameters=dict(selected,goal_id=decision['goal_object_id'],ceiling_id=_property(role,'ceiling_ref'),scope_id=_property(link,'scope'),budget=budget),
            ttl_seconds=self.settings['run_ttl_seconds'])

    def activate(self,run,decision,budget,triggers):
        from nexloop_eios.role_activation import activate_role_plan
        source,queue_service,planner=(self._current(x) for x in (self._source,self._queue_service,self._planner))
        plan=decision['plan'];recipe=plan['recipe'];revisions=decision['revisions']
        planner.bind_effect_context(step_id=recipe['step_id'],step_revision=revisions['PlanStep'],goal_revision=revisions['Goal'],
            consumer_revision=revisions['Consumer'],control_revision=revisions['EffectControl'],run_id=run.run_id,run_token=run.token,executor_token=self._current(self.executor_token))
        command={'schema_version':'1.0','run_id':run.run_id,'tenant_id':source._session.authentication.tenant_id,'world_id':'real','mode':'real',
            'request_id':'plan-reevaluate-'+run.run_id,'trigger_event_id':str(uuid.uuid4()),'role_ref':'role:pending','consumer_ref':'consumer:'+plan['consumer_id'],
            # The Role Run's formal Goal object at the bound revisions (the Role core reads it); the plan's NX-022 goal
            # version is enforced by the precheck control snapshot and by the outcome write (plan version current).
            'goal_version_ref':f"goal:{decision['goal_object_id']}:revision:{revisions['Goal']}:step:{revisions['PlanStep']}:control:{revisions['EffectControl']}",
            'context_manifest_ref':'artifact:context-bind-pending','runtime_profile':self.settings['runtime_profile'],
            'credential_ref':'run:'+run.run_id,'budget':budget,'not_after':run.expires_at.isoformat(),'runtime_owner_epoch':1}
        kinds=sorted({t.get('kind','unknown') for t in triggers}) or ['scheduled']
        body=('复评计划 '+plan['plan_id']+' v'+str(plan['version'])+'：'+', '.join(kinds)+'；先读取最新事实与授权，'
            '可输出 no_action / needs_information / waiting_external / escalate / plan_update / action_intent。')[:8192]
        return activate_role_plan(source=source,queue_service=queue_service,run=run,command=command,body=body,queue=self.queue,
            consumer_id=plan['consumer_id'],role_id=recipe['role_id'],link_id=recipe['link_id'],step_id=recipe['step_id'],
            offering_id=recipe['offering_id'],binding_id=recipe['binding_id'],control_id=recipe['control_id'],
            context_strategy=self.settings['context_strategy'])


def _property(projection,name):
    value=(projection.get('properties') or {}).get(name) if isinstance(projection,dict) else None
    if not isinstance(value,str) or not value:raise PermissionError('Role policy reference unavailable')
    return value
