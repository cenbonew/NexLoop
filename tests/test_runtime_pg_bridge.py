"""Real Pi process, PG Run identity/task fence, and inherited kernel owner lock.

The Unix socket is an isolated test backend transport, not a product HTTP route.
Only its Python backend retains the synthetic Run token, signer and restricted
DSNs. The Node process receives a secret-free command and inherited owner FD.
"""
import importlib.util
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager

import pytest
from test_runtime_authority import authority

ROOT=Path(__file__).resolve().parents[1]
_spec=importlib.util.spec_from_file_location('nexloop_test_owner_launcher',ROOT/'scripts/agent_host.py')
_launcher=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_launcher)
acquire_owner=_launcher.acquire_owner


@contextmanager
def bridge(guard):
    with tempfile.TemporaryDirectory(prefix='nl-pg-bridge-',dir='/tmp') as directory:
        path=Path(directory)/'guard.sock';listener=socket.socket(socket.AF_UNIX)
        listener.bind(str(path));path.chmod(0o600);listener.listen();listener.settimeout(.1)
        stopped=threading.Event();calls=[]
        def serve():
            while not stopped.is_set():
                try:connection,_=listener.accept()
                except socket.timeout:continue
                except OSError:break
                with connection:
                    connection.settimeout(5);data=b''
                    while not data.endswith(b'\n') and len(data)<=65536:
                        part=connection.recv(4096)
                        if not part:break
                        data+=part
                    allowed=False;operation='invalid'
                    try:
                        request=json.loads(data);operation=request['operation']
                        allowed=guard.authorize(request['command'],operation)['authorized']
                    except Exception:pass
                    calls.append((operation,allowed))
                    connection.sendall(json.dumps({'authorized':allowed}).encode())
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        try:yield str(path),calls
        finally:stopped.set();listener.close();thread.join(10);assert not thread.is_alive()


@contextmanager
def pi_process(root,command,socket_path,*,resume=False,mode='blocked'):
    node=shutil.which('node');assert node and subprocess.check_output([node,'--version'],text=True).startswith('v24.')
    _,fd=acquire_owner(root)
    process=None;events=queue.Queue();errors=[]
    try:
        process=subprocess.Popen([node,str(ROOT/'apps/agent-host/test/pi-runtime-pg-process.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            pass_fds=(fd,),env={'PATH':os.environ['PATH'],'NEXLOOP_TEST_OWNER_FD':str(fd)})
        # Only the actual Node process retains the locked open-file description.
        os.close(fd);fd=None
        def outputs():
            for line in process.stdout:
                events.put(json.loads(line))
        def diagnostics():
            for line in process.stderr:errors.append(line)
        readers=[threading.Thread(target=outputs,daemon=True),threading.Thread(target=diagnostics,daemon=True)]
        for reader in readers:reader.start()
        process.stdin.write(json.dumps({'root':str(root),'socket':socket_path,'command':command,'resume':resume,'mode':mode})+'\n');process.stdin.flush()
        yield process,events,errors
    finally:
        if fd is not None:os.close(fd)
        if process is not None:
            if process.poll() is None:process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()
            for reader in readers:reader.join(10)


def receive(events,name):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        item=events.get(timeout=max(.01,deadline-time.monotonic()))
        if item['event']==name:return item
    raise AssertionError('actual Pi event unavailable')


def test_actual_pg_guard_pi_sigkill_owner_reopen(authority,tmp_path):
    guard,command,_,issued,_=authority;root=tmp_path/'runtime';root.mkdir(mode=0o700)
    with bridge(guard) as (path,calls):
        with pi_process(root,command,path) as (process,events,errors):
            accepted=receive(events,'accepted')['receipt'];receive(events,'generation_started')
            with pytest.raises(BlockingIOError):acquire_owner(root)
            inode=(root/command['run_id']/'runtime.sqlite').stat().st_ino
            process.kill();assert process.wait(timeout=10)==-signal.SIGKILL
            assert issued.token not in ''.join(errors)
        with pi_process(root,command,path,resume=True,mode='complete') as (process,events,errors):
            restored=receive(events,'accepted')['receipt'];completed=receive(events,'completed')['inspection']
            assert process.wait(timeout=10)==0
            assert restored==accepted and completed['submission_status']=='done'
            assert completed['persistence']=={'synchronous':2,'journal_mode':'wal'}
            assert (root/command['run_id']/'runtime.sqlite').stat().st_ino==inode
            assert issued.token not in ''.join(errors)
        assert ('model',True) in calls and ('resume',True) in calls
        for file in root.rglob('*'):
            if file.is_file():assert issued.token.encode() not in file.read_bytes()


def test_actual_pg_revoke_stops_pi_tool_dispatch(authority,admin,tmp_path):
    guard,command,_,issued,invocation=authority;root=tmp_path/'runtime';root.mkdir(mode=0o700)
    with bridge(guard) as (path,calls):
        with pi_process(root,command,path) as (process,events,errors):
            receive(events,'accepted');receive(events,'generation_started')
            # Synthetic control-plane revocation, never a business write.
            admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{status}','\"revoked\"') where fact_kind='agent_release' and entity_key=%s",([invocation.release_id],))
            process.stdin.write(json.dumps({'operation':'release'})+'\n');process.stdin.flush()
            rejected=receive(events,'authorization_denied');assert rejected['operation']=='tool'
            time.sleep(.1)
            remaining=[]
            while not events.empty():remaining.append(events.get_nowait())
            assert not any(item['event']=='tool_executed' for item in remaining)
            assert ('tool',False) in calls
            assert issued.token not in ''.join(errors)
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


@pytest.mark.parametrize('authority',[1],indirect=True)
def test_actual_pg_lease_expiry_stops_pi_tool_dispatch(authority,admin,tmp_path):
    guard,command,_,issued,_=authority;root=tmp_path/'runtime';root.mkdir(mode=0o700)
    with bridge(guard) as (path,calls):
        with pi_process(root,command,path) as (process,events,errors):
            receive(events,'accepted');receive(events,'generation_started')
            time.sleep(1.1)
            process.stdin.write(json.dumps({'operation':'release'})+'\n');process.stdin.flush()
            assert receive(events,'authorization_denied')['operation']=='tool'
            assert ('tool',False) in calls
            assert issued.token not in ''.join(errors)
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0
