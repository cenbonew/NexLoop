"""Real Human/49 Plan/Run/52 Artifact and53 supply through actual PG guards."""
import json
from contextlib import ExitStack
import pytest
from psycopg.conninfo import make_conninfo
from test_context_artifacts import context_message,active_worker,assembled_message,business_plan,configured
from nexloop_eios.backend import open_backend
from nexloop_eios.authorization import AuthorizationUnavailable
from nexloop_eios.effect_execution import EffectExecutionUnavailable
from nexloop_eios.service_offerings import json_export_example
from datetime import UTC,datetime,timedelta


def accepted(f,tmp_path):
    assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        body=json.loads(text)
        assert body['schema_version']=='nexloop.context-pack.v2'
        assert body['supply']['offering_id']==f['f']['recipe']['offering_id']
        model=worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model')
        assert model['authorized'] is True
        receipt=worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'Real catalog-bound private JSON export.'})
        return receipt['receipt']


@pytest.mark.parametrize('admission_kind',['message','generic'])
def test_actual_action_worker_scope_and_governed_https_export(context_message,admin,tmp_path,admission_kind):
    from test_agent_host import files,free_port
    from test_trusted_configuration_pg import private,PrivateConfiguration
    from test_local_json_delivery_pg import live_delivery
    from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
    from nexloop_eios.local_json_delivery import DeliveryAuthorityPort,DeliveryServerConfiguration,JsonExportStore
    from nexloop_eios.effect_dispatch import EffectDispatcher
    f=context_message
    if admission_kind=='message':receipt=accepted(f,tmp_path)
    else:
        generic,run_id=generic_plan_run(f['source'],f['planner'],f['f']['recipe'],f['f']['tokens']['assembly-executor'])
        receipt=generic.submit_effect_intent(parameters={'message':'Real catalog-bound private JSON export.'})
        assert admin.execute('select count(*) from authz.nexloop_message_run_issuances where run_id=%s',(run_id,)).fetchone()==(0,)
    intent=receipt['intent_id']
    o=f['original'];credential=private(tmp_path,'offering-executor',f['f']['tokens']['assembly-executor'])
    tls=tmp_path/'offering-tls';tls.mkdir(mode=0o700);unused,key=files(tls)
    origin='https://127.0.0.1:'+str(free_port())
    provider=HttpEffectProvider(EffectProviderConfiguration(origin,credential_file=str(key),ca_file=str(tls/'host-cert.pem'),timeout=2))
    root=tmp_path/'offering-export';root.mkdir(mode=0o700)
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=make_conninfo(o['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'effect-worker',signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration'))
        executor=backend.authenticate(credential.read_text(),world='real')
        store=JsonExportStore(root);stack.callback(store.close)
        server=PrivateConfiguration(server_config=DeliveryServerConfiguration(origin,tls/'host-cert.pem',tls/'host-key.pem',key),authority=DeliveryAuthorityPort(backend,credential,provider.profile_digest),store=store)
        with live_delivery(server):
            result=EffectDispatcher(executor,provider,lease_seconds=30).run_once()
            assert result['status']=='fulfilled' and result['business_action_success'] is True
            product=json.loads((root/(intent+'.export.json')).read_bytes())
            assert product['document']['body']=='Real catalog-bound private JSON export.'
            terminal=executor.read_effect_receipt(intent_id=intent)
            assert terminal['governed_claim_finalized'] is True
    assert admin.execute('select count(*) from runtime.nexloop_effect_catalog_bindings').fetchone()==(1,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(1,)


def test_runtime_source_cannot_create_catalog(context_message):
    with pytest.raises((AuthorizationUnavailable,PermissionError)):
        context_message['source'].create_object(action_name='ServiceOffering.create',action_version=1,intent_id='forbidden-model-catalog',type_name='ServiceOffering',properties=json_export_example(valid_until=(datetime.now(UTC)+timedelta(minutes=3)).isoformat()))


def test_typed_outside_terms_refused_with_current_scope_and_no_intent(context_message,admin,tmp_path):
    from nexloop_eios.service_offerings import CatalogScopeDenied
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        for guarantees,discounts in [(['profit-guarantee'],[]),([],['50%-discount'])]:
            with pytest.raises(CatalogScopeDenied) as denied:
                worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'Allowed text is not a discount grant.'},request_scope={'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,'requested_guarantees':guarantees,'requested_discounts':discounts})
            assert denied.value.scope['guarantees']==[] and denied.value.scope['discounts']==[]
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(0,)


def test_real_insert_then_catalog_read_proof_expires_rolls_back_quota(context_message,admin,tmp_path,monkeypatch):
    import nexloop_eios.service_offerings as catalog
    from nexloop_eios.postgres_artifacts import canonical_payload
    import hmac
    from nexloop_eios.effect_intents import EffectIntentUnavailable
    f=context_message;assert f['relay'].run_once()=='queued'
    admin.execute('create sequence control.offering_actual_insert_seen')
    admin.execute("create function control.offering_insert_delay() returns trigger language plpgsql security definer as $$ begin perform nextval('control.offering_actual_insert_seen');perform pg_sleep(2);return NEW;end $$")
    admin.execute('create trigger offering_insert_delay after insert on runtime.nexloop_effect_intents for each row execute function control.offering_insert_delay()')
    original=catalog._read_envelope
    def short_read(source,*args):
        envelope=original(source,*args);claims=json.loads(envelope['text'])
        until=(datetime.now(UTC)+timedelta(seconds=1.2)).isoformat()
        claims['expires_at']=until
        for proof in claims['property_authorities']:proof['expires_at']=until
        text=canonical_payload(claims);envelope['text']=text
        envelope['signature']=hmac.new(source._backend._signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()
        return envelope
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        monkeypatch.setattr(catalog,'_read_envelope',short_read)
        with pytest.raises(EffectIntentUnavailable):worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'Must roll back after actual signed catalog read expiry.'})
    assert admin.execute('select is_called from control.offering_actual_insert_seen').fetchone()==(True,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_catalog_bindings').fetchone()==(0,)


def test_actual_admit_write_then_current_catalog_read_expiry_rolls_back_attempt(context_message,admin,tmp_path,monkeypatch):
    import nexloop_eios.service_offerings as catalog
    from nexloop_eios.postgres_artifacts import canonical_payload
    import hmac
    f=context_message;receipt=accepted(f,tmp_path);o=f['original']
    admin.execute('create sequence control.offering_actual_attempt_seen')
    admin.execute("create function control.offering_attempt_delay() returns trigger language plpgsql security definer as $$ begin perform nextval('control.offering_actual_attempt_seen');perform pg_sleep(2);return NEW;end $$")
    admin.execute('create trigger offering_attempt_delay after insert on runtime.nexloop_effect_attempts for each row execute function control.offering_attempt_delay()')
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'admit-worker',signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        executor=backend.authenticate(f['f']['tokens']['assembly-executor'],world='real')
        claim=executor.claim_effect(lease_seconds=30);assert claim['intent_id']==receipt['intent_id']
        original=catalog._read_envelope
        def short_read(source,*args):
            envelope=original(source,*args);claims=json.loads(envelope['text']);until=(datetime.now(UTC)+timedelta(seconds=1.2)).isoformat();claims['expires_at']=until
            for proof in claims['property_authorities']:proof['expires_at']=until
            envelope['text']=canonical_payload(claims);envelope['signature']=hmac.new(source._backend._signer.material,('nexloop-object-read-v1:'+envelope['text']).encode(),'sha256').hexdigest()
            return envelope
        monkeypatch.setattr(catalog,'_read_envelope',short_read)
        with pytest.raises(EffectExecutionUnavailable):executor.prepare_effect_dispatch(intent_id=claim['intent_id'],fence=claim['fence'],provider_profile_digest='a'*64)
    assert admin.execute('select is_called from control.offering_actual_attempt_seen').fetchone()==(True,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_provider_bindings').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(1,)


def test_actual_https_scope_denial_is_bounded_current_read_no_receipt(context_message,admin,tmp_path):
    import threading,httpx,secrets
    from test_agent_host import files
    from test_trusted_configuration_pg import private
    from nexloop_eios.runtime_control import create_runtime_guard_server
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        tls=tmp_path/'scope-tls';tls.mkdir(mode=0o700);files(tls)
        key=private(tmp_path,'scope-transport-key',secrets.token_hex(32))
        server=create_runtime_guard_server(worker,port=0,key_file=key,certificate_file=tls/'host-cert.pem',tls_key_file=tls/'host-key.pem')
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05});thread.start()
        try:
            with httpx.Client(verify=str(tls/'host-cert.pem'),trust_env=False,timeout=5) as client:
                response=client.post('https://127.0.0.1:'+str(server.server_port)+'/internal/v1/runtime/effects/submit',headers={'Authorization':'Bearer '+key.read_text()},json={'activation_ref':activation['activation_ref'],'command':command,'parameters':{'message':'Do not grant an invented discount.'},'request_scope':{'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,'requested_guarantees':[],'requested_discounts':['outside-discount']}})
            assert response.status_code==403
            denied=response.json();assert set(denied)=={'code','scope'} and denied['code']=='outside_catalog_terms'
            assert set(denied['scope'])=={'service_code','deliverable','price_amount','currency','guarantees','discounts','limitations','evidence_kind'}
            assert denied['scope']['guarantees']==[] and denied['scope']['discounts']==[]
            assert f['source_token'] not in response.text and key.read_text() not in response.text
        finally:
            server.shutdown();server.server_close();thread.join(5);assert not thread.is_alive()
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)


def generic_plan_run(source,planner,recipe,executor_token):
    import uuid
    key=str(uuid.uuid4())
    goal=planner.create_object(action_name='Goal.create',action_version=1,intent_id=key+'-goal',type_name='Goal',properties={'consumer_id':recipe['consumer_id'],'state':'active','valid_until':recipe['valid_until']})
    step=planner.create_object(action_name='PlanStep.create',action_version=1,intent_id=key+'-step',type_name='PlanStep',properties={'consumer_id':recipe['consumer_id'],'goal_id':goal['object_id'],'control_id':recipe['control_id'],'submitter_principals':[source._session.authentication.subject_principal_id],'action_name':'nexloop.service.request','state':'ready'})
    run=source.issue_run_credential(action_resources=['eios:action:nexloop.service.request:1'])
    planner.bind_effect_context(step_id=step['object_id'],step_revision=1,goal_revision=1,consumer_revision=recipe['consumer_revision'],control_revision=recipe['control_revision'],run_id=run.run_id,run_token=run.token,executor_token=executor_token)
    return source._backend.authenticate_run(run.token,world='real',run_id=run.run_id),run.run_id


def test_genuine_generic_run_governed_catalog_no_message_or_artifact_required(context_message,admin):
    f=context_message
    generic,run_id=generic_plan_run(f['source'],f['planner'],f['f']['recipe'],f['f']['tokens']['assembly-executor'])
    result=generic.submit_effect_intent(parameters={'message':'Generic lawful service uses the formal offering.'})
    assert result['state']=='accepted' and result['business_action_success'] is False
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances where run_id=%s',(run_id,)).fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings where run_id=%s',(run_id,)).fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_catalog_bindings').fetchone()==(1,)


@pytest.mark.parametrize('assembled_message',[False],indirect=True)
def test_genuine_generic_run_missing_catalog_is_denied(assembled_message,admin,tmp_path):
    from pathlib import Path
    from nexloop_eios.effect_intents import EffectIntentUnavailable
    f=assembled_message;o=f['original']
    assert 'offering_id' not in f['recipe'] and 'offering_binding_id' not in f['recipe']
    assert admin.execute("select count(*) from ontology.objects where type_name in ('ServiceOffering','ConsumerServiceOffering')").fetchone()==(0,)
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_api'),artifact_root=tmp_path/'generic-no-catalog',signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        source=backend.authenticate(f['tokens']['assembly-source'],world='real');planner=backend.authenticate(f['tokens']['assembly-planner'],world='real')
        generic,run_id=generic_plan_run(source,planner,f['recipe'],f['tokens']['assembly-executor'])
        with pytest.raises(EffectIntentUnavailable):generic.submit_effect_intent(parameters={'message':'No unregistered offering shortcut.'})
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances where run_id=%s',(run_id,)).fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(0,)


@pytest.mark.parametrize('patch',[{'active':False},{'price_amount':'1'},{'title':'A newly approved revision requires a new matching plan.'}])
def test_governed_catalog_edit_current_revision_denies_model_tool_no_quota(context_message,admin,tmp_path,patch):
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        changed=f['editor'].edit_object(action_name='ServiceOffering.edit',action_version=1,intent_id='approved-catalog-edit',type_name='ServiceOffering',object_id=f['f']['recipe']['offering_id'],expected_revision=1,properties=patch)
        assert changed['revision']==2
        with pytest.raises(AuthorizationUnavailable):worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model')
        from nexloop_eios.effect_intents import EffectIntentUnavailable
        with pytest.raises(EffectIntentUnavailable):worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'Old terms must not remain executable.'})
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(0,)


def test_governed_catalog_edit_after_accept_denies_send_before_any_attempt(context_message,admin,tmp_path):
    f=context_message;receipt=accepted(f,tmp_path);o=f['original']
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'changed-offer-worker',signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        executor=backend.authenticate(f['f']['tokens']['assembly-executor'],world='real')
        claim=executor.claim_effect(lease_seconds=30)
        assert claim['intent_id']==receipt['intent_id']
        changed=f['editor'].edit_object(action_name='ServiceOffering.edit',action_version=1,intent_id='withdraw-catalog-before-send',type_name='ServiceOffering',object_id=f['f']['recipe']['offering_id'],expected_revision=1,properties={'active':False})
        assert changed['revision']==2
        with pytest.raises(EffectExecutionUnavailable):executor.prepare_effect_dispatch(intent_id=claim['intent_id'],fence=claim['fence'],provider_profile_digest='a'*64)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_provider_bindings').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(1,)


def test_genuine_generic_same_slot_changed_terms_never_creates_new_intent(context_message,admin):
    from nexloop_eios.effect_intents import EffectIntentConflict
    f=context_message;generic,run_id=generic_plan_run(f['source'],f['planner'],f['f']['recipe'],f['f']['tokens']['assembly-executor'])
    first=generic.submit_effect_intent(parameters={'message':'Stable business intent survives catalog revision.'})
    f['editor'].edit_object(action_name='ServiceOffering.edit',action_version=1,intent_id='revise-offering-title',type_name='ServiceOffering',object_id=f['f']['recipe']['offering_id'],expected_revision=1,properties={'title':'Revised governed export offering'})
    f['editor'].edit_object(action_name='ConsumerServiceOffering.edit',action_version=1,intent_id='approve-offering-revision',type_name='ConsumerServiceOffering',object_id=f['f']['recipe']['offering_binding_id'],expected_revision=1,properties={'offering_revision':2})
    properties=admin.execute('select properties from ontology.objects where object_id=%s',(f['f']['recipe']['offering_binding_id'],)).fetchone()[0]
    assert set(properties)=={'consumer_id','offering_id','offering_revision','source_principal','active'}, sorted(properties)
    with pytest.raises(EffectIntentConflict):generic.submit_effect_intent(parameters={'message':'Stable business intent survives catalog revision.'})
    assert admin.execute('select intent_id from runtime.nexloop_effect_intents').fetchone()[0].__str__()==first['intent_id']
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
    assert admin.execute('select offering_revision from runtime.nexloop_effect_catalog_bindings').fetchone()==(1,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(1,)


def test_signed_catalog_cross_consumer_and_unrelated_source_denied(context_message):
    import psycopg
    from nexloop_eios.service_offerings import _catalog_envelope
    f=context_message;source=f['source']
    scope={'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,'requested_guarantees':[],'requested_discounts':[]}
    for consumer,digest in [('f'*64,source._session.token_digest),(f['f']['recipe']['consumer_id'],f['editor']._session.token_digest)]:
        envelope=_catalog_envelope(source,offering_id=scope['offering_id'],binding_id=f['f']['recipe']['offering_binding_id'],consumer_id=consumer,request_scope=scope)
        with source._backend._pool.connection() as db,db.transaction(),pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select authz.nexloop_service_catalog_scope(%s,%s,%s,%s,%s)',(digest,'real',*envelope))


def test_catalog_partial_edit_preserves_fields_and_runtime_or_revoked_editor_cannot_edit(context_message,admin):
    from eios.authz import facts as F
    from authority_fixture import replace_fact
    f=context_message;object_id=f['f']['recipe']['offering_id']
    before=admin.execute('select properties from ontology.objects where object_id=%s',(object_id,)).fetchone()[0]
    updated=f['editor'].edit_object(action_name='ServiceOffering.edit',action_version=1,intent_id='partial-maintenance-title',type_name='ServiceOffering',object_id=object_id,expected_revision=1,properties={'title':'Governed maintenance'})
    assert updated['revision']==2
    after=admin.execute('select properties from ontology.objects where object_id=%s',(object_id,)).fetchone()[0]
    assert after=={**before,'title':'Governed maintenance'}
    with pytest.raises((PermissionError,AuthorizationUnavailable)):
        f['source'].edit_object(action_name='ServiceOffering.edit',action_version=1,intent_id='runtime-cannot-set-price',type_name='ServiceOffering',object_id=object_id,expected_revision=2,properties={'price_amount':'99'})
    editor=f['editor'];replace_fact(admin,f['original']['tenant'],'grants',[editor._session.authentication.subject_principal_id,'eios:action:ServiceOffering.edit:1'],F.GrantFacts,grants=[])
    with pytest.raises((PermissionError,AuthorizationUnavailable)):
        editor.edit_object(action_name='ServiceOffering.edit',action_version=1,intent_id='revoked-maintenance',type_name='ServiceOffering',object_id=object_id,expected_revision=2,properties={'price_amount':'99'})
    assert admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(object_id,)).fetchone()==(after,2)
    import psycopg
    with f['source']._backend._pool.connection() as db,db.transaction(),pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("select authz.nexloop_effect_intent_command_v0052('x','real','{}','x','{}')")
