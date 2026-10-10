"""NX-047 Agent outbound Messages over the actual chain (synthetic data only).

Human HTTPS Message → relay → Pi Run (deterministic, echoes the input as its reply)
→ governed service.request intent (outbound record, same transaction) → real local
JSON delivery (ledger advances delivery) → governed Message.agent_create recorder →
same conversation stream → Claim extraction (enterprise commitment) and the governed
Message READ derivation. Admin only asserts, probes and injects explicit faults.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
        created_by='explicit-assembly-owner',created_at=datetime.now(UTC),object_types=manifest['object_types'],select=(ACTION,),
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


@contextmanager
def chain(f,admin,tmp_path,*,body=REPLY,host_update=None,guard_workers=None):
    """Actual HTTPS consumer Message → relay → Pi Run; yields before any delivery."""
    import httpx
    o=f['original'];p=o['paths'];tenant=o['tenant'];tokens=f['tokens'];credentials=f['credential_files']
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
            consumer=client.post('/api/v1/conversations/'+conversation_id+'/messages',headers=headers,json={'body':body}).json()['message']
            recipe=private(tmp_path,'assembly-relay-recipe',json.dumps(f['recipe']));vault=tmp_path/'relay-vault';vault.mkdir(mode=0o700)
            relay=['--database-url-file',str(api_dsn),*common,'--artifact-root',str(tmp_path/'relay-artifacts'),'--vault-root',str(vault),'--recipe-file',str(recipe)]
            relay+=sum((['--'+name+'-credential-file',str(credentials['assembly-'+label])] for name,label in [('route','route'),('source','source'),('planner','planner'),('executor','executor')]),[])
            assert once('nexloop_eios.message_relay_cli',relay,hidden)=='Message relay ready\n{"status":"queued"}\n'
            run=client.get('/api/v1/messages/'+consumer['id']+'/receipt').json()['run']
            host_tls=tmp_path/'host-tls';host_tls.mkdir(mode=0o700);runtime,host_key=files(host_tls)
            guard_port=free_port();guard_key=private(tmp_path,'assembly-guard-key',secrets.token_hex(32))
            guard_tls=tmp_path/'guard-tls';guard_tls.mkdir(mode=0o700);files(guard_tls)
            host_config=effect_configuration(guard_tls,guard_port,guard_key)
            cfg=json.loads(host_config.read_text());cfg.pop('deterministic_effect_message')
            cfg.update(deterministic_message_from_input=True,context_input_protocol='nexloop.context-pack.v2')
            if host_update:host_update(cfg)
            host_config.write_text(json.dumps(cfg))
            with reaped_host(runtime,host_key,host_config) as (_,host_client,_):
                worker=['--database-url-file',str(domain_dsn),*common,'--service-credential-file',str(credentials['assembly-runtime-worker']),
                    '--artifact-root',str(tmp_path/'runtime-worker-artifacts'),'--world','real','--queue','operations',
                    '--host-origin',str(host_client.base_url).rstrip('/'),'--host-control-key-file',str(host_key),'--host-ca-file',str(host_tls/'host-cert.pem'),
                    '--guard-port',str(guard_port),'--guard-key-file',str(guard_key),
                    '--guard-certificate-file',str(guard_tls/'host-cert.pem'),'--guard-tls-key-file',str(guard_tls/'host-key.pem'),'--total-timeout','20']
                if guard_workers is not None:worker+=['--guard-workers',str(guard_workers)]  # deployment shape (ADR-022 §4, ADR-024)
                runtime_output=once('nexloop_eios.runtime_worker',worker,hidden)
            database=runtime/run['run_id']/'runtime.sqlite'
            calls,receipts=tool_evidence(database) if host_update is None else ([],[])
            services=backend.authenticate(recorder_token,world='real')
            yield dict(o=o,p=p,tenant=tenant,tokens=tokens,credentials=credentials,common=common,hidden=hidden,api_dsn=api_dsn,effect_dsn=effect_dsn,
                client=client,headers=headers,conversation_id=conversation_id,consumer=consumer,run=run,database=database,runtime_output=runtime_output,
                calls=calls,receipts=receipts,relay=relay,backend=backend,recorder_token=recorder_token,
                recorder=OutboundMessageRecorder(services._backend._pool,services._session,services._backend._signer))


def delivery_material(tmp_path,*,timeout=3):
    tls=tmp_path/'delivery-tls';tls.mkdir(mode=0o700);_,provider_key=files(tls)
    origin='https://127.0.0.1:'+str(free_port());root=tmp_path/'json-products';root.mkdir(mode=0o700)
    config=private(tmp_path,'assembly-provider-config',json.dumps({'origin':origin,'credential_file':str(provider_key),'ca_file':str(tls/'host-cert.pem'),'timeout':timeout}))
    return {'tls':tls,'origin':origin,'root':root,'provider_key':provider_key,'provider_config':config,'secret':provider_key.read_text()}


@contextmanager
def delivery_service(c,tmp_path,material,*,barrier=None):
    """Actual local JSON delivery service in-process (same construction as its CLI)."""
    import threading
    from nexloop_eios.effect_provider import EffectProviderConfiguration,HttpEffectProvider
    from nexloop_eios.local_json_delivery import DeliveryAuthorityPort,DeliveryServerConfiguration,JsonExportStore,make_server
    m=material;tls=m['tls']
    profile=HttpEffectProvider(EffectProviderConfiguration(m['origin'],credential_file=str(m['provider_key']),ca_file=str(tls/'host-cert.pem'))).profile_digest
    with open_backend(database_url=make_conninfo(c['o']['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'delivery-artifacts',
                      signing_key_file=c['p']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        authority=DeliveryAuthorityPort(backend,c['credentials']['assembly-executor'],profile);authority._port()
        store=JsonExportStore(m['root'])
        if barrier is not None:
            publish=store._immutable
            def held(name,body):
                publish(name,body)
                if name.endswith('.export.json'):barrier['fsynced'].set();barrier['release'].wait(10)
            store._immutable=held
        server=make_server(DeliveryServerConfiguration(m['origin'],tls/'host-cert.pem',tls/'host-key.pem',m['provider_key']),authority,store)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05});thread.start()
        try:yield
        finally:
            if barrier is not None:barrier['release'].set()
            server.shutdown();server.server_close();thread.join(10);store.close()


def effect_worker(c,tmp_path,provider_config,secret,*,lease=30):
    executor=['--database-url-file',str(c['effect_dsn']),*c['common'],'--service-credential-file',str(c['credentials']['assembly-executor']),
        '--artifact-root',str(tmp_path/'effect-worker-artifacts'),'--world','real','--provider-config-file',str(provider_config),'--lease-seconds',str(lease)]
    return once('nexloop_eios.effect_worker',executor,c['hidden']+[secret])


def test_agent_reply_persisted_delivered_materialized_extracted_and_read(assembled_message,admin,tmp_path,monkeypatch):
    f=assembled_message
    with chain(f,admin,tmp_path) as c:
        tenant=c['tenant'];client=c['client'];recorder=c['recorder'];conversation_id=c['conversation_id'];consumer=c['consumer']
        assert 'succeeded' in c['runtime_output']
        receipts=c['receipts'];intent=receipts[0]['intent_id']
        # (1)(2) Same-transaction outbound record; three replayed tool calls → one intent → one record.
        assert len(receipts)==3 and all(r['intent_id']==intent for r in receipts) and c['calls']
        assert outbound(admin,tenant)==[(intent,'persisted',None,None,'agent',consumer['id'],hashlib.sha256(REPLY.encode()).hexdigest())]
        assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s',(tenant,)).fetchone()==(1,)
        # (8) Pi's free assistant text exists in the runtime journal but is never persisted as a Message.
        assistant=assistant_texts(c['database'])
        assert assistant and all(text!=REPLY for text in assistant)
        bodies={row[0] for row in admin.execute("select properties->>'body' from ontology.objects where tenant_id=%s and type_name='Message'",(tenant,)).fetchall()}
        assert not bodies&set(assistant)
        assert admin.execute("select count(*) from runtime.nexloop_conversation_messages where tenant_id=%s and record->>'body'=any(%s)",(tenant,assistant)).fetchone()==(0,)
        # Persisted is not delivered: no stream entry, no relay inbox/outbox row, invisible to the consumer.
        assert admin.execute('select count(*) from runtime.nexloop_conversation_messages where tenant_id=%s',(tenant,)).fetchone()==(1,)
        assert admin.execute('select count(*) from runtime.nexloop_message_outbox where tenant_id=%s',(tenant,)).fetchone()==(1,)
        assert [m['id'] for m in client.get('/api/v1/conversations/'+conversation_id+'/messages').json()['items']]==[consumer['id']]
        assert recorder.pending()==[]
        with pytest.raises(ConversationUnavailable):recorder.record(intent_id=intent)
        material=delivery_material(tmp_path)
        with delivery_service(c,tmp_path,material):
            assert 'fulfilled' in effect_worker(c,tmp_path,material['provider_config'],material['secret'])
        # NX-027 (D7): the accepted delivery is one channel cost unit, unpriced while no unit rate is configured.
        with admin.transaction():
            admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
            assert admin.execute("select entry_id,cost_kind,units::text,amount,basis,consumer_ref from runtime.nexloop_cost_entries where cost_kind='channel'").fetchall()==[
                ('channel:'+intent,'channel','1',None,'unpriced',admin.execute('select consumer_id from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()[0])]
        # (3) Ledger-driven, monotonic, ends delivered.
        history=events(admin,tenant)
        assert history[0][0]=='persisted' and history[-1][1]=='delivered' and all((a,b) in TRANSITIONS for a,b,_ in history)
        # (9) A backwards rewrite is rejected even for the table owner; no delete.
        with admin.transaction():
            admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
            with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
                admin.execute("update runtime.nexloop_outbound_messages set delivery_state='dispatching'")
            with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
                admin.execute('delete from runtime.nexloop_outbound_messages')
        # Recorder failure between prepare and commit writes nothing; the retry records once.
        with monkeypatch.context() as patched:
            patched.setattr(OutboundMessageRecorder,'_create',lambda self,*a,**k:(_ for _ in ()).throw(RuntimeError('synthetic failure')))
            with pytest.raises(Exception):recorder.record(intent_id=intent)
        assert outbound(admin,tenant)[0][2] is None
        # (4) Concurrent consumer acceptance and recorder materialization on the same conversation.
        headers=dict(c['headers']);headers['Idempotency-Key']='nx047-outbound-message-key-2'
        with ThreadPoolExecutor(2) as pool:
            inbound=pool.submit(lambda:client.post('/api/v1/conversations/'+conversation_id+'/messages',headers=headers,json={'body':'还有一个问题。'}))
            materialized=pool.submit(lambda:recorder.run_once())
            assert inbound.result().status_code==202;created=materialized.result()
        assert len(created)==1 and created[0]['created'] is True
        stream=admin.execute('select sequence,message_id,record->>%s from runtime.nexloop_conversation_messages where tenant_id=%s order by sequence',('direction',tenant)).fetchall()
        assert [s for s,_,_ in stream]==[1,2,3] and sorted(d or 'inbound' for _,_,d in stream)==['inbound','inbound','outbound']
        agent_message=created[0]['message']
        assert agent_message['body']==REPLY and agent_message['direction']=='outbound' and agent_message['sender_kind']=='agent' and agent_message['intent_id']==intent
        assert recorder.record(intent_id=intent)=={'message':agent_message,'created':False} and recorder.pending()==[]
        assert admin.execute('select count(*) from runtime.nexloop_message_outbox where tenant_id=%s and message_id=%s',(tenant,agent_message['id'])).fetchone()==(0,)
        assert admin.execute('select count(*) from runtime.nexloop_message_inbox where tenant_id=%s and message_id=%s',(tenant,agent_message['id'])).fetchone()==(0,)
        visible=client.get('/api/v1/conversations/'+conversation_id+'/messages').json()['items']
        assert next(m for m in visible if m['id']==agent_message['id'])['direction']=='outbound'
        # NX-051 (AT-014): the Agent reply's link mirrors the server-derived trigger; its provider facts carry the
        # delivery service's reference and observation time; consumer messages keep server-default facts.
        shown=next(m for m in visible if m['id']==agent_message['id'])
        assert shown['reply_to_message_id']==agent_message['trigger_message_id']==consumer['id']
        reference,observed=admin.execute('select provider_reference,observed_at from runtime.nexloop_effect_observations where tenant_id=%s and intent_id=%s and provider_reference is not null order by observed_at desc limit 1',(tenant,intent)).fetchone()
        assert shown['provider']['namespace']=='nexloop.agent' and shown['provider']['trust']=='server' and shown['provider']['message_ref']==reference and shown['provider']['sequence'] is None
        assert admin.execute('select provider_sent_at from runtime.nexloop_message_provider_facts where tenant_id=%s and message_id=%s',(tenant,agent_message['id'])).fetchone()==(observed,)
        assert admin.execute('select raw_kind,raw_ref,reply_to_message_id,resolution,source from runtime.nexloop_message_reply_links where tenant_id=%s and message_id=%s',(tenant,agent_message['id'])).fetchall()==[('message',consumer['id'],consumer['id'],'resolved','server')]
        assert all(m['provider']['namespace']=='nexloop.api' and m['reply_to_message_id'] is None for m in visible if m.get('direction')!='outbound')
        # (6) Governed READ derivation covers the delivered outbound Message for the Source.
        from nexloop_eios.message_read import message_read_basis,derived_message_read_envelope,_sign
        from nexloop_eios.postgres_artifacts import canonical_payload
        source=c['backend'].authenticate(c['tokens']['assembly-source'],world='real')
        sp,ss,sg=source._backend._pool,source._session,source._backend._signer
        basis=message_read_basis(sp,ss,agent_message['id']);assert basis['mode']=='derived'
        envelope=derived_message_read_envelope(sp,ss,sg,agent_message['id'],basis)
        def read(value,session=ss,world='real'):
            with sp.connection() as db,db.transaction():
                return db.execute('select authz.nexloop_read_object(%s,%s,%s,%s)',(session.token_digest,world,value['text'],value['signature'])).fetchone()[0]
        assert read(envelope)['properties']=={'actor':agent_message['actor'],'body':REPLY}
        # (12) Negative derivations: other world, forged Consumer, principal without rule, other tenant.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):read(envelope,world='shadow')
        forged=json.loads(envelope['text']);forged['derivation_basis']['consumer_id']='f'*64;text=canonical_payload(forged)
        with pytest.raises((psycopg.errors.InsufficientPrivilege,psycopg.errors.InvalidParameterValue)):read({'text':text,'signature':_sign(sg,text)})
        assert message_read_basis(recorder.pool,recorder.session,agent_message['id'])=={'mode':'configured'}
        from multi_authority_fixture import seed_multi_authority
        other_tenant=str(uuid.uuid4());admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')",(other_tenant,))
        consumer_ref='eios:object:Consumer/'+admin.execute('select consumer_id from runtime.nexloop_conversations where conversation_id=%s',(conversation_id,)).fetchone()[0]
        other,_=seed_multi_authority(admin,sp,[(consumer_ref,ResourceType.OBJECT,Operation.READ),('eios:object:Message/'+agent_message['id'],ResourceType.OBJECT,Operation.READ)],
            identity_suffix='-nx047-other-tenant',tenant=other_tenant)
        # Another tenant's principal never reaches the derivation, and even its configured READ finds nothing.
        assert message_read_basis(sp,other,agent_message['id'])=={'mode':'configured'}
        from nexloop_eios.object_reads import AuthorizedObjectReader
        with pytest.raises(psycopg.errors.InsufficientPrivilege):AuthorizedObjectReader(sp,other,sg).get('Message',agent_message['id'])
        foreign=json.loads(envelope['text']);foreign['tenant_id']=other_tenant;text=canonical_payload(foreign)
        with pytest.raises((psycopg.errors.InsufficientPrivilege,psycopg.errors.InvalidParameterValue)):read({'text':text,'signature':_sign(sg,text)},session=other)
        # (5) Extraction from the stored conversation: enterprise commitment from the Agent's delivered words.
        assert admin.execute('select count(*) from runtime.nexloop_claim_extraction_feed where tenant_id=%s and message_id=%s',(tenant,agent_message['id'])).fetchone()==(1,)
        from nexloop_eios.claim_store import ConversationClaimExtractor
        from nexloop_eios.conversation_extraction import DeterministicExtractionProvider,build_user_payload
        from test_claim_store_pg import source_targets
        ids=[m for _,m,_ in stream]
        session,_=seed_multi_authority(admin,sp,source_targets(conversation_id,ids),identity_suffix='-nx047-extractor',tenant=tenant)
        extractor=ConversationClaimExtractor(sp,session,sg,None,timezone='Asia/Shanghai')
        ctx,window=extractor.load_window(conversation_id,ids)
        assert sorted(m.speaker for m in window)==['agent','consumer','consumer']
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


def tool_results(database):
    import sqlite3
    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as connection:
        records=[json.loads(row[0]) for row in connection.execute('select record from entries order by id')]
    return [m for r in records for m in r.get('model',[]) if m.get('role')=='toolResult']


def assistant_texts(database):
    import sqlite3
    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as connection:
        records=[json.loads(row[0]) for row in connection.execute('select record from entries order by id')]
    texts=[]
    for record in records:
        for message in record.get('model',[]):
            if message.get('role')=='assistant':
                texts.extend(item['text'] for item in message.get('content',[]) if item.get('type')=='text' and item.get('text'))
    return texts


def test_scope_denied_reply_is_rejected_by_governance_and_never_persisted(assembled_message,admin,tmp_path):
    """(7) Actual catalog-scope governance refuses the Run's reply; nothing outbound exists."""
    f=assembled_message
    def outside(cfg):cfg['deterministic_effect_request_scope']={'offering_id':f['recipe']['offering_id'],'offering_revision':1,'requested_guarantees':[],'requested_discounts':['outside-discount']}
    with chain(f,admin,tmp_path,host_update=outside) as c:
        tenant=c['tenant']
        assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s',(tenant,)).fetchone()==(0,)
        # Both reply submissions were refused (tool errors are redacted to the Run) …
        denied=[m for m in tool_results(c['database']) if m.get('toolName')=='nexloop.service.request']
        assert len(denied)==2 and all(m.get('isError') is True for m in denied)
        # … and the refusal is the published catalog's: this exact scope is outside its terms,
        # while the same reply without the invented discount is within them (the main test delivers it).
        from nexloop_eios.service_offerings import assess_request_scope
        offering_id=f['recipe']['offering_id']
        offering,revision=admin.execute("select properties,nexloop_revision from ontology.objects where tenant_id=%s and type_name='ServiceOffering' and object_id=%s",(tenant,offering_id)).fetchone()
        assert revision==1  # the Run's scope names the current revision: no revision-conflict refusal
        scope={'offering_id':offering_id,'offering_revision':1,'requested_guarantees':[],'requested_discounts':['outside-discount']}
        assert assess_request_scope(offering,scope,offering_id=offering_id,revision=1)['reason']=='outside_catalog_terms'
        assert assess_request_scope(offering,scope|{'requested_discounts':[]},offering_id=offering_id,revision=1)['reason']=='within_catalog'
        assert outbound(admin,tenant)==[] and c['recorder'].pending()==[]
        assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Message'",(tenant,)).fetchone()==(1,)
        texts=assistant_texts(c['database'])
        assert any('refusal' in text for text in texts)
        assert [m['id'] for m in c['client'].get('/api/v1/conversations/'+c['conversation_id']+'/messages').json()['items']]==[c['consumer']['id']]


def test_unknown_reconciled_by_provider_query_and_recorder_sigkill_recovers(assembled_message,admin,tmp_path):
    """(10) Real transport timeout → unknown; only the governed provider QUERY moves it on.
    (11) Recorder process SIGKILLed mid-transaction after acceptance; restart records once."""
    import signal,subprocess,sys,threading
    f=assembled_message
    with chain(f,admin,tmp_path) as c:
        tenant=c['tenant'];intent=c['receipts'][0]['intent_id']
        barrier={'fsynced':threading.Event(),'release':threading.Event()}
        material=delivery_material(tmp_path,timeout=1);config,secret=material['provider_config'],material['secret']
        def lease_expired():
            deadline=time.monotonic()+10
            while not admin.execute('select lease_until is null or lease_until<=clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()[0]:
                assert time.monotonic()<deadline;time.sleep(.05)
        # Dispatch reaches the provider (export fsynced) but the reply times out: committed dispatching.
        with delivery_service(c,tmp_path,material,barrier=barrier):
            assert '"status":"unknown"' in effect_worker(c,tmp_path,config,secret,lease=3) and barrier['fsynced'].is_set()
        assert outbound(admin,tenant)[0][1]=='dispatching' and c['recorder'].pending()==[]
        # Reconcile while the provider is down: the governed QUERY cannot answer → ledger unknown, no resend.
        lease_expired()
        assert '"status":"unknown"' in effect_worker(c,tmp_path,config,secret,lease=3)
        assert admin.execute('select state from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()==('unknown',)
        assert outbound(admin,tenant)[0][1]=='unknown' and c['recorder'].pending()==[]
        # Provider back: the QUERY observes the original export → delivered. Never a second POST/product.
        lease_expired()
        with delivery_service(c,tmp_path,material):
            assert 'fulfilled' in effect_worker(c,tmp_path,config,secret,lease=3)
        assert len(list(material['root'].glob('*.export.json')))==1
        history=events(admin,tenant)
        assert [(a,b) for a,b,_ in history][:2]==[('persisted','dispatching'),('dispatching','unknown')]
        leaving=[h for h in history if h[0]=='unknown']
        assert len(leaving)==1 and leaving[0][1] in ('provider_accepted','delivered') and leaving[0][2] in ('observation','attempt','intent')
        assert admin.execute("select count(*) from runtime.nexloop_effect_observations where intent_id=%s and provider_state='fulfilled'",(intent,)).fetchone()[0]>=1
        assert outbound(admin,tenant)[0][1]=='delivered'
        assert admin.execute('select count(*) from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()==(1,)
        # (11) Lock the conversation row so the recorder blocks inside its transaction, then SIGKILL it.
        credential=private(tmp_path,'outbound-recorder-credential',c['recorder_token'])
        args=[sys.executable,'-m','nexloop_eios.outbound_messages','--database-url-file',str(c['api_dsn']),*c['common'],
            '--service-credential-file',str(credential),'--artifact-root',str(tmp_path/'recorder-artifacts'),'--world','real','--once']
        with psycopg.connect(make_conninfo(c['o']['pg'],user='nexloop_bootstrap'),autocommit=False) as holder:
            holder.execute('select 1 from runtime.nexloop_conversations where conversation_id=%s for update',(c['conversation_id'],))
            child=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+20
                while not admin.execute("select count(*) from pg_stat_activity where usename='nexloop_api' and wait_event_type='Lock'").fetchone()[0]:
                    assert child.poll() is None and time.monotonic()<deadline;time.sleep(.05)
                child.kill();child.communicate(timeout=10);assert child.returncode==-signal.SIGKILL
            finally:
                if child.poll() is None:child.kill();child.communicate(timeout=10)
            holder.rollback()
        deadline=time.monotonic()+10
        while admin.execute("select count(*) from pg_stat_activity where usename='nexloop_api' and wait_event_type='Lock'").fetchone()[0]:
            assert time.monotonic()<deadline;time.sleep(.05)
        assert outbound(admin,tenant)[0][2] is None
        assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Message'",(tenant,)).fetchone()==(1,)
        restarted=subprocess.run(args,capture_output=True,text=True,timeout=60)
        assert restarted.returncode==0 and restarted.stdout=='Outbound Recorder ready\n{"recorded":1,"replayed":0}\n'
        assert all(value not in restarted.stdout+restarted.stderr for value in c['hidden'])
        assert subprocess.run(args,capture_output=True,text=True,timeout=60).stdout=='Outbound Recorder ready\n{"recorded":0,"replayed":0}\n'
        assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Message'",(tenant,)).fetchone()==(2,)
        assert outbound(admin,tenant)[0][3]==2


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
