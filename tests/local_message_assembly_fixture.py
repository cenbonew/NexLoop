"""Explicit typed publication, real initial HUMAN, real governed setup.
No table seeded authority or business data; declaration builders are inputs to
actual protected configuration50, never authentication success callbacks.
"""
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,uuid
import pytest
from eios.authz import facts as F
from eios.authz.applications import ResourceRestriction,OperationRestriction
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.definitions import ActionDefinition,FunctionDefinition
from eios.ontology.version_resolution import CapabilityContractSnapshot
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.effect_contexts import BIND,registrar_schema
from nexloop_eios.context_artifacts import ACTION as CONTEXT_BIND,context_binding_schema
from context_source_declarations import source_declarations
from nexloop_eios.conversation_messages import CREATE,MESSAGE,READ
from nexloop_eios.conversation_runtime_bridge import ROUTE
from nexloop_eios.conversation_effect_receipts import FUNCTION,QUERY_CAPABILITY
from nexloop_eios.message_assignment_contract import message_assignment_action
from nexloop_eios.message_relay import message_assignment_schema
from nexloop_eios.trusted_configuration import apply_manifest
from nexloop_eios.postgres_artifacts import canonical_payload
from authority_fixture import authority_records
from runtime_effect_fixture import QUEUE
from test_business_setup_pg import business_plan,declared_service,invoke
from test_trusted_configuration_pg import configured,PrivateConfiguration,private
from test_conversation_effect_receipts import schemas as receipt_schemas


def human_declarations(tenant,human,extra=()):
    specs=[('eios:action:'+name+':1',ResourceType.ACTION) for name in [CREATE,MESSAGE,READ]]+list(extra)
    specs.append(('eios:function:'+FUNCTION+':1',ResourceType.FUNCTION))
    specs.append(('eios:function:nexloop.conversation.scope_denial:1',ResourceType.FUNCTION))
    merged={};original=None
    def translate(value):
        if isinstance(value,dict):return {k:translate(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):return [translate(v) for v in value]
        if value==original.subject_id:return human['subject_id']
        if value==original.subject_principal_id:return human['principal_id']
        return 'human' if value=='service' else value
    for target,kind in specs:
        binding,_,rows=authority_records(tenant,target,operation=Operation.EXECUTE,resource_type=kind,identity_suffix='-assembly-human')
        original=binding
        for name,key,fact in rows:
            if name in ('subject','membership','authentication'):continue
            body=translate(fact.model_dump(mode='json'));body.pop('snapshot_digest',None)
            if name=='application':
                resources=[ResourceRestriction(tenant_id=tenant,resource_type=k.value,resource_id=t).model_dump(mode='json') for t,k in specs]
                digest=F.canonical_authority_digest({'resources':resources})
                body.update(resources=resources,version_digest=digest,record_digest=digest,
                    operations=[OperationRestriction(operation=Operation.EXECUTE).model_dump(mode='json')])
            if name=='scope':
                body.update(catalog_scopes=['action.execute','function.execute'],authorized_scopes=['action.execute','function.execute'],
                    catalog_digest=F.canonical_authority_digest({'scopes':['action.execute','function.execute']}))
            actual=type(fact).model_validate_json(json.dumps(body))
            merged[name,tuple(translate(key))]={'kind':name,'key':translate(key),'payload':actual.model_dump(mode='json')}
    return list(merged.values()),original.caller_application_id


@pytest.fixture
def assembled_message(business_plan,admin,tmp_path,request):
    f=business_plan;original=f['f'];tenant=original['tenant'];manifest=f['manifest']
    # The specialized context fixture publishes its own immutable profile.
    publish_context=not {'context_message','offering_denial_plan'}.intersection(request.fixturenames)
    base=next(a for a in manifest['actions'] if a['definition']['stable_name']=='Consumer.create')
    schemas={s['type_name']:s for s in manifest['object_types']};assignment=message_assignment_schema()
    schemas[assignment.type_name]=assignment.model_dump(mode='json')
    refs={name:{**base['definition']['object_types'][0],'stable_name':name,
        'schema_digest':schema_contract_digest(ObjectTypeDefinition.model_validate_json(json.dumps(schema)))} for name,schema in schemas.items()}
    actions={a['definition']['stable_name']:a for a in manifest['actions']}
    names={'Goal.create':['Goal'],'PlanStep.create':['PlanStep'],CREATE:['Conversation'],MESSAGE:['Message'],READ:['Conversation'],
        ROUTE:['Conversation'],QUEUE:['Consumer'],'nexloop.service.query':['Consumer'],BIND:['Consumer','Goal','PlanStep','EffectControl']}
    if publish_context:names[CONTEXT_BIND]=['Consumer','Goal','PlanStep','EffectControl']
    for name,types in names.items():
        body=json.loads(json.dumps(base['definition']));body.pop('contract_digest',None)
        body['stable_name']=name;body['object_types']=[refs[t] for t in types]
        body['governance']['change_scope']['object_types']=body['object_types']
        capability_name='ontology.object.create' if name.endswith('.create') else name
        body['capability_binding']['capability_name']=capability_name
        if name==BIND:body['input_schema']=registrar_schema('bind')
        if name==CONTEXT_BIND:body['input_schema']=context_binding_schema()
        definition=ActionDefinition.model_validate_json(json.dumps(body))
        actions[name]={'definition':definition.model_dump(mode='json'),
            'capability':{**base['capability'],'capability_name':capability_name,'has_side_effects':True}}
    definition,capability=message_assignment_action(tenant=tenant,created_by='explicit-assembly-owner',created_at=datetime.now(UTC),
        capability=CapabilityContractSnapshot.model_validate_json(json.dumps(base['capability'])))
    actions[definition.stable_name]={'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json')}
    input_schema,output_schema=receipt_schemas();digest=hashlib.sha256(canonical_payload({'input':input_schema,'output':output_schema}).encode()).hexdigest()
    function=FunctionDefinition.model_validate_json(json.dumps({'tenant_id':tenant,'definition_type':'function','stable_name':FUNCTION,'version':1,'status':'published',
        'created_by':'explicit-assembly-owner','created_at':datetime.now(UTC).isoformat(),'required_scopes':['function.execute'],
        'capability_binding':{'capability_name':QUERY_CAPABILITY,'capability_version':'1','schema_hash':digest},
        'applies_to':[{'object_type':refs[name]} for name in ['Consumer','Conversation','Message']], 'input_schema':input_schema,'output_schema':output_schema}))
    function_cap=CapabilityContractSnapshot.model_validate_json(json.dumps({'capability_name':QUERY_CAPABILITY,'capability_version':'1','schema_hash':digest,
        'kind':'atomic','has_side_effects':False,'idempotent':True,'required_scopes':['function.execute']}))
    route=[ROUTE,QUEUE]+(['nexloop.reply.fallback'] if 'reply_fallback' in request.keywords else [])  # NX-025: fallback issuance (0110)
    specs={'assembly-route':route,'assembly-planner':['Goal.create','PlanStep.create','MessageAssignment.create',BIND],
        'assembly-source':['nexloop.service.request'],'assembly-executor':['nexloop.service.request','nexloop.service.query'],'assembly-runtime-worker':[QUEUE]}
    secrets_map=json.loads(original['paths']['secrets'].read_text());credentials=list(manifest['service_credentials'])
    facts={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']};tokens={}
    for label,permissions in specs.items():
        binding,end,rows=declared_service(tenant,permissions,'-'+label)
        if label=='assembly-source' and publish_context:
            binding,rows=source_declarations(tenant)
            # Separate immutable credential/application from the specialized
            # context_message profile while retaining the actual Source principal.
            app_id=binding.caller_application_id+':assembly'
            credential_id=binding.credential_id+':assembly'
            binding=binding.model_copy(update={'caller_application_id':app_id,'credential_id':credential_id})
            for row in rows:
                if row['kind']=='application':row['key']=[app_id,'1'];row['payload']['application_id']=app_id
                if row['kind']=='authentication':
                    row['key']=[credential_id];row['payload'].update(caller_application_id=app_id,credential_id=credential_id)
                row['payload'].pop('snapshot_digest',None)
        tokens[label]=secrets.token_urlsafe(48);secrets_map[label]=tokens[label]
        credentials.append({'reference':label,'binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':end.isoformat(),'status':'active'})
        facts.update({(r['kind'],tuple(r['key'])):r for r in rows})
    human_facts,human_app=human_declarations(tenant,f['human']);facts.update({(r['kind'],tuple(r['key'])):r for r in human_facts})
    updated={**manifest,'manifest_id':str(uuid.uuid4()),'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0],
        'object_types':list(schemas.values()),'actions':list(actions.values()),'functions':[{'definition':function.model_dump(mode='json'),'capability':function_cap.model_dump(mode='json')}],
        'authority_facts':list(facts.values()),'service_credentials':credentials,
        'browser_business_applications':[{'application_id':original['application'],'caller_application_id':human_app,'application_version':'1','requested_scopes':['action.execute','function.execute']}]}
    original['paths']['secrets'].write_text(json.dumps(secrets_map))
    apply_manifest(updated,database_url_file=original['paths']['dsn'],signing_key_file=original['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=original['paths']['secrets'])
    f['paths']['executor-credential-file'].write_text(tokens['assembly-executor'])
    # Dedicated two-Run correction scenario gets two units through normal setup.
    if getattr(request.node,'originalname',None) in ('test_real_human_message_v4_bound_artifact','test_fallback_reply_is_refused_once_the_original_reply_was_accepted'):
        f['recipe']['control']['budget_units']=2
        f['paths']['recipe-file'].write_text(json.dumps(f['recipe']))
    setup=invoke(f);assert setup.returncode==0,'real governed setup failed';public=json.loads(setup.stdout)
    expiry=(datetime.now(UTC)+timedelta(seconds=150)).isoformat()
    recipe={'consumer_id':public['consumer_id'],'control_id':public['control_id'],
        'consumer_revision':public['expected_consumer_revision'],'control_revision':public['expected_control_revision'],
        'valid_until':expiry,'role_ref':'role:explicit-source','context_manifest_ref':'artifact:owned-local-demo',
        'runtime_profile':'deterministic-test','budget':{'maximum_model_turns':8,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'},
        'runtime_owner_epoch':1,'queue':'operations'}
    register_catalog=getattr(request,'param',True)
    assert type(register_catalog) is bool
    if register_catalog:
        from message_offering_fixture import install_message_catalog
        from nexloop_eios.backend import open_backend
        from psycopg.conninfo import make_conninfo
        with open_backend(database_url=make_conninfo(original['pg'],user='nexloop_api'),artifact_root=tmp_path/'message-catalog',signing_key_file=original['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
            catalog=install_message_catalog(admin,backend,tenant,public['consumer_id'],tokens['assembly-source'],suffix='-assembly-source',manifest=updated,paths=original['paths'])
        recipe.update(offering_id=catalog['offering_id'],offering_binding_id=catalog['offering_binding_id'])
        tokens['assembly-source']=catalog['source_token'];updated=catalog['manifest']
    files={label:private(tmp_path,label,token) for label,token in tokens.items()}
    yield PrivateConfiguration(base=f,original=original,setup=public,recipe=recipe,credential_files=files,tokens=tokens,manifest=updated)
