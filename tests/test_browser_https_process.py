import os,secrets,socket,ssl,subprocess,sys,time
import httpx
from test_browser_identity_reads import identity
from nexloop_eios.browser_http import COOKIE


def test_actual_https_process_login_reload_csrf_and_shutdown(identity,admin,tmp_path):
    dsn=tmp_path/'identity-dsn';dsn.write_text(identity[0]);dsn.chmod(0o600)
    rate=tmp_path/'rate-key';rate.write_text(secrets.token_hex(32));rate.chmod(0o600)
    cert=tmp_path/'cert.pem';key=tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,capture_output=True)
    cert.chmod(0o600);key.chmod(0o600)
    admin.execute("insert into control.nexloop_browser_rate_policies values('synthetic-a','local_login','nexloop_identity',3,60,true)")
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin=f'https://localhost:{port}'
    cmd=[sys.executable,'-m','nexloop_eios.http_api','--database-url-file',str(tmp_path/'missing'),
         '--signing-key-file',str(tmp_path/'missing-key'),'--artifact-root',str(tmp_path/'unused'),'--signing-key-id','missing',
         '--mode','test','--port',str(port),'--identity-database-url-file',str(dsn),'--browser-rate-key-file',str(rate),
         '--browser-tenant-id','synthetic-a','--browser-application-id','synthetic-browser-app','--browser-origin',origin,
         '--tls-certificate-file',str(cert),'--tls-key-file',str(key)]
    env={k:v for k,v in os.environ.items() if not k.startswith('MODEL_')}
    child=subprocess.Popen(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    context=ssl.create_default_context(cafile=str(cert));tokens=[]
    try:
        with httpx.Client(base_url=origin,verify=context,trust_env=False,timeout=3) as client:
            deadline=time.monotonic()+15
            while True:
                assert child.poll() is None
                try:
                    if client.get('/health/live').status_code==200:break
                except httpx.TransportError:pass
                assert time.monotonic()<deadline;time.sleep(.05)
            response=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':identity[3].username,'password':identity[-1]})
            assert response.status_code==200
            tokens=[client.cookies.get(COOKIE),response.json()['csrf_token']]
            assert client.get('/api/v1/auth/session').status_code==200
            refreshed=client.post('/api/v1/auth/csrf',headers={'Origin':origin});assert refreshed.status_code==200
            tokens.append(refreshed.json()['csrf_token'])
            assert client.post('/api/v1/auth/logout',headers={'Origin':origin,'X-CSRF-Token':tokens[1]}).status_code==401
            assert client.post('/api/v1/auth/logout',headers={'Origin':origin,'X-CSRF-Token':tokens[-1]}).status_code==200
            assert client.get('/api/v1/auth/session').status_code==401
            assert client.get('/health/ready').status_code==503
    finally:
        if child.poll() is None:child.terminate()
        stdout,stderr=child.communicate(timeout=15)
    assert child.returncode in (0,-15)
    assert identity[-1] not in stdout+stderr and all(token not in stdout+stderr for token in tokens)
    assert admin.execute("select count(*) from pg_stat_activity where usename='nexloop_identity'").fetchone()[0]==0
    with httpx.Client(verify=context,trust_env=False,timeout=1) as client:
        try:client.get(origin+'/health/live')
        except httpx.TransportError:pass
        else:raise AssertionError('owned API listener still active')
