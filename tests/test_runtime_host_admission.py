"""Actual optional Host HTTPS admission + restricted PG activation guard.

Uses deterministic frozen Pi, actual inherited owner lock and private TLS files.
No model call, browser credential, production DSN or external effect is used.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import httpx
import pytest
from test_agent_host import files,free_port,trusted_tls
from test_runtime_authority import authority
from test_runtime_activation import synthetic_credentials,activation,reopened_worker
from nexloop_eios.runtime_control import create_runtime_guard_server
ROOT=Path(__file__).resolve().parents[1]
INPUT='synthetic activation input'
class PrivateHeaders(dict):
    def __repr__(self):return '<private synthetic transport headers>'

def guard_workers():
    """NX-049 prototype: NEXLOOP_TEST_GUARD_WORKERS=N (N>1) serves guard_server(..., spawn=...) from N child processes."""
    value=os.environ.get('NEXLOOP_TEST_GUARD_WORKERS','1')
    assert value.isdigit() and 1<=int(value)<=16,'NEXLOOP_TEST_GUARD_WORKERS must be 1..16'
    return int(value)


@contextmanager
def guard_server(worker,tmp_path,key,spawn=None):
    """spawn: dict(database_url, signing_key_file, signing_key_id, artifact_root, token, world) of the
    same identity and Backend configuration as `worker`; used only in multi-process mode."""
    if spawn is not None and guard_workers()>1:
        from nexloop_eios.runtime_guard_worker import GuardFiles,GuardWorkerPool
        private=tmp_path/'guard-spawn';private.mkdir(mode=0o700,exist_ok=True)
        dsn=private/'database-url';dsn.write_text(spawn['database_url']);dsn.chmod(0o600)
        credential=private/'service-credential';credential.write_text(spawn['token']);credential.chmod(0o600)
        pool=GuardWorkerPool(GuardFiles(dsn,Path(spawn['signing_key_file']),spawn['signing_key_id'],credential,Path(spawn['artifact_root']),
            spawn['world'],key,tmp_path/'host-cert.pem',tmp_path/'host-key.pem'),port=0,workers=guard_workers())
        port=pool.start()
        try:yield port
        finally:pool.stop()
        return
    server=create_runtime_guard_server(worker,port=0,key_file=key,
        certificate_file=tmp_path/'host-cert.pem',tls_key_file=tmp_path/'host-key.pem')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server.server_port
    finally:server.shutdown();server.server_close();thread.join(5);assert not thread.is_alive()

@contextmanager
def host(runtime,key,configuration):
    port=free_port();node=shutil.which('node');assert node
    child=subprocess.Popen([sys.executable,str(ROOT/'scripts/agent_host.py'),'--node',node,
        '--runtime-root',str(runtime),'--internal-key-file',str(key),'--port',str(port),
        '--tls-certificate-file',str(key.parent/'host-cert.pem'),'--tls-key-file',str(key.parent/'host-key.pem'),
        '--runtime-config-file',str(configuration)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env={k:v for k,v in os.environ.items() if not k.startswith(('MODEL_','DATABASE_'))})
    with httpx.Client(base_url=f'https://127.0.0.1:{port}',verify=trusted_tls(key),trust_env=False,timeout=5) as client:
        try:
            deadline=time.monotonic()+10
            while True:
                assert child.poll() is None,'configured Host exited'
                try:
                    if client.get('/health/live').status_code==200:break
                except httpx.TransportError:pass
                assert time.monotonic()<deadline;time.sleep(.02)
            yield child,client,PrivateHeaders(Authorization='Bearer '+key.read_text())
        finally:
            if child.poll() is None:child.terminate()
            stdout,stderr=child.communicate(timeout=10)
            assert key.read_text() not in stdout+stderr


def configuration(tmp_path,port,guard_key):
    path=tmp_path/'runtime-config.json';path.write_text(json.dumps({'guard_url':f'https://127.0.0.1:{port}/internal/v1/runtime/authorize',
        'guard_key_file':str(guard_key),'guard_ca_file':str(tmp_path/'host-cert.pem'),'runtime_profile':'deterministic-test'}));path.chmod(0o600)
    return path


def completed(client,headers,ref,command):
    deadline=time.monotonic()+5
    while True:
        result=client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':ref,'command':command})
        assert result.status_code==200,result.text
        if result.json()['submission_status']=='done':return result.json()
        assert time.monotonic()<deadline;time.sleep(.02)


def test_formal_host_activation_sigkill_and_backend_reopen(authority,synthetic_credentials,pg,tmp_path):
    result=activation(authority,synthetic_credentials);_,command,worker,issued,_=authority
    runtime,key=files(tmp_path);guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    body={'activation_ref':result['activation_ref'],'command':command,'input':INPUT}
    model_entered=threading.Event();release_guard=threading.Event()
    class InFlightGuard:
        def authorize_runtime_activation(self,**kwargs):
            result=worker.authorize_runtime_activation(**kwargs)
            if kwargs['operation']=='model':
                model_entered.set();release_guard.wait(3)
            return result
    with guard_server(InFlightGuard(),tmp_path,guard_key) as port:
        config=configuration(tmp_path,port,guard_key)
        with host(runtime,key,config) as (child,client,headers):
            assert client.post('/internal/v1/runs/start',json=body).status_code==401
            assert client.post('/internal/v1/runs/start',headers={**headers,'Origin':'https://untrusted.invalid'},json=body).status_code==401
            accepted=client.post('/internal/v1/runs/start',headers=headers,json=body)
            assert accepted.status_code==202,accepted.text
            receipt=accepted.json();assert model_entered.wait(2),'actual Generation guard was not entered'
            database=runtime/command['run_id']/'runtime.sqlite'
            with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
                generation_ids=[row[0] for row in db.execute("select id from tasks where kind like '%generation%'")]
            assert generation_ids,'actual Pi Generation task was not persisted'
            duplicate=client.post('/internal/v1/runs/start',headers=headers,json=body)
            assert duplicate.status_code==202 and duplicate.json()==receipt
            child.kill();assert child.wait(timeout=10)==-signal.SIGKILL
            release_guard.set()
    # Backend connection/service is replaced, no cached Run token is passed here.
    with reopened_worker(pg,tmp_path,synthetic_credentials) as reopened:
        with guard_server(reopened,tmp_path,guard_key) as port:
            config=configuration(tmp_path,port,guard_key)
            with host(runtime,key,config) as (_,client,headers):
                resumed=client.post('/internal/v1/runs/resume',headers=headers,json=body)
                assert resumed.status_code==202 and resumed.json()==receipt
                restored=completed(client,headers,result['activation_ref'],command)
                assert restored['submission_id']==receipt['submission_id'] and restored['persistence']=={'synchronous':2,'journal_mode':'wal'}
                with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
                    restored_ids=[row[0] for row in db.execute("select id from tasks where kind like '%generation%'")]
                assert set(generation_ids)<=set(restored_ids)
    for path in runtime.rglob('*'):
        if path.is_file():assert issued.token.encode() not in path.read_bytes()


def test_formal_host_live_revocation_and_guard_unavailable(authority,synthetic_credentials,admin,tmp_path):
    result=activation(authority,synthetic_credentials);_,command,worker,_,invocation=authority
    runtime,key=files(tmp_path);guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    with guard_server(worker,tmp_path,guard_key) as port:
        with host(runtime,key,configuration(tmp_path,port,guard_key)) as (_,client,headers):
            body={'activation_ref':result['activation_ref'],'command':command,'input':INPUT}
            assert client.post('/internal/v1/runs/start',headers=headers,json=body).status_code==202
            completed(client,headers,result['activation_ref'],command)
            admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{status}','\"revoked\"') where fact_kind='agent_release' and entity_key=%s",([invocation.release_id],))
            assert client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':result['activation_ref'],'command':command}).status_code==503
            assert client.post('/internal/v1/runs/resume',headers=headers,json=body).status_code==503
            guard_key.write_text(secrets.token_hex(32))
            assert client.post('/internal/v1/runs/cancel',headers=headers,json={'activation_ref':result['activation_ref'],'command':command}).status_code==503


def test_guard_slow_tls_client_does_not_block_other_requests(tmp_path):
    # Transport robustness only; intentionally denies all authority.
    class DeniedWorker:
        def authorize_runtime_activation(self,**kwargs):raise RuntimeError('synthetic denial')
    _,key=files(tmp_path)
    with guard_server(DeniedWorker(),tmp_path,key) as port:
        idle=socket.create_connection(('127.0.0.1',port),timeout=1)
        try:
            with httpx.Client(verify=trusted_tls(key),trust_env=False,timeout=1) as client:
                response=client.post(f'https://127.0.0.1:{port}/internal/v1/runtime/authorize',headers={'Authorization':'Bearer '+key.read_text()},json={'activation_ref':'activation_synthetic','command':{},'operation':'model'})
                assert response.status_code==503
        finally:idle.close()


def test_formal_host_fails_closed_after_guard_listener_stops(authority,synthetic_credentials,tmp_path):
    from contextlib import ExitStack
    result=activation(authority,synthetic_credentials);_,command,worker,_,_=authority
    runtime,key=files(tmp_path);guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    with ExitStack() as guards:
        port=guards.enter_context(guard_server(worker,tmp_path,guard_key))
        with host(runtime,key,configuration(tmp_path,port,guard_key)) as (_,client,headers):
            body={'activation_ref':result['activation_ref'],'command':command,'input':INPUT}
            assert client.post('/internal/v1/runs/start',headers=headers,json=body).status_code==202
            completed(client,headers,result['activation_ref'],command)
            guards.close()
            assert client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':result['activation_ref'],'command':command}).status_code==503
            assert client.post('/internal/v1/runs/resume',headers=headers,json=body).status_code==503
