"""Independent real-PG authority tail and restricted-role checks."""
import hashlib,hmac,json
from datetime import UTC,datetime,timedelta
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from test_scope_denials import denial_plan,declare_record_authority,RECORD
from test_context_artifacts import context_message,assembled_message,business_plan,configured,active_worker
from nexloop_eios.postgres_artifacts import canonical_payload


@pytest.fixture(autouse=True)
def initial_two_account_allowance(monkeypatch):
    import test_trusted_configuration_pg as initial
    original=initial.apply_manifest
    def publish(manifest,**kwargs):
        # Establish allowance before first identity; never reopen exhausted cap.
        if manifest['expected_revision']==0:
            for item in manifest['identity_allowances']:item['maximum_accounts']=2
        return original(manifest,**kwargs)
    monkeypatch.setattr(initial,'apply_manifest',publish)


def test_actual_record_insert_then_catalog_proof_expiry_rolls_back(denial_plan,admin,tmp_path,monkeypatch):
    import nexloop_eios.scope_denials as denial
    f=denial_plan;assert f['relay'].run_once()=='queued'
    admin.execute('create sequence control.denial_insert_seen')
    admin.execute("create function control.denial_insert_wait() returns trigger language plpgsql security definer as $$ begin perform nextval('control.denial_insert_seen');perform pg_sleep(2);return NEW;end $$")
    admin.execute('create trigger denial_insert_wait after insert on runtime.nexloop_scope_denials for each row execute function control.denial_insert_wait()')
    original=denial.catalog_envelope_from_hint
    def short(*args,**kwargs):
        envelope=original(*args,**kwargs);signer=args[1]
        body=json.loads(envelope['payload']);until=(datetime.now(UTC)+timedelta(seconds=1.2)).isoformat()
        for read in body['reads'].values():
            proof=json.loads(read['text']);proof['expires_at']=until
            for prop in proof['property_authorities']:prop['expires_at']=until
            read['text']=canonical_payload(proof)
            read['signature']=hmac.new(signer.material,('nexloop-object-read-v1:'+read['text']).encode(),'sha256').hexdigest()
        envelope['payload']=canonical_payload(body)
        top=json.loads(envelope['text']);top['parameters_digest']=hashlib.sha256(envelope['payload'].encode()).hexdigest()
        envelope['text']=canonical_payload(top)
        envelope['signature']=hmac.new(signer.material,('nexloop-service-catalog-v1:'+envelope['text']).encode(),'sha256').hexdigest()
        return envelope
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        monkeypatch.setattr(denial,'catalog_envelope_from_hint',short)
        with pytest.raises(denial.ScopeDenialUnavailable):
            worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'Actual rejection tail expiry.'},request_scope={'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,'requested_guarantees':['profit'],'requested_discounts':[]})
    assert admin.execute('select is_called from control.denial_insert_seen').fetchone()==(True,)
    assert admin.execute('select count(*) from runtime.nexloop_scope_denials').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_action_claims where action_name=%s',(RECORD,)).fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(0,)

@pytest.mark.parametrize('role',['nexloop_api','nexloop_domain_worker','nexloop_action_worker'])
def test_application_roles_cannot_read_or_insert_denial_directly(denial_plan,role):
    with psycopg.connect(make_conninfo(denial_plan['original']['pg'],user=role),autocommit=True) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from runtime.nexloop_scope_denials')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('insert into runtime.nexloop_scope_denials default values')

@pytest.mark.parametrize('target',['eios:function:nexloop.conversation.scope_denial:1','eios:action:nexloop.conversation.read:1'])
def test_current_human_permission_revocation_blocks_http_projection(denial_plan,admin,tmp_path,target):
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.browser_http import BrowserConfiguration
    from test_trusted_configuration_pg import private
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    f=denial_plan;o=f['original'];origin='https://denial.invalid'
    configuration=ApiConfiguration(f['dsn'],o['paths']['backend_signing'],tmp_path/'query-artifacts','explicit-configuration',BrowserConfiguration(o['paths']['identity'],private(tmp_path,'rate','c'*64),o['tenant'],o['application'],origin),execution_profile='deterministic-test')
    with TestClient(create_app(configuration),base_url=origin) as client:
        assert client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':o['paths']['password'].read_text()}).status_code==200
        url='/api/v1/messages/'+f['message']['id']+'/scope-denial'
        assert client.get(url).status_code==200
        principal=f['f']['base']['human']['principal_id']
        replace_fact(admin,o['tenant'],'grants',[principal,target],F.GrantFacts,grants=[])
        denied=client.get(url);assert denied.status_code==403
        assert f['source_token'] not in denied.text

def test_second_genuine_human_with_function_permission_cannot_read_foreign_message(denial_plan,admin,tmp_path,monkeypatch):
    import copy,uuid
    import message_driven_assembly_fixture as assembly
    from test_trusted_configuration_pg import identity_manifest,create_human,private
    from nexloop_eios.trusted_configuration import apply_manifest
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.browser_http import BrowserConfiguration
    f=denial_plan;o=f['original'];manifest=copy.deepcopy(f['manifest'])
    allowance=copy.deepcopy(o['manifest']['identity_allowances'][0]);allowance['maximum_accounts']=2
    manifest['identity_allowances']=[allowance]
    def publish():
        manifest.update(manifest_id=str(uuid.uuid4()),expected_revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(o['tenant'],)).fetchone()[0])
        apply_manifest(manifest,database_url_file=o['paths']['dsn'],signing_key_file=o['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=o['paths']['secrets'])
    publish()
    body=identity_manifest(o);body['account']['username']='second-user';body['account']['verified_email']='second@example.invalid'
    human=create_human(o,body)
    monkeypatch.setattr(assembly,'FUNCTION','nexloop.conversation.scope_denial')
    facts,application=assembly.human_declarations(o['tenant'],human)
    indexed={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']}
    indexed.update({(r['kind'],tuple(r['key'])):r for r in facts})
    manifest['authority_facts']=list(indexed.values());manifest['identity_allowances'][0]['enabled']=False;publish()
    # The real second Human has current Function permission, but never received
    # governed ConsumerOwnership/Conversation ownership.
    target='eios:function:nexloop.conversation.scope_denial:1'
    assert any(r['kind']=='grants' and r['key']==[human['principal_id'],target] and r['payload']['grants'] for r in facts)
    origin='https://denial.invalid'
    config=ApiConfiguration(f['dsn'],o['paths']['backend_signing'],tmp_path/'foreign-artifacts','explicit-configuration',BrowserConfiguration(o['paths']['identity'],private(tmp_path,'foreign-rate','c'*64),o['tenant'],o['application'],origin),execution_profile='deterministic-test')
    app=create_app(config)
    with TestClient(app,base_url=origin) as client:
        login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'second-user','password':o['paths']['password'].read_text()})
        assert login.status_code==200
        from eios.identity.sessions import BrowserSessionService
        from eios.identity.ports import TrustedIdentityOperator
        from nexloop_eios.browser_http import COOKIE
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.conversation_scope_denials import ConversationScopeDenialPort
        from nexloop_eios.conversation_messages import READ
        from eios.authz.resources import ResourceType
        store=app.state.browser_store
        op=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id=str(uuid.uuid4()),trace_id=str(uuid.uuid4()))
        inspected=BrowserSessionService(store,store,application_id=o['application'],operator=op).inspect(client.cookies.get(COOKIE))
        backend=app.state.backend
        session=authenticate_browser_business(backend._pool,inspected,world='real')
        assert session.authentication.subject_principal_id==human['principal_id']
        port=ConversationScopeDenialPort(backend._pool,session,backend._signer)
        port._bundle()  # Genuine full current Function permission actually passes.
        port._proof('eios:action:'+READ+':1',ResourceType.ACTION,canonical_payload({'verb':'receipt','message_id':f['message']['id']}))
        response=client.get('/api/v1/messages/'+f['message']['id']+'/scope-denial')
        assert response.status_code==403
        assert f['source_token'] not in response.text

def test_record_claim_outcome_mismatch_never_projects_denial(denial_plan,admin,tmp_path):
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.browser_http import BrowserConfiguration
    from test_trusted_configuration_pg import private
    from nexloop_eios.service_offerings import CatalogScopeDenied
    f=denial_plan;o=f['original'];assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        with pytest.raises(CatalogScopeDenied):
            worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'Only consistent operational claims project.'},request_scope={'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,'requested_guarantees':['profit'],'requested_discounts':[]})
    origin='https://denial.invalid'
    config=ApiConfiguration(f['dsn'],o['paths']['backend_signing'],tmp_path/'claim-query','explicit-configuration',BrowserConfiguration(o['paths']['identity'],private(tmp_path,'claim-rate','c'*64),o['tenant'],o['application'],origin),execution_profile='deterministic-test')
    with TestClient(create_app(config),base_url=origin) as client:
        assert client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':o['paths']['password'].read_text()}).status_code==200
        url='/api/v1/messages/'+f['message']['id']+'/scope-denial'
        assert client.get(url).json()['denial'] is not None
        # Technical fault injection on owned disposable Action-claim ledger only;
        # never writes formal objects or manufactures successful authorization.
        assert admin.execute("update runtime.nexloop_action_claims set claim=jsonb_set(claim,'{terminal_outcome,outcome_id}',to_jsonb('wrong-owned-test-outcome'::text)) where action_name=%s",(RECORD,)).rowcount==1
        response=client.get(url)
        assert response.status_code==403
        assert 'record_id' not in response.text
