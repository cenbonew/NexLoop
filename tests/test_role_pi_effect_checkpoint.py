"""Actual Pi/PG Role Run integration, with explicit legacy Context limitation.

Provider is an owned durable loopback fault service, not a real channel/model.
"""
import json,secrets,time,sqlite3
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from psycopg.conninfo import make_conninfo
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
from test_agent_host import files
from test_runtime_host_admission import guard_server,host,completed
from test_runtime_effect_tools import effect_configuration,tool_evidence
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
from nexloop_eios.effect_dispatch import EffectDispatcher
from support.effect_provider import effect_provider

def test_actual_two_pi_role_runs_one_shared_intent_one_real_loopback_effect(role_runtime_plan,admin,tmp_path,monkeypatch):
    plan=role_runtime_plan
    from nexloop_eios.backend import AuthenticatedServices
    measured=[]
    original=AuthenticatedServices.authorize_runtime_activation
    def measured_authorize(self,**kwargs):
        started=time.monotonic()
        try:
            value=original(self,**kwargs);measured.append({'operation':kwargs['operation'],'elapsed':round(time.monotonic()-started,3),'allowed':True});return value
        except Exception:
            measured.append({'operation':kwargs['operation'],'elapsed':round(time.monotonic()-started,3),'allowed':False});raise
    monkeypatch.setattr(AuthenticatedServices,'authorize_runtime_activation',measured_authorize)
    runtime,key=files(tmp_path)
    guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    with guard_server(plan['worker'],tmp_path,guard_key) as port:
        config=effect_configuration(tmp_path,port,guard_key);body=json.loads(config.read_text());body.pop('deterministic_effect_message');body.update(deterministic_message_from_input=True,context_input_protocol='nexloop.context-pack.v3');config.write_text(json.dumps(body))
        with host(runtime,key,config) as (_,client,headers):
            def run(index):
                command=plan['commands'][index];activation=plan['activations'][index]
                admitted=client.post('/internal/v1/runs/start',headers=headers,json={'activation_ref':activation,'command':command,'input':plan['context_packs'][command['run_id']]['input']})
                assert admitted.status_code==202
                deadline=time.monotonic()+30
                while True:
                    response=client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':activation,'command':command})
                    assert response.status_code==200,measured
                    result=response.json()
                    if result['submission_status']=='done':break
                    assert time.monotonic()<deadline,{'submission_status':result['submission_status']}
                    time.sleep(.05)
                assert result['runtime_outcome']=='succeeded' and result['persistence']=={'journal_mode':'wal','synchronous':2}
                database=runtime/command['run_id']/'runtime.sqlite'
                calls,receipts=tool_evidence(database)
                with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as c:
                    records=[json.loads(row[0]) for row in c.execute('select record from entries order by id')]
                model_inputs=[item for record in records for item in record.get('model',[]) if item.get('role')=='user']
                observed=json.dumps(model_inputs)
                assert 'nexloop.context-pack.v3' in observed and 'governed service responsibility' in observed
                assert 'service_trigger' in observed and 'eios:role-trigger:' in observed
                assert len(receipts)==3 and receipts[0]==receipts[1]==receipts[2]
                assert [call['id'] for call in calls if call['name']=='nexloop.service.request']==['message-service-first','message-service-rebuilt']
                return receipts[0]
            receipts=[run(index) for index in range(2)]
    assert receipts[0]==receipts[1] and receipts[0]['business_action_success'] is False
    intent=receipts[0]['intent_id']
    assert admin.execute('select count(distinct principal_id),count(distinct run_id) from runtime.nexloop_effect_submissions where intent_id=%s',(intent,)).fetchone()==(2,2)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,1)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'effect-worker-artifacts',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        executor=backend.authenticate(plan['executor_token'],world='real')
        with effect_provider(tmp_path/'role-provider.sqlite') as provider:
            dispatcher=EffectDispatcher(executor,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)),lease_seconds=3)
            dispatched=dispatcher.run_once()
            assert dispatched['claimed'] is True and dispatched['provider_state']=='accepted' and dispatched['business_action_success'] is False
            snapshot=provider.control('snapshot');assert snapshot['effects']==1 and snapshot['requests']==[('POST',intent,202)]
            provider.control('fulfill',intent_id=intent);time.sleep(3.1)
            settled=dispatcher.run_once()
            assert settled['status']=='fulfilled' and settled['business_action_success'] is True
            assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]
    assert admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==('fulfilled',True)
    for path in runtime.rglob('*'):
        if path.is_file():assert all(run.token.encode() not in path.read_bytes() for run in plan['runs'])
