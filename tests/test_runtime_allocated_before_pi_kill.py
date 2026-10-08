"""Actual CLI kill after PG activation commit, before any Host/Pi submission.
Owned TLS transport pauses the start request; it never grants authority or
manufactures a successful response. All forwarded requests use the actual Host.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,signal,ssl,threading,time
import httpx
from test_runtime_worker import stack,worker_process,assert_redacted
from test_runtime_atomic_accept import accepted_input
from test_runtime_authority import authority
from test_runtime_activation import synthetic_credentials
from test_agent_host import free_port


@contextmanager
def before_start_transport(tmp_path,actual_origin):
    reached=threading.Event();release=threading.Event();errors=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            if self.path=='/internal/v1/runs/start':
                reached.set();release.wait(10)
                payload=b'{"code":"runtime_transport_unavailable"}';status=503
            else:
                try:
                    with httpx.Client(verify=str(tmp_path/'host-cert.pem'),trust_env=False,timeout=2) as client:
                        response=client.post(actual_origin+self.path,headers={'Authorization':self.headers.get('Authorization',''),'Content-Type':'application/json'},content=body)
                        payload=response.content;status=response.status_code
                except Exception:
                    errors.append('forward_unavailable');payload=b'{"code":"runtime_transport_unavailable"}';status=503
            try:
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
            except (BrokenPipeError,ConnectionResetError,ssl.SSLError):pass
    server=ThreadingHTTPServer(('127.0.0.1',free_port()),Handler);server.daemon_threads=False
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(tmp_path/'host-cert.pem',tmp_path/'host-key.pem')
    server.socket=tls.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:yield 'https://127.0.0.1:'+str(server.server_port),reached
    finally:
        release.set();server.shutdown();server.server_close();thread.join(5)
        assert not thread.is_alive() and errors==[]


def test_actual_cli_sigkill_after_allocation_before_pi_submission_recovers_same_run(accepted_input,synthetic_credentials,pg,tmp_path,admin):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with stack(pg,tmp_path,synthetic_credentials) as (runtime,_,arguments,_):
        direct_origin=arguments[arguments.index('--host-origin')+1]
        with before_start_transport(tmp_path,direct_origin) as (origin,reached):
            arguments[arguments.index('--host-origin')+1]=origin
            with worker_process(arguments) as (old,out,err):
                assert reached.wait(10),'actual CLI did not reach unsubmitted start request'
                row=admin.execute('select fencing_token,status from runtime.jobs where job_id=%s',(accepted['task_id'],)).fetchone()
                assert row[1]=='running'
                assert admin.execute('select count(*) from authz.nexloop_runtime_activations where run_id=%s',(issued.run_id,)).fetchone()==(1,)
                assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers where run_id=%s',(issued.run_id,)).fetchone()==(0,)
                assert not (runtime/issued.run_id/'runtime.sqlite').exists()
                old.kill();assert old.wait(10)==-signal.SIGKILL
            assert_redacted(out,err,args,synthetic_credentials,issued)
        time.sleep(3.2)
        arguments[arguments.index('--host-origin')+1]=direct_origin
        with worker_process(arguments+['--once']) as (replacement,out,err):assert replacement.wait(20)==0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        terminal=worker.inspect_task(queue='operations',task_id=accepted['task_id'])
        assert terminal['status']=='succeeded' and terminal['fence']>row[0] and terminal['attempts']==2
        result=terminal['result'];receipt=result['runtime_receipt']
        assert result['run_id']==issued.run_id and receipt['request_id']==args['command']['request_id']
        assert receipt['persistence']=={'journal_mode':'wal','synchronous':2}
        import sqlite3
        with sqlite3.connect(runtime/issued.run_id/'runtime.sqlite') as db:
            assert db.execute('select id from submissions').fetchall()==[(receipt['submission_id'],)]
            assert db.execute('select id from conversations').fetchall()==[(receipt['conversation_id'],)]
