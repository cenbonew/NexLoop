"""Actual 50→HUMAN→governed setup→HTTPS Message→49 relay→Pi→51 export.
Prepared while 51 owns PG; no skips/stubs, and no synthetic provider network.
"""
from contextlib import contextmanager
import json,os,secrets,sqlite3,subprocess,sys,time
import httpx
from psycopg.conninfo import make_conninfo
from local_message_assembly_fixture import assembled_message,business_plan,configured
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


import psycopg
from test_infrastructure_faults import stopped_owned_pg
from effect_execution_fixture import governed_effect_executor,execution_plan
from test_effect_dispatch import independent_worker,accepted
from support.effect_provider import effect_provider

def test_actual_https_message_pg_outage_has_no_acceptance_ack(assembled_message,admin,tmp_path):
    f=assembled_message;o=f['original'];p=o['paths'];tenant=o['tenant'];tokens=f['tokens'];credentials=f['credential_files']
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
            path='/api/v1/conversations/'+conversation_id+'/messages'
            headers['Idempotency-Key']='pg-outage-owned-message'
            before=admin.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()[0]
            with stopped_owned_pg(o['pg']):
                response=client.post(path,headers=headers,json={'body':'one original statement after PG recovery'})
                assert response.status_code not in {200,201,202,204}
                assert response.json()['code']=='dependency_unavailable'
                assert not isinstance(response.json().get('message'),dict) and response.json().get('created') is not True
            with psycopg.connect(o['pg'],autocommit=True) as recovered:
                assert recovered.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()[0]==before
                assert recovered.execute('select count(*) from runtime.nexloop_message_outbox').fetchone()==(0,)
                assert recovered.execute('select count(*) from runtime.jobs').fetchone()==(0,)
            # Existing pool connections can be stale after an immediate crash.
            # Retry the exact same stable key; never assert false readiness.
            deadline=time.monotonic()+10
            while True:
                accepted=client.post(path,headers=headers,json={'body':'one original statement after PG recovery'})
                if accepted.status_code==202:break
                assert accepted.status_code==503 and time.monotonic()<deadline
                time.sleep(.1)
            assert accepted.json()['created'] is True
            replay=client.post(path,headers=headers,json={'body':'one original statement after PG recovery'})
            assert replay.status_code==202 and replay.json()['created'] is False
            assert replay.json()['message']['id']==accepted.json()['message']['id']
            with psycopg.connect(o['pg'],autocommit=True) as recovered:
                assert recovered.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()[0]==before+1
                assert recovered.execute('select count(*) from runtime.nexloop_message_outbox').fetchone()==(1,)


def test_actual_pg_outage_before_first_external_dispatch_has_zero_post(governed_effect_executor,pg,tmp_path):
    f=governed_effect_executor;receipt=accepted(f);intent=receipt['intent_id']
    with effect_provider(tmp_path/'before-dispatch.sqlite') as provider:
        with independent_worker(f,provider.origin,pause_before_admit=True) as (worker,pipe):
            assert pipe.poll(15) and pipe.recv()=={'event':'before_admit'}
            with stopped_owned_pg(pg):
                pipe.send('release')
                assert pipe.poll(15)
                result=pipe.recv();worker.join(10)
                assert worker.exitcode==0 and result['business_action_success'] is False
                assert result['status']=='admission_unavailable'
                assert provider.control('snapshot')['effects']==0
                assert provider.control('snapshot')['requests']==[]
        with psycopg.connect(pg,autocommit=True) as recovered:
            assert recovered.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(0,)
            lease,now=recovered.execute('select lease_until,clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()
        if lease and lease>now:time.sleep((lease-now).total_seconds()+.05)
        with independent_worker(f,provider.origin) as (replacement,pipe):
            assert pipe.poll(15);result=pipe.recv();replacement.join(10)
            assert replacement.exitcode==0 and result['status']=='dispatching'
        assert provider.control('snapshot')['requests']==[('POST',intent,202)]
        assert provider.control('snapshot')['effects']==1
