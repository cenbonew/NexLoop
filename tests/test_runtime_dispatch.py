"""Actual restricted PostgreSQL Worker -> TLS Host -> frozen durable Pi.

No authority/provider/Host response stub replaces the governed integration.
"""
from contextlib import contextmanager
import json
import secrets
import sqlite3
import time

import pytest
from test_runtime_atomic_accept import accepted_input
from test_runtime_authority import authority
from test_runtime_activation import synthetic_credentials
from test_runtime_host_admission import guard_server,host,configuration
from test_agent_host import files
from nexloop_eios.host_control import HostControlConfiguration
from nexloop_eios.runtime_dispatch import RuntimeDispatcher,RuntimeDispatchError


@contextmanager
def dispatch_host(worker,tmp_path):
    runtime,key=files(tmp_path);guard_key=tmp_path/'dispatch-guard.key'
    guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    with guard_server(worker,tmp_path,guard_key) as port:
        with host(runtime,key,configuration(tmp_path,port,guard_key)) as (child,client,headers):
            config=HostControlConfiguration(str(client.base_url).rstrip('/'),key,tmp_path/'host-cert.pem')
            yield runtime,child,client,headers,config


def test_actual_pg_host_worker_runtime_completion(accepted_input,admin,tmp_path,monkeypatch):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1')
    with dispatch_host(worker,tmp_path) as (runtime,_,_,_,config):
        result=RuntimeDispatcher(worker,config,queue='operations',total_timeout=10).run_once()
        assert result['task_id']==accepted['task_id'] and result['status']=='succeeded'
        receipt=result['result']['runtime_receipt']
        assert set(receipt)=={'request_id','conversation_id','submission_id','persistence'}
        assert receipt['request_id']==args['command']['request_id']
        assert receipt['persistence']=={'journal_mode':'wal','synchronous':2}
        assert result['result']=={'scope':'runtime_only','business_action_success':False,'runtime_outcome':'succeeded','run_id':issued.run_id,'runtime_receipt':receipt}
        durable=worker.inspect_task(queue='operations',task_id=accepted['task_id'])
        assert durable['status']=='succeeded' and durable['result']==result['result']
        assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0
        database=runtime/issued.run_id/'runtime.sqlite'
        with sqlite3.connect(database) as db:
            assert db.execute("select count(*) from tasks where kind like '%generation%'").fetchone()[0]>=1
            assert db.execute('select id from conversations').fetchone()[0]==receipt['conversation_id']
            assert db.execute('select id from submissions').fetchone()[0]==receipt['submission_id']
        assert issued.token.encode() not in database.read_bytes()
        assert RuntimeDispatcher(worker,config,queue='operations').run_once()=={'claimed':False}


def test_reclaimed_unexecuted_run_can_start_same_identity(accepted_input,tmp_path):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    first=worker.claim_task(queue='operations',lease_seconds=1);assert first['task_id']==accepted['task_id']
    time.sleep(1.1)
    with dispatch_host(worker,tmp_path) as (runtime,_,_,_,config):
        result=RuntimeDispatcher(worker,config,queue='operations',total_timeout=10).run_once()
        assert result['fence']>first['fence'] and result['status']=='succeeded'
        assert result['result']['run_id']==issued.run_id
        assert (runtime/issued.run_id/'runtime.sqlite').is_file()


def test_reclaimed_authorized_execution_missing_storage_is_not_rebuilt(accepted_input,tmp_path):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    first=worker.claim_task(queue='operations',lease_seconds=1)
    activation=worker.create_runtime_activation(queue='operations',task_id=first['task_id'],fence=first['fence'],
        run_id=issued.run_id,command=args['command'],input=args['input'],owner_epoch=1)
    assert activation['ever_execution_authorized'] is False
    authorization=worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=args['command'],operation='model')
    assert authorization['authorized'] is True and authorization['ever_execution_authorized'] is True
    # This is an actual committed EIOS model-dispatch permission, conservatively
    # enough to forbid reconstruction; it does not claim a provider was called.
    time.sleep(1.1)
    with dispatch_host(worker,tmp_path) as (runtime,_,_,_,config):
        result=RuntimeDispatcher(worker,config,queue='operations',total_timeout=10).run_once()
        assert result['task_id']==accepted['task_id'] and result['fence']>first['fence']
        assert result['status']=='failed' and result['result']['code']=='runtime_reclaimed_state_missing'
        assert not (runtime/issued.run_id).exists()


def test_reclaimed_existing_runtime_preserves_submission(accepted_input,tmp_path):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with dispatch_host(worker,tmp_path) as (runtime,_,client,headers,config):
        first=worker.claim_task(queue='operations',lease_seconds=2)
        activation=worker.create_runtime_activation(queue='operations',task_id=first['task_id'],fence=first['fence'],
            run_id=issued.run_id,command=args['command'],input=args['input'],owner_epoch=1)
        body={'activation_ref':activation['activation_ref'],'command':args['command'],'input':args['input']}
        response=client.post('/internal/v1/runs/start',headers=headers,json=body)
        assert response.status_code==202;receipt=response.json()
        time.sleep(2.1)
        result=RuntimeDispatcher(worker,config,queue='operations',total_timeout=10).run_once()
        assert result['task_id']==accepted['task_id'] and result['fence']>first['fence']
        assert result['status']=='succeeded'
        with sqlite3.connect(runtime/issued.run_id/'runtime.sqlite') as db:
            assert db.execute('select count(*) from submissions').fetchone()[0]==1
            assert db.execute('select id from submissions').fetchone()[0]==receipt['submission_id']


def test_worker_renewal_is_real_pg_before_fenced_finish(accepted_input,tmp_path,monkeypatch):
    api,worker,args,_=accepted_input;api.accept_runtime_event(**args)
    renewed=[];renew=worker.renew_task
    def observe(**kwargs):
        result=renew(**kwargs);renewed.append(result);return result
    monkeypatch.setattr(worker,'renew_task',observe)
    class SlowModelGuard:
        def authorize_runtime_activation(self,**kwargs):
            result=worker.authorize_runtime_activation(**kwargs)
            if kwargs['operation']=='model':time.sleep(1.3)
            return result
    with dispatch_host(SlowModelGuard(),tmp_path) as (_,_,_,_,config):
        result=RuntimeDispatcher(worker,config,queue='operations',lease_seconds=3,request_timeout=.9,total_timeout=10).run_once()
        assert result['status']=='succeeded' and renewed
        assert all(row['fence']==result['fence'] for row in renewed)


def test_actual_host_unavailable_is_unknown_retry_same_run(accepted_input,tmp_path):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with dispatch_host(worker,tmp_path) as (_,child,_,_,config):
        child.kill();child.wait(5)
        result=RuntimeDispatcher(worker,config,queue='operations',total_timeout=3).run_once()
        assert result['task_id']==accepted['task_id'] and result['status']=='retry_wait'
        assert result['result']['runtime_outcome']=='unknown' and result['result']['run_id']==issued.run_id
        assert result['result']['business_action_success'] is False
        assert 'runtime_receipt' not in result['result']


def test_dispatch_transport_key_failure_is_redacted(accepted_input,tmp_path):
    api,worker,args,issued=accepted_input;api.accept_runtime_event(**args)
    with dispatch_host(worker,tmp_path) as (_,_,_,_,config):
        config.key_file.write_text('synthetic-secret-sentinel')
        result=RuntimeDispatcher(worker,config,queue='operations',total_timeout=3).run_once()
        assert result['status']=='retry_wait'
        assert 'synthetic-secret-sentinel' not in json.dumps(result)


def test_worker_reclaim_sigkilled_inflight_pi_with_reopened_backend(accepted_input,synthetic_credentials,pg,tmp_path):
    import signal
    import threading
    from test_runtime_activation import reopened_worker
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    entered=threading.Event();release=threading.Event()
    class InFlightGuard:
        def authorize_runtime_activation(self,**kwargs):
            result=worker.authorize_runtime_activation(**kwargs)
            if kwargs['operation']=='model':entered.set();release.wait(3)
            return result
    runtime,key=files(tmp_path);guard_key=tmp_path/'recovery-guard.key'
    guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    first=worker.claim_task(queue='operations',lease_seconds=1)
    activation=worker.create_runtime_activation(queue='operations',task_id=first['task_id'],fence=first['fence'],run_id=issued.run_id,
        command=args['command'],input=args['input'],owner_epoch=1)
    try:
        with guard_server(InFlightGuard(),tmp_path,guard_key) as port:
            with host(runtime,key,configuration(tmp_path,port,guard_key)) as (child,client,headers):
                response=client.post('/internal/v1/runs/start',headers=headers,json={
                    'activation_ref':activation['activation_ref'],'command':args['command'],'input':args['input']})
                assert response.status_code==202;receipt=response.json();assert entered.wait(2)
                with sqlite3.connect(runtime/issued.run_id/'runtime.sqlite') as db:
                    generations=[row[0] for row in db.execute("select id from tasks where kind like '%generation%'")]
                assert generations
                child.kill();assert child.wait(5)==-signal.SIGKILL
                release.set()
    finally:release.set()
    worker._backend._shutdown();worker._backend._pool.close()
    time.sleep(1.1)
    with reopened_worker(pg,tmp_path,synthetic_credentials) as reopened:
        with guard_server(reopened,tmp_path,guard_key) as port:
            with host(runtime,key,configuration(tmp_path,port,guard_key)) as (_,client,_,):
                config=HostControlConfiguration(str(client.base_url).rstrip('/'),key,tmp_path/'host-cert.pem')
                result=RuntimeDispatcher(reopened,config,queue='operations',total_timeout=10).run_once()
                assert result['task_id']==accepted['task_id'] and result['fence']>first['fence'] and result['status']=='succeeded'
    with sqlite3.connect(runtime/issued.run_id/'runtime.sqlite') as db:
        assert db.execute('select id from submissions').fetchone()[0]==receipt['submission_id']
        assert db.execute('select count(*) from submissions').fetchone()[0]==1
        restored=[row[0] for row in db.execute("select id from tasks where kind like '%generation%'")]
        assert set(generations)<=set(restored)


@pytest.mark.parametrize('mode',['redirect','oversize','trickle_headers','silent_handshake','persistence_missing','persistence_extra','persistence_weak','persistence_string'])
def test_transport_only_tls_is_bounded_without_redirect_or_proxy(tmp_path,mode,monkeypatch):
    # Transport-only evidence: this server supplies no EIOS authority or Run.
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import socketserver
    import ssl
    import threading
    from nexloop_eios.runtime_dispatch import RuntimeHostClient
    _,key=files(tmp_path)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            if mode=='trickle_headers':
                try:
                    for byte in b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n':
                        self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.02)
                except (OSError,ssl.SSLError):pass
                return
            body=b'{}' if mode=='redirect' else b' '*65537
            if mode.startswith('persistence_'):
                value={'run_id':'transport-only','request_id':'transport-only','conversation_id':1,'submission_id':2,
                    'runtime_outcome':'succeeded','persistence':{'journal_mode':'wal','synchronous':2}}
                if mode=='persistence_missing':value.pop('persistence')
                elif mode=='persistence_extra':value['persistence']['unexpected']='synthetic-sensitive-diagnostic'
                elif mode=='persistence_weak':value['persistence']['synchronous']=1
                elif mode=='persistence_string':value['persistence']['synchronous']='2'
                body=json.dumps(value).encode()
            self.send_response(307 if mode=='redirect' else 200)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
            if mode=='redirect':self.send_header('Location','https://untrusted.invalid/credential-capture')
            self.end_headers()
            try:self.wfile.write(body)
            except OSError:pass
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        def handle_error(self,*args):pass
    class Idle(socketserver.BaseRequestHandler):
        def handle(self):self.request.recv(1);time.sleep(.5)
    if mode=='silent_handshake':
        class PlainServer(socketserver.ThreadingTCPServer):
            daemon_threads=True
            def handle_error(self,*args):pass
        server=PlainServer(('127.0.0.1',0),Idle)
    else:
        server=Server(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tmp_path/'host-cert.pem',tmp_path/'host-key.pem')
        server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1')
    configuration=HostControlConfiguration(f'https://127.0.0.1:{server.server_address[1]}',key,tmp_path/'host-cert.pem')
    started=time.monotonic()
    try:
        with pytest.raises(RuntimeDispatchError) as failure:
            RuntimeHostClient(configuration).request('inspect',{'activation_ref':'activation_transport_only',
                'command':{'run_id':'transport-only','request_id':'transport-only'}},deadline=started+.2)
        assert failure.value.code in {'runtime_transport_timeout','runtime_transport_unavailable'}
        assert time.monotonic()-started<.8
        assert key.read_text() not in str(failure.value)
    finally:server.shutdown();server.server_close();thread.join(3)


def test_restricted_worker_pool_statement_and_lock_timeout(authority):
    _,_,worker,_,_=authority
    with worker._backend._pool.connection() as connection:
        assert connection.execute('show statement_timeout').fetchone()[0]=='10s'
        assert connection.execute('show lock_timeout').fetchone()[0]=='3s'
        assert connection.execute('select session_user').fetchone()[0]=='nexloop_domain_worker'
