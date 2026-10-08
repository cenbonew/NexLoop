"""Actual publication50 -> initial HUMAN -> governed setup -> local real export.

No allowed callback, no business SQL seed, no synthetic provider receipt. Only
fixture-owned PostgreSQL/TLS/root resources are used; all business rows Actions.
"""
from contextlib import ExitStack,contextmanager
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,threading,time,uuid
import pytest
from psycopg.conninfo import make_conninfo
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_contexts import BIND,registrar_schema
from nexloop_eios.effect_dispatch import EffectDispatcher
from nexloop_eios.effect_provider import EffectProviderConfiguration,HttpEffectProvider
from nexloop_eios.local_json_delivery import (DeliveryAuthorityPort,DeliveryServerConfiguration,
    JsonExportStore,make_server,BoundedDeliveryRecovery,DeliveryUnavailable)
from nexloop_eios.trusted_configuration import apply_manifest
from test_business_setup_pg import business_plan,declared_service,invoke
from test_trusted_configuration_pg import configured,PrivateConfiguration,private
from test_agent_host import files,free_port
from test_local_json_delivery_files import _snapshot


@pytest.fixture
def delivery_plan(business_plan,admin,tmp_path):
    f=business_plan;tenant=f['f']['tenant'];m=f['manifest']
    base=next(a for a in m['actions'] if a['definition']['stable_name']=='Consumer.create')
    schemas={s['type_name']:s for s in m['object_types']}
    actions=list(m['actions'])
    for name,types in {'Goal.create':['Goal'],'PlanStep.create':['PlanStep'],BIND:['Consumer','Goal','PlanStep','EffectControl'],
                       'nexloop.service.query':['Consumer']}.items():
        body=json.loads(json.dumps(base['definition']));body.pop('contract_digest',None);body['stable_name']=name
        refs=[]
        from eios.ontology.models import ObjectTypeDefinition
        for type_name in types:
            ref={**base['definition']['object_types'][0],'stable_name':type_name,
                'schema_digest':schema_contract_digest(ObjectTypeDefinition.model_validate_json(json.dumps(schemas[type_name])))}
            refs.append(ref)
        body['object_types']=refs;body['governance']['change_scope']['object_types']=refs
        capname='ontology.object.create' if name.endswith('.create') else name
        body['capability_binding']['capability_name']=capname
        if name==BIND:body['input_schema']=registrar_schema('bind')
        from eios.ontology.definitions import ActionDefinition
        definition=ActionDefinition.model_validate_json(json.dumps(body))
        actions.append({'definition':definition.model_dump(mode='json'),'capability':{**base['capability'],'capability_name':capname,'has_side_effects':True}})
    executor,expiry,executor_facts=declared_service(tenant,['nexloop.service.request','nexloop.service.query'],'-delivery-executor')
    planner,pexp,pfacts=declared_service(tenant,['Goal.create','PlanStep.create',BIND],'-local-planner')
    source,sexp,sfacts=declared_service(tenant,['nexloop.service.request'],'-local-source')
    planner_token=secrets.token_urlsafe(48);source_token=secrets.token_urlsafe(48);delivery_token=secrets.token_urlsafe(48)
    facts={(r['kind'],tuple(r['key'])):r for r in m['authority_facts']+executor_facts+pfacts+sfacts}
    credentials=list(m['service_credentials'])
    credentials += [{'reference':ref,'binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':end.isoformat(),'status':'active'}
        for ref,binding,end in [('local-planner',planner,pexp),('local-source',source,sexp),('delivery-executor',executor,expiry)]]
    manifest={**m,'manifest_id':str(uuid.uuid4()),'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0],
        'actions':actions,'authority_facts':list(facts.values()),'service_credentials':credentials}
    p=f['f']['paths'];p['secrets'].write_text(json.dumps({'source':f['f']['token'],'business-owner':f['owner_token'],
        'business-executor':f['executor_token'],'local-planner':planner_token,'local-source':source_token,'delivery-executor':delivery_token}))
    apply_manifest(manifest,database_url_file=p['dsn'],signing_key_file=p['signing'],signing_key_id='explicit-configuration',service_secrets_file=p['secrets'])
    f['paths']['executor-credential-file'].write_text(delivery_token)
    result=invoke(f);assert result.returncode==0, 'actual governed business setup failed'
    setup=json.loads(result.stdout)
    with ExitStack() as stack:
        opts=dict(signing_key_file=p['backend_signing'],signing_key_id='explicit-configuration')
        api=stack.enter_context(open_backend(database_url=make_conninfo(f['f']['pg'],user='nexloop_api'),artifact_root=tmp_path/'local-api',**opts))
        worker=stack.enter_context(open_backend(database_url=make_conninfo(f['f']['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'local-worker',**opts))
        provider_backend=stack.enter_context(open_backend(database_url=make_conninfo(f['f']['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'local-provider',**opts))
        from message_offering_fixture import install_message_catalog
        catalog=install_message_catalog(admin,api,tenant,setup['consumer_id'],source_token,
            suffix='-local-source',manifest=manifest,paths=p)
        manifest=catalog['manifest'];source_token=catalog['source_token']
        planner_service=api.authenticate(planner_token,world='real');source_service=catalog['source']
        expires=(datetime.now(UTC)+timedelta(seconds=180)).isoformat()
        goal=planner_service.create_object(action_name='Goal.create',action_version=1,intent_id='local-goal',type_name='Goal',
            properties={'consumer_id':setup['consumer_id'],'state':'active','valid_until':expires})['object_id']
        step=planner_service.create_object(action_name='PlanStep.create',action_version=1,intent_id='local-step',type_name='PlanStep',properties={
            'consumer_id':setup['consumer_id'],'goal_id':goal,'control_id':setup['control_id'],'submitter_principals':[source.subject_principal_id],
            'action_name':'nexloop.service.request','state':'ready'})['object_id']
        run=source_service.issue_run_credential(action_resources=['eios:action:nexloop.service.request:1'])
        planner_service.bind_effect_context(step_id=step,step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,
            run_id=run.run_id,run_token=run.token,executor_token=delivery_token)
        submitter=api.authenticate_run(run.token,world='real',run_id=run.run_id)
        executor_service=worker.authenticate(delivery_token,world='real')
        credential=private(tmp_path,'local-executor-token',delivery_token)
        tls=tmp_path/'local-tls';tls.mkdir(mode=0o700);unused,key=files(tls)
        origin='https://127.0.0.1:'+str(free_port())
        provider=HttpEffectProvider(EffectProviderConfiguration(origin,credential_file=str(key),ca_file=str(tls/'host-cert.pem'),timeout=2))
        config=DeliveryServerConfiguration(origin,tls/'host-cert.pem',tls/'host-key.pem',key)
        root=tmp_path/'real-delivery';root.mkdir(mode=0o700)
        authority=DeliveryAuthorityPort(provider_backend,credential,provider.profile_digest)
        store=JsonExportStore(root)
        yield PrivateConfiguration(api=api,worker=worker,provider_backend=provider_backend,executor=executor_service,submitter=submitter,
            authority=authority,store=store,root=root,provider=provider,server_config=config,control=setup['control_id'],run=run,
            manifest=manifest,publication_paths=p,manifest_pg=f['f']['pg'],credential_file=credential,source_principal=source.subject_principal_id,executor_principal=executor.subject_principal_id)
        store.close()


@contextmanager
def live_delivery(f):
    server=make_server(f['server_config'],f['authority'],f['store'])
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05});thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join(5);assert not thread.is_alive()


def submit(f):return f['submitter'].submit_effect_intent(parameters={'message':'Actual governed private JSON delivery.\n真实本地产物'})


def test_actual_https_governed_post_fsync_readonly_get_and_original_claim_receipt(delivery_plan,admin):
    f=delivery_plan;accepted=submit(f);intent=accepted['intent_id']
    with live_delivery(f):
        result=EffectDispatcher(f['executor'],f['provider'],lease_seconds=30).run_once()
        assert result['status']=='fulfilled' and result['business_action_success'] is True
        assert result['governed_claim_finalized'] is True
        product=f['root']/(intent+'.export.json')
        assert json.loads(product.read_bytes())['document']['body'].startswith('Actual governed')
        digest=admin.execute('select provider_payload_digest from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()[0]
        before=_snapshot(f['root'])
        actual=f['provider'].query(intent_id=intent,payload_digest=digest)
        assert actual.state=='fulfilled' and _snapshot(f['root'])==before
        receipt=f['executor'].read_effect_receipt(intent_id=intent)
        assert receipt['business_action_success'] is True and receipt['governed_claim_finalized'] is True
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger where control_id=%s',(f['control'],)).fetchone()==(1,1)
    assert admin.execute('select count(*) from runtime.nexloop_effect_control_reservations where intent_id=%s',(intent,)).fetchone()==(1,)
    claim=admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s',(intent,)).fetchone()[0]
    assert claim['terminal_outcome']['status']=='succeeded' and claim['terminal_outcome']['outcome_id']==receipt['receipt_id']


def _fault_delivery_process(options,config,root,credential,profile,suffix,pipe):
    # Test-owned instrumentation pauses ONLY after actual fsync publication;
    # current production EIOS guard runs before this real file operation.
    with open_backend(**options) as backend:
        store=JsonExportStore(root)
        authority=DeliveryAuthorityPort(backend,credential,profile)
        original=store._immutable
        def publish(name,body):
            original(name,body)
            if name.endswith(suffix):
                pipe.send({'event':'product_window','suffix':suffix});pipe.recv()
        store._immutable=publish
        server=make_server(config,authority,store)
        pipe.send({'event':'listening'})
        try:server.serve_forever(poll_interval=.05)
        finally:server.server_close();store.close()


@contextmanager
def fault_delivery(f,tmp_path,suffix):
    import multiprocessing
    options=dict(database_url=make_conninfo(f['manifest_pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'fault-artifacts',
        signing_key_file=f['publication_paths']['backend_signing'],signing_key_id='explicit-configuration')
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=_fault_delivery_process,args=(options,f['server_config'],f['root'],f['credential_file'],f['provider'].profile_digest,suffix,child))
    process.start();child.close()
    try:
        assert parent.poll(10) and parent.recv()=={'event':'listening'}
        yield process,parent
    finally:
        if process.is_alive():process.kill()
        process.join(10);parent.close();assert not process.is_alive()


@contextmanager
def recovery_cli(f,tmp_path):
    import subprocess,sys
    dsn=private(tmp_path,'delivery-cli-dsn',make_conninfo(f['manifest_pg'],user='nexloop_action_worker'))
    c=f['server_config']
    argv=[sys.executable,'-m','nexloop_eios.local_json_delivery','--origin',c.origin,
        '--database-url-file',str(dsn),'--signing-key-file',str(f['publication_paths']['backend_signing']),
        '--signing-key-id','explicit-configuration','--service-credential-file',str(f['credential_file']),
        '--artifact-root',str(tmp_path/'delivery-cli-artifacts'),'--delivery-root',str(f['root']),
        '--certificate-file',str(c.certificate_file),'--tls-key-file',str(c.key_file),
        '--provider-ca-file',str(c.certificate_file),'--provider-credential-file',str(c.credential_file)]
    process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:yield process
    finally:
        process.terminate()
        stdout,stderr=process.communicate(timeout=20)
        assert process.returncode==0 and stdout==stderr==''


@pytest.mark.parametrize('suffix',['.manifest.json','.export.json'])
def test_actual_sigkill_reopen_recovers_original_intent_without_second_post_or_quota(delivery_plan,admin,tmp_path,suffix):
    import signal
    from concurrent.futures import ThreadPoolExecutor
    f=delivery_plan;intent=submit(f)['intent_id']
    cfg=f['provider'].configuration
    provider=HttpEffectProvider(EffectProviderConfiguration(cfg.origin,credential_file=cfg.credential_file,ca_file=cfg.ca_file,timeout=.5))
    calls=[];real_dispatch=provider.dispatch
    def dispatch(**arguments):calls.append(arguments['intent_id']);return real_dispatch(**arguments)
    provider.dispatch=dispatch
    with fault_delivery(f,tmp_path,suffix) as (process,pipe),ThreadPoolExecutor(1) as pool:
        pending=pool.submit(EffectDispatcher(f['executor'],provider,lease_seconds=3).run_once)
        assert pipe.poll(10) and pipe.recv()=={'event':'product_window','suffix':suffix}
        process.kill();process.join(5);assert process.exitcode==-signal.SIGKILL
        result=pending.result(timeout=10)
        assert result['business_action_success'] is False
    manifest=f['root']/(intent+'.manifest.json');product=f['root']/(intent+'.export.json')
    assert manifest.exists()
    old_revision=admin.execute('select action_claim_revision from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()[0]
    with recovery_cli(f,tmp_path) as process:
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            assert process.poll() is None
            finalized=admin.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()[0]
            if finalized:break
            if suffix=='.export.json':
                # Existing real product: original Worker query-only reconciliation.
                EffectDispatcher(f['executor'],provider,lease_seconds=3).run_once()
            time.sleep(.15)
        assert product.exists() and finalized is True
        receipt=f['executor'].read_effect_receipt(intent_id=intent)
        assert receipt['business_action_success'] is True and receipt['governed_claim_finalized'] is True
        digest=admin.execute('select provider_payload_digest from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()[0]
        before=_snapshot(f['root'])
        assert provider.query(intent_id=intent,payload_digest=digest).state=='fulfilled'
        assert _snapshot(f['root'])==before
    assert calls==[intent]
    assert admin.execute('select count(*) from runtime.nexloop_effect_control_reservations where intent_id=%s',(intent,)).fetchone()==(1,)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger where control_id=%s',(f['control'],)).fetchone()==(1,1)
    if suffix=='.manifest.json':
        revision=admin.execute('select action_claim_revision from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()[0]
        assert revision>old_revision
        assert admin.execute('select count(*) from runtime.nexloop_local_delivery_recoveries where intent_id=%s',(intent,)).fetchone()==(1,)


def manifest_window(f,monkeypatch,*,lease_seconds=3):
    intent=submit(f)['intent_id'];original=f['store']._immutable
    def fault(name,body):
        original(name,body)
        if name.endswith('.manifest.json'):raise DeliveryUnavailable()
    monkeypatch.setattr(f['store'],'_immutable',fault)
    with live_delivery(f):
        result=EffectDispatcher(f['executor'],f['provider'],lease_seconds=lease_seconds).run_once()
    monkeypatch.setattr(f['store'],'_immutable',original)
    assert result['business_action_success'] is False
    candidates=f['store'].recovery_candidates(f['provider'].profile_digest)
    assert len(candidates)==1 and candidates[0]['intent_id']==intent
    return intent,candidates[0]


def await_original_action_expiry(admin,intent):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        expired=admin.execute("select (claim->>'lease_expires_at')::timestamptz<=clock_timestamp() from runtime.nexloop_action_claims where intent_id=%s",(intent,)).fetchone()[0]
        if expired:return
        time.sleep(.1)
    assert False,'fixture original Action lease did not expire'


def revoke_grant(f,admin,principal,target):
    from eios.authz import facts as F
    manifest=f['manifest'];facts=[];changed=False
    for row in manifest['authority_facts']:
        if row['kind']=='grants' and row['key']==[principal,target]:
            body={**row['payload'],'grants':[]};body.pop('snapshot_digest',None)
            row={**row,'payload':F.GrantFacts.model_validate_json(json.dumps(body)).model_dump(mode='json')};changed=True
        facts.append(row)
    assert changed
    body={**manifest,'manifest_id':str(uuid.uuid4()),'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(manifest['tenant_id'],)).fetchone()[0],
        'authority_facts':facts}
    p=f['publication_paths']
    apply_manifest(body,database_url_file=p['dsn'],signing_key_file=p['signing'],signing_key_id='explicit-configuration',service_secrets_file=p['secrets'])


def test_actual_active_original_claim_recovery_does_not_occupy_new_effect_lease(delivery_plan,admin,monkeypatch):
    f=delivery_plan;intent,manifest=manifest_window(f,monkeypatch,lease_seconds=30)
    before=admin.execute('select fence,lease_until,lease_credential,state from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()
    claim=admin.execute("select (claim->>'lease_expires_at')::timestamptz>clock_timestamp() from runtime.nexloop_action_claims where intent_id=%s",(intent,)).fetchone()[0]
    assert claim is True
    assert f['authority'].recover_one(f['store'],manifest) is False
    assert admin.execute('select fence,lease_until,lease_credential,state from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()==before
    assert admin.execute('select count(*) from runtime.nexloop_local_delivery_recoveries where intent_id=%s',(intent,)).fetchone()==(0,)
    assert not (f['root']/(intent+'.export.json')).exists()


@pytest.mark.parametrize('revocation',['source_send','executor_send','source_run_expired'])
def test_actual_recovery_current_authority_revocation_denies_real_new_claim_and_file(delivery_plan,admin,monkeypatch,revocation):
    f=delivery_plan
    # timeout / lease rule is enforced by Dispatcher, not relaxed in tests.
    cfg=f['provider'].configuration
    f['provider']=HttpEffectProvider(EffectProviderConfiguration(cfg.origin,credential_file=cfg.credential_file,ca_file=cfg.ca_file,timeout=.5))
    intent,manifest=manifest_window(f,monkeypatch)
    await_original_action_expiry(admin,intent)
    before=admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s',(intent,)).fetchone()[0]
    if revocation=='source_run_expired':
        # Disposable technical authentication fault, never a business object write.
        admin.execute("update authz.nexloop_run_credentials set expires_at=clock_timestamp()-interval '1 second' where run_id=%s",(f['run'].run_id,))
    else:
        principal=f['source_principal'] if revocation=='source_send' else f['executor_principal']
        revoke_grant(f,admin,principal,'eios:action:nexloop.service.request:1')
    assert f['authority'].recover_one(f['store'],manifest) is False
    assert not (f['root']/(intent+'.export.json')).exists()
    assert admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s',(intent,)).fetchone()[0]==before
    assert admin.execute('select count(*) from runtime.nexloop_local_delivery_recoveries where intent_id=%s',(intent,)).fetchone()==(0,)
    assert admin.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==(False,)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger where control_id=%s',(f['control'],)).fetchone()==(1,1)


def test_actual_expired_effect_lease_post_does_not_materialize_any_manifest(delivery_plan,admin):
    from nexloop_eios.effect_provider import EffectProviderUnknown
    f=delivery_plan;intent=submit(f)['intent_id'];job=f['executor'].claim_effect(lease_seconds=3)
    admitted=f['executor'].prepare_effect_dispatch(intent_id=intent,fence=job['fence'],provider_profile_digest=f['provider'].profile_digest)
    await_original_action_expiry(admin,intent)
    with live_delivery(f),pytest.raises(EffectProviderUnknown):
        f['provider'].dispatch(intent_id=intent,payload_digest=admitted['provider_payload_digest'],parameters=admitted['parameters'])
    assert not (f['root']/(intent+'.manifest.json')).exists()
    assert not (f['root']/(intent+'.export.json')).exists()
    assert admin.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==(False,)


def test_actual_same_signed_guard_tail_expiry_does_not_claim_business_success(delivery_plan,admin,monkeypatch):
    from nexloop_eios.local_json_delivery import PROTOCOL
    from nexloop_eios.postgres_artifacts import canonical_payload
    import hmac
    f=delivery_plan;intent=submit(f)['intent_id'];authority=f['authority']
    original_envelope=authority._envelope
    def short_proof(port,verb,target,intent_id,**args):
        text,signature,payload=original_envelope(port,verb,target,intent_id,**args)
        if verb=='deliver':
            proof=json.loads(text);proof['expires_at']=(datetime.now(UTC)+timedelta(seconds=.5)).isoformat()
            text=canonical_payload(proof);signature=hmac.new(port.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        return text,signature,payload
    monkeypatch.setattr(authority,'_envelope',short_proof)
    original_immutable=f['store']._immutable
    def delayed(name,body):
        original_immutable(name,body)
        if name.endswith('.export.json'):time.sleep(.65)
    monkeypatch.setattr(f['store'],'_immutable',delayed)
    with live_delivery(f):
        result=EffectDispatcher(f['executor'],f['provider'],lease_seconds=30).run_once()
    assert result['business_action_success'] is False
    assert (f['root']/(intent+'.export.json')).exists()  # Real effect, not erased to pretend no send.
    assert admin.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==(False,)
    assert admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s',(intent,)).fetchone()[0]['state']=='active'


def test_actual_recovery_reserve_tail_expiry_rolls_back_claim_attempt_and_marker(delivery_plan,admin,monkeypatch):
    from nexloop_eios.local_json_delivery import PROTOCOL
    from nexloop_eios.postgres_artifacts import canonical_payload
    import hmac
    f=delivery_plan;cfg=f['provider'].configuration
    f['provider']=HttpEffectProvider(EffectProviderConfiguration(cfg.origin,credential_file=cfg.credential_file,ca_file=cfg.ca_file,timeout=.5))
    intent,manifest=manifest_window(f,monkeypatch)
    await_original_action_expiry(admin,intent)
    claim_before=admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s',(intent,)).fetchone()[0]
    attempt_before=admin.execute('select attempt_revision,effect_fence,action_claim_revision,action_fencing_token from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()
    original=f['authority']._envelope
    def short(port,verb,target,intent_id,**parameters):
        text,sig,payload=original(port,verb,target,intent_id,**parameters)
        if verb=='recover_reserve':
            c=json.loads(text);c['expires_at']=(datetime.now(UTC)+timedelta(seconds=.5)).isoformat()
            text=canonical_payload(c);sig=hmac.new(port.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        return text,sig,payload
    monkeypatch.setattr(f['authority'],'_envelope',short)
    # Owned disposable technical fault only; delays AFTER genuine reserve and
    # attempt update, before 0051's final currentproof/expiry check.
    admin.execute("create function public.delivery_reserve_delay() returns trigger language plpgsql as $$begin perform pg_sleep(.65);return new;end$$")
    admin.execute('create trigger delivery_reserve_delay after insert on runtime.nexloop_local_delivery_recoveries for each row execute function public.delivery_reserve_delay()')
    try:assert f['authority'].recover_one(f['store'],manifest) is False
    finally:
        admin.execute('drop trigger delivery_reserve_delay on runtime.nexloop_local_delivery_recoveries')
        admin.execute('drop function public.delivery_reserve_delay()')
    assert admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s',(intent,)).fetchone()[0]==claim_before
    assert admin.execute('select attempt_revision,effect_fence,action_claim_revision,action_fencing_token from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()==attempt_before
    assert admin.execute('select count(*) from runtime.nexloop_local_delivery_recoveries where intent_id=%s',(intent,)).fetchone()==(0,)
    assert not (f['root']/(intent+'.export.json')).exists()


def _recovery_after_reserve_process(options,root,credential,profile,pipe):
    with open_backend(**options) as backend:
        store=JsonExportStore(root);authority=DeliveryAuthorityPort(backend,credential,profile)
        actual=store.deliver
        def pause(*arguments,**keywords):
            # recover_reserve committed, recover_deliver real guard admitted,
            # but no new filesystem effect yet. No false authorization callback.
            pipe.send({'event':'reserve_committed_before_product'});pipe.recv()
            return actual(*arguments,**keywords)
        store.deliver=pause
        loop=BoundedDeliveryRecovery(authority,store);loop.start()
        try:
            while True:time.sleep(.1)
        finally:loop.close();store.close()


def test_actual_sigkill_after_recovery_reserve_commit_same_intent_recovers_no_new_quota(delivery_plan,admin,tmp_path,monkeypatch):
    import multiprocessing,signal
    f=delivery_plan;cfg=f['provider'].configuration
    f['provider']=HttpEffectProvider(EffectProviderConfiguration(cfg.origin,credential_file=cfg.credential_file,ca_file=cfg.ca_file,timeout=.5))
    intent,manifest=manifest_window(f,monkeypatch)
    await_original_action_expiry(admin,intent)
    before=admin.execute('select action_claim_revision from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()[0]
    options=dict(database_url=make_conninfo(f['manifest_pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'reserve-kill-artifacts',
        signing_key_file=f['publication_paths']['backend_signing'],signing_key_id='explicit-configuration')
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=_recovery_after_reserve_process,args=(options,f['root'],f['credential_file'],f['provider'].profile_digest,child))
    process.start();child.close()
    try:
        assert parent.poll(10) and parent.recv()=={'event':'reserve_committed_before_product'}
        committed=admin.execute('select action_claim_revision from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()[0]
        assert committed>before
        assert admin.execute('select count(*) from runtime.nexloop_local_delivery_recoveries where intent_id=%s',(intent,)).fetchone()==(1,)
        assert not (f['root']/(intent+'.export.json')).exists()
        process.kill();process.join(5);assert process.exitcode==-signal.SIGKILL
    finally:
        if process.is_alive():process.kill();process.join(5)
        parent.close()
    # Respect the original committed 30-second Action lease; no test mutates it
    # or borrows an expired permit to speed up this recovery evidence.
    with recovery_cli(f,tmp_path) as reopened:
        deadline=time.monotonic()+38
        finalized=False
        while time.monotonic()<deadline:
            assert reopened.poll() is None
            finalized=admin.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()[0]
            if finalized:break
            time.sleep(.2)
        assert finalized is True and (f['root']/(intent+'.export.json')).exists()
        assert f['executor'].read_effect_receipt(intent_id=intent)['business_action_success'] is True
    assert admin.execute('select action_claim_revision from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()[0]>committed
    assert admin.execute('select count(*) from runtime.nexloop_effect_control_reservations where intent_id=%s',(intent,)).fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()==(1,)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger where control_id=%s',(f['control'],)).fetchone()==(1,1)
