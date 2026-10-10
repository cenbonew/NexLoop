"""NX-025 E2E-C: explicit refusal, pause/resume and version interleaving across the plan / Run / dispatch chain.

* C1 explicit refusal first (ADR-023): after the Consumer says "以后别再给我发消息了", a reevaluation Run that
  nevertheless submits a message is refused at dispatch (contact_restricted), zero provider requests; an intent queued
  before the refusal is refused as well.
* C2 AT-006 dispatch side: a pause after an intent was queued refuses it at dispatch (zero provider requests); the
  due plan waits without a Run; resume is a fresh reevaluation (new Run), never a replay of the old intent.
* C3 AT-007 / G9: the goal changes while a reevaluation Run is active: its outcome write-back is refused (40001), its
  intent is refused at dispatch (goal-bound Run), and the queued reevaluation binds the new goal version.
* C4 AT-007: the Consumer's state changes through a governed write: the plan wakes (G5) and is invalidated by its
  stop_if before any Run; the obsolete step is never continued.
Synthetic data only; admin seeds fixtures, injects the owner's control changes through the NX-022 internal functions
the governed owner Actions use (as in the NX-022/NX-024 tests) and probes.
"""
import json
import time
import uuid

import psycopg
import pytest
import runtime_effect_fixture as fixture
from psycopg.conninfo import make_conninfo
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_execution import EffectExecutionUnavailable
from nexloop_eios.effect_provider import EffectProviderConfiguration,HttpEffectProvider
from support.effect_provider import effect_provider
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_claim_matching_pg import Claims,decision,env,matcher,obj  # noqa: F401
from test_closure_plan_run_pg import Timed,actual_host,launched,role_settings,run_on_host  # noqa: F401
from test_plan_outcome_pg import outcome,outcome_worker
from test_plan_reevaluation_pg import GOAL,FakeLauncher,events,feed,make_due,owner_change,publish_goal,step
from test_plan_role_launcher_pg import role_planning,within_ceiling  # noqa: F401
from test_plan_wake_pg import waking  # noqa: F401


LEASE=5  # executor lease: long enough that a refusal is never a lease expiry


def activated(worker,command,text):
    """The runtime worker claims the Run's task and activates it (start authorized), as the Host path does."""
    job=worker.claim_task(queue='operations',lease_seconds=60)
    ref=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=command['run_id'],command=command,input=text,owner_epoch=1)['activation_ref']
    return ref


def submit(worker,ref,command,message='续费提醒'):
    return worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='submit',parameters={'message':message})['receipt']['intent_id']


def dispatch_attempt(p,tmp_path,intent):
    """The executor tries to dispatch the intent; returns (denied, provider requests)."""
    with open_backend(database_url=make_conninfo(p['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/('executor-'+intent[:8]),
            signing_key_file=p['signing_key'],signing_key_id=p['signing_key_id']) as backend,effect_provider(tmp_path/('provider-'+intent[:8]+'.sqlite')) as provider:
        executor=backend.authenticate(p['executor_token'],world='real')
        transport=HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1))
        denied=False
        try:
            claim=executor.claim_effect(lease_seconds=LEASE)  # a later attempt reclaims only after real expiry
            assert claim['intent_id']==intent
            frozen=executor.prepare_effect_dispatch(intent_id=intent,fence=claim['fence'],provider_profile_digest=transport.profile_digest)
            transport.dispatch(intent_id=intent,payload_digest=frozen['provider_payload_digest'],parameters=frozen['parameters'])
        except EffectExecutionUnavailable:denied=True
        return denied,provider.control('snapshot')['requests']


def inbound_refusal(admin,p,body='以后别再给我发消息了'):
    """An inbound consumer Message row exactly as the 0046 commit writes it (this fixture has no browser channel; the
    commit path itself, with a real Human, is tests/test_contact_refusal_pg.py). The 0109 trigger runs on this insert."""
    import hashlib
    from datetime import UTC,datetime
    conversation=hashlib.sha256(('nx025-conversation-'+p['tenant']).encode()).hexdigest()
    admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,'synthetic-human','nx025-refusal') on conflict do nothing",
        (p['tenant'],conversation,p['consumer']))
    sequence=admin.execute('select coalesce(max(sequence),0)+1 from runtime.nexloop_conversation_messages where conversation_id=%s',(conversation,)).fetchone()[0]
    message=hashlib.sha256((conversation+str(sequence)).encode()).hexdigest()
    record={'id':message,'conversation_id':conversation,'sequence':sequence,'actor':'synthetic-human','body':body,'accepted_at':datetime.now(UTC).isoformat(),'status':'accepted'}
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(p['tenant'],))  # tenant context of the commit path's triggers
        admin.execute('insert into runtime.nexloop_conversation_messages values(%s,%s,%s,%s,%s,%s,%s,%s)',
            (p['tenant'],'real',conversation,sequence,message,'nx025-refusal-'+str(sequence),'0'*64,json.dumps(record)))
    return message


def dispatch_reason(p,tmp_path,intent):
    """The control-plane verdict the executor's admission reads (ControlDenied reason, or None when allowed)."""
    from nexloop_eios.goal_controls import ControlDenied,ControlPlane
    with open_backend(database_url=make_conninfo(p['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/('reason-'+intent[:8]),
            signing_key_file=p['signing_key'],signing_key_id=p['signing_key_id']) as backend:
        executor=backend.authenticate(p['executor_token'],world='real')
        try:ControlPlane(executor._backend._pool,executor._session).assert_intent_dispatch(intent)
        except ControlDenied as denied:return str(denied)
    return None


def test_explicit_refusal_stops_contact_even_if_the_run_tries(role_planning,admin,tmp_path):
    """ADR-023 / E2E-C1: after the Consumer's refusal, a reevaluation Run that nevertheless submits a message is refused at
    dispatch (contact_restricted), zero provider requests; the restriction is the inbound check's, not the model's."""
    f=role_planning;p=f['plan']
    from test_plan_outcome_pg import outcome_worker_and_spawn
    service,spawn=outcome_worker_and_spawn(p,admin,'-nx025-refusal');worker=Timed(service)
    inbound_refusal(admin,p)
    assert admin.execute('select active,rule_id from control.nexloop_contact_restrictions where consumer_id=%s',(p['consumer'],)).fetchone()==(True,'stop-contact')
    s=f['establish'](steps=[within_ceiling()])
    assert f['worker']().run_once()['launched']==1
    run_id,command,text=launched(f,s['plan_id'])
    # The deterministic protocol deliberately submits a message anyway (a model ignoring its Context).
    with actual_host(f,tmp_path,worker,'action_intent',spawn) as (runtime,client,headers):
        result=run_on_host(client,headers,worker,command,text)
    assert result['runtime_outcome']=='succeeded'
    (intent,),=admin.execute('select intent_id::text from runtime.nexloop_effect_submissions where run_id=%s',(run_id,)).fetchall()
    assert dispatch_reason(p,tmp_path,intent)=='contact_restricted'
    denied,requests=dispatch_attempt(p,tmp_path,intent)
    assert denied and requests==[]


def test_intent_queued_before_the_refusal_is_refused_too(role_planning,admin,tmp_path):
    f=role_planning;p=f['plan']
    worker=outcome_worker(p,admin,'-nx025-refusal-queued')
    s=f['establish'](steps=[within_ceiling()])
    assert f['worker']().run_once()['launched']==1
    run_id,command,text=launched(f,s['plan_id'])
    ref=activated(worker,command,text);intent=submit(worker,ref,command)
    assert dispatch_reason(p,tmp_path,intent) is None  # dispatchable before the refusal
    inbound_refusal(admin,p,'退订')
    assert dispatch_reason(p,tmp_path,intent) in ('control_revision_stale','contact_restricted')
    denied,requests=dispatch_attempt(p,tmp_path,intent)
    assert denied and requests==[]


def test_pause_refuses_the_queued_intent_and_resume_reevaluates_instead_of_replaying(role_planning,admin,tmp_path):
    f=role_planning;p=f['plan']
    worker=outcome_worker(p,admin,'-nx025-pause')
    s=f['establish'](steps=[within_ceiling()])
    assert f['worker']().run_once()['launched']==1
    run_id,command,text=launched(f,s['plan_id'])
    ref=activated(worker,command,text);intent=submit(worker,ref,command)
    owner_change(admin,p['tenant'],{'operation':'set_control','scope_kind':'consumer','scope_ref':p['consumer'],'paused':True,'reason':'owner pause'},'nx025-pause')
    # AT-006 dispatch side: the queued intent is refused before any provider request.
    denied,requests=dispatch_attempt(p,tmp_path,intent)
    assert denied and requests==[]
    # The due plan waits in PostgreSQL without a Run.
    make_due(admin)
    before=admin.execute('select count(*) from runtime.nexloop_plan_runs where plan_id=%s',(s['plan_id'],)).fetchone()[0]
    assert f['worker']().run_once()['paused']==1
    assert admin.execute('select count(*) from runtime.nexloop_plan_runs where plan_id=%s',(s['plan_id'],)).fetchone()[0]==before
    owner_change(admin,p['tenant'],{'operation':'set_control','scope_kind':'consumer','scope_ref':p['consumer'],'paused':False,'reason':'owner resume'},'nx025-resume')
    (_,payload,_,_),=feed(admin)
    assert payload['triggers'][-1]['kind']=='resume'
    # Resume is a fresh reevaluation: a new bounded Run with the new control state; the old intent stays refused.
    assert f['worker']().run_once()['launched']==1
    runs=admin.execute('select run_id::text,triggers from runtime.nexloop_plan_runs where plan_id=%s order by created_at',(s['plan_id'],)).fetchall()
    assert len(runs)==before+1 and runs[-1][0]!=run_id and runs[-1][1][-1]['kind']=='resume'
    time.sleep(LEASE+0.2)  # the refused attempt's executor lease expires (no fault injection)
    denied,requests=dispatch_attempt(p,tmp_path,intent)
    assert denied and requests==[]


def test_goal_change_during_a_run_refuses_its_outcome_and_intent_and_rebinds_the_next(role_planning,admin,tmp_path):
    f=role_planning;p=f['plan']
    worker=outcome_worker(p,admin,'-nx025-goal')
    s=f['establish'](steps=[within_ceiling()])
    assert f['worker']().run_once()['launched']==1
    run_id,command,text=launched(f,s['plan_id'])
    assert admin.execute('select goal_id,goal_version from control.nexloop_run_goal_bindings where run_id=%s',(run_id,)).fetchone()==(GOAL,1)
    ref=activated(worker,command,text);intent=submit(worker,ref,command)
    publish_goal(admin,p['tenant'],1,'v2')  # the owner publishes goal v2 while the Run is active
    update={'strategy':None,'steps':[step('ask-again')]}
    with pytest.raises(psycopg.errors.SerializationFailure):
        worker.record_plan_outcome(activation_ref=ref,command=command,outcome=outcome('plan_update',plan_update=update))
    assert admin.execute('select count(*) from runtime.nexloop_plan_outcomes where run_id=%s',(run_id,)).fetchone()==(0,)
    assert events(admin,s['plan_id'])[-1][:2]==(1,'active')  # nothing superseded by the stale Run
    denied,requests=dispatch_attempt(p,tmp_path,intent)
    assert denied and requests==[]
    (_,payload,_,_),=feed(admin)
    assert payload['triggers'][-1]['kind']=='goal_version_stale'
    assert f['worker']().run_once()['launched']==1
    (latest,)=admin.execute('select run_id::text from runtime.nexloop_plan_runs where plan_id=%s order by created_at desc limit 1',(s['plan_id'],)).fetchone()
    assert latest!=run_id
    assert admin.execute('select goal_id,goal_version from control.nexloop_run_goal_bindings where run_id=%s',(latest,)).fetchone()==(GOAL,2)


def test_customer_state_change_wakes_the_plan_and_stop_if_ends_it_without_a_run(waking):
    from nexloop_eios.plan_reevaluation import PlanReevaluationWorker
    from test_plan_reevaluation_pg import SETTINGS
    f=waking;admin=f['admin'];c=f['claims']
    # A second plan whose step stops when the Consumer's budget level is known (state change from conversation).
    s={**f['spec'],'plan_id':str(uuid.uuid4()),'steps':[step('ask-budget',stop_if=[{'type_name':'Consumer','object_id':f['consumer'],'property':'budget_level','equals':'两千元以内'}])]}
    f['port']().establish(s)
    f['port']().mark(plan_id=f['spec']['plan_id'],version=1,status='closed',reason='superseded by the budget plan')  # only the stop_if plan stays active
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")
    claim=c.add('stop-budget','预算区间','两千元以内',quote='预算在两千元以内')
    m,_=matcher(f,{claim:decision('eios:property:Consumer/budget_level','两千元以内')})
    assert list(m.process_conversation(f['conversation'])['applied'].values())==['applied']
    keys={row[0] for row in feed(admin)}
    assert 'plan:'+s['plan_id'] in keys  # woken in the writer's transaction (G5)
    launcher=FakeLauncher.__new__(FakeLauncher);launcher.activated=[]
    launcher.issue=lambda *a,**k:pytest.fail('no Run may be issued for an invalidated plan')
    summary=PlanReevaluationWorker(f['worker'],f['port']().session,f['signer'],settings=SETTINGS,launcher=launcher).run_once()
    assert summary['invalidated']>=1 and summary['launched']==0,summary
    assert events(admin,s['plan_id'])[-1][:2]==(1,'invalidated')
    assert obj(admin,f['consumer'])[0]['budget_level']=='两千元以内'
