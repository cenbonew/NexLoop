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


def test_staff_reply_reaches_the_customer_and_is_read_and_extracted_as_the_enterprise(assembled_message,admin,tmp_path):
    """Ruling B on the actual consumer path: the customer reads the staff reply over HTTPS; the governed Message READ derivation
    accepts it for the Source; extraction sees it as the enterprise side and its commitment is a speaker=agent Claim."""
    import hashlib,httpx
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios.backend import open_backend
    from nexloop_eios.claim_store import ConversationClaimExtractor
    from nexloop_eios.conversation_extraction import DeterministicExtractionProvider,build_user_payload
    from nexloop_eios.message_read import derived_message_read_envelope,message_read_basis
    from test_claim_store_pg import source_targets
    f=assembled_message;o=f['original'];p=o['paths'];tenant=o['tenant'];tokens=f['tokens']
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
    with process('nexloop_eios.http_api',api_arguments,hidden) as api_child, \
         open_backend(database_url=make_conninfo(o['pg'],user='nexloop_api'),artifact_root=tmp_path/'staff-backend',
                      signing_key_file=p['backend_signing'],signing_key_id='explicit-configuration') as backend:
        with httpx.Client(base_url=origin,verify=str(api_tls/'host-cert.pem'),trust_env=False,timeout=5) as client:
            wait_live(api_child,client)
            login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()})
            headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token'],'Idempotency-Key':'nx028-staff-conversation-key'}
            conversation=client.post('/api/v1/conversations',headers=headers,json={}).json()['id']
            started=admin.execute('select control.nexloop_governed_takeover(%s,%s,%s,%s,%s,%s)',(tenant,'real','synthetic-staff-principal','human','takeover-staff-e2e',
                Jsonb({'operation':'take_over_conversation','scope_kind':'conversation','scope_ref':conversation,'reason':'人工接手'}))).fetchone()[0]
            headers['Idempotency-Key']='nx028-staff-message-key-1'
            inbound=client.post(f'/api/v1/conversations/{conversation}/messages',headers=headers,json={'body':'付款页面一直报错'}).json()['message']['id']
            text='我是人工客服，我会在明天下午前给你处理进展。'
            # The handler as the governed entry runs it (tenant context set by the wrapper; the wrapper itself is covered in test_staff_reply_pg).
            with admin.transaction():
                admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
                created=admin.execute('select runtime.nexloop_governed_staff_reply(%s,%s,%s,%s,%s,%s)',(tenant,'real','synthetic-staff-principal','human','staff-e2e-1',
                    Jsonb({'operation':'send_staff_reply','conversation_id':conversation,'reply_to':inbound,'text':text}))).fetchone()[0]['message']
            # The customer reads it in the conversation (contract: sender_kind human_takeover, no intent).
            items=client.get(f'/api/v1/conversations/{conversation}/messages').json()['items']
            staff=next(m for m in items if m['id']==created['id'])
            assert staff['direction']=='outbound' and staff['sender_kind']=='human_takeover' and 'intent_id' not in staff and staff['reply_to_message_id']==inbound
            assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='reply-due' and item_key=%s",('reply:'+inbound,)).fetchone()==(0,)
        # Governed READ derivation for the Source covers the staff Message (object, actor, body).
        source=backend.authenticate(tokens['assembly-source'],world='real')
        sp,ss,sg=source._backend._pool,source._session,source._backend._signer
        basis=message_read_basis(sp,ss,created['id']);assert basis['mode']=='derived'
        envelope=derived_message_read_envelope(sp,ss,sg,created['id'],basis)
        with sp.connection() as db,db.transaction():
            read=db.execute('select authz.nexloop_read_object(%s,%s,%s,%s)',(ss.token_digest,'real',envelope['text'],envelope['signature'])).fetchone()[0]
        assert read['properties']=={'actor':'synthetic-staff-principal','body':text}
        # Extraction: the staff message is the enterprise side (speaker agent), its promise a commitment Claim.
        ids=[r[0] for r in admin.execute('select message_id from runtime.nexloop_conversation_messages where tenant_id=%s and conversation_id=%s order by sequence',(tenant,conversation)).fetchall()]
        session,_=seed_multi_authority(admin,sp,source_targets(conversation,ids),identity_suffix='-nx028-staff-extractor',tenant=tenant)
        extractor=ConversationClaimExtractor(sp,session,sg,None,timezone='Asia/Shanghai')
        ctx,window=extractor.load_window(conversation,ids)
        assert sorted(m.speaker for m in window)==['agent','consumer']
        staff_ref=next(m.sequence for m in window if m.speaker=='agent')
        response={'topics':[{'topic':'付款报错','conversation_summary':'人工承诺明天下午前反馈','user_valid_reply':True,'message_refs':[m.sequence for m in window]}],
            'claims':[{'topic_index':0,'message_ref':staff_ref,'quote':'我会在明天下午前给你处理进展','kind':'commitment','subject':{'kind':'enterprise','text':''},
                       'predicate':'处理进展反馈','value':{'type':'string','value':'给出处理进展'}}]}
        extractor.provider=DeterministicExtractionProvider({hashlib.sha256(build_user_payload(window,ctx).encode()).hexdigest():response})
        extractor.extract(conversation_id=conversation,message_ids=ids)
        assert admin.execute("select speaker,epistemic_kind,source_message_id from ontology.nexloop_claims where tenant_id=%s and conversation_id=%s",(tenant,conversation)).fetchall()==[('agent','commitment',created['id'])]
