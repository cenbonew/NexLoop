"""NX-047 Agent outbound Messages over the actual chain (synthetic data only).

Human HTTPS Message → relay → Pi Run (deterministic, echoes the input as its reply)
→ governed service.request intent (outbound record, same transaction) → real local
JSON delivery (ledger advances delivery) → governed Message.agent_create recorder →
same conversation stream → Claim extraction (enterprise commitment) and the governed
Message READ derivation. Admin only asserts, probes and injects explicit faults.
"""
from concurrent.futures import ThreadPoolExecutor
import copy,hashlib,json,secrets,time,uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.backend import open_backend
from nexloop_eios.conversation_messages import ConversationDenied,ConversationUnavailable,MESSAGE
from nexloop_eios.outbound_messages import ACTION,OutboundMessageRecorder
from nexloop_eios.trusted_configuration import apply_manifest
from local_message_assembly_fixture import assembled_message,business_plan,configured
from test_trusted_configuration_pg import private
from test_agent_host import files,free_port
from test_runtime_effect_tools import effect_configuration,tool_evidence
from test_message_runtime_effect_e2e import trusted_host_evidence
from test_local_message_delivery_assembly import process,once,wait_live,reaped_host

REPLY='好的，我会在明天下午前给你处理进展。'
TRANSITIONS={('persisted','dispatching'),('persisted','provider_accepted'),('persisted','delivered'),('persisted','unknown'),('persisted','failed'),
    ('dispatching','provider_accepted'),('dispatching','delivered'),('dispatching','unknown'),('dispatching','failed'),
    ('provider_accepted','delivered'),('unknown','provider_accepted'),('unknown','delivered'),('unknown','failed')}
STATES=('persisted','dispatching','provider_accepted','delivered','unknown','failed')


def publish_recorder(f,admin):
    """Trusted configuration from the versioned deploy manifests only (ADR-020 §3):
    Message.agent_create:1 from deploy/configuration/business-actions.v1.json and the
    recorder principal from deploy/authorization/service-grants.v1.json."""
    from datetime import UTC,datetime,timedelta
    from pathlib import Path
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    from nexloop_eios import business_actions,service_grants
    root=Path(__file__).resolve().parents[1]
    o=f['original'];tenant=o['tenant'];manifest=copy.deepcopy(f['manifest'])
    base=next(a for a in manifest['actions'] if a['definition']['stable_name']==MESSAGE)
    rows=business_actions.compile_actions(business_actions.load(root/'deploy/configuration/business-actions.v1.json'),tenant=tenant,
        created_by='explicit-assembly-owner',created_at=datetime.now(UTC),object_types=manifest['object_types'],
        capability=CapabilityContractSnapshot.model_validate_json(json.dumps(base['capability'])))
    assert [r['definition']['stable_name'] for r in rows]==[ACTION]
    manifest['actions']+=rows
    grants=service_grants.load(root/'deploy/authorization/service-grants.v1.json')
    principal=next(p for p in grants['principals'] if p['role']=='outbound_message_recorder')
    end=datetime.now(UTC)+timedelta(minutes=30)
    binding,compiled=service_grants.compile_principal(grants,tenant,principal,credential_expires_at=end)
    facts={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']}
    for kind,key,fact in compiled:
        payload=fact.model_dump(mode='json');payload.pop('snapshot_digest',None)
        facts[(kind,tuple(key))]={'kind':kind,'key':key,'payload':payload}
    token=secrets.token_urlsafe(48);secret_map=json.loads(o['paths']['secrets'].read_text());secret_map[principal['credential_reference']]=token
    o['paths']['secrets'].write_text(json.dumps(secret_map))
    manifest.update(manifest_id=str(uuid.uuid4()),authority_facts=list(facts.values()),
        service_credentials=manifest['service_credentials']+[{'reference':principal['credential_reference'],'binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':end.isoformat(),'status':'active'}],
        expected_revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0])
    apply_manifest(manifest,database_url_file=o['paths']['dsn'],signing_key_file=o['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=o['paths']['secrets'])
    f['manifest']=manifest
    return token


def outbound(admin,tenant):
    return admin.execute('select intent_id::text,delivery_state,message_id,sequence,sender_kind,trigger_message_id,body_digest from runtime.nexloop_outbound_messages where tenant_id=%s',(tenant,)).fetchall()


def events(admin,tenant):
    return admin.execute('select from_state,to_state,source from runtime.nexloop_outbound_delivery_events where tenant_id=%s order by event_number',(tenant,)).fetchall()


def test_transition_table_is_forward_only(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    for a in STATES:
        for b in STATES:
            got=admin.execute('select authz.nexloop_outbound_transition_allowed(%s,%s)',(a,b)).fetchone()[0]
            assert got==(a==b or (a,b) in TRANSITIONS),(a,b)
    # Terminal states never move.
    for terminal in ('delivered','failed'):
        assert not any(admin.execute('select authz.nexloop_outbound_transition_allowed(%s,%s)',(terminal,b)).fetchone()[0] for b in STATES if b!=terminal)


def test_rejected_intent_admission_leaves_no_outbound(admin,pg):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    with psycopg.connect(make_conninfo(pg,user='nexloop_api')) as api:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute('select authz.nexloop_effect_intent_command(%s,%s,%s,%s,%s)',('0'*64,'real','{"protocol":"nexloop-effect-intent-v1"}','0'*64,'{"verb":"submit","action_version":1}'))
        api.rollback()
        # Only the definers may touch the outbound tables or advance delivery state.
        for statement,args in (('select count(*) from runtime.nexloop_outbound_messages',()),
                               ("update runtime.nexloop_outbound_messages set delivery_state='delivered'",()),
                               ('select authz.nexloop_outbound_advance(%s,%s,%s,%s,%s)',('t','real',str(uuid.uuid4()),'delivered','attempt'))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):api.execute(statement,args)
            api.rollback()
    assert admin.execute('select count(*) from runtime.nexloop_outbound_messages').fetchone()==(0,)


def test_agent_reply_persisted_delivered_materialized_extracted_and_read(assembled_message,admin,tmp_path,monkeypatch):
    f=assembled_message;o=f['original'];p=o['paths'];tenant=o['tenant'];tokens=f['tokens'];credentials=f['credential_files']
    trusted_host_evidence()
    recorder_token=publish_recorder(f,admin)
    common=['--signing-key-file',str(p['backend_signing']),'--signing-key-id','explicit-configuration']
    api_dsn=private(tmp_path,'assembly-api-dsn',make_conninfo(o['pg'],user='nexloop_api'))
    domain_dsn=private(tmp_path,'assembly-domain-dsn',make_conninfo(o['pg'],user='nexloop_domain_worker'))
    effect_dsn=private(tmp_path,'assembly-effect-dsn',make_conninfo(o['pg'],user='nexloop_action_worker'))
    api_tls=tmp_path/'api-tls';api_tls.mkdir(mode=0o700);files(api_tls)
    api_port=free_port();origin='https://127.0.0.1:'+str(api_port)
    rate=private(tmp_path,'assembly-rate-key',secrets.token_hex(32))
    api_arguments=['--database-url-file',str(api_dsn),*common,'--artifact-root',str(tmp_path/'api-artifacts'),'--mode','test','--port',str(api_port),
        '--tls-certificate-file',str(api_tls/'host-cert.pem'),'--tls-key-file',str(api_tls/'host-key.pem'),
        '--identity-database-url-file',str(p['identity']),'--browser-rate-key-file',str(rate),'--browser-tenant-id',tenant,
        '--browser-application-id',o['application'],'--browser-origin',origin]
    hidden=list(tokens.values())+[recorder_token,o['paths']['password'].read_text()]
    import httpx
    with process('nexloop_eios.http_api',api_arguments,hidden) as api_child, \
         open_backend(database_url=make_conninfo(o['pg'],user='nexloop_api'),artifact_root=tmp_path/'outbound-backend',
                      signing_key_file=p['backend_signing'],signing_key_id='explicit-configuration') as backend:
        with httpx.Client(base_url=origin,verify=str(api_tls/'host-cert.pem'),trust_env=False,timeout=5) as client:
            wait_live(api_child,client)
            login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':p['password'].read_text()})
            assert login.status_code==200
            headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token'],'Idempotency-Key':'nx047-outbound-conversation-key'}
            conversation_id=client.post('/api/v1/conversations',headers=headers,json={}).json()['id']
            headers['Idempotency-Key']='nx047-outbound-message-key-1'
            consumer=client.post('/api/v1/conversations/'+conversation_id+'/messages',headers=headers,json={'body':REPLY}).json()['message']
            recipe=private(tmp_path,'assembly-relay-recipe',json.dumps(f['recipe']));vault=tmp_path/'relay-vault';vault.mkdir(mode=0o700)
            relay=['--database-url-file',str(api_dsn),*common,'--artifact-root',str(tmp_path/'relay-artifacts'),'--vault-root',str(vault),'--recipe-file',str(recipe)]
            relay+=sum((['--'+name+'-credential-file',str(credentials['assembly-'+label])] for name,label in [('route','route'),('source','source'),('planner','planner'),('executor','executor')]),[])
            assert once('nexloop_eios.message_relay_cli',relay,hidden)=='Message relay ready\n{"status":"queued"}\n'
            run=client.get('/api/v1/messages/'+consumer['id']+'/receipt').json()['run']
            host_tls=tmp_path/'host-tls';host_tls.mkdir(mode=0o700);runtime,host_key=files(host_tls)
            guard_port=free_port();guard_key=private(tmp_path,'assembly-guard-key',secrets.token_hex(32))
            guard_tls=tmp_path/'guard-tls';guard_tls.mkdir(mode=0o700);files(guard_tls)
            host_config=effect_configuration(guard_tls,guard_port,guard_key)
            cfg=json.loads(host_config.read_text());cfg.pop('deterministic_effect_message');cfg.update(deterministic_message_from_input=True,context_input_protocol='nexloop.context-pack.v2');host_config.write_text(json.dumps(cfg))
            with reaped_host(runtime,host_key,host_config) as (_,host_client,_):
                worker=['--database-url-file',str(domain_dsn),*common,'--service-credential-file',str(credentials['assembly-runtime-worker']),
                    '--artifact-root',str(tmp_path/'runtime-worker-artifacts'),'--world','real','--queue','operations',
                    '--host-origin',str(host_client.base_url).rstrip('/'),'--host-control-key-file',str(host_key),'--host-ca-file',str(host_tls/'host-cert.pem'),
                    '--guard-port',str(guard_port),'--guard-key-file',str(guard_key),
                    '--guard-certificate-file',str(guard_tls/'host-cert.pem'),'--guard-tls-key-file',str(guard_tls/'host-key.pem'),'--total-timeout','20']
                assert 'succeeded' in once('nexloop_eios.runtime_worker',worker,hidden)
                calls,receipts=tool_evidence(runtime/run['run_id']/'runtime.sqlite')
            intent=receipts[0]['intent_id']
            # (1)(2)(8) Same-transaction outbound record; three replayed tool calls → one intent → one record.
            assert len(receipts)==3 and all(r['intent_id']==intent for r in receipts) and calls
            rows=outbound(admin,tenant)
            assert rows==[(intent,'persisted',None,None,'agent',consumer['id'],hashlib.sha256(REPLY.encode()).hexdigest())]
            assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s',(tenant,)).fetchone()==(1,)
            # Persisted is not delivered: no stream entry, no relay inbox/outbox row, invisible to the consumer.
            assert admin.execute('select count(*) from runtime.nexloop_conversation_messages where tenant_id=%s',(tenant,)).fetchone()==(1,)
            assert admin.execute('select count(*) from runtime.nexloop_message_outbox where tenant_id=%s',(tenant,)).fetchone()==(1,)
            assert [m['id'] for m in client.get('/api/v1/conversations/'+conversation_id+'/messages').json()['items']]==[consumer['id']]
            services=backend.authenticate(recorder_token,world='real')
            recorder=OutboundMessageRecorder(services._backend._pool,services._session,services._backend._signer)
            assert recorder.pending()==[]
            with pytest.raises(ConversationUnavailable):recorder.record(intent_id=intent)
            # (10) Injected unknown: only provider evidence can move it on, never a re-dispatch.
            admin.execute("select authz.nexloop_outbound_advance(%s,'real',%s,'unknown','attempt')",(tenant,intent))
            delivery_tls=tmp_path/'delivery-tls';delivery_tls.mkdir(mode=0o700);_,provider_key=files(delivery_tls)
            delivery_origin='https://127.0.0.1:'+str(free_port());delivery_root=tmp_path/'json-products';delivery_root.mkdir(mode=0o700)
            provider_config=private(tmp_path,'assembly-provider-config',json.dumps({'origin':delivery_origin,'credential_file':str(provider_key),'ca_file':str(delivery_tls/'host-cert.pem'),'timeout':3}))
            delivery=['--database-url-file',str(effect_dsn),*common,'--service-credential-file',str(credentials['assembly-executor']),
                '--artifact-root',str(tmp_path/'delivery-artifacts'),'--delivery-root',str(delivery_root),'--origin',delivery_origin,
                '--certificate-file',str(delivery_tls/'host-cert.pem'),'--tls-key-file',str(delivery_tls/'host-key.pem'),
                '--provider-ca-file',str(delivery_tls/'host-cert.pem'),'--provider-credential-file',str(provider_key)]
            with process('nexloop_eios.local_json_delivery',delivery,hidden+[provider_key.read_text()]) as delivery_child:
                import socket,ssl
                from urllib.parse import urlsplit
                deadline=time.monotonic()+10;context=ssl.create_default_context(cafile=str(delivery_tls/'host-cert.pem'))
                while True:
                    assert delivery_child.poll() is None
                    try:
                        with socket.create_connection(('127.0.0.1',urlsplit(delivery_origin).port),timeout=.2) as raw:
                            with context.wrap_socket(raw,server_hostname='127.0.0.1'):break
                    except (OSError,ssl.SSLError):
                        assert time.monotonic()<deadline;time.sleep(.02)
                executor=['--database-url-file',str(effect_dsn),*common,'--service-credential-file',str(credentials['assembly-executor']),
                    '--artifact-root',str(tmp_path/'effect-worker-artifacts'),'--world','real','--provider-config-file',str(provider_config)]
                assert 'fulfilled' in once('nexloop_eios.effect_worker',executor,hidden+[provider_key.read_text()])
            # (3) Ledger-driven, monotonic: the injected unknown was left only by provider evidence.
            history=events(admin,tenant)
            assert history[0]==('persisted','unknown','attempt') and history[-1][1]=='delivered'
            assert ('unknown','dispatching') not in [(a,b) for a,b,_ in history]
            assert all((a,b) in TRANSITIONS for a,b,_ in history)
            assert outbound(admin,tenant)[0][1]=='delivered'
            # (9) A backwards rewrite is rejected even for the table owner.
            with admin.transaction():
                admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
                with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
                    admin.execute("update runtime.nexloop_outbound_messages set delivery_state='dispatching'")
                with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
                    admin.execute('delete from runtime.nexloop_outbound_messages')
            # (11) Recorder crash between prepare and commit writes nothing; the retry records once.
            original=OutboundMessageRecorder._create
            with monkeypatch.context() as patched:
                patched.setattr(OutboundMessageRecorder,'_create',lambda self,*a,**k:(_ for _ in ()).throw(RuntimeError('synthetic crash')))
                with pytest.raises(Exception):recorder.record(intent_id=intent)
            assert outbound(admin,tenant)[0][2] is None and admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Message'",(tenant,)).fetchone()==(1,)
            assert OutboundMessageRecorder._create is original
            # (4) Concurrent consumer acceptance and recorder materialization on the same conversation.
            headers['Idempotency-Key']='nx047-outbound-message-key-2'
            with ThreadPoolExecutor(2) as pool:
                inbound=pool.submit(lambda:client.post('/api/v1/conversations/'+conversation_id+'/messages',headers=dict(headers),json={'body':'还有一个问题。'}))
                materialized=pool.submit(lambda:recorder.run_once())
                assert inbound.result().status_code==202;created=materialized.result()
            assert len(created)==1 and created[0]['created'] is True
            stream=admin.execute('select sequence,message_id,record->>%s from runtime.nexloop_conversation_messages where tenant_id=%s order by sequence',('direction',tenant)).fetchall()
            assert [s for s,_,_ in stream]==[1,2,3] and sorted(d or 'inbound' for _,_,d in stream)==['inbound','inbound','outbound']
            agent_message=created[0]['message']
            assert agent_message['body']==REPLY and agent_message['direction']=='outbound' and agent_message['sender_kind']=='agent' and agent_message['intent_id']==intent
            assert recorder.record(intent_id=intent)=={'message':agent_message,'created':False}
            assert recorder.pending()==[]
            # No relay self-loop: the Agent's words never enter the inbound outbox.
            assert admin.execute('select count(*) from runtime.nexloop_message_outbox where tenant_id=%s and message_id=%s',(tenant,agent_message['id'])).fetchone()==(0,)
            assert admin.execute('select count(*) from runtime.nexloop_message_inbox where tenant_id=%s and message_id=%s',(tenant,agent_message['id'])).fetchone()==(0,)
            visible=client.get('/api/v1/conversations/'+conversation_id+'/messages').json()['items']
            assert agent_message['id'] in [m['id'] for m in visible] and next(m for m in visible if m['id']==agent_message['id'])['direction']=='outbound'
            # (6) Governed READ derivation covers the delivered outbound Message for the Source.
            from nexloop_eios.message_read import message_read_basis,derived_message_read_envelope,_sign
            from nexloop_eios.postgres_artifacts import canonical_payload
            source=backend.authenticate(tokens['assembly-source'],world='real')
            sp,ss,sg=source._backend._pool,source._session,source._backend._signer
            basis=message_read_basis(sp,ss,agent_message['id']);assert basis['mode']=='derived'
            envelope=derived_message_read_envelope(sp,ss,sg,agent_message['id'],basis)
            def read(value,world='real'):
                with sp.connection() as db,db.transaction():
                    return db.execute('select authz.nexloop_read_object(%s,%s,%s,%s)',(ss.token_digest,world,value['text'],value['signature'])).fetchone()[0]
            assert read(envelope)['properties']=={'actor':agent_message['actor'],'body':REPLY}
            # (12) Negative derivations for the outbound Message.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):read(envelope,world='shadow')
            forged=json.loads(envelope['text']);forged['derivation_basis']['consumer_id']='f'*64;text=canonical_payload(forged)
            with pytest.raises((psycopg.errors.InsufficientPrivilege,psycopg.errors.InvalidParameterValue)):read({'text':text,'signature':_sign(sg,text)})
            assert message_read_basis(services._backend._pool,services._session,agent_message['id'])=={'mode':'configured'}
            # (5) Extraction from the stored conversation: enterprise commitment from the Agent's delivered words.
            assert admin.execute('select count(*) from runtime.nexloop_claim_extraction_feed where tenant_id=%s and message_id=%s',(tenant,agent_message['id'])).fetchone()==(1,)
            from nexloop_eios.claim_store import ConversationClaimExtractor
            from nexloop_eios.conversation_extraction import DeterministicExtractionProvider,build_user_payload
            from multi_authority_fixture import seed_multi_authority
            from test_claim_store_pg import source_targets
            ids=[m for _,m,_ in stream]
            session,_=seed_multi_authority(admin,sp,source_targets(conversation_id,ids),identity_suffix='-nx047-extractor',tenant=tenant)
            extractor=ConversationClaimExtractor(sp,session,sg,None,timezone='Asia/Shanghai')
            ctx,window=extractor.load_window(conversation_id,ids)
            assert [m.speaker for m in window]==['consumer','agent','consumer'] or [m.speaker for m in window]==['consumer','consumer','agent']
            agent_ref=next(m.sequence for m in window if m.speaker=='agent')
            response={'topics':[{'topic':'处理进展','conversation_summary':'企业承诺明天下午前反馈','user_valid_reply':True,'message_refs':[1,2,3]}],
                'claims':[{'topic_index':0,'message_ref':agent_ref,'quote':'我会在明天下午前给你处理进展','kind':'commitment','subject':{'kind':'enterprise','text':''},
                           'predicate':'处理进展反馈','value':{'type':'string','value':'给出处理进展'}}]}
            extractor.provider=DeterministicExtractionProvider({hashlib.sha256(build_user_payload(window,ctx).encode()).hexdigest():response})
            first=extractor.extract(conversation_id=conversation_id,message_ids=ids)
            claim=extractor.read(conversation_id=conversation_id)['statements'][0]
            assert claim['speaker']=='agent' and claim['epistemic_kind']=='commitment' and claim['source']['message_id']==agent_message['id']
            # AT-041 structural premise: an ambiguous deadline with a latest-bound window, never a chosen minute.
            assert claim['valid_time']['kind']=='deadline' and claim['valid_time']['status']=='ambiguous' and len(claim['valid_time']['latest_bound_window'])==2
            # AT-020 commitment level: re-running the same input version adds nothing.
            again=extractor.extract(conversation_id=conversation_id,message_ids=ids)
            assert again['replay'] is True and again['claim_ids']==sorted(first['claim_ids'])
            assert admin.execute("select count(*) from ontology.nexloop_claims where tenant_id=%s and epistemic_kind='commitment'",(tenant,)).fetchone()==(1,)
            # (13) AT-040 structural premise: delivered never marks the commitment fulfilled or anything resolved.
            assert admin.execute('select resolution_state from ontology.nexloop_claims where tenant_id=%s',(tenant,)).fetchall()==[('unresolved',)]
            types=dict(admin.execute('select type_name,count(*) from ontology.objects where tenant_id=%s group by type_name',(tenant,)).fetchall())
            assert types['Message']==3 and 'Commitment' not in types and 'Problem' not in types
            assert admin.execute('select count(*) from runtime.nexloop_outbound_messages where tenant_id=%s',(tenant,)).fetchone()==(1,)


def test_browser_human_cannot_record_agent_messages(assembled_message,admin,tmp_path):
    """(14) Human takeover is NX-028; until then no Human (consumer) may author Agent Messages."""
    f=assembled_message;o=f['original'];publish_recorder(f,admin)
    from test_browser_evidence import authenticate
    from test_browser_session_creation import create
    from nexloop_eios.browser_identity import open_browser_identity
    from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.postgres_artifacts import AuthoritySigner
    from nexloop_eios.assembly import open_core
    human=f['base']['human'];tenant=o['tenant'];signer=AuthoritySigner('explicit-configuration',o['paths']['backend_signing'].read_bytes())
    with open_browser_identity(make_conninfo(o['pg'],user='nexloop_identity')) as identity_pool,open_core(make_conninfo(o['pg'],user='nexloop_api')) as pool:
        uow=PostgresBrowserSessionUnitOfWork(identity_pool,tenant_id=tenant,application_id=o['application'])
        identity=(make_conninfo(o['pg'],user='nexloop_identity'),uow.get_subject(human['subject_id']),uow.get_membership(tenant,human['principal_id']),uow.get_local_account(tenant,human['account_id']),o['paths']['password'].read_text())
        actual=authenticate_browser_business(pool,create(uow,identity,authenticate(uow,identity).evidence).session,world='real')
        with pytest.raises(ConversationDenied):OutboundMessageRecorder(pool,actual,signer)
        with pool.connection() as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute('select authz.nexloop_outbound_message_command(%s,%s,%s,%s,%s)',(actual.token_digest,'real','{}','0'*64,'{"verb":"pending","limit":1}'))
    assert admin.execute('select count(*) from runtime.nexloop_outbound_messages').fetchone()==(0,)
