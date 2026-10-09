"""NX-024 strategy / plan / reevaluation scheduling on clean catalog PostgreSQL.

Built on the governed runtime effect fixture (real Sources, Planner, Consumer / Goal / PlanStep / EffectControl
objects, runtime worker with actual activations). Owner control changes and goal versions go through the NX-022
internal state functions the governed owner Actions use (as in the NX-022 dispatch tests). Plan state is written
only through the signed ports; admin only seeds, injects faults and probes. Synthetic data.
"""
from datetime import UTC,datetime,timedelta
import json
import uuid

import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from psycopg.types.json import Jsonb
import runtime_effect_fixture as fixture
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from nexloop_eios.goal_controls import goal_payload
from nexloop_eios.plan_reevaluation import (LaunchedRun,PlanPort,PlanReevaluationWorker,PlanUnavailable,load_settings,plan_settings,run_budget)
from nexloop_eios.work_feed import WorkFeed
from test_context_v6_pg import STRATEGY,seed_strategy
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SETTINGS=load_settings(ROOT/'deploy/configuration/plan-reevaluation.v1.json')
GOAL='renewal-q4'
FEED_TARGET=('eios:action:NexLoop.feed.plan-reevaluate:1',)


def owner_change(admin,tenant,body,intent):
    with admin.transaction():
        return admin.execute("select control.nexloop_nx022_owner_change(%s,'real','synthetic-owner',%s,%s)",(tenant,intent,Jsonb(body))).fetchone()[0]


def publish_goal(admin,tenant,expected,label):
    now=datetime.now(UTC);start,end=now-timedelta(days=1),now+timedelta(days=60)
    if expected==0:
        owner_change(admin,tenant,{'operation':'approve_metric','metric_id':'renewal-rate','version':1,'name':'Renewal rate','aggregation':'ratio_of_sums',
            'unit':'ratio','currency':None,'maturity_seconds':3600,'refund_rule':'net_of_refunds','cohort_rule':'frozen cohort'},'nx024-metric')
    body=goal_payload(request_id='nx024-goal-'+label,operation='publish_goal',goal_id=GOAL,goal_kind='long_term',expected_current_version=expected,
        objective='Q4 续费，不为指标硬发消息',period_start=start,period_end=end,priority=3,change_summary='synthetic '+label,
        key_results=[dict(kr_key='renewal',metric_id='renewal-rate',metric_version=1,target='0.70',direction='at_least',window_start=start,window_end=end)])
    with admin.transaction():
        return admin.execute("select control.nexloop_nx022_write_goal(%s,'real','synthetic-owner','human',%s,%s,false)",(tenant,'nx024-goal-'+label,Jsonb(body))).fetchone()[0]


def reevaluator(plan,admin,suffix='-plan-reevaluator'):
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    token=fixture.seed_multi_uuid(admin,plan['tenant'],[('eios:action:NexLoop.feed.plan-reevaluate:1',ResourceType.ACTION,Operation.EXECUTE),
        ('eios:action:nexloop.plan.reevaluate:1',ResourceType.ACTION,Operation.EXECUTE)],suffix=suffix)
    return lambda:plan['backend_worker'].authenticate(token,world='real')


def services(service):return service._backend._pool,service._session,service._backend._signer


def step(key='confirm-renewal',**changes):
    return {'step_key':key,'step_object_id':None,'prerequisites':['客户已收到续费提醒'],'expected_result':'确认续费意向，不催促',
        'stop_if':[],'reassess_at':None,'budget':{'maximum_model_turns':6,'maximum_tool_calls':20,'active_timeout_seconds':120},'intent_ref':None}|changes


def spec(plan,**changes):
    return {'plan_id':str(uuid.uuid4()),'consumer_id':plan['consumer'],'goal_version_ref':f'goal:{GOAL}@1','strategy':{'content':'先确认意向，再谈付款方式'},
        'context_strategy_ref':'context-strategy:recent_plus_required@1',
        'recipe':{'role_id':'a'*64,'link_id':'b'*64,'step_id':plan['step'],'offering_id':'c'*64,'binding_id':'d'*64,'control_id':plan['control']},
        'settings':plan_settings(SETTINGS),'steps':[step()]}|changes


class FakeLauncher:
    """Records the bounded Run the worker would start (the Role launcher is exercised separately)."""
    def __init__(self,plan):self.plan,self.activated=plan,[]
    def issue(self):
        source=self.plan['api'].authenticate(self.plan['source_tokens'][0],world='real')  # current session (seeding moves the directory)
        run=source.issue_run_credential(action_resources=['eios:action:'+fixture.EFFECT+':1'])
        return LaunchedRun(run.run_id,run.token,run.expires_at)
    def activate(self,run,decision,budget,triggers):self.activated.append((run.run_id,decision,budget,triggers))


@pytest.fixture
def planning(runtime_effect_plan,admin):
    plan=runtime_effect_plan;tenant=plan['tenant']
    seed_strategy(admin,tenant,STRATEGY)
    publish_goal(admin,tenant,0,'v1')
    session=reevaluator(plan,admin)
    def port():return PlanPort(*services(session()))
    def worker(launcher=None):return PlanReevaluationWorker(*services(session()),settings=SETTINGS,launcher=launcher or FakeLauncher(plan))
    yield dict(plan=plan,admin=admin,tenant=tenant,session=session,port=port,worker=worker)


def feed(admin):
    return admin.execute("select item_key,payload,status,available_at>clock_timestamp() from runtime.nexloop_work_feed where feed='plan-reevaluate' order by item_key").fetchall()


def events(admin,plan_id):
    return admin.execute('select version,status,reason from runtime.nexloop_plan_events where plan_id=%s order by event_id',(plan_id,)).fetchall()


def make_due(admin):admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp()-interval '1 second' where feed='plan-reevaluate'")


def test_establish_derives_snapshot_and_reassess_due_then_reevaluates_once(planning):
    """M15/M16: a plan with reassess_at waits in PostgreSQL (no resident loop) and wakes as one bounded Run."""
    f=planning;admin=f['admin'];due=(datetime.now(UTC)+timedelta(hours=2)).replace(microsecond=0)
    s=spec(f['plan'],steps=[step(reassess_at=due.isoformat().replace('+00:00','Z'))])
    created=f['port']().establish(s)
    assert created['version']==1 and created['replay'] is False
    assert f['port']().establish(s)['replay'] is True
    with pytest.raises(psycopg.errors.SerializationFailure):f['port']().establish({**s,'settings':{**s['settings'],'self_trigger_cap':5}})
    row=admin.execute('select control_snapshot,recipe->\'goal_chain\',strategy_ref from runtime.nexloop_plans where plan_id=%s',(s['plan_id'],)).fetchone()
    assert row[0]['goals']==[{'goal_id':GOAL,'version':1}] and row[0]['scopes']==[{'kind':'consumer','ref':f['plan']['consumer']}] and row[1]==[GOAL]
    assert row[2]=='strategy:'+s['plan_id']+'@1'
    (key,payload,status,future),=feed(admin)
    assert key=='plan:'+s['plan_id'] and payload['triggers'][0]['kind']=='reassess_due' and status=='pending' and future
    w=f['worker']();assert not any(w.run_once().values())  # not due yet
    make_due(admin);launcher=FakeLauncher(f['plan'])
    summary=f['worker'](launcher).run_once()
    assert summary['launched']==1 and feed(admin)==[]
    (run_id,decision,budget,triggers),=launcher.activated
    assert decision['decision']=='reevaluate' and decision['goal_version_ref']==f'goal:{GOAL}@1' and triggers[0]['kind']=='reassess_due'
    # Budget: the configured ceiling, further limited by the plan step budgets (and never above the configuration).
    assert run_budget(SETTINGS,decision['plan_steps'])=={'maximum_model_turns':6,'maximum_tool_calls':12,'active_timeout_seconds':120,'maximum_cost':'1.0','currency':'USD'}
    assert admin.execute('select plan_id::text,version from runtime.nexloop_plan_runs where run_id=%s',(run_id,)).fetchone()==(s['plan_id'],1)
    # Plan records are append-only.
    with pytest.raises(psycopg.Error):admin.execute("update runtime.nexloop_plans set settings='{}' where plan_id=%s",(s['plan_id'],))


def test_pause_waits_without_a_run_and_resume_is_not_replay(planning):
    """Pause → precheck says paused (no Run); resume → the old snapshot is stale → one fresh reevaluation."""
    f=planning;admin=f['admin'];s=spec(f['plan']);f['port']().establish(s)
    consumer=f['plan']['consumer'];launcher=FakeLauncher(f['plan'])
    owner_change(admin,f['tenant'],{'operation':'set_control','scope_kind':'consumer','scope_ref':consumer,'paused':True,'reason':'owner pause'},'nx024-pause')
    (key,payload,_,_),=feed(admin);assert payload['triggers'][-1]['kind']=='control_paused'
    assert f['worker'](launcher).run_once()['paused']==1 and launcher.activated==[] and feed(admin)==[]
    owner_change(admin,f['tenant'],{'operation':'set_control','scope_kind':'consumer','scope_ref':consumer,'paused':False,'reason':'owner resume'},'nx024-resume')
    assert feed(admin)[0][1]['triggers'][-1]['kind']=='resume'
    assert f['worker'](launcher).run_once()['launched']==1
    assert launcher.activated[0][1]['reasons']==['control_revision_stale']
    # An unrelated consumer's pause does not touch this plan.
    owner_change(admin,f['tenant'],{'operation':'set_control','scope_kind':'consumer','scope_ref':'e'*64,'paused':True,'reason':'other'},'nx024-other')
    assert feed(admin)==[]


def test_goal_version_change_reevaluates_with_the_current_version(planning):
    f=planning;admin=f['admin'];s=spec(f['plan']);f['port']().establish(s);launcher=FakeLauncher(f['plan'])
    publish_goal(admin,f['tenant'],1,'v2')
    assert feed(admin)[0][1]['triggers'][-1]['kind']=='goal_version_stale'
    assert f['worker'](launcher).run_once()['launched']==1
    decision=launcher.activated[0][1]
    assert 'goal_version_stale' in decision['reasons'] and decision['goal_version_ref']==f'goal:{GOAL}@2'
    # A new plan cannot be established on the superseded version.
    with pytest.raises(psycopg.Error):f['port']().establish(spec(f['plan']))


def test_stop_condition_invalidates_without_a_model_call(planning):
    """PRD M15: once the customer's situation resolves the step, the old plan is not executed blindly."""
    f=planning;admin=f['admin'];launcher=FakeLauncher(f['plan'])
    s=spec(f['plan'],steps=[step(stop_if=[{'type_name':'PlanStep','object_id':f['plan']['step'],'property':'state','equals':'done'}])])
    f['port']().establish(s)
    admin.execute("update ontology.objects set properties=properties||'{\"state\":\"done\"}' where object_id=%s",(f['plan']['step'],))  # fault: situation resolved
    admin.execute("select authz.nexloop_plan_feed_touch(%s,'real',%s,'{\"kind\":\"external_result\",\"cause\":\"external\"}',null)",(f['tenant'],s['plan_id']))
    assert f['worker'](launcher).run_once()['invalidated']==1 and launcher.activated==[]
    assert events(admin,s['plan_id'])[-1][1:]==('invalidated','stop_if:confirm-renewal')
    assert f['port']().precheck(s['plan_id'])['decision']=='closed'


def test_self_triggers_are_throttled_but_external_ones_are_not(planning):
    """M16: a plan's own Runs cannot re-trigger it indefinitely; the cap and window are configuration."""
    f=planning;admin=f['admin'];launcher=FakeLauncher(f['plan'])
    s=spec(f['plan'],settings={'self_window_seconds':600,'self_trigger_cap':1,'min_reassess_seconds':0});f['port']().establish(s)
    touch=lambda cause:admin.execute("select authz.nexloop_plan_feed_touch(%s,'real',%s,%s,null)",(f['tenant'],s['plan_id'],Jsonb({'kind':'reassess_due','cause':cause})))
    touch('self');assert f['worker'](launcher).run_once()['launched']==1
    touch('self');summary=f['worker'](launcher).run_once()
    assert summary['throttled']==1 and len(launcher.activated)==1
    (key,payload,status,future),=feed(admin);assert future and status=='pending'
    last=admin.execute("select last_code from runtime.nexloop_work_feed where item_key=%s",(key,)).fetchone()[0];assert last=='self_trigger_throttled'
    touch('external');make_due(admin)
    assert f['worker'](launcher).run_once()['launched']==1 and len(launcher.activated)==2


def test_dispatch_denial_and_strategy_change_mark_the_plan(planning):
    f=planning;admin=f['admin'];s=spec(f['plan']);f['port']().establish(s);launcher=FakeLauncher(f['plan'])
    # T4: a new Context strategy version.
    seed_strategy(admin,f['tenant'],{**STRATEGY,'version':2})
    assert feed(admin)[0][1]['triggers'][-1]=={**feed(admin)[0][1]['triggers'][-1],'kind':'strategy_changed','ref':'context-strategy:recent_plus_required@2'}
    f['worker'](launcher).run_once()
    assert feed(admin)==[]
    # T2: a reevaluation Run's task is denied at dispatch by a control check (fault-injected terminal state of a
    # fixture task whose Run is linked to this plan) → the plan is marked again in the same transaction.
    job=f['plan']['jobs'][0]['task_id'];run_id=f['plan']['runs'][0].run_id
    f['port']().link_run(plan_id=s['plan_id'],version=1,run_id=run_id,triggers=[])
    admin.execute("update runtime.jobs set status='failed',result='{\"code\":\"control_paused\"}' where job_id=%s",(job,))
    (key,payload,_,_),=feed(admin)
    assert payload['triggers'][-1]['kind']=='dispatch_denied' and payload['triggers'][-1]['code']=='control_paused' and payload['triggers'][-1]['run_id']==run_id
    # Other failure codes (not a control denial) do not re-trigger.
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")
    job2=f['plan']['jobs'][1]['task_id'];f['port']().link_run(plan_id=s['plan_id'],version=1,run_id=f['plan']['runs'][1].run_id,triggers=[])
    admin.execute("update runtime.jobs set status='failed',result='{\"code\":\"runtime_transport_timeout\"}' where job_id=%s",(job2,))
    assert feed(admin)==[]


def test_ports_refuse_other_principals_and_shapes(planning):
    f=planning;admin=f['admin']
    with pytest.raises(PlanUnavailable):PlanPort(*services(f['plan']['worker'])).precheck(str(uuid.uuid4()))  # no plan.reevaluate grant
    bad=spec(f['plan']);bad['steps'][0]['budget']['maximum_model_turns']=65
    with pytest.raises(psycopg.errors.InvalidParameterValue):f['port']().establish(bad)
    with pytest.raises(psycopg.errors.InvalidParameterValue):f['port']().establish({**spec(f['plan']),'tenant_id':f['tenant']})
    with pytest.raises(psycopg.Error):f['port']().establish(spec(f['plan'],context_strategy_ref='context-strategy:unknown_strategy@1'))
    s=spec(f['plan']);f['port']().establish(s)
    with pytest.raises(psycopg.Error):f['port']().link_run(plan_id=s['plan_id'],version=1,run_id=str(uuid.uuid4()),triggers=[])  # no such Run
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_scheduler'):
        with psycopg.connect(make_conninfo(f['plan']['pg'],user=role)) as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select count(*) from runtime.nexloop_plans').fetchone()
