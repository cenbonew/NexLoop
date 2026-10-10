"""NX-024 run-outcome write-back (plan B) through the runtime worker facade on clean catalog PostgreSQL.

A reevaluation Run is a real governed Run: issued by a Source, bound by the Planner, accepted, claimed and activated by a
runtime worker that holds the queue grant plus nexloop.plan.outcome:1. The outcome is recorded only for the Run's live
activation, by the worker holding its task lease, with the exact command, for the plan version the Run was linked to.
Synthetic data; admin only seeds and probes.
"""
import copy
from datetime import UTC,datetime,timedelta
import uuid

import psycopg
import pytest
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
import runtime_effect_fixture as fixture
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_reevaluation_pg import planning,services,spec,step  # noqa: F401
from nexloop_eios.plan_reevaluation import PlanPort,PlanUnavailable

INPUT='reevaluate the plan for this consumer'
REFUSED=(AuthorizationUnavailable,PlanUnavailable,psycopg.Error)


def outcome(kind='no_action',**changes):
    return {'schema_version':'1.0','kind':kind,'reasons':['客户刚确认过，暂不打扰'],'reassess_at':None,'evidence_refs':[],'intent_ref':None,'plan_update':None}|changes


def outcome_worker_and_spawn(plan,admin,suffix):
    """The outcome-capable runtime worker, and its guard_server spawn configuration (same identity and Backend
    configuration; used when NEXLOOP_TEST_GUARD_WORKERS>1, as the deployed four guard processes, ADR-024)."""
    token=fixture.seed_multi_uuid(admin,plan['tenant'],[('eios:action:'+fixture.QUEUE+':1',ResourceType.ACTION,Operation.EXECUTE),
        ('eios:action:nexloop.plan.outcome:1',ResourceType.ACTION,Operation.EXECUTE)],suffix=suffix)
    return plan['backend_worker'].authenticate(token,world='real'),{**plan['worker_spawn'],'token':token}


def outcome_worker(plan,admin,suffix):
    return outcome_worker_and_spawn(plan,admin,suffix)[0]


def reevaluation_run(plan,worker,label):
    """One fresh governed Run, accepted and activated by `worker` (same path as the runtime effect fixture)."""
    source=plan['api'].authenticate(plan['source_tokens'][0],world='real')
    run=source.issue_run_credential(action_resources=['eios:action:'+fixture.EFFECT+':1'])
    planner=plan['api'].authenticate(plan['planner_token'],world='real')  # current sessions (seeding moves the directory)
    owner=plan['api'].authenticate(plan['owner_token'],world='real')
    planner.bind_effect_context(step_id=plan['step'],step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,run_id=run.run_id,run_token=run.token,executor_token=plan['executor_token'])
    command={'schema_version':'1.0','run_id':run.run_id,'tenant_id':plan['tenant'],'world_id':'real','mode':'real','request_id':'nx024-'+str(uuid.uuid4()),'trigger_event_id':str(uuid.uuid4()),
        'role_ref':'role:plan-reevaluator','consumer_ref':'consumer:'+plan['consumer'],'goal_version_ref':'goal:'+plan['goal']+':revision:1:step:1:control:1',
        'context_manifest_ref':'artifact:synthetic-effect','runtime_profile':'deterministic-test','credential_ref':'run_credential:'+run.run_id,
        'budget':{'maximum_model_turns':8,'maximum_tool_calls':12,'active_timeout_seconds':300,'maximum_cost':'1.0','currency':'USD'},
        'not_after':run.expires_at.isoformat(),'runtime_owner_epoch':1}
    accepted=owner.accept_runtime_event(queue='operations',source_id='nx024',event_id='nx024-'+label,run_token=run.token,command=command,input=INPUT)
    job=worker.claim_task(queue='operations',lease_seconds=60)
    assert job['task_id']==accepted['task_id']
    ref=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=run.run_id,command=command,input=INPUT,owner_epoch=1)['activation_ref']
    assert worker.authorize_runtime_activation(activation_ref=ref,command=command,operation='start',input=INPUT)['authorized'] is True
    return run,command,ref,job


@pytest.fixture
def outcomes(planning):
    f=planning;plan=f['plan'];admin=f['admin']
    worker=outcome_worker(plan,admin,'-nx024-outcome-worker')
    s=spec(plan);f['port']().establish(s)
    run,command,ref,job=reevaluation_run(plan,worker,'a')
    f['port']().link_run(plan_id=s['plan_id'],version=1,run_id=run.run_id,triggers=[{'kind':'reassess_due','cause':'system'}])
    yield dict(f,worker=worker,spec=s,run=run,command=command,ref=ref,job=job)


def plan_events(admin,plan_id):
    return admin.execute('select version,status from runtime.nexloop_plan_events where plan_id=%s order by event_id',(plan_id,)).fetchall()


def test_normal_outcome_records_once_replays_and_conflicts(outcomes):
    """no_action is a normal result; the same body replays, a different body for the same Run is a conflict."""
    f=outcomes;admin=f['admin'];w=f['worker']
    first=w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome())
    assert first['recorded'] is True and first['replay'] is False and first['kind']=='no_action' and first['version']==1 and first['new_version'] is None
    assert w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome())['replay'] is True
    with pytest.raises(psycopg.errors.SerializationFailure):
        w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome('escalate'))
    assert admin.execute('select kind,plan_id::text,version from runtime.nexloop_plan_outcomes where run_id=%s',(f['run'].run_id,)).fetchone()==('no_action',f['spec']['plan_id'],1)
    with pytest.raises(psycopg.Error):admin.execute("update runtime.nexloop_plan_outcomes set kind='escalate' where run_id=%s",(f['run'].run_id,))


def test_plan_update_supersedes_and_an_old_version_run_cannot_write(outcomes):
    """plan_update appends version 2 (superseded/active events, self-caused due); a Run linked to v1 then cannot record."""
    f=outcomes;admin=f['admin'];w=f['worker'];plan_id=f['spec']['plan_id']
    due=(datetime.now(UTC)+timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00','Z')
    update={'strategy':'先问是否需要发票，再确认续费','steps':[step('ask-invoice',reassess_at=due)]}
    # A second Run linked to v1 before the update lands.
    run2,command2,ref2,_=reevaluation_run(f['plan'],w,'b')
    f['port']().link_run(plan_id=plan_id,version=1,run_id=run2.run_id,triggers=[{'kind':'external_result','cause':'system'}])
    result=w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome('plan_update',plan_update=update))
    assert result['new_version']==2 and result['version']==1
    assert plan_events(admin,plan_id)[-2:]==[(1,'superseded'),(2,'active')]
    row=admin.execute('select created_by_run,strategy_ref from runtime.nexloop_plans where plan_id=%s and version=2',(plan_id,)).fetchone()
    assert str(row[0])==f['run'].run_id and row[1]=='strategy:'+plan_id+'@2'
    assert admin.execute('select step_key from runtime.nexloop_plan_steps where plan_id=%s and version=2',(plan_id,)).fetchall()==[('ask-invoice',)]
    with pytest.raises(psycopg.errors.SerializationFailure):
        w.record_plan_outcome(activation_ref=ref2,command=command2,outcome=outcome())
    assert admin.execute('select count(*) from runtime.nexloop_plan_outcomes where run_id=%s',(run2.run_id,)).fetchone()[0]==0


def test_action_intent_must_be_submitted_by_this_run(outcomes):
    """An intent_ref the Run did not submit is refused; one it submitted through the governed bridge is accepted."""
    f=outcomes;w=f['worker']
    with pytest.raises(PlanUnavailable):
        w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome('action_intent',intent_ref=str(uuid.uuid4())))
    submitted=w.runtime_effect_tool(activation_ref=f['ref'],command=f['command'],tool_operation='submit',parameters={'message':'service'})
    intent=submitted['receipt']['intent_id']
    result=w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome('action_intent',intent_ref=intent))
    assert result['kind']=='action_intent'
    assert str(f['admin'].execute('select intent_ref from runtime.nexloop_plan_outcomes where run_id=%s',(f['run'].run_id,)).fetchone()[0])==intent


def test_identity_failures_write_nothing(outcomes):
    """Wrong activation, changed command, an unlinked Run, a worker without the task lease, or a bad shape: no row."""
    f=outcomes;admin=f['admin'];w=f['worker']
    def count():return admin.execute('select count(*) from runtime.nexloop_plan_outcomes').fetchone()[0]
    with pytest.raises(REFUSED):
        w.record_plan_outcome(activation_ref='activation_'+str(uuid.uuid4()),command=f['command'],outcome=outcome())
    changed=copy.deepcopy(f['command']);changed['consumer_ref']='consumer:'+'f'*64
    with pytest.raises(REFUSED):
        w.record_plan_outcome(activation_ref=f['ref'],command=changed,outcome=outcome())
    with pytest.raises(ValueError):
        w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome(tenant_id=f['tenant']))
    with pytest.raises(ValueError):
        w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome('plan_update'))
    # The fixture's runtime worker holds another activation's lease but lacks nexloop.plan.outcome:1.
    with pytest.raises(REFUSED):
        f['plan']['worker'].record_plan_outcome(activation_ref=f['plan']['activations'][0],command=f['plan']['commands'][0],outcome=outcome())
    # An activated Run never linked to a plan cannot record.
    run3,command3,ref3,_=reevaluation_run(f['plan'],w,'c')
    with pytest.raises(PlanUnavailable):
        w.record_plan_outcome(activation_ref=ref3,command=command3,outcome=outcome())
    # A second outcome-capable worker cannot record for a Run whose task lease it does not hold.
    other=outcome_worker(f['plan'],admin,'-nx024-other-worker')
    with pytest.raises(REFUSED):
        other.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome())
    assert count()==0


def test_link_run_requires_a_known_run_credential(outcomes):
    f=outcomes
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        f['port']().link_run(plan_id=f['spec']['plan_id'],version=1,run_id=str(uuid.uuid4()),triggers=[])
    assert isinstance(f['port'](),PlanPort)


def test_external_result_for_an_outcome_intent_wakes_the_plan(outcomes,tmp_path):
    """Ruling 4: an external result arriving for an intent the plan recorded is a reevaluation trigger (T6).
    The executor dispatches and queries a synthetic loopback provider through the governed execution ledger."""
    from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
    from support.effect_provider import effect_provider
    from test_plan_reevaluation_pg import feed
    f=outcomes;w=f['worker'];admin=f['admin']
    intent=w.runtime_effect_tool(activation_ref=f['ref'],command=f['command'],tool_operation='submit',parameters={'message':'service'})['receipt']['intent_id']
    w.record_plan_outcome(activation_ref=f['ref'],command=f['command'],outcome=outcome('waiting_external',intent_ref=intent))
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")  # probe only the new trigger
    from nexloop_eios.backend import open_backend
    from psycopg.conninfo import make_conninfo
    p=f['plan']
    with open_backend(database_url=make_conninfo(p['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'executor-artifacts',
            signing_key_file=p['signing_key'],signing_key_id=p['signing_key_id']) as backend,effect_provider(tmp_path/'provider-nx024.sqlite') as provider:
        executor=backend.authenticate(p['executor_token'],world='real')
        transport=HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1))
        claim=executor.claim_effect(lease_seconds=30)
        assert claim['intent_id']==intent
        frozen=executor.prepare_effect_dispatch(intent_id=intent,fence=claim['fence'],provider_profile_digest=transport.profile_digest)
        sent=transport.dispatch(intent_id=intent,payload_digest=frozen['provider_payload_digest'],parameters=frozen['parameters'])
        executor.record_effect_observation(intent_id=intent,fence=claim['fence'],provider_profile_digest=transport.profile_digest,
            provider_payload_digest=sent.payload_digest,provider_state=sent.state,provider_reference=sent.provider_reference)
    (key,payload,status,_),=feed(admin)
    assert key=='plan:'+f['spec']['plan_id'] and status=='pending'
    assert payload['triggers'][-1]['kind']=='external_result' and payload['triggers'][-1]['cause']!='self'
