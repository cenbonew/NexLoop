from contextlib import ExitStack
from datetime import UTC,datetime,timedelta
import secrets,uuid,time,json
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.backend import open_backend
from nexloop_eios.runtime_authority import RuntimeAuthority
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.errors import AuthorizationUnavailable
from authority_fixture import seed_authority
from agent_authority_fixture import seed_agent
from test_postgres_action_claims import governance_inputs
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest

@pytest.fixture
def authority(admin,pg,tmp_path,request):
    bootstrap(admin);tenant=str(uuid.uuid4());material=secrets.token_bytes(32)
    key=tmp_path/'synthetic-authority';key.write_bytes(material);key.chmod(0o600)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',('synthetic-runtime',material))
    target='eios:action:NexLoop.queue.operations:1'
    api_token,_=seed_authority(admin,tenant,target,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-queue-api')
    worker_token,_=seed_authority(admin,tenant,target,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-queue-worker')
    agent_token,invocation=seed_agent(admin,tenant=tenant)
    inputs=governance_inputs()
    definition=type(inputs['action_definition']).model_validate_json(inputs['action_definition'].model_dump_json().replace('synthetic-a',tenant))
    schema=ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True)
    ref=definition.object_types[0].model_copy(update={'schema_digest':schema_contract_digest(schema)})
    body=definition.model_dump(mode='json');body.pop('contract_digest',None)
    body['object_types']=[ref.model_dump(mode='json')];body['governance']['change_scope']['object_types']=body['object_types']
    definition=type(definition).model_validate_json(json.dumps(body))
    admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s)',(tenant,'Consumer',1,Jsonb(schema.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',(tenant,'real','eios:action:Consumer.create:1',Jsonb(definition.model_dump(mode='json')),Jsonb(inputs['capability_snapshot'].model_dump(mode='json'))))
    with ExitStack() as stack:
        config=dict(signing_key_file=key,signing_key_id='synthetic-runtime')
        api=stack.enter_context(open_backend(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'api',**config))
        worker=stack.enter_context(open_backend(database_url=make_conninfo(pg,user='nexloop_domain_worker'),artifact_root=tmp_path/'worker',**config))
        issued=api.authenticate(agent_token,world='real').issue_run_credential(action_resources=['eios:action:Consumer.create:1'])
        command={'schema_version':'1.0','run_id':issued.run_id,'tenant_id':tenant,'world_id':'real','mode':'real','request_id':'synthetic-runtime-'+str(uuid.uuid4()),'trigger_event_id':str(uuid.uuid4()),'role_ref':'role:synthetic','consumer_ref':'consumer:synthetic','goal_version_ref':'goal:synthetic-v1','context_manifest_ref':'artifact:synthetic','runtime_profile':'deterministic-test','credential_ref':'run_credential:'+issued.run_id,'budget':{'maximum_model_turns':8,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'},'not_after':issued.expires_at.isoformat(),'runtime_owner_epoch':1}
        api.authenticate(api_token,world='real').accept_event(queue='operations',source_id='synthetic',event_id='synthetic-event',payload={'run_command':command})
        service=worker.authenticate(worker_token,world='real');job=service.claim_task(queue='operations',lease_seconds=getattr(request,'param',30))
        guard=RuntimeAuthority(service,api,issued,'operations',job['task_id'],job['fence'])
        yield guard,command,service,issued,invocation

def test_actual_eios_run_identity_and_task_lease_guard(authority):
    guard,command,_,issued,_=authority
    for operation in ['start','resume','inspect','cancel','model','tool']:
        result=guard.authorize(command,operation)
        assert result['authorized'] and result['run_id']==issued.run_id and issued.token not in repr(result)
    assert issued.token not in repr(guard)

@pytest.mark.parametrize('field',['run_id','tenant_id','role_ref','world_id','budget','runtime_owner_epoch'])
def test_caller_context_cannot_replace_persisted_command(authority,field):
    guard,command,_,_,_=authority;other={**command,field:{} if field=='budget' else 'forged'}
    with pytest.raises(AuthorizationUnavailable):guard.authorize(other,'model')

def test_finished_task_stops_runtime_dispatch(authority):
    guard,command,worker,_,_=authority
    worker.finish_task(queue='operations',task_id=guard.task_id,fence=guard.fence,status='succeeded')
    with pytest.raises(AuthorizationUnavailable):guard.authorize(command,'tool')

def test_source_agent_revocation_stops_runtime_dispatch(authority,admin):
    guard,command,_,issued,invocation=authority
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{status}','\"revoked\"') where fact_kind='agent_release' and entity_key=%s",([invocation.release_id],))
    with pytest.raises(AuthorizationUnavailable):guard.authorize(command,'model')

@pytest.mark.parametrize('authority',[1],indirect=True)
def test_task_lease_expiry_stops_runtime_dispatch(authority):
    guard,command,_,_,_=authority
    time.sleep(1.1)
    with pytest.raises(AuthorizationUnavailable):guard.authorize(command,'model')
