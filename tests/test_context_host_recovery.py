"""Actual core52 artifact/guard + independently compiled context Host + real51 IO."""
from contextlib import contextmanager
import hashlib,json,os,secrets,shutil,sqlite3,subprocess,sys,time
from pathlib import Path
import httpx
import pytest
from psycopg.conninfo import make_conninfo
from test_context_artifacts import context_message,assembled_message,business_plan,configured
from test_agent_host import files,free_port
from test_trusted_configuration_pg import private
from test_local_message_delivery_assembly import once,process
from test_runtime_effect_tools import effect_configuration,tool_evidence
ROOT=Path(__file__).resolve().parents[1]

@contextmanager
def context_host(runtime,key,configuration):
    port=free_port();node=shutil.which('node');assert node
    assert subprocess.run([node,'--version'],capture_output=True,text=True,check=True).stdout.startswith('v24.')
    entry=ROOT/'apps/agent-host/dist/main.js';assert entry.is_file() and not entry.is_symlink()
    child=subprocess.Popen([sys.executable,str(ROOT/'scripts/agent_host.py'),'--node',node,
     '--runtime-root',str(runtime),'--internal-key-file',str(key),'--port',str(port),
     '--tls-certificate-file',str(key.parent/'host-cert.pem'),'--tls-key-file',str(key.parent/'host-key.pem'),
     '--runtime-config-file',str(configuration)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
     env={k:v for k,v in os.environ.items() if not k.startswith(('MODEL_','DATABASE_'))})
    with httpx.Client(base_url='https://127.0.0.1:'+str(port),verify=str(key.parent/'host-cert.pem'),trust_env=False,timeout=5) as client:
        try:
            end=time.monotonic()+10
            while True:
                assert child.poll() is None
                try:
                    if client.get('/health/live').status_code==200:break
                except httpx.TransportError:pass
                assert time.monotonic()<end;time.sleep(.02)
            yield child,client
        finally:
            if child.poll() is None:child.terminate()
            try:stdout,stderr=child.communicate(timeout=10)
            except subprocess.TimeoutExpired:child.kill();stdout,stderr=child.communicate(timeout=10)
            assert key.read_text() not in stdout+stderr


@pytest.mark.parametrize('fault',[None,'artifact_deleted'])
def test_sigkill_context_host_and_worker_reopen_same_bound_pack(context_message,admin,tmp_path,fault):
    f=context_message;base=f['f'];original=f['original'];p=original['paths'];tokens=base['tokens'];credential=base['credential_files']
    assert f['relay'].run_once()=='queued'
    ledger=admin.execute('select artifact_id,pack_text,pack_digest,run_id from runtime.nexloop_context_artifact_bindings').fetchone()
    pack=json.loads(ledger[1]);assert pack['user_statement']['body']==f['message']['body']
    assert hashlib.sha256(ledger[1].encode()).hexdigest()==ledger[2]
    assert f['source'].read_artifact(ledger[0])==ledger[1].encode()
    job_id,command,input=admin.execute("select job_id,normalized_input->'run_command',normalized_input->>'input' from runtime.jobs").fetchone()
    assert input==ledger[1] and command['context_manifest_ref']=='artifact:'+ledger[0]
    common=['--signing-key-file',str(p['backend_signing']),'--signing-key-id','explicit-configuration']
    domain_dsn=private(tmp_path,'domain-dsn',make_conninfo(original['pg'],user='nexloop_domain_worker'))
    effect_dsn=private(tmp_path,'effect-dsn',make_conninfo(original['pg'],user='nexloop_action_worker'))
    host_tls=tmp_path/'host-tls';host_tls.mkdir(mode=0o700);runtime,host_key=files(host_tls)
    guard_tls=tmp_path/'guard-tls';guard_tls.mkdir(mode=0o700);files(guard_tls)
    guard_key=private(tmp_path,'guard-key',secrets.token_hex(32));port=free_port()
    config=effect_configuration(guard_tls,port,guard_key);body=json.loads(config.read_text());body.pop('deterministic_effect_message')
    body.update(deterministic_message_from_input=True,context_input_protocol='nexloop.context-pack.v2');config.write_text(json.dumps(body))
    hidden=list(tokens.values())+[f['source_token']]
    with context_host(runtime,host_key,config) as (node,host):
        args=['--database-url-file',str(domain_dsn),*common,'--service-credential-file',str(credential['assembly-runtime-worker']),
         '--artifact-root',str(tmp_path/'runtime-artifacts'),'--world','real','--queue','operations','--host-origin',str(host.base_url).rstrip('/'),
         '--host-control-key-file',str(host_key),'--host-ca-file',str(host_tls/'host-cert.pem'),'--guard-port',str(port),
         '--guard-key-file',str(guard_key),'--guard-certificate-file',str(guard_tls/'host-cert.pem'),'--guard-tls-key-file',str(guard_tls/'host-key.pem'),'--total-timeout','20']
        import signal
        import psycopg
        blocker=psycopg.connect(original['pg'],autocommit=True)
        admin.execute("create function runtime.context_recovery_barrier() returns trigger language plpgsql as $$begin perform pg_advisory_xact_lock(782341); return NEW; end$$")
        admin.execute("create trigger context_recovery_barrier before insert on runtime.nexloop_effect_submissions for each row execute function runtime.context_recovery_barrier()")
        blocker.execute('select pg_advisory_lock(782341)')
        args+=['--lease-seconds','6','--request-timeout','2','--poll-seconds','.1','--tick-seconds','.05']
        try:
            with process('nexloop_eios.runtime_worker',args,hidden) as old_worker:
                deadline=time.monotonic()+15
                while True:
                    waiting=admin.execute("select count(*) from pg_stat_activity where wait_event='advisory' and query like '%nexloop%'").fetchone()[0]
                    if waiting:break
                    assert old_worker.poll() is None and time.monotonic()<deadline
                    time.sleep(.01)
                database=runtime/command['run_id']/'runtime.sqlite'
                with sqlite3.connect(database) as db:
                    original_submission=db.execute('select id,conversation_id from submissions').fetchone()
                    assert original_submission
                first_fence=admin.execute('select fencing_token from runtime.jobs where job_id=%s',(job_id,)).fetchone()[0]
                node.kill();assert node.wait(10)==-signal.SIGKILL
                old_worker.kill();assert old_worker.wait(10)==-signal.SIGKILL
        finally:
            blocker.execute('select pg_advisory_unlock(782341)');blocker.close()
        admin.execute('drop trigger context_recovery_barrier on runtime.nexloop_effect_submissions')
        admin.execute('drop function runtime.context_recovery_barrier()')
    if fault:
        # Disposable technical Artifact metadata fault, not a business write.
        admin.execute("update runtime.nexloop_local_artifacts set status='deleted' where artifact_id=%s",(ledger[0],))
    time.sleep(6.2)
    with context_host(runtime,host_key,config) as (_,host):
        args[args.index('--host-origin')+1]=str(host.base_url).rstrip('/')
        output=once('nexloop_eios.runtime_worker',args,hidden)
        if fault:
            assert '"status":"succeeded"' not in output
            assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
            with sqlite3.connect(database) as db:
                assert db.execute('select id,conversation_id from submissions').fetchall()==[original_submission]
            return
        assert '"status":"succeeded"' in output
        assert admin.execute('select fencing_token from runtime.jobs where job_id=%s',(job_id,)).fetchone()[0]>first_fence
        with sqlite3.connect(database) as db:
            assert db.execute('select id,conversation_id from submissions').fetchall()==[original_submission]
        result=admin.execute('select result from runtime.jobs where job_id=%s',(job_id,)).fetchone()[0]
        assert result['scope']=='runtime_only' and result['business_action_success'] is False
        assert result['runtime_receipt']['persistence']=={'journal_mode':'wal','synchronous':2}
        database=runtime/command['run_id']/'runtime.sqlite'
        with sqlite3.connect(database) as db:assert db.execute('select count(*) from submissions').fetchone()==(1,)
        calls,receipts=tool_evidence(database);requests=[call for call in calls if call['name']=='nexloop.service.request']
        assert [call['arguments'] for call in requests]==[{'message':f['message']['body']},{'message':f['message']['body']}]
        assert len(receipts)==3 and receipts[0]==receipts[1]==receipts[2]
        intent=receipts[0]['intent_id']
