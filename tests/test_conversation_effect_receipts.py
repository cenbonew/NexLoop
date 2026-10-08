"""Candidate-only real Function/Human/PG receipts; no model/channel network.

Synthetic authority/publication setup only. Objects, message routing, Intent
admission and Action terminal evidence use their actual governed backend ports.
"""
from datetime import UTC,datetime
import hashlib,json,uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.applications import ResourceRestriction
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.definitions import FunctionDefinition
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import CapabilityContractSnapshot,validate_capability_binding
from nexloop_eios.backend import open_backend
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.conversation_effect_receipts import ConversationEffectReceiptPort,FUNCTION,QUERY_CAPABILITY
from nexloop_eios.conversation_messages import ConversationDenied,READ
from nexloop_eios.conversation_runtime_bridge import ConversationRuntimeBridge
from nexloop_eios.postgres_artifacts import canonical_payload
from authority_fixture import authority_records,replace_fact
from message_runtime_fixture import message_plan


def schemas():
    nullable={'type':['string','null']}
    receipt={'type':'object','additionalProperties':False,'properties':{
        'intent_id':{'type':'string'},'receipt_id':{'type':'string'},
        'state':{'enum':['accepted','dispatching','unknown','observed_fulfilled','fulfilled','failed','confirmed']},
        'scope':{'const':'service_delivery'},'provider_state':{'enum':['accepted','fulfilled','not_found',None]},
        'governed_claim_finalized':{'type':'boolean'},'business_action_success':{'type':'boolean'}},
        'required':['intent_id','receipt_id','state','scope','provider_state','governed_claim_finalized','business_action_success']}
    run={'type':'object','additionalProperties':False,'properties':{key:{'type':'string'} for key in ['run_id','request_id','task_id','status']},'required':['run_id','request_id','task_id','status']}
    input_schema={'type':'object','additionalProperties':False,'properties':{'message_id':{'type':'string','minLength':64,'maxLength':64}},'required':['message_id']}
    output_schema={'type':'object','additionalProperties':False,'properties':{'message_id':{'type':'string'},'run':{'anyOf':[{'type':'null'},run]},'receipt':{'anyOf':[{'type':'null'},receipt]}},'required':['message_id','run','receipt']}
    return input_schema,output_schema


@pytest.fixture
def receipt_plan(message_plan,admin):
    f=message_plan;tenant=f['tenant'];target='eios:function:'+FUNCTION+':1'
    old=authenticate_browser_business(f['api']._pool,f['issued'].session,world='real')
    binding,_,records=authority_records(tenant,target,operation=Operation.EXECUTE,resource_type=ResourceType.FUNCTION,identity_suffix='-human')
    def convert(value):
        if isinstance(value,dict):return {key:convert(item) for key,item in value.items()}
        if isinstance(value,list):return [convert(item) for item in value]
        if value==binding.subject_id:return old.authentication.subject_id
        if value==binding.subject_principal_id:return old.authentication.subject_principal_id
        if value=='service':return 'human'
        return value
    # Canonical synthetic permission configuration, never formal data writes.
    for kind,key,fact in records:
        if kind not in ('resource_graph','grants','scope','controls','policies'):continue
        body=convert(fact.model_dump(mode='json'));body.pop('snapshot_digest',None)
        actual=type(fact).model_validate_json(json.dumps(body))
        admin.execute('insert into authz.nexloop_authority_facts values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
            (tenant,kind,convert(key),Jsonb(actual.model_dump(mode='json'))))
    for entity_key,body in admin.execute("select entity_key,payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='scope' and entity_key[1]=%s",(tenant,old.authentication.subject_principal_id)).fetchall():
        body.pop('snapshot_digest',None);body['catalog_scopes']=['action.execute','function.execute'];body['authorized_scopes']=body['catalog_scopes']
        body['catalog_digest']=F.canonical_authority_digest({'scopes':body['catalog_scopes']})
        scope=F.ScopeAuthorityFacts.model_validate_json(json.dumps(body))
        admin.execute("update authz.nexloop_authority_facts set payload=%s where tenant_id=%s and fact_kind='scope' and entity_key=%s",(Jsonb(scope.model_dump(mode='json')),tenant,entity_key))
    appkey=[old.authentication.caller_application_id,'1']
    row=admin.execute("select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='application' and entity_key=%s",(tenant,appkey)).fetchone()[0]
    app=F.ApplicationFacts.model_validate_json(json.dumps(row));resources=app.resources+(ResourceRestriction(tenant_id=tenant,resource_type='function',resource_id=target),)
    digest=F.canonical_authority_digest({'resources':[r.model_dump(mode='json') for r in resources]})
    body=app.model_dump(mode='json');body.pop('snapshot_digest',None)
    body.update(resources=[r.model_dump(mode='json') for r in resources],version_digest=digest,record_digest=digest)
    app=F.ApplicationFacts.model_validate_json(json.dumps(body))
    admin.execute("update authz.nexloop_authority_facts set payload=%s where tenant_id=%s and fact_kind='application' and entity_key=%s",(Jsonb(app.model_dump(mode='json')),tenant,appkey))
    admin.execute('update control.nexloop_browser_business_applications set requested_scopes=%s where tenant_id=%s',(Jsonb(['action.execute','function.execute']),tenant))
    rows=admin.execute("select type_name,version,definition from ontology.object_type_versions where tenant_id=%s and type_name in ('Consumer','Conversation','Message') order by type_name",(tenant,)).fetchall()
    from eios.ontology.models import ObjectTypeDefinition
    refrows=[]
    for name,version,body in rows:
        actual=ObjectTypeDefinition.model_validate_json(json.dumps(body));ref={'tenant_id':tenant,'schema_type':'object_type','stable_name':name,'version':version,'schema_digest':schema_contract_digest(actual)}
        refrows.append({'reference':ref,'definition':body})
    input_schema,output_schema=schemas();schema_hash=hashlib.sha256(canonical_payload({'input':input_schema,'output':output_schema}).encode()).hexdigest()
    capability=CapabilityContractSnapshot.model_validate_json(json.dumps({'capability_name':QUERY_CAPABILITY,'capability_version':'1','schema_hash':schema_hash,
        'kind':'atomic','has_side_effects':False,'idempotent':True,'required_scopes':['function.execute']}))
    definition=FunctionDefinition.model_validate_json(json.dumps({'tenant_id':tenant,'definition_type':'function','stable_name':FUNCTION,'version':1,'status':'published',
        'created_by':'synthetic-definition-owner','created_at':datetime.now(UTC).isoformat(),'required_scopes':['function.execute'],
        'capability_binding':{'capability_name':QUERY_CAPABILITY,'capability_version':'1','schema_hash':schema_hash},
        'applies_to':[{'object_type':entry['reference']} for entry in refrows],'input_schema':input_schema,'output_schema':output_schema}))
    validate_capability_binding(definition,capability)
    admin.execute('insert into control.nexloop_function_definitions values(%s,%s,%s,%s,%s,%s,true)',
        (tenant,'real',target,Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json')),Jsonb(refrows)))
    # Actual authority configuration changes realm epochs. Reissue/rebind a real
    # Run using original authentic service credentials, never a copied identity.
    source=f['api'].authenticate(f['source_token'],world='real')
    run=source.issue_run_credential(action_resources=['eios:action:nexloop.service.request:1'])
    planner=f['api'].authenticate(f['planner_token'],world='real')
    planner.bind_effect_context(step_id=f['step'],step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,
        run_id=run.run_id,run_token=run.token,executor_token=f['executor_token'])
    f['run']=run;f['command']={**f['command'],'run_id':run.run_id,'credential_ref':'run_credential:'+run.run_id,'not_after':run.expires_at.isoformat()}
    f['bridge']=ConversationRuntimeBridge(f['api'].authenticate(f['owner_token'],world='real'))
    def query():return f['api'].authenticate_browser(f['issued'].session).read_message_service_receipt(message_id=f['message']['id'])
    f['receipt_query']=query
    yield f


def routed(f):
    f['bridge'].bind_message(message_id=f['message']['id'],run_token=f['run'].token,command=f['command'])
    return f['bridge'].deliver_one(run_token=f['run'].token)


def accepted(f):
    routed(f)
    return f['api'].authenticate_run(f['run'].token,world='real',run_id=f['run'].run_id).submit_effect_intent(parameters={'message':'one synthetic governed service'})


def test_no_route_no_ack_returns_owned_null_without_fabricated_receipt(receipt_plan):
    f=receipt_plan
    empty={'message_id':f['message']['id'],'run':None,'receipt':None}
    assert f['receipt_query']()==empty
    f['bridge'].bind_message(message_id=f['message']['id'],run_token=f['run'].token,command=f['command'])
    assert f['receipt_query']()==empty
    delivered=f['bridge'].deliver_one(run_token=f['run'].token)
    value=f['receipt_query']();assert value['receipt'] is None
    assert value['run']['run_id']==delivered['run_id'] and value['run']['task_id']==delivered['task_id']


def test_actual_intent_accepted_is_not_service_fulfilled(receipt_plan):
    f=receipt_plan;intent=accepted(f);receipt=f['receipt_query']()['receipt']
    assert receipt['intent_id']==intent['intent_id'] and receipt['state']=='accepted'
    assert receipt['business_action_success'] is False and receipt['governed_claim_finalized'] is False and receipt['provider_state'] is None


def test_original_governed_claim_required_for_business_success_no_provider_network(receipt_plan,admin,tmp_path):
    f=receipt_plan;intent=accepted(f)
    config=dict(database_url=make_conninfo(f['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'executor-artifact',signing_key_file=f['signing_key'],signing_key_id='runtime-effect')
    with open_backend(**config) as backend:
        executor=backend.authenticate(f['executor_token'],world='real');claimed=executor.claim_effect(lease_seconds=30)
        profile='c'*64;admitted=executor.prepare_effect_dispatch(intent_id=intent['intent_id'],fence=claimed['fence'],provider_profile_digest=profile)
        executor.record_effect_observation(intent_id=intent['intent_id'],fence=claimed['fence'],provider_profile_digest=profile,
            provider_payload_digest=intent['provider_payload_digest'],provider_state='fulfilled',provider_reference='synthetic-no-network-fulfillment')
    receipt=f['receipt_query']()['receipt']
    assert receipt['state']=='fulfilled' and receipt['business_action_success'] is True and receipt['governed_claim_finalized'] is True
    assert admin.execute("select claim->>'state',claim->'terminal_outcome'->>'status' from runtime.nexloop_action_claims where action_name='nexloop.service.request'").fetchone()==('terminal','succeeded')
    # This proves Query consistency with actual governed synthetic evidence,
    # not real provider delivery. No model/channel transport is exercised.


def test_wrong_owner_and_revoke_either_current_permission_denied(receipt_plan,admin):
    f=receipt_plan;routed(f)
    service=f['api'].authenticate_browser(f['issued'].session)
    with pytest.raises(ConversationDenied):service.read_message_service_receipt(message_id='f'*64)
    principal=f['issued'].session.principal_id
    replace_fact(admin,f['tenant'],'grants',[principal,'eios:function:'+FUNCTION+':1'],F.GrantFacts,grants=[])
    with pytest.raises(ConversationDenied):f['api'].authenticate_browser(f['issued'].session).read_message_service_receipt(message_id=f['message']['id'])


def test_service_cannot_query_as_human_and_table_or_function_bypass_denied(receipt_plan):
    f=receipt_plan
    with pytest.raises(ConversationDenied):ConversationEffectReceiptPort(f['api']._pool,f['api'].authenticate(f['owner_token'],world='real')._session,f['api']._signer)
    with psycopg.connect(make_conninfo(f['pg'],user='nexloop_api')) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():db.execute('select * from control.nexloop_function_definitions')
    with psycopg.connect(make_conninfo(f['pg'],user='nexloop_domain_worker')) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():db.execute("select authz.nexloop_conversation_effect_receipt_query('x','real','{}','x','{}')")


def test_published_function_same_version_is_immutable(receipt_plan,admin):
    f=receipt_plan
    with pytest.raises(psycopg.errors.DataException),admin.transaction():
        admin.execute("update control.nexloop_function_definitions set definition=jsonb_set(definition,'{input_schema}', '{}'::jsonb) where tenant_id=%s",(f['tenant'],))
    assert f['receipt_query']()['receipt'] is None


def test_function_active_revocation_advances_epoch_and_denies_fresh_query(receipt_plan,admin):
    f=receipt_plan;assert f['receipt_query']()['receipt'] is None
    before=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['tenant'],)).fetchone()
    admin.execute('update control.nexloop_function_definitions set active=false where tenant_id=%s',(f['tenant'],))
    after=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['tenant'],)).fetchone()
    assert after[0]>before[0]
    with pytest.raises(ConversationDenied):f['receipt_query']()


def execution_backend(f,tmp_path):
    return open_backend(database_url=make_conninfo(f['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'query-executor',
        signing_key_file=f['signing_key'],signing_key_id='runtime-effect')


def test_unknown_result_never_becomes_business_success(receipt_plan,tmp_path):
    f=receipt_plan;intent=accepted(f)
    with execution_backend(f,tmp_path) as backend:
        executor=backend.authenticate(f['executor_token'],world='real');claim=executor.claim_effect(lease_seconds=30)
        executor.prepare_effect_dispatch(intent_id=intent['intent_id'],fence=claim['fence'],provider_profile_digest='c'*64)
        executor.record_effect_unknown(intent_id=intent['intent_id'],fence=claim['fence'])
    receipt=f['receipt_query']()['receipt']
    assert receipt['state']=='unknown' and receipt['provider_state'] is None
    assert receipt['business_action_success'] is False and receipt['governed_claim_finalized'] is False


def test_observed_fulfilled_after_source_revoke_remains_not_governed_success(receipt_plan,admin,tmp_path):
    f=receipt_plan;intent=accepted(f)
    with execution_backend(f,tmp_path) as backend:
        executor=backend.authenticate(f['executor_token'],world='real');claim=executor.claim_effect(lease_seconds=30)
        executor.prepare_effect_dispatch(intent_id=intent['intent_id'],fence=claim['fence'],provider_profile_digest='c'*64)
        admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s",(f['run'].run_id,))
        executor=backend.authenticate(f['executor_token'],world='real')
        value=executor.record_effect_observation(intent_id=intent['intent_id'],fence=claim['fence'],provider_profile_digest='c'*64,
            provider_payload_digest=intent['provider_payload_digest'],provider_state='fulfilled',provider_reference='synthetic-observation-no-network')
        assert value['state']=='observed_fulfilled' and value['business_action_success'] is False
    receipt=f['receipt_query']()['receipt']
    assert receipt['state']=='observed_fulfilled' and receipt['provider_state']=='fulfilled'
    assert receipt['business_action_success'] is False and receipt['governed_claim_finalized'] is False
    assert admin.execute("select claim->>'state' from runtime.nexloop_action_claims where action_name='nexloop.service.request'").fetchone()[0]!='terminal'


def test_current_read_ownership_grant_revoke_denies_despite_valid_function(receipt_plan,admin):
    f=receipt_plan;routed(f);assert f['receipt_query']()['run'] is not None
    principal=f['issued'].session.principal_id
    replace_fact(admin,f['tenant'],'grants',[principal,'eios:action:'+READ+':1'],F.GrantFacts,grants=[])
    with pytest.raises(ConversationDenied):f['receipt_query']()


def test_current_send_only_has_no_read_only_function_permission(receipt_plan,admin):
    f=receipt_plan;intent=accepted(f);principal=f['issued'].session.principal_id
    # Revoke only the genuine Function grant. Service.request was accepted via
    # the independently authenticated Run; it gives the Human no Query grant.
    replace_fact(admin,f['tenant'],'grants',[principal,'eios:function:'+FUNCTION+':1'],F.GrantFacts,grants=[])
    with pytest.raises(ConversationDenied):f['receipt_query']()
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(intent['intent_id'],)).fetchone()[0]=='accepted'


def test_signed_wrong_world_cannot_read_business_receipt(receipt_plan):
    f=receipt_plan
    port=ConversationEffectReceiptPort(f['api']._pool,authenticate_browser_business(f['api']._pool,f['issued'].session,world='real'),f['api']._signer)
    payload=canonical_payload({'verb':'bundle'});claims=port._proof('eios:function:'+FUNCTION+':1',ResourceType.FUNCTION,payload)
    text,signature=port._envelope(claims)
    with port.pool.connection() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():
            db.execute('select authz.nexloop_conversation_effect_receipt_query(%s,%s,%s,%s,%s)',(port.session.token_digest,'shadow',text,signature,payload))


def test_http_current_cookie_projects_actual_function_receipt_and_rechecks_revoke(receipt_plan,admin,tmp_path):
    import secrets
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.browser_http import BrowserConfiguration,COOKIE
    f=receipt_plan;intent=accepted(f);origin='https://receipt.example'
    def private(name,value):
        path=tmp_path/name;path.write_text(value);path.chmod(0o600);return path
    config=ApiConfiguration(private('http-api-dsn',make_conninfo(f['pg'],user='nexloop_api')),f['signing_key'],tmp_path/'http-artifacts','runtime-effect',
        BrowserConfiguration(private('http-identity-dsn',make_conninfo(f['pg'],user='nexloop_identity')),private('http-rate-key',secrets.token_hex(32)),f['tenant'],'synthetic-webchat',origin),
        execution_profile='deterministic-test',conversation_stream_seconds=1)
    path='/api/v1/messages/'+f['message']['id']+'/receipt'
    with TestClient(create_app(config),base_url=origin) as client:
        assert client.get(path).status_code==401
        client.cookies.set(COOKIE,f['issued'].session_token.get_secret_value())
        actual=client.get(path);assert actual.status_code==200
        assert actual.json()['receipt']['intent_id']==intent['intent_id']
        assert actual.json()['receipt']['state']=='accepted' and actual.json()['receipt']['business_action_success'] is False
        assert client.get(path,headers={'Origin':'https://other.example'}).status_code==403
        replace_fact(admin,f['tenant'],'grants',[f['issued'].session.principal_id,'eios:function:'+FUNCTION+':1'],F.GrantFacts,grants=[])
        assert client.get(path).status_code==403
