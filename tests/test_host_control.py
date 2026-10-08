"""Actual API-to-Node control-plane network requests, never a mocked Host."""
from dataclasses import replace
import secrets
import pytest
from fastapi.testclient import TestClient
from nexloop_eios.host_control import HostControlConfiguration, probe_host
from nexloop_eios.http_api import ApiConfiguration, create_app
from test_agent_host import files, running


def test_actual_api_to_node_auth_rotation_and_host_shutdown(tmp_path):
    runtime,key=files(tmp_path)
    caller_key=tmp_path/'caller-key';caller_key.write_text(key.read_text());caller_key.chmod(0o600)
    config=ApiConfiguration(tmp_path/'absent-dsn',tmp_path/'absent-signer',tmp_path/'unused','absent')
    with running(runtime,key) as (host,_,port):
        control=HostControlConfiguration(f'https://127.0.0.1:{port}',caller_key,key.parent/'host-cert.pem')
        with TestClient(create_app(replace(config,host_control=control))) as client:
            response=client.get('/health/ready');assert response.status_code==503
            assert response.json()['host']=={'reachable':True,'authenticated':True,'owner_lock':True,'product_ready':False}
            assert not response.json()['ready'] and key.read_text() not in response.text
            new=secrets.token_hex(32);key.write_text(new)
            assert client.get('/health/ready').json()['host']=={'reachable':True,'authenticated':False,'owner_lock':False,'product_ready':False}
            caller_key.write_text(new)
            assert client.get('/health/ready').json()['host']['authenticated']
            caller_key.chmod(0o644)
            assert not client.get('/health/ready').json()['host']['authenticated']
            caller_key.chmod(0o600)
            host.terminate();host.wait(timeout=10)
            assert client.get('/health/ready').json()['host']=={'reachable':False,'authenticated':False,'owner_lock':False,'product_ready':False}
            assert client.get('/health/live').status_code==200


@pytest.mark.parametrize('origin',['http://127.0.0.1:8100','https://localhost:8100','http://127.0.0.1:80','http://127.0.0.1:8100/','http://127.0.0.1:8100?key=secret','http://user:password@127.0.0.1:8100','http://127.0.0.1:8100/internal','http://untrusted.invalid:8100'])
def test_unapproved_origins_refused_before_network(tmp_path,origin):
    with pytest.raises(ValueError):HostControlConfiguration(origin,tmp_path/'missing',tmp_path/'missing-ca')


def test_host_probe_never_forwards_key_to_redirect_or_ambient_proxy(tmp_path,monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from threading import Thread
    from contextlib import ExitStack
    import ssl
    _,source_key=files(tmp_path)
    certificate=source_key.parent/'host-cert.pem';server_key=source_key.parent/'host-key.pem'
    contacted=[]
    class ForbiddenDestination(BaseHTTPRequestHandler):
        def do_GET(self):
            contacted.append('forbidden')
            self.send_response(200);self.end_headers();self.wfile.write(b'{}')
        def log_message(self,*args):pass
    class RedirectHost(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302);self.send_header('Location',destination);self.end_headers()
        def log_message(self,*args):pass
    with ExitStack() as stack:
        def listener(handler):
            server=ThreadingHTTPServer(('127.0.0.1',0),handler)
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(str(certificate),str(server_key))
            server.socket=tls.wrap_socket(server.socket,server_side=True)
            thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            def stop():server.shutdown();server.server_close();thread.join(timeout=5)
            stack.callback(stop)
            return f'https://127.0.0.1:{server.server_port}'
        destination=listener(ForbiddenDestination)
        origin=listener(RedirectHost)
        monkeypatch.setenv('HTTPS_PROXY',destination);monkeypatch.setenv('https_proxy',destination)
        monkeypatch.setenv('NO_PROXY','');monkeypatch.setenv('no_proxy','')
        key=tmp_path/'caller';key.write_text(secrets.token_hex(32));key.chmod(0o600)
        result=probe_host(HostControlConfiguration(origin,key,certificate))
        assert result=={'reachable':True,'authenticated':False,'owner_lock':False,'product_ready':False}
        assert contacted==[]


def test_actual_api_process_cli_connects_to_actual_node_host(tmp_path):
    import subprocess,sys,time
    import httpx
    from test_agent_host import free_port
    runtime,key=files(tmp_path)
    with running(runtime,key) as (host,_,host_port):
        port=free_port();origin=f'http://127.0.0.1:{port}'
        process=subprocess.Popen([sys.executable,'-m','nexloop_eios.http_api','--mode','test',
            '--database-url-file',str(tmp_path/'missing-dsn'),'--signing-key-file',str(tmp_path/'missing-signer'),
            '--artifact-root',str(tmp_path/'unused'),'--signing-key-id','missing','--port',str(port),
            '--host-origin',f'https://127.0.0.1:{host_port}','--host-control-key-file',str(key),'--host-ca-file',str(key.parent/'host-cert.pem')],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            with httpx.Client(base_url=origin,trust_env=False,timeout=3) as client:
                deadline=time.monotonic()+10
                while True:
                    assert process.poll() is None
                    try:
                        if client.get('/health/live').status_code==200:break
                    except httpx.TransportError:pass
                    assert time.monotonic()<deadline;time.sleep(.05)
                response=client.get('/health/ready');assert response.status_code==503
                assert response.json()['host']['authenticated'] and response.json()['host']['owner_lock']
                assert key.read_text() not in response.text and not response.json()['product_ready']
                host.terminate();host.wait(timeout=10)
                assert not client.get('/health/ready').json()['host']['reachable']
        finally:
            if process.poll() is None:process.terminate()
            out,err=process.communicate(timeout=10)
            assert key.read_text() not in out+err
        assert process.returncode in (0,-15)
        with pytest.raises(httpx.TransportError):httpx.get(origin+'/health/live',timeout=1,trust_env=False)


def test_actual_host_untrusted_ca_refused(tmp_path):
    runtime,key=files(tmp_path)
    alternate=tmp_path/'alternate';alternate.mkdir(mode=0o700);files(alternate)
    with running(runtime,key) as (_,_,port):
        config=HostControlConfiguration(f'https://127.0.0.1:{port}',key,alternate/'host-cert.pem')
        assert probe_host(config)=={'reachable':False,'authenticated':False,'owner_lock':False,'product_ready':False}


def test_actual_host_certificate_hostname_mismatch_refused(tmp_path):
    import socket,subprocess,time
    from test_agent_host import free_port,start
    runtime,key=files(tmp_path)
    certificate=key.parent/'host-cert.pem';tls_key=key.parent/'host-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(tls_key),'-out',str(certificate),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,capture_output=True)
    port=free_port();child=start(runtime,key,port)
    try:
        deadline=time.monotonic()+10
        while True:
            assert child.poll() is None
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=1):break
            except OSError:pass
            assert time.monotonic()<deadline;time.sleep(.05)
        config=HostControlConfiguration(f'https://127.0.0.1:{port}',key,certificate)
        assert probe_host(config)=={'reachable':False,'authenticated':False,'owner_lock':False,'product_ready':False}
    finally:
        if child.poll() is None:child.terminate()
        out,err=child.communicate(timeout=10)
        assert key.read_text() not in out+err


def test_host_probe_does_not_honor_ambient_tls_key_logging(tmp_path,monkeypatch):
    runtime,key=files(tmp_path)
    forbidden=tmp_path/'forbidden-tls-key-log'
    with running(runtime,key) as (_,_,port):
        # Set after the observer client's context was created. Only the actual
        # API probe constructs its TLS context under this ambient setting.
        monkeypatch.setenv('SSLKEYLOGFILE',str(forbidden))
        result=probe_host(HostControlConfiguration(f'https://127.0.0.1:{port}',key,key.parent/'host-cert.pem'))
        assert result['authenticated'] and result['owner_lock']
        assert not forbidden.exists(),'Host TLS session secrets were written through ambient configuration'
