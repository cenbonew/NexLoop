"""Actual 50→HUMAN→governed setup→HTTPS Message→49 relay→Pi→51 export.
Manual opt-in verification: pytest tests/verification_real_model.py.
Requires an explicit private stage configuration; default CI does not collect this file.
No implicit .env reads, no skips/stubs, and no synthetic provider network.
Reports stay in the owned pytest temporary directory, never overwrite tracked evidence.
"""
from contextlib import contextmanager
import json,os,secrets,sqlite3,subprocess,sys,time,uuid
import httpx
from pathlib import Path
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
    result=subprocess.run([sys.executable,'-m',module,*arguments,'--once'],capture_output=True,text=True,timeout=150)
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


def test_one_real_deepseek_native_event_catalog_v2_governed_delivery(message_driven_assembly,admin,tmp_path,request):
    f=message_driven_assembly;o=f['original'];p=o['paths'];tenant=o['tenant'];tokens=f['tokens'];credentials=f['credential_files']
    from nexloop_eios.model_profile import load_model_profile
    from nexloop_eios.private_configuration import read_private_text
    supplied=os.environ.get('NEXLOOP_REAL_MODEL_CONFIGURATION_FILE')
    assert supplied,'explicit private model configuration file required; no implicit .env read'
    configuration=json.loads(read_private_text(supplied,maximum=65536))
    assert set(configuration)=={'MODEL_PROVIDER','MODEL_ID','MODEL_BASE_URL','MODEL_CREDENTIALS_FILE','stage'} and configuration['stage'] is True
    assert configuration['MODEL_PROVIDER']=='deepseek' and configuration['MODEL_ID']=='deepseek-flash' and configuration['MODEL_BASE_URL']=='https://api.deepseek.com'
    profile=load_model_profile(environment={k:v for k,v in configuration.items() if k!='stage'},stage=True)
    assert profile.provider=='deepseek' and profile.validation_mode=='real_validation_pending'
    model_key=private(tmp_path,'real-model-key',profile.credential_for_provider())
    report_path=tmp_path/'real-model-result.json'
    def finish_evidence():
        import hashlib
        report=json.loads(report_path.read_text()) if report_path.exists() else {}
        private_key=model_key.read_text() if model_key.exists() else ''
        try:
            databases=list(tmp_path.rglob('runtime.sqlite'))
            sqlite_evidence=[]
            for database in databases:
                with sqlite3.connect(database) as db:
                    rows=[json.loads(r[0]) for r in db.execute('select record from entries')]
                    assistants=[r for r in rows if r.get('kind')=='pi.assistant']
                    # Records may use typed wrappers; record only safe aggregate fields.
                    sqlite_evidence.append({'sha256':hashlib.sha256(database.read_bytes()).hexdigest(),'entries':len(rows),'submissions':db.execute('select count(*) from submissions').fetchone()[0],'assistant_records':len(assistants)})
                    if private_key and private_key in json.dumps(rows):raise AssertionError('private material persisted')
            scanned=0
            for artifact in tmp_path.rglob('*'):
                if artifact.is_file() and artifact!=model_key:
                    scanned+=1
                    if private_key and private_key.encode() in artifact.read_bytes():raise AssertionError('private material leaked')
        finally:
            model_key.unlink(missing_ok=True)
        report.update({'secret_scan':{'own_files_scanned':scanned,'leaks':0,'temporary_key_removed':not model_key.exists()},'sqlite':sqlite_evidence,'protocol_scope':'owned snapshot core56 main8610de7; actual catalog53/partial-edit54/native55/scope-denial56; terminal status is in validation_stage','validation_stage':safe_stage['current'],'provider_failure_stage':'unavailable_stage_unknown' if report.get('runtime_task_status')!='succeeded' else None})
        report_path.write_text(json.dumps(report,indent=2)+'\n')
    request.addfinalizer(finish_evidence)
    safe_stage={'current':'configuration_loaded'}
    def stage(name):
        safe_stage['current']=name
        if report_path.exists():
            value=json.loads(report_path.read_text());value['validation_stage']=name
            report_path.write_text(json.dumps(value,indent=2)+'\n')
    model_config=private(tmp_path,'real-model-config',json.dumps({'MODEL_PROVIDER':'deepseek','MODEL_ID':'deepseek-flash','MODEL_BASE_URL':'https://api.deepseek.com','MODEL_CREDENTIALS_FILE':str(model_key),'stage':True}))
    f['recipe']['runtime_profile']='deepseek-flash'
    f['recipe']['budget']={'maximum_model_turns':2,'maximum_tool_calls':2,'active_timeout_seconds':120,'maximum_cost':'0.62','currency':'USD'}
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
    hidden=list(tokens.values())+[o['paths']['password'].read_text(),model_key.read_text()]
    with process('nexloop_eios.http_api',api_arguments,hidden) as api_child:
        with httpx.Client(base_url=origin,verify=str(api_tls/'host-cert.pem'),trust_env=False,timeout=5) as client:
            wait_live(api_child,client)
            login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()})
            assert login.status_code==200
            headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token'],'Idempotency-Key':'owned-local-conversation-key'}
            conversation=client.post('/api/v1/conversations',headers=headers,json={});assert conversation.status_code==200
            conversation_id=conversation.json()['id']
            bodies=['This is a synthetic sandbox validation, not a real customer request. Use nexloop.service.request exactly once with message equal to REAL_MODEL_SYNTHETIC_DELIVERY_20261008. Do not change that text. Once the tool returns accepted, finish with a brief acknowledgement; accepted is not external fulfillment. Do not use a different business key or request another effect.']
            expected_body='REAL_MODEL_SYNTHETIC_DELIVERY_20261008'
            identities=[]
            for index,user_body in enumerate(bodies):
                headers['Idempotency-Key']='message-driven-key-'+str(index)

                provider_event_id=str(uuid.uuid4());native_payload={'schema_version':'nexloop.native-message.v1','provider_event_id':provider_event_id,'body':user_body}
                message_response=client.post('/api/v1/conversations/'+conversation_id+'/native-messages',headers=headers,json=native_payload)
                assert message_response.status_code==202
                message=message_response.json()['message'];message_id=message['id']
                assert client.get('/api/v1/messages/'+message_id+'/receipt').json()=={'message_id':message_id,'run':None,'receipt':None}
                assert client.get('/health/ready').status_code==503
                recipe=private(tmp_path,'assembly-relay-recipe',json.dumps(f['recipe']));vault=tmp_path/('relay-vault-'+str(index));vault.mkdir(mode=0o700)
                relay=['--database-url-file',str(api_dsn),*common,'--artifact-root',str(tmp_path/('relay-artifacts-'+str(index))),'--vault-root',str(vault),'--recipe-file',str(recipe)]
                relay += sum((['--'+name+'-credential-file',str(credentials['assembly-'+label])] for name,label in [('route','route'),('source','source'),('planner','planner'),('executor','executor')]),[])
                assert once('nexloop_eios.message_relay_cli',relay,hidden)=='Message relay ready\n{"status":"queued"}\n'
                routed=client.get('/api/v1/messages/'+message_id+'/receipt').json();run=routed['run'];assert run and routed['receipt'] is None
                payload=admin.execute('select normalized_input from runtime.jobs where job_id=%s',(run['task_id'],)).fetchone()[0]
                pack=json.loads(payload['input']);assert pack['schema_version']=='nexloop.context-pack.v2' and pack['user_statement']['body']==message['body']
                assert payload['run_command']['context_manifest_ref']=='artifact:'+pack['bindings']['artifact_id']
                assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='MessageAssignment'",(tenant,)).fetchone()==(index+1,)
                # Independent real runtime CLI owns the actual TLS guard. Node only
                # receives opaque activation metadata; no DB/Run/provider secrets.
                host_tls=tmp_path/('host-tls-'+str(index));host_tls.mkdir(mode=0o700);runtime,host_key=files(host_tls)
                guard_port=free_port();guard_key=private(tmp_path,'assembly-guard-key',secrets.token_hex(32))
                guard_tls=tmp_path/('guard-tls-'+str(index));guard_tls.mkdir(mode=0o700);files(guard_tls)
                host_config=effect_configuration(guard_tls,guard_port,guard_key)
                config=json.loads(host_config.read_text());config.pop('deterministic_effect_message');config['runtime_profile']='deepseek-flash';config['model_configuration_file']=str(model_config);config['maximum_request_cost']='0.31';config['context_input_protocol']='nexloop.context-pack.v2'
                host_config.write_text(json.dumps(config))
                with reaped_host(runtime,host_key,host_config) as (_,host_client,_):
                    worker=['--database-url-file',str(domain_dsn),*common,'--service-credential-file',str(credentials['assembly-runtime-worker']),
                        '--artifact-root',str(tmp_path/'runtime-worker-artifacts'),'--world','real','--queue','operations',
                        '--host-origin',str(host_client.base_url).rstrip('/'),'--host-control-key-file',str(host_key),'--host-ca-file',str(host_tls/'host-cert.pem'),
                        '--guard-port',str(guard_port),'--guard-key-file',str(guard_key),
                        '--guard-certificate-file',str(guard_tls/'host-cert.pem'),'--guard-tls-key-file',str(guard_tls/'host-key.pem'), '--total-timeout','110']
                    # effect_configuration generates the guard TLS certificate pair
                    # at its actual documented test-owned location.
                    runtime_output=once('nexloop_eios.runtime_worker',worker,hidden)
                    result=admin.execute('select status,result from runtime.jobs where job_id=%s',(run['task_id'],)).fetchone()
                    report=report_path
                    report.parent.mkdir(parents=True,exist_ok=True)
                    report.write_text(json.dumps({'provider':'deepseek','model':'deepseek-flash','runtime_task_status':result[0],'runtime_outcome':result[1].get('runtime_outcome'),'runtime_code':result[1].get('code'),'runtime_receipt':result[1].get('runtime_receipt'),'run_id':run['run_id'],'scope':'one actual model Run; synthetic user input; owned PG'},indent=2)+'\n')
                    assert '"status": "succeeded"' in runtime_output or '"status":"succeeded"' in runtime_output
                    task=admin.execute('select result from runtime.jobs where job_id=%s',(run['task_id'],)).fetchone()[0]
                    assert task['scope']=='runtime_only' and task['business_action_success'] is False
                    assert task['runtime_receipt']['request_id']==run['request_id'] and task['runtime_receipt']['persistence']=={'journal_mode':'wal','synchronous':2}
                    with sqlite3.connect(runtime/run['run_id']/'runtime.sqlite') as db:assert db.execute('select count(*) from submissions').fetchone()==(1,)
                    calls,receipts=tool_evidence(runtime/run['run_id']/'runtime.sqlite');assert receipts and all(r==receipts[0] for r in receipts)
                    requests=[call for call in calls if call['name']=='nexloop_service_request']
                    stage('validating_exact_tool_arguments')
                    expected_scope={'offering_id':pack['supply']['offering_id'],'offering_revision':pack['supply']['offering_revision'],'requested_guarantees':[],'requested_discounts':[]}
                    assert [call['arguments'] for call in requests]==[{'message':expected_body,'request_scope':expected_scope}], 'actual tool arguments differ from the exact published catalog scope'
                    stage('exact_tool_arguments_verified')
                    with sqlite3.connect(runtime/run['run_id']/'runtime.sqlite') as db:
                        rows=[json.loads(r[0]) for r in db.execute('select record from entries')]
                    assert 'deepseek' in json.dumps(rows) and 'deepseek-flash' in json.dumps(rows)
                    if model_key.read_text() in json.dumps(rows):raise AssertionError('private model material persisted')
                    intent=receipts[0]['intent_id'];receipt_id=receipts[0]['receipt_id']
                stage('starting_actual_delivery')
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
                    stage('actual_effect_fulfilled')
                    final=client.get('/api/v1/messages/'+message_id+'/receipt');assert final.status_code==200
                    observed=final.json();assert observed['run']['run_id']==run['run_id']
                    receipt=observed['receipt'];assert receipt['intent_id']==intent and receipt['receipt_id']==receipt_id
                    assert receipt['provider_state']=='fulfilled' and receipt['governed_claim_finalized'] is True and receipt['business_action_success'] is True
                    product=json.loads((delivery_root/(intent+'.export.json')).read_text())
                    assert product['intent_id']==intent and product['format']=='nexloop.json-export.v1'
                    assert product['document']['body']==expected_body and len(list(delivery_root.glob('*.export.json')))==1
                    import hashlib
                    report=json.loads(report_path.read_text());report.update({'actual_export_sha256':hashlib.sha256((delivery_root/(intent+'.export.json')).read_bytes()).hexdigest(),'final_human_receipt':receipt,'tool_requests':len(requests),'actual_provider_delivery':'LocalJsonDelivery HTTPS','model_validation':'passed'});report_path.write_text(json.dumps(report,indent=2)+'\n')
                # Repost the same stable browser event key/payload: persisted same
                # Message/route/Run/task/Intent/receipt, no new budget/provider POST.
                headers['Idempotency-Key']='independent-native-replay-transport'
                duplicate=client.post('/api/v1/conversations/'+conversation_id+'/native-messages',headers=headers,json=native_payload)
                stage('validating_native_replay')
                assert duplicate.status_code==202 and duplicate.json()['created'] is False
                assert duplicate.json()['message']['id']==message_id
                assert once('nexloop_eios.message_relay_cli',relay,hidden)=='Message relay ready\n{"status":"idle"}\n'
                assert client.get('/api/v1/messages/'+message_id+'/receipt').json()==observed
                identities.append((message_id,run['run_id'],run['task_id'],intent,receipt_id))
            assert all(len({row[column] for row in identities})==1 for column in range(5))
            assert admin.execute('select count(*) from runtime.nexloop_native_web_events').fetchone()==(1,)
            assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s',(tenant,)).fetchone()==(1,)
            assert admin.execute('select count(*) from runtime.nexloop_effect_attempts where tenant_id=%s',(tenant,)).fetchone()==(1,)
            stage('validating_final_ledger')
            assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger where tenant_id=%s',(tenant,)).fetchone()==(1,)

            stage('end_to_end_passed')
