"""Actual standalone restricted CLI with clean PG and synthetic durable TLS provider."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import http.client
import json
import select
import signal
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import pytest
from effect_execution_fixture import governed_effect_executor,execution_plan
from support.effect_provider import effect_provider
from test_agent_host import files


def private(path,value):
    path.write_text(value);path.chmod(0o600);return path


@contextmanager
def durable_tls_provider(tmp_path):
    _,credential=files(tmp_path)
    with effect_provider(tmp_path/'provider.sqlite') as durable:
        endpoint=urlsplit(durable.origin)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def exchange(self):
                if self.headers.get('Authorization')!='Bearer '+credential.read_text():
                    self.send_error(401);return
                connection=http.client.HTTPConnection(endpoint.hostname,endpoint.port,timeout=15)
                try:
                    body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                    headers={'Content-Type':'application/json'}
                    if self.headers.get('Idempotency-Key'):headers['Idempotency-Key']=self.headers['Idempotency-Key']
                    connection.request(self.command,self.path,body=body or None,headers=headers)
                    response=connection.getresponse();content=response.read()
                    self.send_response(response.status);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(content)));self.end_headers()
                    try:self.wfile.write(content)
                    except (BrokenPipeError,ConnectionResetError,ssl.SSLError):pass
                finally:connection.close()
            do_POST=exchange;do_GET=exchange
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(tmp_path/'host-cert.pem',tmp_path/'host-key.pem')
        server.socket=context.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        configuration=private(tmp_path/'provider.json',json.dumps({'origin':'https://127.0.0.1:'+str(server.server_port),
            'credential_file':str(credential),'ca_file':str(tmp_path/'host-cert.pem'),'timeout':1}))
        try:yield durable,configuration,credential
        finally:server.shutdown();server.server_close();thread.join(5)


def cli_arguments(fixture,tmp_path,provider_configuration,*,once=True):
    config=fixture.executor_configuration()
    dsn=private(tmp_path/'database-url',config['database_url'])
    token=private(tmp_path/'service-token',config['service_token'])
    args=[sys.executable,'-m','nexloop_eios.effect_worker','--database-url-file',str(dsn),
        '--service-credential-file',str(token),'--signing-key-file',str(config['signing_key_file']),
        '--signing-key-id',config['signing_key_id'],'--artifact-root',str(tmp_path/'cli-artifacts'),
        '--provider-config-file',str(provider_configuration),'--world','real','--lease-seconds','3','--tick-seconds','.05']
    if once:args.append('--once')
    return args,[config['database_url'],config['service_token']],token


def ready(process):
    assert select.select([process.stdout],[],[],15)[0]
    assert process.stdout.readline()=='Effect Worker ready\n'


def safe_result(process,sentinels,timeout=15):
    out,err=process.communicate(timeout=timeout)
    assert all(value not in out+err for value in sentinels)
    return process.returncode,out,err


def test_once_actual_governed_pg_dispatch_persistent_provider(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor
    receipt=fixture.ports[0].submit_effect_intent(parameters={'message':'private synthetic effect input'})
    with durable_tls_provider(tmp_path) as (provider,configuration,credential):
        args,sentinels,_=cli_arguments(fixture,tmp_path,configuration)
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        code,out,err=safe_result(process,sentinels+[credential.read_text(),'private synthetic effect input'])
        assert code==0 and err==''
        assert out=='Effect Worker ready\n'+json.dumps({'claimed':True,'status':'dispatching'},separators=(',',':'))+'\n'
        assert provider.control('snapshot')['requests']==[('POST',receipt['intent_id'],202)]
        assert provider.control('snapshot')['effects']==1
        assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(receipt['intent_id'],)).fetchone()==('dispatching',)


def test_cli_sigkill_after_provider_commit_reopen_queries_original(governed_effect_executor,tmp_path):
    fixture=governed_effect_executor;receipt=fixture.ports[0].submit_effect_intent(parameters={'message':'synthetic kill'})
    with durable_tls_provider(tmp_path) as (provider,configuration,credential):
        args,sentinels,_=cli_arguments(fixture,tmp_path,configuration)
        provider.control('pause')
        old=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            ready(old);assert provider.wait('accepted_committed')['intent_id']==receipt['intent_id']
            old.kill();code,out,err=safe_result(old,sentinels+[credential.read_text()]);assert code==-signal.SIGKILL
        finally:
            if old.poll() is None:old.kill();old.communicate()
        provider.control('release');time.sleep(3.1)
        new=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        code,out,err=safe_result(new,sentinels+[credential.read_text()])
        assert code==0 and '"status":"dispatching"' in out and err==''
        assert provider.control('snapshot')['requests']==[('POST',receipt['intent_id'],202),('GET',receipt['intent_id'],200)]
        assert provider.control('snapshot')['effects']==1


@pytest.mark.parametrize('case',['missing_credential','unsafe_configuration','wrong_role'])
def test_private_config_failure_before_any_claim(governed_effect_executor,admin,tmp_path,case):
    fixture=governed_effect_executor;receipt=fixture.ports[0].submit_effect_intent(parameters={'message':'never send'})
    with durable_tls_provider(tmp_path) as (provider,configuration,credential):
        args,sentinels,_=cli_arguments(fixture,tmp_path,configuration)
        if case=='missing_credential':credential.unlink()
        elif case=='unsafe_configuration':configuration.chmod(0o644)
        else:
            from psycopg.conninfo import make_conninfo
            private(tmp_path/'database-url',make_conninfo(fixture.pg,user='nexloop_api'))
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        code,out,err=safe_result(process,sentinels)
        assert code==1 and out=='' and err=='Effect Worker unavailable\n'
        assert provider.control('snapshot')['requests']==[]
        assert admin.execute('select fence from runtime.nexloop_effect_outbox where intent_id=%s',(receipt['intent_id'],)).fetchone()==(0,)


@pytest.mark.parametrize('change',['rotation','revocation'])
def test_idle_sigterm_and_fresh_authority_never_claim(governed_effect_executor,admin,tmp_path,change):
    fixture=governed_effect_executor
    with durable_tls_provider(tmp_path) as (provider,configuration,credential):
        args,sentinels,token=cli_arguments(fixture,tmp_path,configuration,once=False)
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            ready(process)
            if change=='rotation':private(token,'synthetic-invalid-rotated-service-credential')
            else:fixture.revoke_executor_query_grant()
            time.sleep(.15)
            if change=='revocation':
                # Grant configuration advances tenant authority epoch. Mint a
                # genuine fresh source Run and rebind through actual EIOS,
                # rather than weakening the old Run's correctly stale proof.
                from nexloop_eios.effect_contexts import EFFECT
                plan=fixture.plan;backend=plan['backend']
                source=backend.authenticate(plan['submitter_token'],world='real')
                run=source.issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])
                planner=backend.authenticate(plan['planner_token'],world='real')
                planner.bind_effect_context(step_id=plan['step'],step_revision=1,goal_revision=1,
                    consumer_revision=1,control_revision=1,run_id=run.run_id,run_token=run.token,executor_token=plan['executor_token'])
                source=backend.authenticate_run(run.token,world='real',run_id=run.run_id)
            else:source=fixture.ports[0]
            receipt=source.submit_effect_intent(parameters={'message':'rotated service cannot send'})
            time.sleep(.3);process.terminate()
            code,out,err=safe_result(process,sentinels+[credential.read_text(),'synthetic-invalid-rotated-service-credential'])
            assert code==0 and out=='' and err==''
            assert provider.control('snapshot')['requests']==[]
            assert admin.execute('select fence from runtime.nexloop_effect_outbox where intent_id=%s',(receipt['intent_id'],)).fetchone()==(0,)
        finally:
            if process.poll() is None:process.kill();process.communicate()


@pytest.mark.parametrize('argument,value',[('--lease-seconds','2'),('--tick-seconds','nan'),('--world','shadow')])
def test_argument_ranges_fixed_no_value_echo(argument,value):
    arguments=[sys.executable,'-m','nexloop_eios.effect_worker']
    for name in ('database-url-file','signing-key-file','service-credential-file','artifact-root','provider-config-file'):
        arguments.extend(['--'+name,'/synthetic-private-unused'])
    if argument!='--world':arguments.extend(['--world','real'])
    arguments.extend([argument,value])
    process=subprocess.run(arguments,capture_output=True,text=True)
    assert process.returncode==2 and process.stdout=='' and process.stderr=='Effect Worker configuration unavailable\n'
