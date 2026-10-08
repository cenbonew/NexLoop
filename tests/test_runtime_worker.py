"""Independent Runtime Worker CLI + actual TLS Node Host + restricted PG.

Only synthetic private configuration is provisioned in disposable test paths.
No authorization stub, production credential or direct business SQL is used.
"""
from contextlib import contextmanager
import json
import os
import queue
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo
from test_runtime_atomic_accept import accepted_input
from test_runtime_authority import authority
from test_runtime_activation import synthetic_credentials
from test_runtime_host_admission import host,configuration
from test_agent_host import files,free_port

ROOT=Path(__file__).resolve().parents[1]


def private(path,value):
    path.write_text(value);path.chmod(0o600);return path


def config(pg,tmp_path,tokens,guard_port,origin,key):
    dsn=private(tmp_path/'worker-dsn',make_conninfo(pg,user='nexloop_domain_worker'))
    credential=private(tmp_path/'worker-service-credential',tokens['-queue-worker'])
    guard_key=private(tmp_path/'worker-guard-key',secrets.token_hex(32))
    return [sys.executable,'-m','nexloop_eios.runtime_worker',
        '--database-url-file',str(dsn),'--signing-key-file',str(tmp_path/'synthetic-authority'),
        '--service-credential-file',str(credential),'--artifact-root',str(tmp_path/'cli-artifacts'),
        '--signing-key-id','synthetic-runtime','--world','real','--queue','operations',
        '--host-origin',origin,'--host-control-key-file',str(key),'--host-ca-file',str(tmp_path/'host-cert.pem'),
        '--guard-port',str(guard_port),'--guard-key-file',str(guard_key),
        '--guard-certificate-file',str(tmp_path/'host-cert.pem'),'--guard-tls-key-file',str(tmp_path/'host-key.pem'),
        '--lease-seconds','3','--total-timeout','10','--request-timeout','1','--poll-seconds','.1','--tick-seconds','.05'],guard_key


@contextmanager
def worker_process(arguments,*,ready=True):
    child=subprocess.Popen(arguments,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env={key:value for key,value in os.environ.items() if not key.startswith(('MODEL_','DATABASE_'))})
    events=queue.Queue();stdout=[];stderr=[]
    def read(stream,target):
        for line in stream:
            target.append(line)
            if stream is child.stdout:events.put(line)
    readers=[threading.Thread(target=read,args=(child.stdout,stdout),daemon=True),
        threading.Thread(target=read,args=(child.stderr,stderr),daemon=True)]
    for reader in readers:reader.start()
    try:
        if ready:
            deadline=time.monotonic()+15
            while True:
                try:line=events.get(timeout=.1)
                except queue.Empty:
                    assert time.monotonic()<deadline and child.poll() is None,'Worker unavailable before ready'
                    continue
                if 'Runtime Worker ready' in line:break
                assert time.monotonic()<deadline and child.poll() is None,'Worker unavailable before ready'
        yield child,stdout,stderr
    finally:
        if child.poll() is None:child.terminate()
        child.wait(timeout=15)
        for reader in readers:reader.join(5)
        child.stdout.close();child.stderr.close()


def assert_redacted(stdout,stderr,args,tokens,issued):
    output=''.join(stdout+stderr)
    assert issued.token not in output
    assert all(token not in output for token in tokens.values())
    assert args['input'] not in output
    assert 'dbname=' not in output and 'user=nexloop_domain_worker' not in output


@contextmanager
def stack(pg,tmp_path,tokens):
    runtime,key=files(tmp_path);guard_port=free_port()
    arguments,guard_key=config(pg,tmp_path,tokens,guard_port,'https://127.0.0.1:1',key)
    cfg=configuration(tmp_path,guard_port,guard_key)
    with host(runtime,key,cfg) as (node,client,headers):
        arguments[arguments.index('--host-origin')+1]=str(client.base_url).rstrip('/')
        yield runtime,node,arguments,guard_port


def test_cli_once_consumes_persisted_runtime_task_and_finishes_real_pg(accepted_input,synthetic_credentials,pg,tmp_path):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with stack(pg,tmp_path,synthetic_credentials) as (runtime,_,arguments,_):
        with worker_process(arguments+['--once']) as (process,out,err):
            assert process.wait(timeout=20)==0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        task=worker.inspect_task(queue='operations',task_id=accepted['task_id'])
        assert task['status']=='succeeded' and task['result']['business_action_success'] is False
        with sqlite3.connect(runtime/issued.run_id/'runtime.sqlite') as db:
            assert db.execute('select count(*) from submissions').fetchone()[0]==1


def test_cli_idle_sigterm_exits_and_releases_guard_port(accepted_input,synthetic_credentials,pg,tmp_path):
    _,_,args,issued=accepted_input
    with stack(pg,tmp_path,synthetic_credentials) as (_,_,arguments,port):
        with worker_process(arguments) as (process,out,err):
            process.terminate();assert process.wait(timeout=10)==0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        with socket.socket() as listener:listener.bind(('127.0.0.1',port))


@pytest.mark.parametrize('fault',['guard_bind','public_credential','revoked_credential','invalid_ca'])
def test_cli_startup_failure_never_claims_task(accepted_input,synthetic_credentials,pg,tmp_path,admin,fault):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with stack(pg,tmp_path,synthetic_credentials) as (_,_,arguments,port):
        busy=None
        try:
            if fault=='guard_bind':
                busy=socket.socket();busy.bind(('127.0.0.1',port));busy.listen()
            elif fault=='public_credential':(tmp_path/'worker-service-credential').chmod(0o644)
            elif fault=='invalid_ca':private(tmp_path/'host-cert.pem','synthetic-invalid-PEM')
            else:admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",(worker._session.token_digest,))
            with worker_process(arguments+['--once'],ready=False) as (process,out,err):
                assert process.wait(timeout=15)!=0
            assert_redacted(out,err,args,synthetic_credentials,issued)
            task=admin.execute('select status,fencing_token from runtime.jobs where job_id=%s',(accepted['task_id'],)).fetchone()
            assert task==('pending',0)
        finally:
            if busy:busy.close()


@pytest.mark.parametrize('option,value',[('--lease-seconds','0'),('--total-timeout','nan'),('--poll-seconds','inf'),('--tick-seconds','0')])
def test_cli_invalid_timing_arguments_fail_closed(accepted_input,synthetic_credentials,pg,tmp_path,admin,option,value):
    api,_,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with stack(pg,tmp_path,synthetic_credentials) as (_,_,arguments,_):
        arguments[arguments.index(option)+1]=value
        with worker_process(arguments+['--once'],ready=False) as (process,out,err):assert process.wait(timeout=10)!=0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        assert admin.execute('select fencing_token from runtime.jobs where job_id=%s',(accepted['task_id'],)).fetchone()[0]==0


def test_cli_idle_credential_rotation_failure_prevents_new_claim(accepted_input,synthetic_credentials,pg,tmp_path,admin):
    api,_,args,issued=accepted_input
    with stack(pg,tmp_path,synthetic_credentials) as (_,_,arguments,_):
        with worker_process(arguments) as (process,out,err):
            private(tmp_path/'worker-service-credential','synthetic-invalid-rotated-credential')
            time.sleep(.2)
            accepted=api.accept_runtime_event(**args)
            time.sleep(.3)
            assert process.poll() is None
            assert admin.execute('select status,fencing_token from runtime.jobs where job_id=%s',(accepted['task_id'],)).fetchone()==('pending',0)
            process.terminate();assert process.wait(timeout=10)==0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        assert 'synthetic-invalid-rotated-credential' not in ''.join(out+err)


def test_cli_wrong_sql_role_fails_before_claim(accepted_input,synthetic_credentials,pg,tmp_path,admin):
    api,_,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with stack(pg,tmp_path,synthetic_credentials) as (_,_,arguments,_):
        private(tmp_path/'worker-dsn',make_conninfo(pg,user='nexloop_api'))
        with worker_process(arguments+['--once'],ready=False) as (process,out,err):assert process.wait(timeout=10)!=0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        assert admin.execute('select fencing_token from runtime.jobs where job_id=%s',(accepted['task_id'],)).fetchone()[0]==0


def test_cli_killed_worker_and_host_automatically_reclaim_same_run(accepted_input,synthetic_credentials,pg,tmp_path,admin):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with stack(pg,tmp_path,synthetic_credentials) as (runtime,node,arguments,port):
        arguments[arguments.index('--poll-seconds')+1]='1'
        with worker_process(arguments) as (process,out,err):
            database=runtime/issued.run_id/'runtime.sqlite';deadline=time.monotonic()+10
            while True:
                try:
                    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
                        submission=db.execute('select id from submissions').fetchone()
                        generations=[row[0] for row in db.execute("select id from tasks where kind like '%generation%'")]
                    if submission and generations:break
                except sqlite3.OperationalError:pass
                assert time.monotonic()<deadline;time.sleep(.005)
            # Kill the runtime first so an absent guard cannot become a durable
            # model failure during the interval between the two SIGKILLs.
            node.kill();assert node.wait(timeout=10)==-signal.SIGKILL
            process.kill();assert process.wait(timeout=10)==-signal.SIGKILL
            first=admin.execute('select status,fencing_token from runtime.jobs where job_id=%s',(accepted['task_id'],)).fetchone()
            assert first[0]=='running'
        assert_redacted(out,err,args,synthetic_credentials,issued)
        time.sleep(3.1)
        guard_key=tmp_path/'worker-guard-key'
        with host(runtime,tmp_path/'internal-key',configuration(tmp_path,port,guard_key)) as (_,client,_):
            arguments[arguments.index('--host-origin')+1]=str(client.base_url).rstrip('/')
            with worker_process(arguments+['--once']) as (replacement,out,err):assert replacement.wait(timeout=20)==0
            assert_redacted(out,err,args,synthetic_credentials,issued)
        task=worker.inspect_task(queue='operations',task_id=accepted['task_id'])
        assert task['status']=='succeeded' and task['fence']>first[1], task['result']
        with sqlite3.connect(database) as db:
            assert db.execute('select count(*) from submissions').fetchone()[0]==1
            assert db.execute('select id from submissions').fetchone()[0]==submission[0]
            assert set(generations)<={row[0] for row in db.execute("select id from tasks where kind like '%generation%'")}


def test_cli_sigterm_drains_current_run_without_claiming_next(accepted_input,authority,synthetic_credentials,pg,tmp_path,admin):
    import copy
    import uuid
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    second=authority[0].backend.authenticate(synthetic_credentials['agent'],world='real').issue_run_credential(
        action_resources=['eios:action:Consumer.create:1'])
    command=copy.deepcopy(args['command']);command.update(run_id=second.run_id,request_id=str(uuid.uuid4()),
        trigger_event_id=str(uuid.uuid4()),credential_ref='run_credential:'+second.run_id,not_after=second.expires_at.isoformat())
    following=api.accept_runtime_event(**{**args,'event_id':str(uuid.uuid4()),'run_token':second.token,'command':command})
    with stack(pg,tmp_path,synthetic_credentials) as (runtime,_,arguments,_):
        arguments[arguments.index('--poll-seconds')+1]='1'
        with worker_process(arguments) as (process,out,err):
            deadline=time.monotonic()+10;database=runtime/issued.run_id/'runtime.sqlite'
            while True:
                try:
                    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
                        submission=db.execute('select id from submissions').fetchone()
                    if submission:break
                except sqlite3.OperationalError:pass
                assert time.monotonic()<deadline;time.sleep(.005)
            current=admin.execute('select status,fencing_token from runtime.jobs where job_id=%s',(accepted['task_id'],)).fetchone()
            assert current[0]=='running'
            process.terminate();assert process.wait(timeout=15)==0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        assert second.token not in ''.join(out+err)
        done=worker.inspect_task(queue='operations',task_id=accepted['task_id'])
        assert done['status']=='succeeded' and done['fence']==current[1]
        assert admin.execute('select status,fencing_token from runtime.jobs where job_id=%s',(following['task_id'],)).fetchone()==('pending',0)
        with sqlite3.connect(database) as db:
            assert db.execute('select id from submissions').fetchall()==[submission]


def test_cli_live_service_revocation_prevents_pending_claim(accepted_input,synthetic_credentials,pg,tmp_path,admin):
    from contextlib import ExitStack
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    with stack(pg,tmp_path,synthetic_credentials) as (_,_,arguments,_):
        with ExitStack() as children:
            # A technical job row lock keeps SKIP LOCKED claims idle while the
            # actual Worker starts authenticated. Revocation and lock release
            # commit together, so the fresh next tick must reject the service.
            with admin.transaction():
                admin.execute('select job_id from runtime.jobs where job_id=%s for update',(accepted['task_id'],))
                process,out,err=children.enter_context(worker_process(arguments))
                admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",(worker._session.token_digest,))
            time.sleep(.3)
            assert process.poll() is None
            assert admin.execute('select status,fencing_token from runtime.jobs where job_id=%s',(accepted['task_id'],)).fetchone()==('pending',0)
            process.terminate();assert process.wait(timeout=10)==0
        assert_redacted(out,err,args,synthetic_credentials,issued)


@pytest.mark.parametrize('execution_authorized',[False,True])
def test_cli_reclaim_missing_sqlite_uses_committed_execution_marker(accepted_input,synthetic_credentials,pg,tmp_path,execution_authorized):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    prior=worker.claim_task(queue='operations',lease_seconds=3)
    assert prior['task_id']==accepted['task_id']
    activated=worker.create_runtime_activation(queue='operations',task_id=prior['task_id'],fence=prior['fence'],
        run_id=issued.run_id,command=args['command'],input=args['input'],owner_epoch=args['command']['runtime_owner_epoch'])
    assert activated['ever_execution_authorized'] is False
    if execution_authorized:
        authorized=worker.authorize_runtime_activation(activation_ref=activated['activation_ref'],
            command=args['command'],operation='model')
        assert authorized['ever_execution_authorized'] is True
    # Independent predecessor ended after the actual PG claim/enrollment, before
    # a Host created any SQLite. Only committed PG execution evidence decides
    # whether the next real CLI may start this exact Run or must fail closed.
    time.sleep(3.1)
    with stack(pg,tmp_path,synthetic_credentials) as (runtime,_,arguments,_):
        database=runtime/issued.run_id/'runtime.sqlite';assert not database.exists()
        with worker_process(arguments+['--once']) as (process,out,err):assert process.wait(timeout=20)==0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        result=worker.inspect_task(queue='operations',task_id=accepted['task_id'])
        assert result['fence']>prior['fence']
        if execution_authorized:
            assert result['status']=='failed' and result['result']['code']=='runtime_reclaimed_state_missing'
            assert not database.exists()
        else:
            assert result['status']=='succeeded' and result['result']['run_id']==issued.run_id
            with sqlite3.connect(database) as db:
                assert db.execute('select count(*) from submissions').fetchone()[0]==1


@pytest.mark.parametrize('partial',['directory','empty_sqlite'])
def test_cli_reclaim_safe_partial_initialization_same_run_full(accepted_input,synthetic_credentials,pg,tmp_path,partial):
    api,worker,args,issued=accepted_input;accepted=api.accept_runtime_event(**args)
    prior=worker.claim_task(queue='operations',lease_seconds=3)
    assert prior['task_id']==accepted['task_id']
    activated=worker.create_runtime_activation(queue='operations',task_id=prior['task_id'],fence=prior['fence'],
        run_id=issued.run_id,command=args['command'],input=args['input'],owner_epoch=args['command']['runtime_owner_epoch'])
    assert activated['ever_execution_authorized'] is False
    time.sleep(3.1)
    with stack(pg,tmp_path,synthetic_credentials) as (runtime,_,arguments,_):
        directory=runtime/issued.run_id;directory.mkdir(mode=0o700)
        database=directory/'runtime.sqlite'
        if partial=='empty_sqlite':
            # Explicit synthetic interrupted initialization, with no binding,
            # schema, task or orphan WAL/SHM; not an asserted process-kill proof.
            with sqlite3.connect(database):pass
            database.chmod(0o600)
        with worker_process(arguments+['--once']) as (process,out,err):
            assert process.wait(timeout=20)==0
        assert_redacted(out,err,args,synthetic_credentials,issued)
        terminal=worker.inspect_task(queue='operations',task_id=accepted['task_id'])
        assert terminal['status']=='succeeded' and terminal['fence']>prior['fence']
        assert terminal['attempts']==2
        receipt=terminal['result']['runtime_receipt']
        assert terminal['result']['run_id']==issued.run_id and receipt['request_id']==args['command']['request_id']
        assert receipt['persistence']=={'journal_mode':'wal','synchronous':2}
        with sqlite3.connect(database) as db:
            submissions=db.execute('select id from submissions').fetchall()
            assert submissions==[(receipt['submission_id'],)]
            assert db.execute('select count(*) from conversations').fetchone()[0]==1
            assert db.execute('select id from conversations').fetchone()[0]==receipt['conversation_id']
