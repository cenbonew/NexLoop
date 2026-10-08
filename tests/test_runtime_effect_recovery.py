"""Actual Pi→PG intent→durable provider crossed SIGKILL recovery, synthetic only."""
from contextlib import contextmanager
import secrets
import signal
import sqlite3
import threading
import time

from psycopg.conninfo import make_conninfo
from nexloop_eios.backend import open_backend
from runtime_effect_fixture import runtime_effect_plan,PrivatePlan
from support.effect_provider import effect_provider
from test_effect_dispatch import independent_worker
from test_agent_host import files
from test_runtime_host_admission import guard_server,host,completed
from test_runtime_effect_tools import effect_configuration,tool_evidence


class ExecutorConfiguration:
    def __init__(self,plan,tmp_path):self.plan,self.tmp_path=plan,tmp_path
    def executor_configuration(self):
        return PrivatePlan(database_url=make_conninfo(self.plan['pg'],user='nexloop_action_worker'),
            signing_key_file=self.plan['signing_key'],signing_key_id=self.plan['signing_key_id'],
            artifact_root=self.tmp_path/'effect-worker-artifacts',service_token=self.plan['executor_token'],world='real')


@contextmanager
def fresh_runtime_worker(plan,tmp_path):
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_domain_worker'),
        artifact_root=tmp_path/'reopened-runtime-worker',signing_key_file=plan['signing_key'],
        signing_key_id=plan['signing_key_id']) as backend:
        yield backend.authenticate(plan['worker_token'],world='real')


def test_same_pi_run_survives_host_and_effect_worker_kill_query_first(runtime_effect_plan,admin,tmp_path):
    plan=runtime_effect_plan;command=plan['commands'][0];job=plan['jobs'][0];old_ref=plan['activations'][0]
    runtime,key=files(tmp_path)
    guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    generation_paused=threading.Event();release_generation=threading.Event()
    class ActualGuardBarrier:
        def __init__(self):self.generations=0
        def __getattr__(self,name):return getattr(plan['worker'],name)
        def authorize_runtime_activation(self,**arguments):
            actual=plan['worker'].authorize_runtime_activation(**arguments)
            if arguments['operation']=='model':
                self.generations+=1
                # Adapter checks model authority before and after durable
                # budget reservation. Third check begins next Generation,
                # after the first ToolResult has actually been persisted.
                if self.generations==3:
                    generation_paused.set();release_generation.wait(1.8)
            return actual
    executor=ExecutorConfiguration(plan,tmp_path)
    with effect_provider(tmp_path/'durable-provider.sqlite') as provider:
        provider.control('pause')
        with independent_worker(executor,provider.origin,start_barrier=True) as (old_effect,effect_pipe):
            with guard_server(ActualGuardBarrier(),tmp_path,guard_key) as port:
                config=effect_configuration(tmp_path,port,guard_key)
                with host(runtime,key,config) as (old_host,client,headers):
                    plan['worker'].renew_task(queue='operations',task_id=job['task_id'],fence=job['fence'],lease_seconds=3)
                    body={'activation_ref':old_ref,'command':command,'input':'persist one service intent'}
                    started=client.post('/internal/v1/runs/start',headers=headers,json=body)
                    assert started.status_code==202;mapping=started.json()
                    assert generation_paused.wait(1),'second actual Generation did not reach guard'
                    calls,receipts=tool_evidence(runtime/command['run_id']/'runtime.sqlite')
                    assert receipts and receipts[0]['state']=='accepted' and receipts[0]['business_action_success'] is False
                    intent=receipts[0]['intent_id'];receipt_id=receipts[0]['receipt_id']
                    effect_pipe.send('start')
                    assert provider.wait('accepted_committed')['intent_id']==intent
                    original=admin.execute('select action_fencing_token,action_claim_revision from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()
                    assert original and admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==('dispatching',)
                    old_host.kill();assert old_host.wait(10)==-signal.SIGKILL
                    old_effect.kill();old_effect.join(10);assert old_effect.exitcode==-signal.SIGKILL
                    release_generation.set()
        assert provider.control('snapshot')['requests']==[('POST',intent,202)]
        provider.control('release');provider.control('fulfill',intent_id=intent)
        time.sleep(3.1)
        with fresh_runtime_worker(plan,tmp_path) as worker:
            reclaimed=worker.claim_task(queue='operations',lease_seconds=30)
            assert reclaimed['task_id']==job['task_id'] and reclaimed['fence']>job['fence']
            activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=reclaimed['fence'],
                run_id=command['run_id'],command=command,input='persist one service intent',owner_epoch=command['runtime_owner_epoch'])
            ref=activation['activation_ref']
            with guard_server(worker,tmp_path,guard_key) as port:
                with host(runtime,key,effect_configuration(tmp_path,port,guard_key)) as (_,client,headers):
                    stale=client.post('/internal/v1/runs/resume',headers=headers,json={'activation_ref':old_ref,'command':command,'input':'persist one service intent'})
                    assert stale.status_code==503
                    resumed=client.post('/internal/v1/runs/resume',headers=headers,json={'activation_ref':ref,'command':command,'input':'persist one service intent'})
                    assert resumed.status_code==202 and resumed.json()==mapping
                    restored=completed(client,headers,ref,command)
                    assert restored['runtime_outcome']=='succeeded'
                    assert restored['persistence']=={'journal_mode':'wal','synchronous':2}
                    assert restored['request_id']==command['request_id']
                    assert restored['conversation_id']==mapping['conversation_id'] and restored['submission_id']==mapping['submission_id']
                    with sqlite3.connect(runtime/command['run_id']/'runtime.sqlite') as database:
                        assert database.execute('select count(*) from submissions').fetchone()[0]==1
                    calls,receipts=tool_evidence(runtime/command['run_id']/'runtime.sqlite')
                    assert all(receipt['intent_id']==intent and receipt['receipt_id']==receipt_id for receipt in receipts)
                    with independent_worker(executor,provider.origin) as (new_effect,pipe):
                        assert pipe.poll(15);settled=pipe.recv();new_effect.join(10)
                        assert new_effect.exitcode==0 and settled['business_action_success'] is True
                    worker.finish_task(queue='operations',task_id=job['task_id'],fence=reclaimed['fence'],status='succeeded',result={'run_id':command['run_id'],'runtime_outcome':restored['runtime_outcome']})
        assert provider.control('snapshot')['effects']==1
        assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]
        row=admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()
        assert row==('fulfilled',True)
        claim=admin.execute("select claim from runtime.nexloop_action_claims where tenant_id=%s and world='real' and action_name='nexloop.service.request' and intent_id=%s",(plan['tenant'],intent)).fetchone()[0]
        assert claim['state']=='terminal' and claim['binding']['invocation_id']==intent
        assert claim['terminal_outcome']['status']=='succeeded' and claim['terminal_outcome']['outcome_id']==receipt_id
        config=executor.executor_configuration();token=config.pop('service_token');world=config.pop('world')
        with open_backend(**config) as backend:
            terminal=backend.authenticate(token,world=world).read_effect_receipt(intent_id=intent)
            assert terminal['receipt_id']==receipt_id and terminal['business_action_success'] is True
        assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,)
    for path in runtime.rglob('*'):
        if path.is_file():
            content=path.read_bytes()
            assert all(run.token.encode() not in content for run in plan['runs'])
            assert guard_key.read_bytes() not in content
