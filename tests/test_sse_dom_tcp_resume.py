"""AT-015 browser side, end to end: the mounted WebChat (jsdom, undici EventSource) against the actual API process over TLS.

The Node test cuts the TCP connection under the live stream, commits messages while it is down, lets the EventSource resume with
Last-Event-ID, and checks the DOM shows every committed message exactly once and in order (apps/web/test/e2e/sse-tcp-resume.e2e.ts).
Run serially after `nvm use 24`."""
import json,os,secrets,shutil,ssl,subprocess
import httpx
from pathlib import Path
from psycopg.conninfo import make_conninfo
from local_message_assembly_fixture import assembled_message,business_plan,configured
from test_precise_pg_admission_outage import process,wait_live
from test_trusted_configuration_pg import private
from test_agent_host import files,free_port

ROOT=Path(__file__).resolve().parents[1]


def test_mounted_dom_shows_each_committed_message_once_across_a_tcp_cut_and_last_event_id_resume(assembled_message,admin,tmp_path):
    f=assembled_message;o=f['original'];p=o['paths'];port=free_port();origin=f'https://127.0.0.1:{port}'
    dsn=private(tmp_path,'api-dsn',make_conninfo(o['pg'],user='nexloop_api'));rate=private(tmp_path,'rate-key',secrets.token_hex(32))
    tls=tmp_path/'tls';tls.mkdir(mode=0o700);files(tls);cert=tls/'host-cert.pem'
    args=['--database-url-file',str(dsn),'--signing-key-file',str(p['backend_signing']),'--signing-key-id','explicit-configuration','--artifact-root',str(tmp_path/'artifacts'),'--mode','test','--execution-profile','deterministic-test','--port',str(port),'--tls-certificate-file',str(cert),'--tls-key-file',str(tls/'host-key.pem'),'--identity-database-url-file',str(p['identity']),'--browser-rate-key-file',str(rate),'--browser-tenant-id',o['tenant'],'--browser-application-id',o['application'],'--browser-origin',origin]
    pnpm=shutil.which('pnpm');assert pnpm,'pnpm (Node 24) is required for the mounted DOM test'
    with process('nexloop_eios.http_api',args,tuple(f['tokens'].values())+(p['password'].read_text(),)) as child:
        with httpx.Client(base_url=origin,verify=ssl.create_default_context(cafile=str(cert)),trust_env=False,timeout=5) as client:
            wait_live(child,client)
            login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()});assert login.status_code==200
            csrf=login.json()['csrf_token'];headers={'Origin':origin,'X-CSRF-Token':csrf,'Idempotency-Key':'dom-tcp-conversation-unique-key'}
            created=client.post('/api/v1/conversations',headers=headers,json={});assert created.status_code==200
            cid=created.json()['id'];base='/api/v1/conversations/'+cid
            for index in (1,2):
                r=client.post(base+'/messages',headers={**headers,'Idempotency-Key':f'dom-tcp-resume-stable-{index}'},json={'body':f'committed TCP replay {index}'});assert r.status_code==202
            cookie='; '.join(f'{k}={v}' for k,v in client.cookies.items())
            session=private(tmp_path,'dom-session',json.dumps({'origin':origin,'port':port,'ca_file':str(cert),'cookie':cookie,'csrf':csrf,'origin_header':origin,'conversation_id':cid}))
            result=tmp_path/'dom-result.json'
            env={k:v for k,v in os.environ.items() if not k.startswith(('MODEL_','DATABASE_'))}
            env.update(NEXLOOP_SSE_E2E_SESSION_FILE=str(session),NEXLOOP_SSE_E2E_RESULT_FILE=str(result))
            run=subprocess.run([pnpm,'exec','vitest','run','--config','apps/web/test/e2e/vitest.e2e.config.mjs'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=180)
            assert run.returncode==0,run.stdout[-4000:]+run.stderr[-2000:]
            assert csrf not in run.stdout+run.stderr and cookie not in run.stdout+run.stderr
            observed=json.loads(result.read_text())
            assert observed['shown']==[f'committed TCP replay {i}' for i in range(1,6)]
            assert observed['events_per_connection']==[['3'],['4','5']] and observed['relay_connections']>=2
            assert observed['stream_unavailable_shown'] is False
            read=client.get(base+'/messages');assert read.status_code==200 and [x['body'] for x in read.json()['items']]==observed['shown']
            assert admin.execute('select count(*) from runtime.nexloop_message_outbox').fetchone()==(5,)
            output=os.environ.get('NEXLOOP_DOM_SSE_EVIDENCE_FILE')
            if output:
                assert Path(output).is_absolute();Path(output).write_text(json.dumps({'conversation_id':cid,**observed},indent=2)+'\n')
