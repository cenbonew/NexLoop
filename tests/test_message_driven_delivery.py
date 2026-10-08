"""Actual 50→HUMAN→governed setup→HTTPS Message→49 relay→Pi→51 export.
Prepared while 51 owns PG; no skips/stubs, and no synthetic provider network.
"""
from contextlib import contextmanager
import json,os,secrets,sqlite3,subprocess,sys,time
import httpx
from psycopg.conninfo import make_conninfo
from message_driven_assembly_fixture import message_driven_assembly,business_plan,configured
from test_trusted_configuration_pg import private
from test_agent_host import files,free_port
from test_runtime_effect_tools import effect_configuration,tool_evidence
from test_message_runtime_effect_e2e import actual_host,trusted_host_evidence


@contextmanager
def reaped_host(*arguments):
    child=None
    try:
        with actual_host(*arguments) as hosted:
            child=hosted[0]
            yield hosted
    finally:
        if child is not None and child.poll() is None:
            child.kill();child.communicate(timeout=10)


@contextmanager
def process(module,arguments,secrets_to_hide=()):
    child=subprocess.Popen([sys.executable,'-m',module,*arguments],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env={k:v for k,v in os.environ.items() if not k.startswith(('MODEL_','DATABASE_'))})
    try:yield child
    finally:
        if child.poll() is None:child.terminate()
        try:stdout,stderr=child.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill();stdout,stderr=child.communicate(timeout=10)
        assert all(value not in stdout+stderr for value in secrets_to_hide)


def once(module,arguments,secrets_to_hide):
    result=subprocess.run([sys.executable,'-m',module,*arguments,'--once'],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,'actual CLI failed without exposing private diagnostics'
    assert all(value not in result.stdout+result.stderr for value in secrets_to_hide)
    return result.stdout


def wait_live(child,client):
    end=time.monotonic()+10
    while True:
        assert child.poll() is None,'owned-local process exited'
        try:
            if client.get('/health/live').status_code==200:return
        except httpx.TransportError:pass
        assert time.monotonic()<end;time.sleep(.02)


def test_two_user_messages_export_their_exact_body_and_replay_once(message_driven_assembly,admin,tmp_path):
    f=message_driven_assembly;o=f['original'];p=o['paths'];tenant=o['tenant'];tokens=f['tokens'];credentials=f['credential_files']
    trusted_host_evidence()
    common=['--signing-key-file',str(p['backend_signing']),'--signing-key-id','explicit-configuration']
    api_dsn=private(tmp_path,'assembly-api-dsn',make_conninfo(o['pg'],user='nexloop_api'))
    domain_dsn=private(tmp_path,'assembly-domain-dsn',make_conninfo(o['pg'],user='nexloop_domain_worker'))
    effect_dsn=private(tmp_path,'assembly-effect-dsn',make_conninfo(o['pg'],user='nexloop_action_worker'))
    api_tls=tmp_path/'api-tls';api_tls.mkdir(mode=0o700);_,api_key=files(api_tls)
    api_port=free_port();origin='https://127.0.0.1:'+str(api_port)
    rate=private(tmp_path,'assembly-rate-key',secrets.token_hex(32))
    api_arguments=['--database-url-file',str(api_dsn),*common,'--artifact-root',str(tmp_path/'api-artifacts'),'--mode','test','--port',str(api_port),
        '--tls-certificate-file',str(api_tls/'host-cert.pem'),'--tls-key-file',str(api_tls/'host-key.pem'),
        '--identity-database-url-file',str(p['identity']),'--browser-rate-key-file',str(rate),'--browser-tenant-id',tenant,
        '--browser-application-id',o['application'],'--browser-origin',origin]
    hidden=list(tokens.values())+[o['paths']['password'].read_text()]
    with process('nexloop_eios.http_api',api_arguments,hidden) as api_child:
        with httpx.Client(base_url=origin,verify=str(api_tls/'host-cert.pem'),trust_env=False,timeout=5) as client:
            wait_live(api_child,client)
            login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()})
            assert login.status_code==200
            headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token'],'Idempotency-Key':'owned-local-conversation-key'}
            conversation=client.post('/api/v1/conversations',headers=headers,json={});assert conversation.status_code==200
            conversation_id=conversation.json()['id']
            bodies=['第一条真实用户输入：保留标点和换行。\nmessage-A','第二条不同用户输入：message-B，不能使用 A 或固定配置内容。']
            identities=[]
            for index,user_body in enumerate(bodies):
                headers['Idempotency-Key']='message-driven-key-'+str(index)

                message_response=client.post('/api/v1/conversations/'+conversation_id+'/messages',headers=headers,json={'body':user_body})
                assert message_response.status_code==202
                message=message_response.json()['message'];message_id=message['id']
                assert client.get('/api/v1/messages/'+message_id+'/receipt').json()=={'message_id':message_id,'run':None,'receipt':None}
                assert client.get('/health/ready').status_code==503
                recipe=private(tmp_path,'assembly-relay-recipe',json.dumps(f['recipe']));vault=tmp_path/('relay-vault-'+str(index));vault.mkdir(mode=0o700)
                relay=['--database-url-file',str(api_dsn),*common,'--artifact-root',str(tmp_path/('relay-artifacts-'+str(index))),'--vault-root',str(vault),'--recipe-file',str(recipe)]
                relay += sum((['--'+name+'-credential-file',str(credentials['assembly-'+label])] for name,label in [('route','route'),('source','source'),('planner','planner'),('executor','executor')]),[])
                assert once('nexloop_eios.message_relay_cli',relay,hidden)=='Message relay ready\n{"status":"queued"}\n'
                routed=client.get('/api/v1/messages/'+message_id+'/receipt').json();run=routed['run'];assert run and routed['receipt'] is None
                assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='MessageAssignment'",(tenant,)).fetchone()==(index+1,)
                # Independent real runtime CLI owns the actual TLS guard. Node only
                # receives opaque activation metadata; no DB/Run/provider secrets.
                host_tls=tmp_path/('host-tls-'+str(index));host_tls.mkdir(mode=0o700);runtime,host_key=files(host_tls)
                guard_port=free_port();guard_key=private(tmp_path,'assembly-guard-key',secrets.token_hex(32))
                guard_tls=tmp_path/('guard-tls-'+str(index));guard_tls.mkdir(mode=0o700);files(guard_tls)
                host_config=effect_configuration(guard_tls,guard_port,guard_key)
                config=json.loads(host_config.read_text());config.pop('deterministic_effect_message');config['deterministic_message_from_input']=True
                host_config.write_text(json.dumps(config))
                with reaped_host(runtime,host_key,host_config) as (_,host_client,_):
                    worker=['--database-url-file',str(domain_dsn),*common,'--service-credential-file',str(credentials['assembly-runtime-worker']),
                        '--artifact-root',str(tmp_path/'runtime-worker-artifacts'),'--world','real','--queue','operations',
                        '--host-origin',str(host_client.base_url).rstrip('/'),'--host-control-key-file',str(host_key),'--host-ca-file',str(host_tls/'host-cert.pem'),
                        '--guard-port',str(guard_port),'--guard-key-file',str(guard_key),
                        '--guard-certificate-file',str(guard_tls/'host-cert.pem'),'--guard-tls-key-file',str(guard_tls/'host-key.pem'), '--total-timeout','20']
                    # effect_configuration generates the guard TLS certificate pair
                    # at its actual documented test-owned location.
                    runtime_output=once('nexloop_eios.runtime_worker',worker,hidden)
                    assert '"status": "succeeded"' in runtime_output or '"status":"succeeded"' in runtime_output
                    task=admin.execute('select result from runtime.jobs where job_id=%s',(run['task_id'],)).fetchone()[0]
                    assert task['scope']=='runtime_only' and task['business_action_success'] is False
                    assert task['runtime_receipt']['request_id']==run['request_id'] and task['runtime_receipt']['persistence']=={'journal_mode':'wal','synchronous':2}
                    with sqlite3.connect(runtime/run['run_id']/'runtime.sqlite') as db:assert db.execute('select count(*) from submissions').fetchone()==(1,)
                    calls,receipts=tool_evidence(runtime/run['run_id']/'runtime.sqlite');assert len(receipts)==3 and receipts[0]==receipts[1]==receipts[2]
                    requests=[call for call in calls if call['name']=='nexloop.service.request']
                    assert [call['arguments'] for call in requests]==[{'message':user_body},{'message':user_body}]
                    intent=receipts[0]['intent_id'];receipt_id=receipts[0]['receipt_id']
                delivery_tls=tmp_path/('delivery-tls-'+str(index));delivery_tls.mkdir(mode=0o700);_,provider_key=files(delivery_tls)
                delivery_origin='https://127.0.0.1:'+str(free_port());delivery_root=tmp_path/('actual-json-products-'+str(index));delivery_root.mkdir(mode=0o700)
                provider_config=private(tmp_path,'assembly-provider-config',json.dumps({'origin':delivery_origin,'credential_file':str(provider_key),
                    'ca_file':str(delivery_tls/'host-cert.pem'),'timeout':3}))
                delivery=['--database-url-file',str(effect_dsn),*common,'--service-credential-file',str(credentials['assembly-executor']),
                    '--artifact-root',str(tmp_path/'delivery-artifacts'),'--delivery-root',str(delivery_root),'--origin',delivery_origin,
                    '--certificate-file',str(delivery_tls/'host-cert.pem'),'--tls-key-file',str(delivery_tls/'host-key.pem'),
                    '--provider-ca-file',str(delivery_tls/'host-cert.pem'),'--provider-credential-file',str(provider_key)]
                with process('nexloop_eios.local_json_delivery',delivery,hidden+[provider_key.read_text()]) as delivery_child:
                    # No pretend health endpoint: wait for actual TLS listener.
                    import socket,ssl
                    deadline=time.monotonic()+10;context=ssl.create_default_context(cafile=str(delivery_tls/'host-cert.pem'))
                    from urllib.parse import urlsplit
                    while True:
                        assert delivery_child.poll() is None
                        try:
                            with socket.create_connection(('127.0.0.1',urlsplit(delivery_origin).port),timeout=.2) as raw:
                                with context.wrap_socket(raw,server_hostname='127.0.0.1'):break
                        except (OSError,ssl.SSLError):
                            assert time.monotonic()<deadline;time.sleep(.02)
                    executor=['--database-url-file',str(effect_dsn),*common,'--service-credential-file',str(credentials['assembly-executor']),
                        '--artifact-root',str(tmp_path/'effect-worker-artifacts'),'--world','real','--provider-config-file',str(provider_config)]
                    effect_output=once('nexloop_eios.effect_worker',executor,hidden+[provider_key.read_text()])
                    assert 'fulfilled' in effect_output
                    final=client.get('/api/v1/messages/'+message_id+'/receipt');assert final.status_code==200
                    observed=final.json();assert observed['run']['run_id']==run['run_id']
                    receipt=observed['receipt'];assert receipt['intent_id']==intent and receipt['receipt_id']==receipt_id
                    assert receipt['provider_state']=='fulfilled' and receipt['governed_claim_finalized'] is True and receipt['business_action_success'] is True
                    product=json.loads((delivery_root/(intent+'.export.json')).read_text())
                    assert product['intent_id']==intent and product['format']=='nexloop.json-export.v1'
                    assert product['document']['body']==user_body and len(list(delivery_root.glob('*.export.json')))==1
                # Repost the same stable browser event key/payload: persisted same
                # Message/route/Run/task/Intent/receipt, no new budget/provider POST.
                duplicate=client.post('/api/v1/conversations/'+conversation_id+'/messages',headers=headers,json={'body':user_body})
                assert duplicate.status_code==202 and duplicate.json()['created'] is False
                assert duplicate.json()['message']['id']==message_id
                assert once('nexloop_eios.message_relay_cli',relay,hidden)=='Message relay ready\n{"status":"idle"}\n'
                assert client.get('/api/v1/messages/'+message_id+'/receipt').json()==observed
                identities.append((message_id,run['run_id'],run['task_id'],intent,receipt_id))
            assert all(len({row[column] for row in identities})==2 for column in range(5))
            assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s',(tenant,)).fetchone()==(2,)
            assert admin.execute('select count(*) from runtime.nexloop_effect_attempts where tenant_id=%s',(tenant,)).fetchone()==(2,)
            assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger where tenant_id=%s',(tenant,)).fetchone()==(2,)
