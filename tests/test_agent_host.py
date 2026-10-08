"""Actual Node Host/OS-lock lifecycle, with owned private synthetic credentials."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import shutil
import signal
import ssl
import socket
import subprocess
import sys
import time

import httpx
import pytest

ROOT=Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        return listener.getsockname()[1]


def files(tmp_path):
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    key=tmp_path/'internal-key';key.write_text(secrets.token_hex(32));key.chmod(0o600)
    certificate=tmp_path/'host-cert.pem';tls_key=tmp_path/'host-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(tls_key),'-out',str(certificate),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
    certificate.chmod(0o600);tls_key.chmod(0o600)
    return runtime,key


def trusted_tls(key):
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(key.parent/'host-cert.pem'))
    return context


def start(runtime,key,port):
    node=shutil.which('node')
    assert node and (ROOT/'apps/agent-host/dist/main.js').is_file(),'Node24 and built actual Host required'
    return subprocess.Popen([sys.executable,str(ROOT/'scripts/agent_host.py'),'--node',node,
        '--runtime-root',str(runtime),'--internal-key-file',str(key),'--port',str(port),
        '--tls-certificate-file',str(key.parent/'host-cert.pem'),'--tls-key-file',str(key.parent/'host-key.pem')],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)


@contextmanager
def running(runtime,key):
    original_key=key.read_text()
    port=free_port();child=start(runtime,key,port)
    context=trusted_tls(key)
    with httpx.Client(base_url=f'https://127.0.0.1:{port}',verify=context,trust_env=False,timeout=2) as client:
        try:
            deadline=time.monotonic()+10
            while True:
                assert child.poll() is None,'Host exited during startup'
                try:
                    if client.get('/health/live').status_code==200:break
                except httpx.TransportError:pass
                assert time.monotonic()<deadline
                time.sleep(.05)
            yield child,client,port
        finally:
            if child.poll() is None:child.terminate()
            out,err=child.communicate(timeout=10)
            assert original_key not in out+err and key.read_text() not in out+err


def test_actual_host_internal_auth_rotation_and_owned_shutdown(tmp_path):
    runtime,key=files(tmp_path)
    with running(runtime,key) as (child,client,port):
        route='/internal/v1/health/ready';token=key.read_text()
        auth={'Authorization':'Bearer '+token}
        assert client.get(route).status_code==401
        assert client.get(route,headers={'Authorization':'Bearer '+secrets.token_hex(32)}).status_code==401
        response=client.get(route,headers=auth)
        assert response.status_code==503 and response.json()['foundation']=={'owner_lock':True,'internal_auth':True}
        assert not response.json()['product_ready']
        for headers in ({'Origin':'https://untrusted.invalid'},{'Sec-Fetch-Site':'same-origin'},{'Host':'untrusted.invalid'}):
            assert client.get(route,headers={**auth,**headers}).status_code==401
        assert client.post('/internal/v1/runs',headers=auth,json={'run_id':'untrusted'}).status_code==404
        rotated=tmp_path/'rotated-key';rotated.write_text(secrets.token_hex(32));rotated.chmod(0o600);rotated.replace(key)
        assert client.get(route,headers=auth).status_code==401
        auth={'Authorization':'Bearer '+key.read_text()}
        assert client.get(route,headers=auth).json()['foundation']['internal_auth']
        key.chmod(0o644)
        assert client.get(route,headers=auth).status_code==503
        key.chmod(0o600)
        runtime.chmod(0o755)
        assert client.get(route,headers=auth).status_code==503
        runtime.chmod(0o700)
        assert client.get(route,headers=auth).json()['foundation']['owner_lock']
    assert child.returncode==0
    with open(runtime/'.nexloop-owner.lock','rb') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with pytest.raises(httpx.TransportError):
        httpx.get(f'https://127.0.0.1:{port}/health/live',verify=trusted_tls(key),timeout=1,trust_env=False)


def test_second_host_refused_and_sigkill_reopen_keeps_runtime_files(tmp_path):
    runtime,key=files(tmp_path)
    retained=runtime/'synthetic-runtime-evidence';retained.write_bytes(b'do-not-delete-on-owner-recovery')
    with running(runtime,key) as (first,client,_):
        owner=runtime/'.nexloop-owner.lock';inode=owner.stat().st_ino
        with open(owner,'rb') as contender:
            with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
        second_port=free_port();second=start(runtime,key,second_port)
        out,err=second.communicate(timeout=10)
        assert second.returncode==1 and key.read_text() not in out+err
        with pytest.raises(httpx.TransportError):
            httpx.get(f'https://127.0.0.1:{second_port}/health/live',verify=trusted_tls(key),timeout=1,trust_env=False)
        assert client.get('/health/live').status_code==200
        first.send_signal(signal.SIGKILL);first.wait(timeout=10)
    assert first.returncode==-signal.SIGKILL
    with running(runtime,key) as (_,client,_):
        response=client.get('/internal/v1/health/ready',headers={'Authorization':'Bearer '+key.read_text()})
        assert response.status_code==503 and response.json()['foundation']['owner_lock']
        assert owner.stat().st_ino==inode and retained.read_bytes()==b'do-not-delete-on-owner-recovery'


@pytest.mark.parametrize('fault',['root_mode','owner_symlink','owner_content','owner_hardlink','key_symlink','key_mode'])
def test_private_startup_refused_without_listener(tmp_path,fault):
    runtime,key=files(tmp_path);owner=runtime/'.nexloop-owner.lock'
    if fault=='root_mode':runtime.chmod(0o755)
    elif fault=='owner_symlink':owner.symlink_to(key)
    elif fault=='owner_content':owner.write_text('foreign runtime owner');owner.chmod(0o600)
    elif fault=='owner_hardlink':os.link(key,owner)
    elif fault=='key_symlink':
        actual=tmp_path/'actual-key';key.rename(actual);key.symlink_to(actual)
    elif fault=='key_mode':key.chmod(0o644)
    port=free_port();child=start(runtime,key,port);out,err=child.communicate(timeout=10)
    assert child.returncode==1 and key.read_text() not in out+err
    with pytest.raises(httpx.TransportError):
        httpx.get(f'https://127.0.0.1:{port}/health/live',verify=trusted_tls(key),timeout=1,trust_env=False)
