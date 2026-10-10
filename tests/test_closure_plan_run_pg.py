"""NX-025 E2E-B: context → plan → reevaluation Run with the actual Host and Pi → run-outcome write-back.

A due plan is reevaluated by the plan worker: production Role issuance, Context v6 carrying the plan (G4),
activation by a runtime worker that holds the queue grant and nexloop.plan.outcome:1; the actual Host runs Pi with
the explicit deterministic plan protocol (test profile; no semantic inference), which reads the plan item from the
bound v6 pack and records the outcome through the Host tool → guard HTTPS → signed SQL port.

* AT-029: KR not met, no reasonable contact → normal no_action: no intent, no outbox, no outbound Message;
  the plan stays active and waits for its own reassess time.
* Action path: the Run submits one governed service intent, records action_intent naming it; the executor's
  observation of that intent wakes the plan again (T6) and the next reevaluation Run is actually launched.
Guard-side request durations are recorded per operation (2 s tool limit, ADR-022 / NX-049). Synthetic data only.
"""
from contextlib import contextmanager
import json
import secrets
import time

import pytest
import runtime_effect_fixture as fixture
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_outcome_pg import outcome_worker_and_spawn
from test_plan_role_launcher_pg import role_planning,within_ceiling  # noqa: F401
from test_plan_reevaluation_pg import SETTINGS,feed,make_due


@pytest.fixture
def role_settings():
    # Configurable, not hard-coded (decision 3): the actual-Host tests run the deterministic test profile.
    return {**SETTINGS,'runtime_profile':'deterministic-test'}

TOOL_LIMIT_SECONDS=2.0


class Timed:
    """The guard's worker, with each guard-side request timed (no behaviour change)."""
    def __init__(self,worker):self._worker,self.durations=worker,[]
    def __getattr__(self,name):
        target=getattr(self._worker,name)
        if not callable(target):return target
        def timed(*args,**kwargs):
            start=time.monotonic()
            try:return target(*args,**kwargs)
            finally:self.durations.append((name,kwargs.get('operation') or kwargs.get('tool_operation'),time.monotonic()-start))
        return timed


def launched(f,plan_id):
    """The Run the plan worker just launched: (run_id, command, input) from its durable runtime task."""
    admin=f['admin']
    (run_id,)=admin.execute('select run_id::text from runtime.nexloop_plan_runs where plan_id=%s order by created_at desc limit 1',(plan_id,)).fetchone()
    (task_id,)=admin.execute('select task_id from authz.nexloop_runtime_run_bindings where run_id=%s',(run_id,)).fetchone()
    (payload,)=admin.execute('select normalized_input from runtime.jobs where job_id::text=%s',(task_id,)).fetchone()
    return run_id,payload['run_command'],payload['input']


@contextmanager
def actual_host(f,tmp_path,worker,mode,spawn=None):
    from test_agent_host import files
    from test_runtime_host_admission import guard_server,host
    from test_runtime_effect_tools import effect_configuration
    runtime,key=files(tmp_path)
    guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    with guard_server(worker,tmp_path,guard_key,spawn=spawn) as port:
        config=effect_configuration(tmp_path,port,guard_key);body=json.loads(config.read_text());body.pop('deterministic_effect_message')
        body.update(context_input_protocol='nexloop.context-pack.v6',plan_outcome_tool=True,deterministic_plan_outcome=mode);config.write_text(json.dumps(body))
        with host(runtime,key,config) as (_,client,headers):yield runtime,client,headers


def run_on_host(client,headers,worker,command,text):
    """Claim the Run's task as the outcome-capable runtime worker, activate it, start it on the Host, wait."""
    job=worker.claim_task(queue='operations',lease_seconds=60)
    activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=command['run_id'],command=command,
        input=text,owner_epoch=1)['activation_ref']
    admitted=client.post('/internal/v1/runs/start',headers=headers,json={'activation_ref':activation,'command':command,'input':text})
    assert admitted.status_code==202,admitted.text
    deadline=time.monotonic()+60
    while True:
        response=client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':activation,'command':command})
        assert response.status_code==200;result=response.json()
        if result['submission_status']=='done':return result
        assert time.monotonic()<deadline;time.sleep(.05)


def tool_names(runtime,run_id):
    from test_runtime_effect_tools import tool_evidence
    calls,_=tool_evidence(runtime/run_id/'runtime.sqlite')
    return [c['name'] for c in calls]


def assert_within_tool_limit(timed):
    from test_runtime_host_admission import guard_workers
    if guard_workers()>1:
        # Multi-process guard (deployment, ADR-024): the Host's requests are served by child processes, not timed here;
        # the Host's own 2 s timeout still applies to each of them (a late one fails the Run or its inspect with 503).
        return
    summary={}
    for name,op,d in timed.durations:summary.setdefault(f'{name}:{op}',[]).append(round(d*1000))
    print('NX025_GUARD_MS',json.dumps(summary,sort_keys=True))  # evidence: guard-side milliseconds per request (pytest -s)
    slow=[(name,op,round(d,3)) for name,op,d in timed.durations if d>=TOOL_LIMIT_SECONDS]
    assert timed.durations and not slow,slow


def test_no_reasonable_contact_is_a_normal_no_action(role_planning,admin,tmp_path):
    """AT-029 end to end on the actual Host: KR not met, yet no message is forced."""
    f=role_planning
    # Provision the runtime worker first: seeding authority later would change the Source directory the Run was issued under.
    service,spawn=outcome_worker_and_spawn(f['plan'],admin,'-nx025-runtime-a');worker=Timed(service)
    s=f['establish'](steps=[within_ceiling()])
    assert f['worker']().run_once()['launched']==1
    run_id,command,text=launched(f,s['plan_id'])
    pack=json.loads(text)
    assert [i['ref'] for i in pack['open_work'] if i['subsection']=='plan']==[f"nexloop:plan:{s['plan_id']}@1"]
    with actual_host(f,tmp_path,worker,'no_action',spawn) as (runtime,client,headers):
        result=run_on_host(client,headers,worker,command,text)
    assert result['runtime_outcome']=='succeeded'
    assert tool_names(runtime,run_id)==['nexloop.plan.outcome']
    kind,version,intent,reasons=admin.execute("select kind,version,intent_ref,outcome->'reasons' from runtime.nexloop_plan_outcomes where run_id=%s",(run_id,)).fetchone()
    assert (kind,version,intent)==('no_action',1,None) and reasons[0].startswith(f"plan:{s['plan_id']}@1")  # the outcome cites the plan it read
    # Normal result, not failure: no intent, no outbox, no outbound Message for this Run; the plan stays active.
    assert admin.execute('select count(*) from runtime.nexloop_effect_submissions where run_id=%s',(run_id,)).fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_outbound_messages').fetchone()==(0,)
    assert admin.execute('select version,status from runtime.nexloop_plan_events where plan_id=%s order by event_id desc limit 1',(s['plan_id'],)).fetchone()==(1,'active')
    # Every model request of this Run was recorded against its v6 Context (AT-027 on the reevaluation Run).
    from test_context_v6_pg import model_requests
    assert len(model_requests(admin,f['plan']['tenant']))>=2
    assert_within_tool_limit(worker)


def test_action_intent_then_external_result_launches_the_next_reevaluation(role_planning,admin,tmp_path):
    """Action path: governed intent → action_intent outcome → executor observation (T6) → next bounded Run."""
    from nexloop_eios.backend import open_backend
    from nexloop_eios.effect_provider import EffectProviderConfiguration,HttpEffectProvider
    from psycopg.conninfo import make_conninfo
    from support.effect_provider import effect_provider
    f=role_planning;p=f['plan']
    service,spawn=outcome_worker_and_spawn(p,admin,'-nx025-runtime-b');worker=Timed(service)
    s=f['establish'](steps=[within_ceiling()])
    assert f['worker']().run_once()['launched']==1
    run_id,command,text=launched(f,s['plan_id'])
    with actual_host(f,tmp_path,worker,'action_intent',spawn) as (runtime,client,headers):
        assert run_on_host(client,headers,worker,command,text)['runtime_outcome']=='succeeded'
    assert tool_names(runtime,run_id)==['nexloop.service.request','nexloop.plan.outcome']
    (intent,)=admin.execute('select intent_id::text from runtime.nexloop_effect_submissions where run_id=%s',(run_id,)).fetchone()
    assert admin.execute('select kind,intent_ref::text from runtime.nexloop_plan_outcomes where run_id=%s',(run_id,)).fetchone()==('action_intent',intent)
    assert_within_tool_limit(worker)
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")  # observe only the external-result wake
    with open_backend(database_url=make_conninfo(p['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'executor-artifacts',
            signing_key_file=p['signing_key'],signing_key_id=p['signing_key_id']) as backend,effect_provider(tmp_path/'provider-nx025.sqlite') as provider:
        executor=backend.authenticate(p['executor_token'],world='real')
        transport=HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1))
        claim=executor.claim_effect(lease_seconds=30);assert claim['intent_id']==intent
        frozen=executor.prepare_effect_dispatch(intent_id=intent,fence=claim['fence'],provider_profile_digest=transport.profile_digest)
        sent=transport.dispatch(intent_id=intent,payload_digest=frozen['provider_payload_digest'],parameters=frozen['parameters'])
        executor.record_effect_observation(intent_id=intent,fence=claim['fence'],provider_profile_digest=transport.profile_digest,
            provider_payload_digest=sent.payload_digest,provider_state=sent.state,provider_reference=sent.provider_reference)
    (key,payload,status,_),=feed(admin)
    assert key=='plan:'+s['plan_id'] and payload['triggers'][-1]['kind']=='external_result'
    # The external result is a reevaluation trigger: the next bounded Run is actually launched for the same plan version.
    summary=f['worker']().run_once()
    assert summary['launched']==1,summary
    assert admin.execute('select count(*) from runtime.nexloop_plan_runs where plan_id=%s and version=1',(s['plan_id'],)).fetchone()==(2,)
