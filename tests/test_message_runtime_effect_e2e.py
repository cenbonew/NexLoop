"""One actual Human Message→Run→Pi Tool→durable provider→Human Function.

Deterministic frozen Pi and synthetic effect service. No
business SQL writes, authority callbacks, MemoryStorage, real keys or channels.
"""
from contextlib import contextmanager
import hashlib,json,os,secrets,shutil,signal,sqlite3,subprocess,sys,time
from pathlib import Path

import httpx
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from nexloop_eios.backend import open_backend
from nexloop_eios.runtime_dispatch import RuntimeDispatcher
from nexloop_eios.host_control import HostControlConfiguration
from nexloop_eios.conversation_messages import ConversationDenied
from runtime_effect_fixture import PrivatePlan
from message_runtime_fixture import message_plan
from test_conversation_effect_receipts import receipt_plan,FUNCTION
from test_agent_host import files,free_port,trusted_tls
from test_runtime_host_admission import guard_server,completed,PrivateHeaders
from test_runtime_effect_tools import effect_configuration,tool_evidence
from test_effect_dispatch import independent_worker
from support.effect_provider import effect_provider
from authority_fixture import replace_fact

REPOSITORY_ROOT=Path(__file__).resolve().parents[1]
TRUSTED_HOST_ROOT=REPOSITORY_ROOT


def trusted_host_evidence():
    """Use this repository's compiled Host; local CI builds it before this test."""
    node=shutil.which('node');assert node
    assert subprocess.run([node,'--version'],check=True,capture_output=True,text=True).stdout.strip().startswith('v24.')
    relative=['scripts/agent_host.py']+[str(path.relative_to(REPOSITORY_ROOT)) for path in sorted((REPOSITORY_ROOT/'apps/agent-host/src').rglob('*.ts'))]
    hashes={}
    for name in relative:
        source=REPOSITORY_ROOT/name
        hashes[name]=hashlib.sha256(source.read_bytes()).hexdigest()
    entry=TRUSTED_HOST_ROOT/'apps/agent-host/dist/main.js'
    assert entry.is_file() and not entry.is_symlink()
    hashes['apps/agent-host/dist/main.js']=hashlib.sha256(entry.read_bytes()).hexdigest()
    return hashes


@contextmanager
def actual_host(runtime,key,configuration):
    trusted_host_evidence();port=free_port();node=shutil.which('node')
    process=subprocess.Popen([sys.executable,str(TRUSTED_HOST_ROOT/'scripts/agent_host.py'),'--node',node,
        '--runtime-root',str(runtime),'--internal-key-file',str(key),'--port',str(port),
        '--tls-certificate-file',str(key.parent/'host-cert.pem'),'--tls-key-file',str(key.parent/'host-key.pem'),
        '--runtime-config-file',str(configuration)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env={k:v for k,v in os.environ.items() if not k.startswith(('MODEL_','DATABASE_'))})
    with httpx.Client(base_url='https://127.0.0.1:'+str(port),verify=trusted_tls(key),trust_env=False,timeout=5) as client:
        try:
            deadline=time.monotonic()+10
            while True:
                assert process.poll() is None,'trusted actual Host exited'
                try:
                    if client.get('/health/live').status_code==200:break
                except httpx.TransportError:pass
                assert time.monotonic()<deadline;time.sleep(.02)
            yield process,client,PrivateHeaders(Authorization='Bearer '+key.read_text())
        finally:
            if process.poll() is None:process.terminate()
            stdout,stderr=process.communicate(timeout=10)
            assert key.read_text() not in stdout+stderr


class ExecutorConfiguration:
    def __init__(self,fixture,tmp_path):self.fixture,self.tmp_path=fixture,tmp_path
    def __repr__(self):return '<private actual candidate executor configuration>'
    def executor_configuration(self):
        f=self.fixture
        return PrivatePlan(database_url=make_conninfo(f['pg'],user='nexloop_action_worker'),
            signing_key_file=f['signing_key'],signing_key_id='runtime-effect',
            artifact_root=self.tmp_path/'effect-worker-artifacts',service_token=f['executor_token'],world='real')


@contextmanager
def fresh_worker(f,tmp_path):
    with open_backend(database_url=make_conninfo(f['pg'],user='nexloop_domain_worker'),artifact_root=tmp_path/'fresh-domain-artifacts',
        signing_key_file=f['signing_key'],signing_key_id='runtime-effect') as backend:
        yield backend.authenticate(f['worker_token'],world='real')


def route(f,worker):
    f['bridge'].bind_message(message_id=f['message']['id'],run_token=f['run'].token,command=f['command'])
    queued=f['bridge'].deliver_one(run_token=f['run'].token)
    job=worker.claim_task(queue='operations',lease_seconds=60)
    assert job['task_id']==queued['task_id']
    activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],
        run_id=f['run'].run_id,command=f['command'],input=f['message']['body'],owner_epoch=1)
    return queued,job,activation['activation_ref']


def settle(executor,provider):
    with independent_worker(executor,provider.origin) as (process,pipe):
        assert pipe.poll(15)
        result=pipe.recv();process.join(10)
        assert process.exitcode==0 and result['event']=='settled' and result['claimed'] is True
        return result


def await_available(admin,intent):
    deadline=time.monotonic()+5
    while admin.execute('select available_at>clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()[0]:
        assert time.monotonic()<deadline;time.sleep(.05)


def test_same_message_pi_effect_worker_and_current_human_receipt(receipt_plan,admin,tmp_path):
    f=receipt_plan;command=f['command'];runtime,key=files(tmp_path)
    guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    assert f['receipt_query']()=={'message_id':f['message']['id'],'run':None,'receipt':None}
    with fresh_worker(f,tmp_path) as worker:
        f['bridge'].bind_message(message_id=f['message']['id'],run_token=f['run'].token,command=command)
        queued=f['bridge'].deliver_one(run_token=f['run'].token)
        projected=f['receipt_query']()
        assert projected['run']['run_id']==command['run_id'] and projected['run']['task_id']==queued['task_id'] and projected['receipt'] is None
        with guard_server(worker,tmp_path,guard_key) as port:
            with actual_host(runtime,key,effect_configuration(tmp_path,port,guard_key)) as (_,client,headers):
                host_control=HostControlConfiguration(str(client.base_url).rstrip('/'),key,tmp_path/'host-cert.pem')
                dispatched=RuntimeDispatcher(worker,host_control,queue='operations',total_timeout=15).run_once()
                assert dispatched['claimed'] is True and dispatched['task_id']==queued['task_id'] and dispatched['status']=='succeeded'
                result=worker.inspect_task(queue='operations',task_id=queued['task_id'])['result']
                assert result['scope']=='runtime_only' and result['runtime_outcome']=='succeeded' and result['business_action_success'] is False
                assert result['run_id']==command['run_id']
                persistence=result['runtime_receipt']
                assert persistence['request_id']==command['request_id'] and persistence['persistence']=={'journal_mode':'wal','synchronous':2}
                with sqlite3.connect(runtime/command['run_id']/'runtime.sqlite') as database:
                    assert database.execute('select count(*) from submissions').fetchone()==(1,)
                    assert database.execute('select id from submissions').fetchone()[0]==persistence['submission_id']
                    assert database.execute('select count(*) from conversations where id=?',(persistence['conversation_id'],)).fetchone()==(1,)
                calls,receipts=tool_evidence(runtime/command['run_id']/'runtime.sqlite')
                requests=[call for call in calls if call['name']=='nexloop.service.request']
                assert [call['id'] for call in requests]==['service-request-first','service-request-rebuilt']
                assert len(receipts)==3 and receipts[0]==receipts[1]==receipts[2]
                intent=receipts[0]['intent_id'];receipt_id=receipts[0]['receipt_id']
                before=f['receipt_query']()['receipt']
                assert before['intent_id']==intent and before['receipt_id']==receipt_id and before['state']=='accepted'
                assert before['provider_state'] is None and before['governed_claim_finalized'] is False and before['business_action_success'] is False
                with effect_provider(tmp_path/'durable-message-provider.sqlite') as provider:
                    executor=ExecutorConfiguration(f,tmp_path)
                    accepted=settle(executor,provider)
                    assert accepted['status']=='dispatching' and accepted['provider_state']=='accepted' and accepted['business_action_success'] is False
                    observed=f['receipt_query']()['receipt']
                    assert observed['state']=='dispatching' and observed['provider_state']=='accepted'
                    assert observed['business_action_success'] is False and observed['governed_claim_finalized'] is False
                    # Fulfillment is a real independent provider ledger change;
                    # only actual subsequent HTTP GET supplies PG observation.
                    provider.control('fulfill',intent_id=intent);await_available(admin,intent)
                    terminal=settle(executor,provider)
                    assert terminal['status']=='fulfilled' and terminal['provider_state']=='fulfilled' and terminal['business_action_success'] is True
                    final=f['receipt_query']()['receipt']
                    assert final['intent_id']==intent and final['receipt_id']==receipt_id and final['state']=='fulfilled'
                    assert final['provider_state']=='fulfilled' and final['governed_claim_finalized'] is True and final['business_action_success'] is True
                    snapshot=provider.control('snapshot')
                    assert snapshot['effects']==1 and snapshot['requests']==[('POST',intent,202),('GET',intent,200)]
                    assert snapshot['persistence']=={'journal_mode':'wal','synchronous':2}
                    principal=f['issued'].session.principal_id
                    replace_fact(admin,f['tenant'],'grants',[principal,'eios:function:'+FUNCTION+':1'],F.GrantFacts,grants=[])
                    with pytest.raises(ConversationDenied):f['receipt_query']()
                    assert provider.control('snapshot')['requests']==snapshot['requests']
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_submissions').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(1,)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,1)
    assert admin.execute("select claim->>'state',claim->'terminal_outcome'->>'status' from runtime.nexloop_action_claims where action_name='nexloop.service.request'").fetchone()==('terminal','succeeded')
    assert admin.execute('select task_id,run_id::text from runtime.nexloop_message_routes').fetchone()==(queued['task_id'],command['run_id'])
    for path in runtime.rglob('*'):
        if path.is_file():
            contents=path.read_bytes()
            for secret in (f['run'].token,f['executor_token'],f['issued'].session_token.get_secret_value(),guard_key.read_text()):assert secret.encode() not in contents
