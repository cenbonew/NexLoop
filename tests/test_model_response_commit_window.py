"""Exact provider-result-before-SQLite-classification SIGKILL test.

Actual opaque activation TLS guard + real EIOS SourceRun/queue + frozen Pi and
local51 genuine file effect. No authorize callback granting access. PG execution
uses isolated disposable fixtures; offline collection is not runtime evidence.
"""
from contextlib import contextmanager
import json,os,queue,secrets,shutil,signal,sqlite3,subprocess,threading,time
from pathlib import Path
import pytest
from psycopg.conninfo import make_conninfo
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_dispatch import EffectDispatcher
from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
from nexloop_eios.local_json_delivery import JsonExportStore,DeliveryAuthorityPort,DeliveryServerConfiguration,make_server
from runtime_effect_fixture import runtime_effect_plan,PrivatePlan
from test_agent_host import files,free_port
from test_runtime_host_admission import guard_server
from test_runtime_effect_recovery import fresh_runtime_worker
from test_runtime_effect_tools import tool_evidence
from test_runtime_pg_bridge import acquire_owner
ROOT=Path(__file__).resolve().parents[1]
LOST='model-returned-uncommitted';REBUILT='model-regenerated-after-kill'


def private(path,value):path.write_text(value);path.chmod(0o600);return path


@contextmanager
def driver(runtime,command,activation_ref,port,key,*,resume=False):
    node=shutil.which('node');assert node and subprocess.check_output([node,'--version'],text=True).startswith('v24.')
    unused,fd=acquire_owner(runtime);process=None;events=queue.Queue();errors=[]
    cfg={'root':str(runtime),'command':command,'activation_ref':activation_ref,'resume':resume,
        'guard_url':f'https://127.0.0.1:{port}/internal/v1/runtime/authorize','guard_key_file':str(key),
        'guard_ca_file':str(key.parent/'host-cert.pem'),'input':'persist one service intent'}
    try:
        process=subprocess.Popen([node,'--disable-warning=ExperimentalWarning',str(ROOT/'tests/pi-response-window-driver.mjs')],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,pass_fds=(fd,),env={'PATH':os.environ['PATH'],'NEXLOOP_TEST_OWNER_FD':str(fd)})
        os.close(fd);fd=None
        def output():
            for line in process.stdout:events.put(json.loads(line))
        def diagnostic():
            for line in process.stderr:errors.append(line)
        readers=[threading.Thread(target=output,daemon=True),threading.Thread(target=diagnostic,daemon=True)]
        for reader in readers:reader.start()
        process.stdin.write(json.dumps(cfg)+'\n');process.stdin.flush()
        yield process,events,errors
    finally:
        if fd is not None:os.close(fd)
        if process is not None:
            if process.poll() is None:process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()
            for reader in readers:reader.join(5)


def receive(events,expected):
    event=events.get(timeout=15)
    assert event['event']==expected
    return event


def durable_rows(database):
    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
        entries=[json.loads(row[0]) for row in db.execute('select record from entries order by id')]
        tasks=[(kind,json.loads(record)) for kind,record in db.execute('select kind,record from tasks order by id')]
        statuses=db.execute('select status from submissions').fetchall()
    return entries,tasks,statuses


@contextmanager
def actual_local_delivery(plan,tmp_path):
    token=private(tmp_path/'real-local-executor',plan['executor_token'])
    transport=private(tmp_path/'real-local-transport',secrets.token_hex(32))
    origin='https://127.0.0.1:'+str(free_port())
    provider=HttpEffectProvider(EffectProviderConfiguration(origin,credential_file=str(transport),ca_file=str(tmp_path/'host-cert.pem'),timeout=1))
    config=DeliveryServerConfiguration(origin,tmp_path/'host-cert.pem',tmp_path/'host-key.pem',transport)
    root=tmp_path/'real-export';root.mkdir(mode=0o700)
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'real-local-authority-artifacts',
        signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        store=JsonExportStore(root);authority=DeliveryAuthorityPort(backend,token,provider.profile_digest)
        server=make_server(config,authority,store);actual=server.RequestHandlerClass;requests=[]
        class ActualCount(actual):
            def do_POST(self):requests.append(('POST',self.path));return super().do_POST()
            def do_GET(self):requests.append(('GET',self.path));return super().do_GET()
        server.RequestHandlerClass=ActualCount
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05});thread.start()
        try:yield backend.authenticate(plan['executor_token'],world='real'),provider,root,requests
        finally:server.shutdown();server.server_close();thread.join(5);store.close();assert not thread.is_alive()


def test_actual_returned_second_model_response_before_sqlite_commit_sigkill_same_pg_intent(runtime_effect_plan,admin,tmp_path):
    plan=runtime_effect_plan;command=plan['commands'][0];job=plan['jobs'][0];ref=plan['activations'][0]
    runtime,unused=files(tmp_path)
    key=private(tmp_path/'real-guard-key',secrets.token_hex(32))
    database=runtime/command['run_id']/'runtime.sqlite'
    with actual_local_delivery(plan,tmp_path) as (executor,provider,products,requests):
        with guard_server(plan['worker'],tmp_path,key) as port:
            plan['worker'].renew_task(queue='operations',task_id=job['task_id'],fence=job['fence'],lease_seconds=3)
            with driver(runtime,command,ref,port,key) as (process,events,errors):
                original=receive(events,'accepted')['receipt']
                returned=receive(events,'model_response_returned_before_sqlite_commit')
                assert returned['tool_call_ids']==[LOST] and returned['run_id']==command['run_id']
                assert len(returned['response_digest'])==64
                entries,tasks,statuses=durable_rows(database)
                # Model terminal response has returned at actual afterResponse;
                # neither AssistantEntry nor ToolTask classification is committed.
                assert LOST not in json.dumps(entries)
                tool_tasks=[record for kind,record in tasks if record['kind']=='pi.tool']
                assert len(tool_tasks)==1 and LOST not in json.dumps(tool_tasks)
                assert statuses!=[('done',)]
                calls,receipts=tool_evidence(database)
                assert [c['id'] for c in calls if c['name']=='nexloop.service.request']==['service-request-first']
                assert receipts and receipts[0]['business_action_success'] is False
                intent=receipts[0]['intent_id'];receipt_id=receipts[0]['receipt_id']
                effect=EffectDispatcher(executor,provider,lease_seconds=30).run_once()
                assert effect['business_action_success'] is True
                product=products/(intent+'.export.json');bytes_before=product.read_bytes();inode=database.stat().st_ino
                # Actual kernel owner rejects a second process's lock acquisition.
                with pytest.raises(BlockingIOError):acquire_owner(runtime)
                process.kill();assert process.wait(timeout=10)==-signal.SIGKILL
                assert errors==[]
            time.sleep(3.1)
            with fresh_runtime_worker(plan,tmp_path) as worker:
                reclaimed=worker.claim_task(queue='operations',lease_seconds=30)
                assert reclaimed['task_id']==job['task_id'] and reclaimed['fence']>job['fence']
                activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=reclaimed['fence'],
                    run_id=command['run_id'],command=command,input='persist one service intent',owner_epoch=command['runtime_owner_epoch'])
                with guard_server(worker,tmp_path,key) as port:
                    with driver(runtime,command,activation['activation_ref'],port,key,resume=True) as (process,events,errors):
                        restored=receive(events,'accepted')['receipt'];inspection=receive(events,'completed')['inspection']
                        assert process.wait(timeout=10)==0 and errors==[]
                        assert restored==original and database.stat().st_ino==inode
                        assert inspection['persistence']=={'journal_mode':'wal','synchronous':2}
                        assert inspection['runtime_outcome']=='succeeded'
                entries,tasks,statuses=durable_rows(database)
                tool_tasks=[record for kind,record in tasks if record['kind']=='pi.tool']
                assert LOST not in json.dumps(tool_tasks) and REBUILT in json.dumps(tool_tasks)
                assert statuses==[('done',)]
                calls,receipts=tool_evidence(database)
                assert all(r['intent_id']==intent and r['receipt_id']==receipt_id for r in receipts)
                assert any(c['id']==REBUILT for c in calls)
                worker.finish_task(queue='operations',task_id=job['task_id'],fence=reclaimed['fence'],status='succeeded',result={'run_id':command['run_id'],'runtime_outcome':'succeeded'})
            assert product.read_bytes()==bytes_before
            assert requests==[('POST','/v1/effects')]
            actual=executor.read_effect_receipt(intent_id=intent)
            assert actual['receipt_id']==receipt_id and actual['business_action_success'] is True
            assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
            assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()==(1,)
            assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(1,)
            assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,)
    for path in runtime.rglob('*'):
        if path.is_file():
            assert all(run.token.encode() not in path.read_bytes() for run in plan['runs'])
            assert key.read_bytes() not in path.read_bytes()
