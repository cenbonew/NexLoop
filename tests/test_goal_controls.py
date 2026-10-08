"""NX-022 actual PostgreSQL Goal/KR versions, control revisions and budget gate.

Human owner = real password/browser session; Agent = real Agent invocation
credential; dispatcher/ingestion = service credential. All writes go through
the governed `Goal.*`/`Metric.*`/`Control.*`/`Budget.*` Actions or the
session-bound control functions; the admin connection only seeds authority
and inspects state.
"""
from datetime import UTC,datetime,timedelta
from decimal import Decimal
import json
import uuid
import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.goal_controls import ControlDenied,ControlPlane,GoalGovernedActions
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from authority_fixture import replace_fact
from multi_authority_fixture import seed_multi_authority
from goal_fixture import ACTIONS,authenticate_human,register_goal_actions,seed_agent_author,seed_human_owner,seed_service
from nexloop_eios.authorization import authenticate_service
from test_action_definitions import published_action
from test_browser_identity_reads import identity
from test_browser_session_creation import uow

NOW=datetime.now(UTC).replace(microsecond=0)
YEAR=(NOW-timedelta(days=30),NOW+timedelta(days=335))
QUARTER=(NOW-timedelta(days=10),NOW+timedelta(days=80))


class Env(dict):
    def __getattr__(self,name):return self[name]


@pytest.fixture
def env(identity,uow,published_action,admin):
    reader,base,capability=published_action
    register_goal_actions(admin,base,capability)
    browser=seed_human_owner(admin,reader.pool,list(ACTIONS),identity,uow)
    agent_token=seed_agent_author(admin,reader.pool,['Goal.propose','Goal.publish','Metric.approve','Control.set'])
    service_token=seed_service(admin,reader.pool,['Goal.publish','Control.set','Budget.set','Consumer.create'],suffix='-dispatcher')
    e=Env(reader=reader,service_token=service_token)
    def reauthenticate():
        # Seeding publishes authority metadata (new directory hash): sessions are
        # authenticated afterwards, exactly as a live deployment re-authenticates.
        e.human=authenticate_human(reader.pool,browser)
        e.agent=authenticate_service(reader.pool,agent_token,world='real')
        e.service=authenticate_service(reader.pool,service_token,world='real')
        e.owner=GoalGovernedActions(reader.pool,e.human,reader.signer)
        e.agent_actions=GoalGovernedActions(reader.pool,e.agent,reader.signer)
        e.service_actions=GoalGovernedActions(reader.pool,e.service,reader.signer)
        e.control=ControlPlane(reader.pool,e.service);e.agent_control=ControlPlane(reader.pool,e.agent)
    e.reauthenticate=reauthenticate;reauthenticate();owner=e.owner
    owner.approve_metric(action_name='Metric.approve',action_version=1,request_id='metric-renewal-1',metric_id='renewal-rate',version=1,
        name='Renewal rate',aggregation='ratio_of_sums',unit='ratio',maturity_seconds=3600,refund_rule='net_of_refunds',
        cohort_rule='subscriptions expiring in window; frozen cohort; 7d grace')
    owner.approve_metric(action_name='Metric.approve',action_version=1,request_id='metric-resolved-1',metric_id='resolved-problems',version=1,
        name='Resolved problems',aggregation='count',unit='problem',maturity_seconds=0,refund_rule='not_applicable',cohort_rule='verified resolutions')
    e.long=owner.publish_goal(action_name='Goal.publish',action_version=1,**goal('long-term','long_term',0,YEAR,target='0.80',request_id='long-v1'))
    e.stage=owner.publish_goal(action_name='Goal.publish',action_version=1,**goal('q4-stage','stage',0,QUARTER,target='0.70',request_id='stage-v1',parent={'goal_id':'long-term','version':1}))
    return e


def goal(goal_id,kind,expected,period,*,target,request_id,parent=None,kr_key='renewal',metric='renewal-rate'):
    return dict(request_id=request_id,goal_id=goal_id,goal_kind=kind,expected_current_version=expected,objective=f'{goal_id} objective',
        period_start=period[0],period_end=period[1],priority=2,parent=parent,budget={'model':'25.00'},constraints=['respect contact refusal'],
        change_summary='synthetic owner change',key_results=[dict(kr_key=kr_key,metric_id=metric,metric_version=1,target=target,
        direction='at_least',window_start=period[0],window_end=period[1])])


def agent_goal(goal_id,expected,*,request_id,parent_version=1,target='5'):
    fields=goal(goal_id,'agent',expected,QUARTER,target=target,request_id=request_id,parent={'goal_id':'q4-stage','version':parent_version},
        kr_key='resolved',metric='resolved-problems')
    fields.pop('goal_kind');fields['role_ref']='role:customer-success'
    return fields


def counts(admin):
    return admin.execute("select (select count(*) from control.nexloop_goal_versions),(select count(*) from control.nexloop_key_results),"
        "(select coalesce(max(revision),0) from control.nexloop_control_heads)").fetchone()


def test_owner_versions_are_immutable_and_record_impact(env,admin):
    proposal=env.agent_actions.propose_agent_goal(action_name='Goal.propose',action_version=1,**agent_goal('agent-cs',0,request_id='agent-cs-v1'))
    assert proposal['version']==1 and proposal['goal_id']=='agent-cs'
    row=admin.execute("select publisher_kind,parent_goal_id,parent_version,role_ref from control.nexloop_goal_versions where goal_id='agent-cs'").fetchone()
    assert row==('agent','q4-stage',1,'role:customer-success')
    v2=env.owner.publish_goal(action_name='Goal.publish',action_version=1,**goal('q4-stage','stage',1,QUARTER,target='0.75',request_id='stage-v2',parent={'goal_id':'long-term','version':1}))
    assert v2['version']==2 and v2['control_revision']>env.stage['control_revision']
    impact=v2['impact']
    assert impact['previous_version']==1 and impact['kr_changed']==['renewal'] and impact['kr_added']==[] and impact['kr_removed']==[]
    assert impact['children_requiring_realignment']==['agent-cs']
    assert admin.execute("select version,status from control.nexloop_goal_versions where goal_id='q4-stage' order by version").fetchall()==[(1,'superseded'),(2,'published')]
    # History stays explainable: v1 and its KR are still readable unchanged.
    old=env.control.read_goal('q4-stage',1)
    assert old['status']=='superseded' and old['key_results'][0]['target']=='0.70' and env.control.read_goal('q4-stage')['version']==2
    assert env.control.read_goal('agent-cs')['aligned'] is False
    # Published versions/KRs cannot be rewritten in place, not even by the table owner path.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("update control.nexloop_goal_versions set objective='rewritten' where goal_id='q4-stage' and version=2")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("update control.nexloop_key_results set target=0.1 where goal_id='q4-stage'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("delete from control.nexloop_goal_versions where goal_id='q4-stage' and version=1")
    with env.reader.pool.connection() as c:
        for table in ('nexloop_goal_versions','nexloop_key_results','nexloop_control_scopes','nexloop_budget_limits','nexloop_metric_definitions'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():c.execute(f'select * from control.{table}')


def test_at005_agent_cannot_change_parent_kr_metric_period_or_target(env,admin):
    before=counts(admin)
    parent_rewrite=goal('q4-stage','stage',1,(QUARTER[0],QUARTER[1]+timedelta(days=30)),target='0.10',request_id='agent-rewrites-parent',parent={'goal_id':'long-term','version':1})
    # Agent holds a current EXECUTE grant on Goal.publish, yet owner capability requires a human.
    with pytest.raises(psycopg.Error,match='human goal authority required'):
        env.agent_actions.publish_goal(action_name='Goal.publish',action_version=1,**parent_rewrite)
    # The Agent proposal Action cannot address an upper goal either.
    hijack=agent_goal('q4-stage',1,request_id='agent-hijack-stage')
    with pytest.raises(psycopg.Error,match='upper goal is immutable'):
        env.agent_actions.propose_agent_goal(action_name='Goal.propose',action_version=1,**hijack)
    # Statistical definition (MetricDefinition) and controls are owner-only.
    with pytest.raises(psycopg.Error,match='human goal authority required'):
        env.agent_actions.approve_metric(action_name='Metric.approve',action_version=1,request_id='agent-metric',metric_id='renewal-rate',version=2,
            name='Renewal rate',aggregation='count',unit='ratio',maturity_seconds=0,refund_rule='gross',cohort_rule='agent redefinition')
    with pytest.raises(psycopg.Error,match='human goal authority required'):
        env.agent_actions.set_control(action_name='Control.set',action_version=1,request_id='agent-pause',scope_kind='tenant',scope_ref='*',paused=False,reason='agent')
    # A service credential with grants is not a human owner either.
    with pytest.raises(psycopg.Error,match='human goal authority required'):
        env.service_actions.publish_goal(action_name='Goal.publish',action_version=1,**dict(parent_rewrite,request_id='service-rewrites-parent'))
    # Agent's own sub-goal cannot point at a non-current parent version.
    env.owner.publish_goal(action_name='Goal.publish',action_version=1,**goal('q4-stage','stage',1,QUARTER,target='0.75',request_id='stage-v2b',parent={'goal_id':'long-term','version':1}))
    with pytest.raises(psycopg.Error,match='parent goal version is not current'):
        env.agent_actions.propose_agent_goal(action_name='Goal.propose',action_version=1,**agent_goal('agent-late',0,request_id='agent-late',parent_version=1))
    assert admin.execute("select target,window_end from control.nexloop_key_results where goal_id='q4-stage' and goal_version=1").fetchone()[0]==Decimal('0.70')
    assert admin.execute("select count(*) from control.nexloop_metric_definitions where metric_id='renewal-rate'").fetchone()==(1,)
    assert counts(admin)[0]==before[0]+1  # only the human v2 was published


def test_owner_publish_requires_current_eios_grant_and_approved_metric(env,admin):
    before=counts(admin)
    with pytest.raises(psycopg.Error,match='approved MetricDefinition'):
        env.owner.publish_goal(action_name='Goal.publish',action_version=1,**goal('unmetered','long_term',0,YEAR,target='1',request_id='unmetered',metric='free-form-sql'))
    with pytest.raises(psycopg.Error,match='goal version conflict'):
        env.owner.publish_goal(action_name='Goal.publish',action_version=1,**goal('q4-stage','stage',0,QUARTER,target='0.9',request_id='stale-expected',parent={'goal_id':'long-term','version':1}))
    replace_fact(admin,'synthetic-a','grants',[env.human.authentication.subject_principal_id,'eios:action:Goal.publish:1'],F.GrantFacts,grants=[])
    env.reauthenticate()  # a fresh, valid human session without the EXECUTE grant
    with pytest.raises(Exception) as denied:
        env.owner.publish_goal(action_name='Goal.publish',action_version=1,**goal('q4-stage','stage',1,QUARTER,target='0.9',request_id='revoked-owner',parent={'goal_id':'long-term','version':1}))
    assert type(denied.value) is ActionAuthorizationDenied
    assert counts(admin)==before
    # The same human can still change controls where the grant is current.
    env.owner.set_control(action_name='Control.set',action_version=1,request_id='still-owner',scope_kind='role',scope_ref='role:sales',paused=True,reason='grant check')


def test_governed_replay_is_idempotent_and_payload_reuse_conflicts(env,admin):
    args=agent_goal('agent-replay',0,request_id='agent-replay-1')
    first=env.agent_actions.propose_agent_goal(action_name='Goal.propose',action_version=1,**args)
    again=env.agent_actions.propose_agent_goal(action_name='Goal.propose',action_version=1,**args)
    assert again=={'outcome_id':first['outcome_id'],'operation':'propose_agent_goal','replayed':True}
    changed=dict(args,target='9')
    with pytest.raises(Exception):env.agent_actions.propose_agent_goal(action_name='Goal.propose',action_version=1,**changed)
    assert admin.execute("select count(*) from control.nexloop_goal_versions where goal_id='agent-replay'").fetchone()==(1,)


def test_kr_from_approved_definition_real_matured_only(env,admin):
    def observe(oid,num,den,mode,at):
        return env.control.record_observation(metric_id='renewal-rate',metric_version=1,observation_id=oid,numerator=num,denominator=den,
            occurred_at=at,data_mode=mode,source_ref='commerce:'+oid,evidence_refs=['evidence:'+oid])
    old=NOW-timedelta(days=2)
    observe('real-1','8','10','real',old)
    observe('test-1','10','10','test',old)
    observe('fresh-1','0','10','real',NOW-timedelta(minutes=5))  # inside 1h maturity window
    assert observe('real-1','8','10','real',old)['replayed'] is True
    with pytest.raises(ControlDenied) as conflict:observe('real-1','9','10','real',old)
    assert conflict.value.reason=='observation_conflict'
    with pytest.raises(psycopg.Error,match='trusted service identity'):
        env.agent_control.record_observation(metric_id='renewal-rate',metric_version=1,observation_id='agent-fake',numerator='10',denominator='10',
            occurred_at=old,data_mode='real',source_ref='agent:fake')
    with pytest.raises(psycopg.Error):
        env.control.record_observation(metric_id='renewal-rate',metric_version=1,observation_id='sim-1',numerator='1',denominator='1',
            occurred_at=old,data_mode='simulation',source_ref='twin:1')
    kr=env.control.compute_key_result(goal_id='q4-stage',goal_version=1,kr_key='renewal')
    assert Decimal(kr['value'])==Decimal('0.8') and kr['met'] is True and kr['target']=='0.70'
    assert (kr['matured_observations'],kr['immature_observations'],kr['excluded_non_real_observations'])==(1,1,1)
    assert kr['refund_rule']=='net_of_refunds' and kr['aggregation']=='ratio_of_sums'


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_at007_goal_revision_and_customer_change_invalidate_old_plan(env,admin):
    from eios.ontology.definitions import ActionDefinition
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    from nexloop_eios.object_edits import GovernedObjectEditor
    proposal=env.agent_actions.propose_agent_goal(action_name='Goal.propose',action_version=1,**agent_goal('agent-plan',0,request_id='agent-plan-v1'))
    consumer=GovernedObjectCreator(env.reader.pool,env.service,env.reader.signer).create(action_name='Consumer.create',
        action_version=1,intent_id='plan-consumer',type_name='Consumer',properties={'preference':'service'})['object_id']
    # Governed Consumer.edit contract and an editor identity (seeding moves the directory hash).
    raw=admin.execute("select definition,capability from control.nexloop_action_definitions where resource_id='eios:action:Consumer.create:1'").fetchone()
    definition,capability=ActionDefinition.model_validate_json(json.dumps(raw[0])),CapabilityContractSnapshot.model_validate_json(json.dumps(raw[1]))
    edit=definition.model_copy(update={'stable_name':'Consumer.edit','capability_binding':definition.capability_binding.model_copy(update={'capability_name':'consumer.edit'}),'contract_digest':None})
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
        ('synthetic-a','real','eios:action:Consumer.edit:1',Jsonb(edit.model_dump(mode='json')),Jsonb(capability.model_copy(update={'capability_name':'consumer.edit'}).model_dump(mode='json'))))
    editor_session,_=seed_multi_authority(admin,env.reader.pool,[('eios:action:Consumer.edit:1',ResourceType.ACTION,Operation.EXECUTE),
        ('eios:object:Consumer/'+consumer,ResourceType.OBJECT,Operation.EDIT),('eios:property:Consumer/'+consumer+'/preference',ResourceType.PROPERTY,Operation.EDIT)],identity_suffix='-consumer-editor')
    env.reauthenticate();control=env.control
    planned=control.snapshot(scopes=[('consumer',consumer),('strategy','renewal-v1')],goals=['agent-plan'],objects=[('Consumer',consumer)])
    assert planned['goals']==[{'goal_id':'agent-plan','version':1}] and planned['objects'][0]['revision']==1
    assert control.assert_dispatch(planned)['snapshot_revision']==planned['control_revision']
    run=uuid.uuid4()
    assert control.bind_run(run_id=run,goal_id='agent-plan',goal_version=proposal['version'])['goal_version']==1
    # Customer state changes through a governed EIOS edit while the step waits.
    GovernedObjectEditor(env.reader.pool,editor_session,env.reader.signer).edit(action_name='Consumer.edit',action_version=1,intent_id='consumer-changed',
        type_name='Consumer',object_id=consumer,expected_revision=1,properties={'preference':'no-contact'})
    with pytest.raises(ControlDenied) as stale:control.assert_dispatch(planned)
    assert stale.value.reason=='object_revision_stale'
    replanned=control.snapshot(scopes=[('consumer',consumer)],goals=['agent-plan'],objects=[('Consumer',consumer)])
    control.assert_dispatch(replanned)
    # Owner revises the ancestor goal: the agent plan no longer dispatches.
    env.owner.publish_goal(action_name='Goal.publish',action_version=1,**goal('q4-stage','stage',1,QUARTER,target='0.75',request_id='stage-v2-plan',parent={'goal_id':'long-term','version':1}))
    with pytest.raises(ControlDenied) as goal_stale:control.assert_dispatch(replanned)
    assert goal_stale.value.reason=='goal_version_stale'
    with pytest.raises(ControlDenied):control.bind_run(run_id=uuid.uuid4(),goal_id='agent-plan',goal_version=1)
    with pytest.raises(ControlDenied):control.snapshot(goals=['agent-plan'])
    # Realignment: the Agent proposes v2 against the current parent and planning resumes.
    env.agent_actions.propose_agent_goal(action_name='Goal.propose',action_version=1,**agent_goal('agent-plan',1,request_id='agent-plan-v2',parent_version=2))
    fresh=control.snapshot(goals=['agent-plan'])
    assert fresh['goals']==[{'goal_id':'agent-plan','version':2}] and control.assert_dispatch(fresh)
    trace=admin.execute('select goal_id,goal_version from control.nexloop_run_goal_bindings where run_id=%s',(run,)).fetchall()
    assert trace==[('agent-plan',1)]


def test_at006_pause_rejects_queued_dispatch_and_resume_requires_reevaluation(env,admin):
    planned=env.control.snapshot(scopes=[('strategy','renewal-v1'),('consumer','c-1'),('action_type','message.send')],goals=['q4-stage'])
    unrelated=env.control.snapshot(scopes=[('strategy','onboarding-v1')])
    pause=env.owner.set_control(action_name='Control.set',action_version=1,request_id='pause-renewal',scope_kind='strategy',scope_ref='renewal-v1',paused=True,reason='owner review')
    assert pause['control_revision']>planned['control_revision']
    with pytest.raises(ControlDenied) as paused:env.control.assert_dispatch(planned)
    assert paused.value.reason=='control_paused'
    with pytest.raises(ControlDenied):env.control.snapshot(scopes=[('strategy','renewal-v1')])
    env.control.assert_dispatch(unrelated)  # other strategies keep dispatching
    env.owner.set_control(action_name='Control.set',action_version=1,request_id='resume-renewal',scope_kind='strategy',scope_ref='renewal-v1',paused=False,reason='review done')
    # Resume is not replay: the old queued item stays stale and must be re-evaluated.
    with pytest.raises(ControlDenied) as stale:env.control.assert_dispatch(planned)
    assert stale.value.reason=='control_revision_stale'
    env.control.assert_dispatch(env.control.snapshot(scopes=[('strategy','renewal-v1')],goals=['q4-stage']))
    env.owner.set_control(action_name='Control.set',action_version=1,request_id='pause-tenant',scope_kind='tenant',scope_ref='*',paused=True,reason='incident')
    with pytest.raises(ControlDenied) as tenant:env.control.assert_dispatch(unrelated)
    assert tenant.value.reason=='control_paused'
    events=admin.execute('select revision,event_kind,scope_kind,scope_ref from control.nexloop_control_events where event_kind in (%s,%s) order by revision',('pause','resume')).fetchall()
    assert [e[1:] for e in events]==[('pause','strategy','renewal-v1'),('resume','strategy','renewal-v1'),('pause','tenant','*')]
    # Only the dispatcher service identity may run the dispatch gate.
    with pytest.raises(psycopg.Error,match='trusted service identity'):env.agent_control.assert_dispatch(planned)


def test_dispatch_gate_inside_caller_transaction_orders_against_pause(env,admin):
    planned=env.control.snapshot(scopes=[('consumer','c-locked')])
    with env.reader.pool.connection() as c,c.transaction():
        env.control.assert_dispatch(planned,connection=c)
        held=admin.execute("select count(*) from pg_locks l join pg_class r on r.oid=l.relation where r.relname='nexloop_control_heads' and l.mode='RowShareLock'").fetchone()[0]
        assert held>=1
    env.owner.set_control(action_name='Control.set',action_version=1,request_id='pause-locked',scope_kind='consumer',scope_ref='c-locked',paused=True,reason='contact refusal')
    with env.reader.pool.connection() as c,c.transaction():
        with pytest.raises(ControlDenied):env.control.assert_dispatch(planned,connection=c)


def test_at043_tenant_budget_blocks_new_consumption_and_keeps_existing(env,admin):
    with pytest.raises(ControlDenied) as missing:
        env.control.reserve_budget(budget_kind='model',consumption_id='call-0',amount='0.01',unit='USD',source_ref='model-call:0')
    assert missing.value.reason=='budget_unconfigured'
    period=(NOW-timedelta(hours=1),NOW+timedelta(days=30))
    env.owner.set_budget(action_name='Budget.set',action_version=1,request_id='budget-model-1',budget_kind='model',unit='USD',limit_amount='1.00',period_start=period[0],period_end=period[1])
    planned=env.control.snapshot(budgets=['model'])
    assert env.control.reserve_budget(budget_kind='model',consumption_id='call-1',amount='0.60',unit='USD',source_ref='model-call:1')['remaining']=='0.40'
    env.control.reserve_budget(budget_kind='model',consumption_id='call-2',amount='0.30',unit='USD',source_ref='model-call:2')
    with pytest.raises(ControlDenied) as exhausted:
        env.control.reserve_budget(budget_kind='model',consumption_id='call-3',amount='0.20',unit='USD',source_ref='model-call:3')
    assert exhausted.value.reason=='budget_exhausted'
    assert env.control.reserve_budget(budget_kind='model',consumption_id='call-1',amount='0.60',unit='USD',source_ref='model-call:1')['replayed'] is True
    with pytest.raises(ControlDenied) as reuse:
        env.control.reserve_budget(budget_kind='model',consumption_id='call-1',amount='0.05',unit='USD',source_ref='model-call:1')
    assert reuse.value.reason=='budget_consumption_conflict'
    # Owner lowers the cap below what is already used: recorded legal consumption stays.
    env.owner.set_budget(action_name='Budget.set',action_version=1,request_id='budget-model-2',budget_kind='model',unit='USD',limit_amount='0.50',period_start=period[0],period_end=period[1])
    with pytest.raises(ControlDenied):
        env.control.reserve_budget(budget_kind='model',consumption_id='call-4',amount='0.01',unit='USD',source_ref='model-call:4')
    status=env.control.budget_status('model')
    assert status['exhausted'] is True and status['used']=='0.90' and status['consumption_count']==2
    assert admin.execute("select consumption_id,amount::text from control.nexloop_budget_consumption order by 1").fetchall()==[('call-1','0.60'),('call-2','0.30')]
    with pytest.raises(ControlDenied) as stale:env.control.assert_dispatch(planned)  # budget change advances control revision
    assert stale.value.reason=='control_revision_stale'
    # Incentive budget: ISO currency minor units, integers only.
    env.owner.set_budget(action_name='Budget.set',action_version=1,request_id='budget-incentive-1',budget_kind='incentive',unit='CNY',limit_amount='1000',period_start=period[0],period_end=period[1])
    with pytest.raises(psycopg.Error,match='unit mismatch'):
        env.control.reserve_budget(budget_kind='incentive',consumption_id='coupon-1',amount='10.5',unit='CNY',source_ref='offer:1')
    env.control.reserve_budget(budget_kind='incentive',consumption_id='coupon-2',amount='1000',unit='CNY',source_ref='offer:2')
    with pytest.raises(ControlDenied):env.control.reserve_budget(budget_kind='incentive',consumption_id='coupon-3',amount='1',unit='CNY',source_ref='offer:3')
    with pytest.raises(psycopg.Error,match='trusted service identity'):
        env.agent_control.reserve_budget(budget_kind='incentive',consumption_id='agent-spend',amount='1',unit='CNY',source_ref='agent')
    with pytest.raises(psycopg.Error,match='append-only'):admin.execute("delete from control.nexloop_budget_consumption")


def test_tenant_isolation_of_goal_reads(env,admin):
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values('synthetic-b','active') on conflict do nothing")
    other,_=seed_multi_authority(admin,env.reader.pool,[('eios:action:Goal.publish:1',ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-other-tenant',tenant='synthetic-b')
    foreign=ControlPlane(env.reader.pool,other)
    with pytest.raises(psycopg.Error,match='goal unavailable'):foreign.read_goal('q4-stage')
    assert foreign.snapshot()['control_revision']==0
    with pytest.raises(psycopg.Error,match='key result unavailable'):foreign.compute_key_result(goal_id='q4-stage',goal_version=1,kr_key='renewal')
    assert admin.execute("select count(*) from control.nexloop_goals where tenant_id='synthetic-b'").fetchone()==(0,)
