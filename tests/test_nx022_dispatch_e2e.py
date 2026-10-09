"""NX-022 close-out: owner controls are enforced at dispatch (AT-006/AT-007 control plane, AT-043 budget).

Effect path: actual governed plan -> Run submission (server-side control snapshot
captured by the 0097 trigger) -> independent effect Worker process -> loopback
provider; a denied admission must produce zero provider requests.
Runtime path: actual accepted runtime task -> RuntimeDispatcher.run_once against the
real TLS Host; a denied Run is failed before any Host start.

Owner control changes (pause/resume, budget, goal version) are written through the
same internal control-state functions the governed owner Actions use
(control.nexloop_nx022_owner_change / _write_goal); the human-only governed Action
chain for those changes is covered by tests/test_goal_controls.py.
"""
from datetime import UTC,datetime,timedelta
import uuid
import pytest
from psycopg.types.json import Jsonb
from nexloop_eios.goal_controls import ControlDenied,ControlPlane
from nexloop_eios.runtime_dispatch import RuntimeDispatcher
from support.effect_provider import effect_provider
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_effect_dispatch import independent_worker,accepted
from test_runtime_dispatch import dispatch_host
from test_runtime_atomic_accept import accepted_input  # noqa: F401
from test_runtime_authority import authority  # noqa: F401
from test_runtime_activation import synthetic_credentials  # noqa: F401

NOW=datetime.now(UTC).replace(microsecond=0)
PERIOD=(NOW-timedelta(days=1),NOW+timedelta(days=30))


def owner_change(admin,tenant,body,world='real'):
    intent='owner-'+uuid.uuid4().hex
    return admin.execute('select control.nexloop_nx022_owner_change(%s,%s,%s,%s,%s)',(tenant,world,'synthetic-owner-principal',intent,Jsonb({'request_id':intent,**body}))).fetchone()[0]


def pause(admin,tenant,kind,ref,paused=True):
    return owner_change(admin,tenant,{'operation':'set_control','scope_kind':kind,'scope_ref':ref,'paused':paused,'reason':'owner test'})


def publish_goal(admin,tenant,goal_id,expected,target):
    owner_change(admin,tenant,{'operation':'approve_metric','metric_id':'dispatch-metric','version':1,'name':'m','aggregation':'count',
        'unit':'event','currency':None,'maturity_seconds':0,'refund_rule':'not_applicable','cohort_rule':'synthetic'}) if expected==0 else None
    intent='goal-'+uuid.uuid4().hex
    body={'request_id':intent,'operation':'publish_goal','goal_id':goal_id,'goal_kind':'long_term','expected_current_version':expected,'parent':None,
        'objective':'dispatch goal','period_start':PERIOD[0].isoformat(),'period_end':PERIOD[1].isoformat(),'priority':2,'budget':{},'constraints':[],
        'change_summary':'owner revision','key_results':[{'kr_key':'k','metric_id':'dispatch-metric','metric_version':1,'target':target,
        'direction':'at_least','window_start':PERIOD[0].isoformat(),'window_end':PERIOD[1].isoformat()}]}
    return admin.execute('select control.nexloop_nx022_write_goal(%s,%s,%s,%s,%s,%s,false)',(tenant,'real','synthetic-owner-principal','human',intent,Jsonb(body))).fetchone()[0]


def snapshot_rows(admin,intent):
    return admin.execute('select run_id,snapshot from runtime.nexloop_effect_dispatch_controls where intent_id=%s order by captured_at',(intent,)).fetchall()


def dispatch_once(fixture,provider):
    with independent_worker(fixture,provider.origin) as (worker,pipe):
        assert pipe.poll(20);result=pipe.recv();worker.join(10)
        assert worker.exitcode==0,result
    return result


def wait_for_reclaim(admin,intent):
    # The refused worker's outbox lease and Action claim lease (3 s worker lease) expire
    # naturally; nothing is reset by hand, so the next Worker reclaims as in production.
    import time
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        row=admin.execute('''select o.lease_until<=clock_timestamp() and coalesce((a.claim->>'lease_expires_at')::timestamptz<=clock_timestamp(),true)
            from runtime.nexloop_effect_outbox o left join runtime.nexloop_action_claims a on a.intent_id=o.intent_id::text
            where o.intent_id=%s''',(intent,)).fetchone()
        if row is None or row[0] in (True,None):return
        time.sleep(0.2)
    raise AssertionError('leases did not expire')


def resubmit_from_new_run(fixture):
    plan=fixture.plan
    run=plan['submitter'].issue_run_credential(action_resources=['eios:action:nexloop.service.request:1'])
    plan['planner'].bind_effect_context(step_id=plan['step'],step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,
        run_id=run.run_id,run_token=run.token,executor_token=plan['executor_token'])
    port=plan['backend'].authenticate_run(run.token,world='real',run_id=run.run_id)
    return port.submit_effect_intent(parameters={'message':'one governed service'}),run


# ---------------------------------------------------------------- effect path

def test_effect_paused_after_enqueue_is_denied_with_zero_effect_then_resume_needs_reevaluation(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    rows=snapshot_rows(admin,intent)
    assert len(rows)==2 and {s['scopes'][0]['ref'] for _,s in rows}=={fixture.plan['consumer']}  # captured server-side per submitting Run
    pause(admin,fixture.tenant,'consumer',fixture.plan['consumer'])
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        result=dispatch_once(fixture,provider)
        assert result['status']=='admission_unavailable' and result['business_action_success'] is False
        assert provider.control('snapshot')['effects']==0
        # Resume is not replay: the queued intent's snapshot predates the control changes.
        pause(admin,fixture.tenant,'consumer',fixture.plan['consumer'],paused=False)
        wait_for_reclaim(admin,intent)
        assert dispatch_once(fixture,provider)['status']=='admission_unavailable'
        assert provider.control('snapshot')['effects']==0
        # Re-evaluation: a new Run re-submits the same business intent after resume.
        again,_=resubmit_from_new_run(fixture)
        assert again['intent_id']==intent
        wait_for_reclaim(admin,intent)
        resumed=dispatch_once(fixture,provider)
        assert resumed['status']!='admission_unavailable' and resumed['claimed'] is True
        snapshot=provider.control('snapshot');assert snapshot['effects']==1 and snapshot['requests']==[('POST',intent,202)]


def test_effect_goal_revision_after_enqueue_denies_old_plan(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor
    publish_goal(admin,fixture.tenant,'dispatch-goal',0,'1')
    executor=ControlPlane(fixture.plan['backend']._pool,fixture.plan['executor'])
    for run in fixture.runs:executor.bind_run(run_id=run.run_id,goal_ref='goal:dispatch-goal@1')
    receipt=accepted(fixture);intent=receipt['intent_id']
    assert all(s['goals']==[{'goal_id':'dispatch-goal','version':1}] for _,s in snapshot_rows(admin,intent))
    publish_goal(admin,fixture.tenant,'dispatch-goal',1,'2')  # owner revises the goal: control revision advances
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        assert dispatch_once(fixture,provider)['status']=='admission_unavailable'
        assert provider.control('snapshot')['effects']==0
    with pytest.raises(ControlDenied) as stale:executor.assert_intent_dispatch(intent)
    assert stale.value.reason=='goal_version_stale'


def test_effect_unrelated_control_change_does_not_block_and_dispatches(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    pause(admin,fixture.tenant,'consumer','f'*64)  # another consumer
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        result=dispatch_once(fixture,provider)
        assert result['claimed'] is True and result['status']!='admission_unavailable'
        assert provider.control('snapshot')['effects']==1


# ---------------------------------------------------------------- runtime path

def runtime_task(api,args,**command):
    args['command'].update(command)
    return api.accept_runtime_event(**args)


def run_dispatch(worker,tmp_path):
    with dispatch_host(worker,tmp_path) as (runtime,_,_,_,config):
        return RuntimeDispatcher(worker,config,queue='operations',total_timeout=10).run_once()


def test_runtime_paused_after_enqueue_is_failed_before_host_start(accepted_input,admin,tmp_path):
    api,worker,args,issued=accepted_input;tenant=args['command']['tenant_id']
    accepted=runtime_task(api,args)
    pause(admin,tenant,'role',args['command']['role_ref'])
    result=run_dispatch(worker,tmp_path)
    assert result['task_id']==accepted['task_id'] and result['status']=='failed'
    assert result['result']['code']=='control_paused' and 'runtime_receipt' not in result['result']
    assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone()==(0,)


def test_runtime_goal_revision_after_enqueue_denies_old_plan_and_current_plan_runs(accepted_input,admin,tmp_path):
    api,worker,args,issued=accepted_input;tenant=args['command']['tenant_id']
    publish_goal(admin,tenant,'run-goal',0,'1')
    accepted=runtime_task(api,args,goal_version_ref='goal:run-goal@1')
    publish_goal(admin,tenant,'run-goal',1,'2')  # plan was made against v1
    result=run_dispatch(worker,tmp_path)
    assert result['task_id']==accepted['task_id'] and result['status']=='failed' and result['result']['code']=='goal_version_stale'
    assert admin.execute('select count(*) from control.nexloop_run_goal_bindings').fetchone()==(0,)


def test_runtime_current_goal_binds_run_and_dispatches(accepted_input,admin,tmp_path):
    api,worker,args,issued=accepted_input;tenant=args['command']['tenant_id']
    publish_goal(admin,tenant,'run-goal',0,'1')
    accepted=runtime_task(api,args,goal_version_ref='goal:run-goal@1')
    result=run_dispatch(worker,tmp_path)
    assert result['task_id']==accepted['task_id'] and result['status']=='succeeded'
    assert admin.execute('select goal_id,goal_version from control.nexloop_run_goal_bindings where run_id=%s',(issued.run_id,)).fetchall()==[('run-goal',1)]


@pytest.mark.parametrize('limit,expected',[('0.50','budget_exhausted'),('5.00','succeeded')])
def test_runtime_model_budget_is_reserved_per_run_and_exhaustion_denies(accepted_input,admin,tmp_path,limit,expected):
    api,worker,args,issued=accepted_input;tenant=args['command']['tenant_id']
    budget=args['command']['budget']
    owner_change(admin,tenant,{'operation':'set_budget','budget_kind':'model','unit':budget['currency'],'limit_amount':limit,
        'period_start':PERIOD[0].isoformat(),'period_end':PERIOD[1].isoformat()})
    accepted=runtime_task(api,args)  # enqueued after the budget was set
    result=run_dispatch(worker,tmp_path)
    assert result['task_id']==accepted['task_id']
    if expected=='succeeded':
        assert result['status']=='succeeded'
        assert admin.execute("select consumption_id,amount::text from control.nexloop_budget_consumption").fetchall()==[('run:'+issued.run_id,budget['maximum_cost'])]
    else:
        assert result['status']=='failed' and result['result']['code']=='budget_exhausted'
        assert admin.execute('select count(*) from control.nexloop_budget_consumption').fetchone()==(0,)
