"""NX-031 (dispatcher 2026-10-11): runtime evidence that the login rate limit holds in an actual API process. The load lane never
relaxes it; with the assembled trusted configuration's value (10 attempts per 60 s for operator nexloop_identity and one client
digest) the 11th login from the same client within the window is refused with 429 and a Retry-After, before any password check."""
import os,secrets,socket,ssl,subprocess,sys,time
import httpx
from test_browser_identity_reads import identity


def test_eleventh_login_in_the_window_is_refused_with_retry_after(identity,admin,tmp_path):
    dsn=tmp_path/'identity-dsn';dsn.write_text(identity[0]);dsn.chmod(0o600)
    rate=tmp_path/'rate-key';rate.write_text(secrets.token_hex(32));rate.chmod(0o600)
    cert=tmp_path/'cert.pem';key=tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,capture_output=True)
    cert.chmod(0o600);key.chmod(0o600)
    admin.execute("insert into control.nexloop_browser_rate_policies values('synthetic-a','local_login','nexloop_identity',10,60,true)")
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin=f'https://localhost:{port}'
    cmd=[sys.executable,'-m','nexloop_eios.http_api','--database-url-file',str(tmp_path/'missing'),
         '--signing-key-file',str(tmp_path/'missing-key'),'--artifact-root',str(tmp_path/'unused'),'--signing-key-id','missing',
         '--mode','test','--port',str(port),'--identity-database-url-file',str(dsn),'--browser-rate-key-file',str(rate),
         '--browser-tenant-id','synthetic-a','--browser-application-id','synthetic-browser-app','--browser-origin',origin,
         '--tls-certificate-file',str(cert),'--tls-key-file',str(key)]
    child=subprocess.Popen(cmd,env={k:v for k,v in os.environ.items() if not k.startswith('MODEL_')},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    context=ssl.create_default_context(cafile=str(cert))
    try:
        with httpx.Client(base_url=origin,verify=context,trust_env=False,timeout=5) as client:
            deadline=time.monotonic()+15
            while True:
                assert child.poll() is None
                try:
                    if client.get('/health/live').status_code==200:break
                except httpx.TransportError:pass
                assert time.monotonic()<deadline;time.sleep(.05)
            body={'username':identity[3].username,'password':identity[-1]}
            statuses=[]
            for _ in range(10):
                client.cookies.clear()
                statuses.append(client.post('/api/v1/auth/login',headers={'Origin':origin},json=body).status_code)
            assert statuses==[200]*10
            client.cookies.clear()
            refused=client.post('/api/v1/auth/login',headers={'Origin':origin},json=body)
            assert refused.status_code==429 and refused.json()=={'code':'rate_limited'}
            assert 1<=int(refused.headers['Retry-After'])<=60
            # A wrong password in the same window is refused by the limit as well (the limit is consumed before the password check).
            assert client.post('/api/v1/auth/login',headers={'Origin':origin},json={**body,'password':'wrong-'+secrets.token_hex(4)}).status_code==429
    finally:
        if child.poll() is None:child.terminate()
        stdout,stderr=child.communicate(timeout=15)
    assert identity[-1] not in stdout+stderr
