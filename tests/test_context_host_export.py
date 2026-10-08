"""Actual core52 artifact/guard + independently compiled context Host + real51 IO."""
from contextlib import contextmanager
import hashlib,json,os,secrets,shutil,sqlite3,subprocess,sys,time
from pathlib import Path
import httpx
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
            yield client
        finally:
            if child.poll() is None:child.terminate()
            try:stdout,stderr=child.communicate(timeout=10)
            except subprocess.TimeoutExpired:child.kill();stdout,stderr=child.communicate(timeout=10)
            assert key.read_text() not in stdout+stderr


def test_actual_bound_pack_goes_through_pg_guard_and_exports_original_statement(context_message,admin,tmp_path):
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
    with context_host(runtime,host_key,config) as host:
        args=['--database-url-file',str(domain_dsn),*common,'--service-credential-file',str(credential['assembly-runtime-worker']),
         '--artifact-root',str(tmp_path/'runtime-artifacts'),'--world','real','--queue','operations','--host-origin',str(host.base_url).rstrip('/'),
         '--host-control-key-file',str(host_key),'--host-ca-file',str(host_tls/'host-cert.pem'),'--guard-port',str(port),
         '--guard-key-file',str(guard_key),'--guard-certificate-file',str(guard_tls/'host-cert.pem'),'--guard-tls-key-file',str(guard_tls/'host-key.pem'),'--total-timeout','20']
        output=once('nexloop_eios.runtime_worker',args,hidden);assert '"status":"succeeded"' in output
        result=admin.execute('select result from runtime.jobs where job_id=%s',(job_id,)).fetchone()[0]
        assert result['scope']=='runtime_only' and result['business_action_success'] is False
        assert result['runtime_receipt']['persistence']=={'journal_mode':'wal','synchronous':2}
        database=runtime/command['run_id']/'runtime.sqlite'
        with sqlite3.connect(database) as db:assert db.execute('select count(*) from submissions').fetchone()==(1,)
        calls,receipts=tool_evidence(database);requests=[call for call in calls if call['name']=='nexloop.service.request']
        assert [call['arguments'] for call in requests]==[{'message':f['message']['body']},{'message':f['message']['body']}]
        assert len(receipts)==3 and receipts[0]==receipts[1]==receipts[2]
        intent=receipts[0]['intent_id']
    tls=tmp_path/'delivery-tls';tls.mkdir(mode=0o700);_,provider_key=files(tls)
    origin='https://127.0.0.1:'+str(free_port());delivery_root=tmp_path/'real-json-products';delivery_root.mkdir(mode=0o700)
    provider=private(tmp_path,'provider-config',json.dumps({'origin':origin,'credential_file':str(provider_key),'ca_file':str(tls/'host-cert.pem'),'timeout':3}))
    args=['--database-url-file',str(effect_dsn),*common,'--service-credential-file',str(credential['assembly-executor']),
     '--artifact-root',str(tmp_path/'delivery-artifacts'),'--delivery-root',str(delivery_root),'--origin',origin,
     '--certificate-file',str(tls/'host-cert.pem'),'--tls-key-file',str(tls/'host-key.pem'),'--provider-ca-file',str(tls/'host-cert.pem'),'--provider-credential-file',str(provider_key)]
    with process('nexloop_eios.local_json_delivery',args,hidden+[provider_key.read_text()]) as provider_process:
        import socket,ssl
        from urllib.parse import urlsplit
        end=time.monotonic()+10;context=ssl.create_default_context(cafile=str(tls/'host-cert.pem'))
        while True:
            assert provider_process.poll() is None
            try:
                with socket.create_connection(('127.0.0.1',urlsplit(origin).port),timeout=.2) as raw:
                    with context.wrap_socket(raw,server_hostname='127.0.0.1'):break
            except (OSError,ssl.SSLError):assert time.monotonic()<end;time.sleep(.02)
        executor=['--database-url-file',str(effect_dsn),*common,'--service-credential-file',str(credential['assembly-executor']),
         '--artifact-root',str(tmp_path/'effect-worker-artifacts'),'--world','real','--provider-config-file',str(provider)]
        assert 'fulfilled' in once('nexloop_eios.effect_worker',executor,hidden+[provider_key.read_text()])
    product=json.loads((delivery_root/(intent+'.export.json')).read_text())
    assert product['document']['body']==f['message']['body'] and product['intent_id']==intent
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(1,)
    assert admin.execute('select governed_claim_finalized from runtime.nexloop_effect_intents').fetchone()==(True,)
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.browser_http import BrowserConfiguration
    origin='https://context.invalid'
    browser=BrowserConfiguration(p['identity'],private(tmp_path,'read-rate-key','a'*64),original['tenant'],original['application'],origin)
    config=ApiConfiguration(f['dsn'],p['backend_signing'],f['root'],'explicit-configuration',browser)
    with TestClient(create_app(config),base_url=origin) as client:
        logged=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()});assert logged.status_code==200
        response=client.get('/api/v1/messages/'+f['message']['id']+'/receipt');assert response.status_code==200
        receipt=response.json()['receipt'];assert receipt['intent_id']==intent and receipt['governed_claim_finalized'] is True and receipt['business_action_success'] is True
    assert f['relay'].run_once()=='idle'
