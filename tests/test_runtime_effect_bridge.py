"""Actual governed plan + opaque owned Runtime activation bridge, no mock auth."""
from contextlib import contextmanager
import copy
import secrets
import uuid
import httpx
import pytest
from nexloop_eios.effect_intents import EffectIntentConflict,EffectIntentUnavailable
from runtime_effect_fixture import runtime_effect_plan
from test_agent_host import files,trusted_tls
from test_runtime_host_admission import guard_server


@contextmanager
def activation(fixture,admin,pg,tmp_path,*,lease_seconds=30):
    worker=fixture['worker'];job=fixture['jobs'][0]
    if lease_seconds!=30:
        worker.renew_task(queue='operations',task_id=job['task_id'],fence=job['fence'],lease_seconds=lease_seconds)
    yield worker,fixture['activations'][0],fixture['commands'][0]


def ledger(admin):
    counts=tuple(admin.execute('select count(*) from '+table).fetchone()[0] for table in (
        'runtime.nexloop_effect_intents','runtime.nexloop_effect_outbox','runtime.nexloop_effect_submissions',
        'runtime.nexloop_effect_control_reservations','authz.nexloop_runtime_execution_markers'))
    return counts,admin.execute('select sum(reserved_units) from control.nexloop_effect_contexts').fetchone()[0],admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()[0]


def test_actual_bridge_submit_find_replay_and_payload_conflict(runtime_effect_plan,admin,pg,tmp_path):
    fixture=runtime_effect_plan
    with activation(fixture,admin,pg,tmp_path) as (worker,ref,command):
        first=worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='submit',parameters={'message':'service'})
        assert first['run_id']==fixture['runs'][0].run_id and first['receipt']['business_action_success'] is False
        snapshot=ledger(admin)
        assert worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='submit',parameters={'message':'service'})==first
        assert worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='find',intent_id=first['receipt']['intent_id'])==first
        with pytest.raises(EffectIntentConflict):
            worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='submit',parameters={'message':'different'})
        assert ledger(admin)==snapshot
        assert all(run.token not in repr(first) for run in fixture['runs'])


@pytest.mark.parametrize('fault',['activation','command','revocation','unknown_intent'])
def test_bridge_identity_failures_have_no_intent_or_marker_writes(runtime_effect_plan,admin,pg,tmp_path,fault):
    fixture=runtime_effect_plan
    with activation(fixture,admin,pg,tmp_path) as (worker,ref,command):
        before=ledger(admin);args={'activation_ref':ref,'command':command,'tool_operation':'submit','parameters':{'message':'service'}}
        if fault=='activation':args['activation_ref']='activation_'+str(uuid.uuid4())
        elif fault=='command':
            args['command']=copy.deepcopy(command);args['command']['consumer_ref']='eios:Consumer/'+'f'*64
        elif fault=='revocation':
            admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{grants}','[]'::jsonb) where tenant_id=%s and fact_kind='grants'",(fixture['tenant'],))
        else:args={'activation_ref':ref,'command':command,'tool_operation':'find','intent_id':str(uuid.uuid4())}
        with pytest.raises(EffectIntentUnavailable):worker.runtime_effect_tool(**args)
        assert ledger(admin)==before


def test_final_queue_lease_expiry_rolls_back_intent_quota_and_execution_marker(runtime_effect_plan,admin,pg,tmp_path):
    fixture=runtime_effect_plan
    # Synthetic delay only on the disposable technical ledger, not a production
    # migration or an invented authorizer. All identity/proof checks remain real.
    admin.execute("create function public.synthetic_bridge_delay() returns trigger language plpgsql as $$begin perform pg_sleep(2);return new;end$$")
    admin.execute('grant execute on function public.synthetic_bridge_delay() to nexloop_owner')
    admin.execute('create trigger synthetic_bridge_delay after insert on runtime.nexloop_effect_intents for each row execute function public.synthetic_bridge_delay()')
    with activation(fixture,admin,pg,tmp_path,lease_seconds=1) as (worker,ref,command):
        before=ledger(admin)
        with pytest.raises(EffectIntentUnavailable):
            worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='submit',parameters={'message':'service'})
        assert ledger(admin)==before
        assert before[0]==(0,0,0,0,0)


@pytest.mark.parametrize('field',['consumer_ref','goal_version_ref'])
def test_first_enrollment_wrong_runtime_refs_cannot_admit_intent(runtime_effect_plan,admin,field):
    fixture=runtime_effect_plan
    run=fixture['sources'][0].issue_run_credential(action_resources=['eios:action:nexloop.service.request:1'])
    fixture['planner'].bind_effect_context(step_id=fixture['step'],step_revision=1,goal_revision=1,
        consumer_revision=1,control_revision=1,run_id=run.run_id,run_token=run.token,
        executor_token=fixture['executor_token'])
    command=copy.deepcopy(fixture['commands'][0])
    command.update(run_id=run.run_id,request_id='wrong-refs-'+str(uuid.uuid4()),trigger_event_id=str(uuid.uuid4()),
        credential_ref='run_credential:'+run.run_id,not_after=run.expires_at.isoformat())
    command[field]='consumer:'+'f'*64 if field=='consumer_ref' else 'goal:'+fixture['goal']+':revision:2:step:1:control:1'
    accepted=fixture['owner'].accept_runtime_event(queue='operations',source_id='wrong-refs',event_id=str(uuid.uuid4()),
        run_token=run.token,command=command,input='synthetic intentionally wrong declarations')
    worker=fixture['worker'];job=worker.claim_task(queue='operations',lease_seconds=60)
    assert job['task_id']==accepted['task_id']
    registered=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],
        run_id=run.run_id,command=command,input='synthetic intentionally wrong declarations',owner_epoch=1)
    before=ledger(admin)
    with pytest.raises(EffectIntentUnavailable):
        worker.runtime_effect_tool(activation_ref=registered['activation_ref'],command=command,
            tool_operation='submit',parameters={'message':'service'})
    assert ledger(admin)==before


def test_actual_https_bridge_maps_conflict_409_and_denial_without_private_output(runtime_effect_plan,admin,pg,tmp_path):
    fixture=runtime_effect_plan
    with activation(fixture,admin,pg,tmp_path) as (worker,ref,command):
        _,key=files(tmp_path);guard_key=tmp_path/'bridge-guard-key'
        guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
        with guard_server(worker,tmp_path,guard_key) as port,httpx.Client(base_url=f'https://127.0.0.1:{port}',
            verify=trusted_tls(key),trust_env=False,timeout=5) as client:
            headers={'Authorization':'Bearer '+guard_key.read_text()}
            body={'activation_ref':ref,'command':command,'parameters':{'message':'service'}}
            path='/internal/v1/runtime/effects/submit'
            first=client.post(path,headers=headers,json=body);assert first.status_code==200
            assert client.post(path,headers=headers,json=body).json()==first.json()
            before=ledger(admin)
            conflict=client.post(path,headers=headers,json={**body,'parameters':{'message':'changed'}})
            assert conflict.status_code==409 and conflict.json()=={'code':'intent_payload_conflict'}
            assert ledger(admin)==before
            denied=client.post(path,headers=headers,json={**body,'activation_ref':'activation_'+str(uuid.uuid4())})
            assert denied.status_code==403 and denied.json()=={'code':'effect_intent_unavailable'}
            for response in (first,conflict,denied):
                assert all(run.token not in response.text for run in fixture['runs'])
                assert '_run_digest' not in response.text and 'parameters' not in response.json()
