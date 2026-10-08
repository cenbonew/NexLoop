"""Actual current Human authentication + governed two-tenant HTTP boundaries."""
from contextlib import ExitStack,contextmanager
from pathlib import Path
import json,secrets,ssl
import httpx,pytest,psycopg
from psycopg.conninfo import make_conninfo
from local_message_assembly_fixture import assembled_message
from test_business_setup_pg import business_plan
from test_trusted_configuration_pg import configured,private,PrivateConfiguration
from test_trusted_configuration_pg import bootstrap,authority_records,Operation,ResourceType,apply_manifest
from datetime import UTC,datetime,timedelta
import hashlib,uuid
from test_precise_pg_admission_outage import process,wait_live
from test_agent_host import files,free_port
from nexloop_eios.backend import open_backend
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.conversation_messages import ConversationMessagePort


@contextmanager
def same_signer_configured(pg,admin,tmp_path,shared_key):
    bootstrap(admin)
    admin.execute('set log_parameter_max_length=0')
    admin.execute('alter system set log_parameter_max_length=0')
    admin.execute('alter system set log_parameter_max_length_on_error=0')
    admin.execute('select pg_reload_conf()')
    with psycopg.connect(pg) as clean:
        assert clean.execute('show log_parameter_max_length').fetchone()[0]=='0'
        assert clean.execute('show log_parameter_max_length_on_error').fetchone()[0]=='0'
    admin.execute('alter role nexloop_configurator login')
    tenant=str(uuid.uuid4());target='eios:artifact:explicit-configuration-source'
    binding,_,rows=authority_records(tenant,target,operation=Operation.READ,resource_type=ResourceType.ARTIFACT)
    token=secrets.token_urlsafe(48);key=shared_key;seal=secrets.token_hex(32)
    identityapp='explicit-local-login';expiry=(datetime.now(UTC)+timedelta(hours=1)).isoformat()
    manifest={'schema_version':'1.0','manifest_id':str(uuid.uuid4()),'tenant_id':tenant,'expected_revision':0,'tenant_status':'active',
        'object_types':[],'actions':[],'functions':[],
        'authority_facts':[{'kind':kind,'key':key,'payload':fact.model_dump(mode='json')} for kind,key,fact in rows],
        'service_credentials':[{'reference':'source','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expiry,'status':'active'}],
        'browser_applications':[{'application_id':identityapp,'active':True}],'browser_business_applications':[],
        'browser_rate_policies':[{'action':'local_login','operator_principal_id':'nexloop_identity','maximum_attempts':10,'window_seconds':60,'active':True}],
        'identity_allowances':[{'application_id':identityapp,'enabled':True,'maximum_accounts':1,'operator_label':'explicit-technical-owner','idempotency_key_digest':hashlib.sha256(bytes.fromhex(seal)).hexdigest()}]}
    paths=PrivateConfiguration(dsn=private(tmp_path,'configuration-dsn',make_conninfo(pg,user='nexloop_configurator')),signing=private(tmp_path,'signing-key',key),
        secrets=private(tmp_path,'service-secrets',json.dumps({'source':token})),identity=private(tmp_path,'identity-dsn',make_conninfo(pg,user='nexloop_identity')),
        seal=private(tmp_path,'identity-idempotency-key',seal),password=private(tmp_path,'human-password',secrets.token_urlsafe(24)))
    raw_signing=tmp_path/'backend-signing-key';raw_signing.write_bytes(bytes.fromhex(key));raw_signing.chmod(0o600);paths['backend_signing']=raw_signing
    result=apply_manifest(manifest,database_url_file=paths['dsn'],signing_key_file=paths['signing'],signing_key_id='explicit-configuration',service_secrets_file=paths['secrets'])
    yield PrivateConfiguration(pg=pg,tenant=tenant,manifest=manifest,paths=paths,result=result,token=token,application=identityapp,target=target,binding=binding)


def setup(stack,pg,admin,root,request,shared_key=None):
    root.mkdir(mode=0o700)
    conf=stack.enter_context(same_signer_configured(pg,admin,root,shared_key) if shared_key else contextmanager(configured.__wrapped__)(pg,admin,root))
    plan=stack.enter_context(contextmanager(business_plan.__wrapped__)(conf,admin,root))
    return stack.enter_context(contextmanager(assembled_message.__wrapped__)(plan,admin,root,request))


def api(stack,f,root):
    o=f['original'];p=o['paths'];port=free_port();origin=f'https://127.0.0.1:{port}'
    dsn=private(root,'http-api-dsn',make_conninfo(o['pg'],user='nexloop_api'));rate=private(root,'http-rate-key',secrets.token_hex(32));tls=root/'http-tls';tls.mkdir(mode=0o700);files(tls)
    args=['--database-url-file',str(dsn),'--signing-key-file',str(p['backend_signing']),'--signing-key-id','explicit-configuration','--artifact-root',str(root/'http-artifacts'),'--mode','test','--execution-profile','deterministic-test','--port',str(port),'--tls-certificate-file',str(tls/'host-cert.pem'),'--tls-key-file',str(tls/'host-key.pem'),'--identity-database-url-file',str(p['identity']),'--browser-rate-key-file',str(rate),'--browser-tenant-id',o['tenant'],'--browser-application-id',o['application'],'--browser-origin',origin]
    child=stack.enter_context(process('nexloop_eios.http_api',args,tuple(f['tokens'].values())+(p['password'].read_text(),)))
    client=stack.enter_context(httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(tls/'host-cert.pem')),trust_env=False,timeout=5));wait_live(child,client)
    login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()});assert login.status_code==200
    headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token'],'Idempotency-Key':'actual-tenant-conversation'}
    created=client.post('/api/v1/conversations',headers=headers,json={});assert created.status_code==200
    return client,headers,created.json()['id']


def test_actual_two_tenant_http_and_restricted_pg_reject_foreign_real_objects(pg,admin,tmp_path,request):
    with ExitStack() as stack:
        a=setup(stack,pg,admin,tmp_path/'a',request);b=setup(stack,pg,admin,tmp_path/'b',request,a['original']['paths']['signing'].read_text())
        ca,ha,ida=api(stack,a,tmp_path/'a');cb,hb,idb=api(stack,b,tmp_path/'b')
        secret='B_ONLY_SYNTHETIC_PRIVATE_STATEMENT'
        rb=cb.post('/api/v1/conversations/'+idb+'/messages',headers={**hb,'Idempotency-Key':'tenant-b-private-message'},json={'body':secret});assert rb.status_code==202
        mid=rb.json()['message']['id'];tb=b['original']['tenant'];ta=a['original']['tenant'];assert ta!=tb
        before=admin.execute('select object_id,nexloop_revision,properties from ontology.objects where tenant_id=%s order by object_id',(tb,)).fetchall();claim_before=admin.execute('select count(*) from runtime.nexloop_action_claims where tenant_id=%s',(ta,)).fetchone()
        for method,path,body in [('GET',f'/api/v1/conversations/{idb}/messages',None),('GET',f'/api/v1/conversations/{idb}/events',None),('GET',f'/api/v1/messages/{mid}/receipt',None),('POST',f'/api/v1/conversations/{idb}/messages',{'body':'A must never append to B'})]:
            r=ca.request(method,path,headers={**ha,'Idempotency-Key':'cross-tenant-attempt-key'},json=body)
            assert r.status_code in (403,404) and secret not in r.text and idb not in r.text and mid not in r.text
        assert admin.execute('select object_id,nexloop_revision,properties from ontology.objects where tenant_id=%s order by object_id',(tb,)).fetchall()==before
        assert admin.execute('select count(*) from runtime.nexloop_action_claims where tenant_id=%s',(ta,)).fetchone()==claim_before
        # Application role cannot bypass the reader by direct SQL access.
        with psycopg.connect(make_conninfo(pg,user='nexloop_api')) as restricted:
            with pytest.raises(psycopg.errors.InsufficientPrivilege),restricted.transaction():restricted.execute('select properties from ontology.objects where tenant_id=%s',(tb,))


def test_actual_http_body_header_spoofs_do_not_override_server_human(pg,admin,tmp_path,request):
    with ExitStack() as stack:
        a=setup(stack,pg,admin,tmp_path/'a',request);b=setup(stack,pg,admin,tmp_path/'b',request,a['original']['paths']['signing'].read_text())
        ca,ha,ida=api(stack,a,tmp_path/'a');cb,hb,idb=api(stack,b,tmp_path/'b');ta=a['original']['tenant'];tb=b['original']['tenant']
        before=admin.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()
        for field,value in [('tenant_id',tb),('scope',['ontology.schema.review']),('actor',b['base']['human']['principal_id']),('world_id','shadow')]:
            r=ca.post(f'/api/v1/conversations/{ida}/messages',headers={**ha,'Idempotency-Key':'invalid-body-field-'+field},json={'body':'must not commit',field:value});assert r.status_code==422
        assert admin.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()==before
        forged={**ha,'Idempotency-Key':'header-spoof-owned-message','X-Tenant-ID':tb,'X-Tenant':tb,'X-Scope':'ontology.schema.review','X-Actor':b['base']['human']['principal_id'],'X-World':'shadow'}
        r=ca.post(f'/api/v1/conversations/{ida}/messages',headers=forged,json={'body':'original authenticated A only'});assert r.status_code==202
        item=r.json()['message'];row=admin.execute('select tenant_id,world,properties from ontology.objects where object_id=%s',(item['id'],)).fetchone()
        assert row[0]==ta and row[1]=='real' and row[2]['actor']==a['base']['human']['principal_id']
        r=ca.get(f'/api/v1/conversations/{idb}/messages',headers=forged);assert r.status_code in (403,404)
