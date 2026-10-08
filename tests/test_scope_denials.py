"""Owned clean PostgreSQL; governed facts only, no mock authorization/provider."""
import copy,hashlib,json,uuid
from pathlib import Path
import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.definitions import ActionDefinition
from nexloop_eios.scope_denials import RECORD,record_schema
from nexloop_eios.service_offerings import CatalogScopeDenied
from nexloop_eios.trusted_configuration import apply_manifest
from nexloop_eios.message_relay import MessageRelay
from test_context_artifacts import context_message,assembled_message,business_plan,configured,active_worker
import test_context_artifacts as context_fixture

def record_source_declarations(monkeypatch):
    original=context_fixture.source_declarations
    def declarations(tenant,catalog_targets=(),*,custom_specs=None,identity_suffix='-assembly-source'):
        if custom_specs is None:
            custom_specs=[('eios:action:nexloop.service.request:1',ResourceType.ACTION,Operation.EXECUTE),
                ('eios:action:'+context_fixture.ACTION+':1',ResourceType.ACTION,Operation.EXECUTE),
                ('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.CREATE),
                ('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.READ),
                ('eios:action:'+RECORD+':1',ResourceType.ACTION,Operation.EXECUTE)]
        return original(tenant,catalog_targets,custom_specs=custom_specs,identity_suffix=identity_suffix)
    # Typed configuration builder only; all decisions remain actual EIOS/PG.
    monkeypatch.setattr(context_fixture,'source_declarations',declarations)

@pytest.fixture(autouse=True)
def declare_record_authority(monkeypatch):
    record_source_declarations(monkeypatch)

@pytest.fixture
def denial_plan(context_message,admin):
    f=context_message;o=f['original'];manifest=copy.deepcopy(f['manifest'])
    base=next(a for a in manifest['actions'] if a['definition']['stable_name']=='nexloop.plan.bind_effect_context')
    body=copy.deepcopy(base['definition']);body.pop('contract_digest',None)
    body.update(stable_name=RECORD,input_schema=record_schema())
    body['capability_binding']['capability_name']=RECORD
    definition=ActionDefinition.model_validate_json(json.dumps(body))
    manifest['actions'].append({'definition':definition.model_dump(mode='json'),
        'capability':{**base['capability'],'capability_name':RECORD}})
    base_function=next(item for item in manifest['functions'] if item['definition']['stable_name']=='nexloop.conversation.service_receipt')
    publication=copy.deepcopy(base_function)
    publication['definition'].pop('contract_digest',None)
    publication['definition']['stable_name']='nexloop.conversation.scope_denial'
    from nexloop_eios.conversation_scope_denials import query_schemas
    input_schema,output=query_schemas()
    publication['definition']['input_schema']=input_schema
    publication['definition']['output_schema']=output
    from nexloop_eios.postgres_artifacts import canonical_payload
    sh=hashlib.sha256(canonical_payload({'input':publication['definition']['input_schema'],'output':output}).encode()).hexdigest()
    for data in (publication['capability'],publication['definition']['capability_binding']):data.update(capability_name='nexloop.conversation.scope_denials.read',schema_hash=sh)
    from eios.ontology.definitions import FunctionDefinition
    publication['definition']=FunctionDefinition.model_validate_json(json.dumps(publication['definition'])).model_dump(mode='json')
    manifest['functions'].append(publication)
    manifest.update(manifest_id=str(uuid.uuid4()),expected_revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(o['tenant'],)).fetchone()[0])
    apply_manifest(manifest,database_url_file=o['paths']['dsn'],signing_key_file=o['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=o['paths']['secrets'])
    # Migration0056 is applied by the registered clean bootstrap, once.
    f['source']=f['backend'].authenticate(f['source_token'],world='real')
    f['route']=f['backend'].authenticate(f['f']['tokens']['assembly-route'],world='real')
    f['planner']=f['backend'].authenticate(f['f']['tokens']['assembly-planner'],world='real')
    f['relay']=MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],vault=f['vault'],recipe=f['f']['recipe'])
    yield f


def test_actual_runtime_denial_commits_once_without_intent_quota(denial_plan,admin,tmp_path):
    f=denial_plan;assert f['relay'].run_once()=='queued'
    scope={'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,
        'requested_guarantees':['profit-guarantee'],'requested_discounts':[]}
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        for unused in range(2):
            with pytest.raises(CatalogScopeDenied) as denied:
                worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,
                    tool_operation='submit',parameters={'message':'A user request is not a guarantee.'},request_scope=scope)
            assert denied.value.scope['guarantees']==[] and denied.value.scope['discounts']==[]
        from nexloop_eios.scope_denials import ScopeDenialConflict
        with pytest.raises(ScopeDenialConflict):
            worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,
                tool_operation='submit',parameters={'message':'Different payload must not overwrite.'},request_scope=scope)
    assert admin.execute('select count(*) from runtime.nexloop_scope_denials').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name=%s and claim->>'state'='terminal' and claim->'terminal_outcome'->>'status'='succeeded'",(RECORD,)).fetchone()==(1,)


def test_current_human_function_reads_true_denial_not_receipt(denial_plan,admin,tmp_path):
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.browser_http import BrowserConfiguration
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.conversation_scope_denials import ConversationScopeDenialPort
    from test_trusted_configuration_pg import private
    f=denial_plan;o=f['original'];origin='https://denial.invalid'
    rate=private(tmp_path,'scope-human-rate-key','c'*64)
    config=ApiConfiguration(f['dsn'],o['paths']['backend_signing'],tmp_path/'query-artifacts','explicit-configuration',
        BrowserConfiguration(o['paths']['identity'],rate,o['tenant'],o['application'],origin),execution_profile='deterministic-test')
    with TestClient(create_app(config),base_url=origin) as client:
        logged=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':o['paths']['password'].read_text()});assert logged.status_code==200
        url='/api/v1/messages/'+f['message']['id']+'/scope-denial'
        initial=client.get(url);assert initial.status_code==200
        assert initial.json()=={'message_id':f['message']['id'],'denial':None}
        assert f['relay'].run_once()=='queued'
        scope={'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,'requested_guarantees':[],'requested_discounts':['half-price']}
        with active_worker(f,tmp_path) as (worker,activation,command,text):
            with pytest.raises(CatalogScopeDenied):worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'Not an authorized discount.'},request_scope=scope)
        response=client.get(url);assert response.status_code==200
        result=response.json()
        assert result['denial']['code']=='outside_catalog_terms' and result['denial']['scope']['discounts']==[]
        assert set(result)=={'message_id','denial'} and 'receipt' not in result

@pytest.mark.parametrize('grant_target',['eios:action:nexloop.service.scope_denial.record:1','eios:property:ServiceOffering'])
def test_current_record_permission_is_not_inferred_from_catalog_read(denial_plan,admin,tmp_path,grant_target):
    from eios.authz import facts as F
    from authority_fixture import replace_fact
    from nexloop_eios.scope_denials import ScopeDenialUnavailable
    from nexloop_eios.effect_intents import EffectIntentUnavailable
    f=denial_plan;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        if grant_target.startswith('eios:property:'):
            target='eios:property:ServiceOffering/'+f['f']['recipe']['offering_id']+'/title'
        else:target=grant_target
        replace_fact(admin,f['original']['tenant'],'grants',[f['source']._session.authentication.subject_principal_id,target],F.GrantFacts,grants=[])
        scope={'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,'requested_guarantees':['promise'],'requested_discounts':[]}
        with pytest.raises((ScopeDenialUnavailable,EffectIntentUnavailable)):
            worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'No inference of authority.'},request_scope=scope)
    assert admin.execute('select count(*) from runtime.nexloop_scope_denials').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(0,)


def test_real_pi_distinct_tool_calls_persist_denial_and_human_http(denial_plan,admin,tmp_path):
    import secrets,sqlite3
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.browser_http import BrowserConfiguration
    from test_agent_host import files
    from test_runtime_host_admission import guard_server,host,configuration,completed
    from test_trusted_configuration_pg import private
    f=denial_plan;o=f['original'];assert f['relay'].run_once()=='queued'
    runtime,key=files(tmp_path);guard_key=private(tmp_path,'scope-guard-key',secrets.token_hex(32))
    scope={'offering_id':f['f']['recipe']['offering_id'],'offering_revision':1,
        'requested_guarantees':['profit-guarantee'],'requested_discounts':[]}
    with active_worker(f,tmp_path) as (worker,activation,command,text),guard_server(worker,tmp_path,guard_key) as port:
        config=configuration(tmp_path,port,guard_key);body=json.loads(config.read_text())
        body.update(effect_tools=True,deterministic_message_from_input=True,
            context_input_protocol='nexloop.context-pack.v2',deterministic_effect_request_scope=scope)
        config.write_text(json.dumps(body));config.chmod(0o600)
        with host(runtime,key,config) as (_,client,headers):
            started=client.post('/internal/v1/runs/start',headers=headers,
                json={'activation_ref':activation['activation_ref'],'command':command,'input':text})
            assert started.status_code==202
            import time
            deadline=time.monotonic()+20
            while True:
                inspected=client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':activation['activation_ref'],'command':command})
                if inspected.status_code==503:
                    # Read-only transient dependency recheck, not execution
                    # resubmission or authorization fallback. Permanent denial
                    # still fails this same absolute deadline.
                    assert time.monotonic()<deadline,'runtime inspect dependency unavailable beyond deadline'
                    time.sleep(.2)
                    continue
                assert inspected.status_code==200
                result=inspected.json()
                if result['submission_status']=='done':break
                assert time.monotonic()<deadline,'bounded actual Pi refusal completion deadline'
                time.sleep(.2)
            assert result['persistence']=={'journal_mode':'wal','synchronous':2}
            # Actual Pi tool errors remain errors. Operational record success is
            # neither Runtime success nor business delivery success.
            assert result['runtime_outcome']=='failed'
    database=runtime/command['run_id']/'runtime.sqlite'
    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
        records=[json.loads(row[0]) for row in db.execute('select record from entries order by id')]
    calls=[];denials=[]
    for record in records:
        for message in record.get('model',[]):
            if message.get('role')=='assistant':calls.extend(item for item in message.get('content',[]) if item.get('type')=='toolCall')
            if message.get('role')=='toolResult' and message.get('toolName')=='nexloop.service.request':
                assert message.get('isError') is True
                denials.extend(json.loads(content['text']) for content in message['content'] if content.get('type')=='text')
    assert [call['id'] for call in calls]==['message-service-first','message-service-rebuilt']
    assert len(denials)==2 and denials[0]==denials[1] and denials[0]['code']=='outside_catalog_terms'
    assert admin.execute('select count(*) from runtime.nexloop_scope_denials').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()==(0,)
    assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(0,)
    origin='https://pi-scope.invalid';rate=private(tmp_path,'pi-scope-rate','d'*64)
    api=ApiConfiguration(f['dsn'],o['paths']['backend_signing'],tmp_path/'pi-query-artifacts','explicit-configuration',
        BrowserConfiguration(o['paths']['identity'],rate,o['tenant'],o['application'],origin),execution_profile='deterministic-test')
    with TestClient(create_app(api),base_url=origin) as client:
        assert client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':o['paths']['password'].read_text()}).status_code==200
        response=client.get('/api/v1/messages/'+f['message']['id']+'/scope-denial')
        assert response.status_code==200
        assert response.json()['denial']['scope']==denials[0]['scope']
        assert response.json()['denial']['code']=='outside_catalog_terms'
        receipt=client.get('/api/v1/messages/'+f['message']['id']+'/receipt')
        assert receipt.status_code==200 and receipt.json()['receipt'] is None
    assert guard_key.read_bytes() not in database.read_bytes()
