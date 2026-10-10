"""NX-028 AT-044 on the actual consumer path: while a person has taken the conversation over, the relay issues no message Run;
after hand-back the latest unanswered message is routed normally (D5) and earlier ones are not replayed.

Real HTTPS consumer Messages through the production app, the actual message relay CLI (`--once`), clean catalog
PostgreSQL. The takeover start / end are the 0152 handler and end functions called by admin (the human governed Action
chain in front of them is covered by test_takeover_pg and test_workbench_actions_http). No Host or Pi is needed here.
"""
import json,secrets

from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: F401
from test_trusted_configuration_pg import private
from test_agent_host import files,free_port
from test_local_message_delivery_assembly import process,once,wait_live


def issued(admin,tenant):
    return [r[0] for r in admin.execute('select message_id from authz.nexloop_message_run_issuances where tenant_id=%s order by issued_at',(tenant,)).fetchall()]


def test_relay_waits_during_takeover_and_routes_only_the_latest_after_handback(assembled_message,admin,tmp_path):
    import httpx
    f=assembled_message;o=f['original'];p=o['paths'];tenant=o['tenant'];tokens=f['tokens'];credentials=f['credential_files']
    common=['--signing-key-file',str(p['backend_signing']),'--signing-key-id','explicit-configuration']
    api_dsn=private(tmp_path,'assembly-api-dsn',make_conninfo(o['pg'],user='nexloop_api'))
    api_tls=tmp_path/'api-tls';api_tls.mkdir(mode=0o700);files(api_tls)
    api_port=free_port();origin='https://127.0.0.1:'+str(api_port)
    rate=private(tmp_path,'assembly-rate-key',secrets.token_hex(32))
    api_arguments=['--database-url-file',str(api_dsn),*common,'--artifact-root',str(tmp_path/'api-artifacts'),'--mode','test','--port',str(api_port),
        '--tls-certificate-file',str(api_tls/'host-cert.pem'),'--tls-key-file',str(api_tls/'host-key.pem'),
        '--identity-database-url-file',str(p['identity']),'--browser-rate-key-file',str(rate),'--browser-tenant-id',tenant,
        '--browser-application-id',o['application'],'--browser-origin',origin]
    hidden=list(tokens.values())+[p['password'].read_text()]
    recipe=private(tmp_path,'assembly-relay-recipe',json.dumps(f['recipe']));vault=tmp_path/'relay-vault';vault.mkdir(mode=0o700)
    relay=['--database-url-file',str(api_dsn),*common,'--artifact-root',str(tmp_path/'relay-artifacts'),'--vault-root',str(vault),'--recipe-file',str(recipe)]
    relay+=sum((['--'+name+'-credential-file',str(credentials['assembly-'+label])] for name,label in [('route','route'),('source','source'),('planner','planner'),('executor','executor')]),[])
    with process('nexloop_eios.http_api',api_arguments,hidden) as api_child:
        with httpx.Client(base_url=origin,verify=str(api_tls/'host-cert.pem'),trust_env=False,timeout=5) as client:
            wait_live(api_child,client)
            login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()})
            headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token'],'Idempotency-Key':'nx028-takeover-conversation-key'}
            conversation=client.post('/api/v1/conversations',headers=headers,json={}).json()['id']
            assert client.get(f'/api/v1/conversations/{conversation}/handling').json()=={'conversation_id':conversation,'handled_by':'agent'}
            started=admin.execute('select control.nexloop_governed_takeover(%s,%s,%s,%s,%s,%s)',(tenant,'real','synthetic-staff-principal','human','takeover-e2e',
                Jsonb({'operation':'take_over_conversation','scope_kind':'conversation','scope_ref':conversation,'reason':'人工接手'}))).fetchone()[0]
            assert client.get(f'/api/v1/conversations/{conversation}/handling').json()['handled_by']=='human'  # D4 status bar
            ids=[]
            for n,body in enumerate(['我的订单怎么还没到？','还在吗？']):
                headers['Idempotency-Key']=f'nx028-takeover-message-key-{n}'
                ids.append(client.post(f'/api/v1/conversations/{conversation}/messages',headers=headers,json={'body':body}).json()['message']['id'])
            # During the takeover the relay issues no message Run (the item stays and is retried later).
            import subprocess,sys
            refused=subprocess.run([sys.executable,'-m','nexloop_eios.message_relay_cli',*relay,'--once'],capture_output=True,text=True,timeout=30)
            assert refused.returncode==1 and refused.stderr=='Message relay unavailable\n'  # fixed line, nothing private
            assert all(value not in refused.stdout+refused.stderr for value in hidden) and issued(admin,tenant)==[]
            ended=admin.execute('select control.nexloop_takeover_end(%s,%s,%s,%s,%s,%s)',(tenant,'real',started['takeover_id'],'handback','synthetic-staff-principal','handback-e2e')).fetchone()[0]
            assert ended['resumed_message_id']==ids[1] and ended['settled']==1
            assert client.get(f'/api/v1/conversations/{conversation}/handling').json()['handled_by']=='agent'
            # After hand-back: the latest message is routed to a Run; the earlier one is never replayed.
            admin.execute('update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp() where tenant_id=%s',(tenant,))  # the refused lease ends now
            assert once('nexloop_eios.message_relay_cli',relay,hidden)=='Message relay ready\n{"status":"queued"}\n'
            assert issued(admin,tenant)==[ids[1]]
            once('nexloop_eios.message_relay_cli',relay,hidden)
            assert issued(admin,tenant)==[ids[1]]
            assert admin.execute("select status from runtime.nexloop_message_outbox where message_id=%s",(ids[0],)).fetchone()==('delivered',)
