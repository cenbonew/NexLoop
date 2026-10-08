"""Actual isolated PG + Human auth + HTTPS production WebChat preview.

No business response/auth callback; optional ego-browser preview holds only this
owned disposable fixture. Synthetic credentials stay in ignored mode600 files.
"""
import json,os,secrets,socket,ssl,subprocess,sys,time
from pathlib import Path
import httpx
from psycopg.conninfo import make_conninfo
from test_conversation_effect_receipts import receipt_plan,accepted
from message_runtime_fixture import message_plan
from nexloop_eios.browser_http import COOKIE


def test_web_chat_actual_https_static_and_governed_receipt(receipt_plan,admin,tmp_path):
    f=receipt_plan;intent=accepted(f)
    def private(name,value):
        path=tmp_path/name;path.write_text(value);path.chmod(0o600);return path
    dsn=private('api-dsn',make_conninfo(f['pg'],user='nexloop_api'))
    identity_dsn=private('identity-dsn',make_conninfo(f['pg'],user='nexloop_identity'))
    rate=private('rate-key',secrets.token_hex(32))
    cert=tmp_path/'cert.pem';key=tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,capture_output=True)
    cert.chmod(0o600);key.chmod(0o600)
    admin.execute('insert into control.nexloop_browser_rate_policies values(%s,%s,%s,20,60,true)',(f['tenant'],'local_login','nexloop_identity'))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin=f'https://localhost:{port}'
    configuration=private('api-configuration',json.dumps({'dsn':str(dsn),'key':str(f['signing_key']),'identity':str(identity_dsn),'rate':str(rate),'tenant':f['tenant'],'origin':origin,'artifact':str(tmp_path/'artifacts'),'cert':str(cert),'tlskey':str(key),'port':port,'web_root':str(Path('apps/web/dist').resolve())}))
    code="""import json,sys,uvicorn
from pathlib import Path
from nexloop_eios.http_api import ApiConfiguration,create_app
from nexloop_eios.browser_http import BrowserConfiguration
c=json.loads(Path(sys.argv[1]).read_text())
config=ApiConfiguration(Path(c['dsn']),Path(c['key']),Path(c['artifact']),'runtime-effect',BrowserConfiguration(Path(c['identity']),Path(c['rate']),c['tenant'],'synthetic-webchat',c['origin']),web_root=Path(c['web_root']),execution_profile='deterministic-test',conversation_stream_seconds=1)
uvicorn.run(create_app(config),host='127.0.0.1',port=c['port'],ssl_certfile=c['cert'],ssl_keyfile=c['tlskey'],log_level='warning',access_log=False)
"""
    env={k:v for k,v in os.environ.items() if not k.startswith('MODEL_')}
    child=subprocess.Popen([sys.executable,'-c',code,str(configuration)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    context=ssl.create_default_context(cafile=str(cert));token=f['issued'].session_token.get_secret_value()
    preview=Path('.ci-results/web-chat-preview-private.json');stop=Path('.ci-results/web-chat-preview-stop')
    command=Path('.ci-results/web-chat-preview-command');reply=Path('.ci-results/web-chat-preview-reply.json')
    try:
        with httpx.Client(base_url=origin,verify=context,trust_env=False,timeout=3) as client:
            deadline=time.monotonic()+15
            while True:
                assert child.poll() is None
                try:
                    if client.get('/health/live').status_code==200:break
                except httpx.TransportError:pass
                assert time.monotonic()<deadline;time.sleep(.05)
            assert client.get('/').status_code==200
            client.cookies.set(COOKIE,token)
            result=client.get('/api/v1/messages/'+f['message']['id']+'/receipt')
            assert result.status_code==200 and result.json()['receipt']['intent_id']==intent['intent_id']
            assert result.json()['receipt']['business_action_success'] is False
            if os.environ.get('NEXLOOP_WEB_CHAT_PREVIEW')=='1':
                assert not any(p.exists() for p in (preview,stop,command,reply))
                identity=f.get('identity')
                info={'url':origin,'session_token':token,'certificate_path':str(cert),'message_id':f['message']['id'],'conversation_id':f['conversation']['id']}
                if identity:info.update(username=identity[3].username,password=identity[-1])
                preview.write_text(json.dumps(info));preview.chmod(0o600)
                deadline=time.monotonic()+600
                while not stop.exists():
                    assert child.poll() is None and time.monotonic()<deadline
                    if command.exists():
                        verb=command.read_text();command.unlink()
                        if verb=='revoke_function':
                            from authority_fixture import replace_fact
                            from eios.authz import facts as F
                            from nexloop_eios.conversation_effect_receipts import FUNCTION
                            replace_fact(admin,f['tenant'],'grants',[f['issued'].session.principal_id,'eios:function:'+FUNCTION+':1'],F.GrantFacts,grants=[])
                            reply.write_text(json.dumps({'completed':'revoke_function'}));reply.chmod(0o600)
                        else:raise AssertionError('preview control rejected')
                    time.sleep(.1)
    finally:
        if child.poll() is None:child.terminate()
        stdout,stderr=child.communicate(timeout=15)
        for path in (preview,stop,command,reply):
            if path.exists():path.unlink()
    assert child.returncode in (0,-15)
    assert token not in stdout+stderr
    if f.get('identity'):assert f['identity'][-1] not in stdout+stderr
    with httpx.Client(verify=context,trust_env=False,timeout=1) as client:
        try:client.get(origin+'/health/live')
        except httpx.TransportError:pass
        else:raise AssertionError('owned HTTPS listener still running')
