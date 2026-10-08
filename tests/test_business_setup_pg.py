"""Actual technical publication/identity creation followed by governed setup CLI."""
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,subprocess,sys,uuid
from pathlib import Path
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.applications import ResourceRestriction,OperationRestriction
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.effect_contexts import effect_plan_schemas,CONFIGURE,registrar_schema
from nexloop_eios.conversation_messages import conversation_schemas
from nexloop_eios.trusted_configuration import apply_manifest
from authority_fixture import authority_records
from test_postgres_action_claims import governance_inputs
from test_trusted_configuration_pg import configured,identity_manifest,create_human,private,PrivateConfiguration


def declared_service(tenant,names,suffix):
    targets=['eios:action:'+name+':1' for name in names];records={};apps=[]
    for target in targets:
        binding,expiry,rows=authority_records(tenant,target,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix=suffix)
        for name,key,fact in rows:
            if name=='application':apps.append(fact)
            records[name,tuple(key)]=(name,key,fact)
    app=apps[0].model_copy(update={'resources':tuple(ResourceRestriction(tenant_id=tenant,resource_type='action',resource_id=target) for target in targets),
        'operations':(OperationRestriction(operation=Operation.EXECUTE),)})
    binding=binding.model_copy(update={'caller_application_digest':app.version_digest})
    for index,(name,key,fact) in list(records.items()):
        if name=='application':fact=app
        if name=='authentication':fact=F.CredentialAuthenticationFacts(**binding.model_dump(),status='active',expires_at=expiry,repository_witness=fact.repository_witness)
        if name=='grants':
            role=next(row[2].roles[0] for row in records.values() if row[0]=='subject_authority')
            fact=fact.model_copy(update={'grants':tuple(g.model_copy(update={'role_digest':role.digest}) for g in fact.grants)})
        body=fact.model_dump(mode='json');body.pop('snapshot_digest',None)
        records[index]=(name,key,type(fact).model_validate_json(json.dumps(body)))
    return binding,expiry,[{'kind':name,'key':key,'payload':fact.model_dump(mode='json')} for name,key,fact in records.values()]


@pytest.fixture
def business_plan(configured,admin,tmp_path):
    f=configured;tenant=f['tenant'];body=identity_manifest(f);human=create_human(f,body)
    schemas={s.type_name:s for s in effect_plan_schemas()}
    schemas.update({s.type_name:s for s in conversation_schemas()})
    schemas['Consumer']=ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True)
    base=governance_inputs();original=base['action_definition'];original_cap=base['capability_snapshot']
    refs={name:original.object_types[0].model_copy(update={'tenant_id':tenant,'stable_name':name,'schema_digest':schema_contract_digest(schema)}) for name,schema in schemas.items()}
    actions=[]
    for name,types in {'Consumer.create':['Consumer'],'EffectControl.create':['EffectControl'],'ConsumerOwnership.create':['ConsumerOwnership'],
        CONFIGURE:['EffectControl'],'nexloop.service.request':['Consumer']}.items():
        declaration=json.loads(json.dumps(original.model_dump(mode='json')).replace('synthetic-a',tenant));declaration.pop('contract_digest',None)
        declaration['stable_name']=name;declaration['object_types']=[refs[t].model_dump(mode='json') for t in types]
        declaration['governance']['change_scope']['object_types']=declaration['object_types']
        capability_name='ontology.object.create' if name.endswith('.create') else name
        declaration['capability_binding']['capability_name']=capability_name
        if name==CONFIGURE:declaration['input_schema']=registrar_schema('configure')
        if name=='nexloop.service.request':
            declaration['governance']['change_scope']['target_systems']=['service']
            declaration['input_schema']={'type':'object','properties':{'message':{'type':'string','minLength':1}},'required':['message'],'additionalProperties':False}
        definition=type(original).model_validate_json(json.dumps(declaration));capability=original_cap.model_copy(update={'capability_name':capability_name,'has_side_effects':True})
        actions.append({'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json')})
    owner,owner_expiry,owner_facts=declared_service(tenant,['Consumer.create','EffectControl.create','ConsumerOwnership.create',CONFIGURE],'-business-owner')
    executor,executor_expiry,executor_facts=declared_service(tenant,['nexloop.service.request'],'-business-executor')
    owner_token=secrets.token_urlsafe(48);executor_token=secrets.token_urlsafe(48)
    facts={(r['kind'],tuple(r['key'])):r for r in f['manifest']['authority_facts']+owner_facts+executor_facts}
    manifest={**f['manifest'],'manifest_id':str(uuid.uuid4()),'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0],
        'object_types':[s.model_dump(mode='json') for s in schemas.values()],'actions':actions,'authority_facts':list(facts.values()),
        'identity_allowances':[],
        'service_credentials':f['manifest']['service_credentials']+[
            {'reference':'business-owner','binding':owner.model_dump(mode='json'),'worlds':['real'],'expires_at':owner_expiry.isoformat(),'status':'active'},
            {'reference':'business-executor','binding':executor.model_dump(mode='json'),'worlds':['real'],'expires_at':executor_expiry.isoformat(),'status':'active'}]}
    f['paths']['secrets'].write_text(json.dumps({'source':f['token'],'business-owner':owner_token,'business-executor':executor_token}))
    apply_manifest(manifest,database_url_file=f['paths']['dsn'],signing_key_file=f['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=f['paths']['secrets'])
    recipe={'schema_version':'1.0','request_id':str(uuid.uuid4()),'human_principal_id':human['principal_id'],
        'control':{'budget_units':1,'valid_until':(datetime.now(UTC)+timedelta(seconds=180)).isoformat()}}
    material={'database-url-file':make_conninfo(f['pg'],user='nexloop_api'),'owner-credential-file':owner_token,
        'executor-credential-file':executor_token,'recipe-file':json.dumps(recipe)}
    argv=[];paths={}
    for name,value in material.items():
        paths[name]=private(tmp_path,name,value);argv+=['--'+name,str(paths[name])]
    argv+=['--signing-key-file',str(f['paths']['backend_signing']),'--signing-key-id','explicit-configuration','--artifact-root',str(tmp_path/'setup-artifacts')]
    yield PrivateConfiguration(f=f,argv=argv,recipe=recipe,paths=paths,human=human,owner_token=owner_token,executor_token=executor_token,manifest=manifest)


def invoke(f,extra=()):
    return subprocess.run([sys.executable,'-m','nexloop_eios.business_setup',*f['argv'],*extra],capture_output=True,text=True,timeout=30)


def test_actual_configured_initial_human_and_governed_setup_replay(business_plan,admin):
    f=business_plan;result=invoke(f);assert result.returncode==0 and result.stderr==''
    body=json.loads(result.stdout);assert body['configured'] is True and body['tenant_id']==f['f']['tenant'] and body['world_id']=='real'
    assert body['expected_consumer_revision']==body['expected_control_revision']==1 and 'current_consumer_revision' not in body
    assert admin.execute("select count(*) from ontology.objects where type_name in('Consumer','EffectControl','ConsumerOwnership')").fetchone()==(3,)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,0)
    assert admin.execute('select consumer_id,principal_id from control.nexloop_consumer_owners').fetchone()==(body['consumer_id'],f['human']['principal_id'])
    again=invoke(f);assert again.returncode==0 and json.loads(again.stdout)==body
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(3,)
    assert all(secret not in result.stdout+result.stderr+again.stdout+again.stderr for secret in (f['owner_token'],f['executor_token'],f['f']['token']))


def test_changed_budget_same_request_conflict_never_overwrites(business_plan,admin):
    f=business_plan;assert invoke(f).returncode==0
    changed={**f['recipe'],'control':{**f['recipe']['control'],'budget_units':2}}
    f['paths']['recipe-file'].write_text(json.dumps(changed))
    result=invoke(f);assert result.returncode==1 and result.stdout=='' and result.stderr=='Business setup unavailable\n'
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,0)
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(3,)


def test_self_named_human_never_becomes_registered_consumer_owner(business_plan,admin):
    f=business_plan;changed={**f['recipe'],'human_principal_id':'nonexistent-self-named-human'}
    f['paths']['recipe-file'].write_text(json.dumps(changed))
    result=invoke(f);assert result.returncode==1 and result.stderr=='Business setup unavailable\n' and result.stdout==''
    assert admin.execute('select count(*) from control.nexloop_consumer_owners').fetchone()==(0,)
    # Earlier independently governed creation remains recoverable, not success.
    assert admin.execute("select count(*) from ontology.objects where type_name='ConsumerOwnership'").fetchone()==(0,)
    f['paths']['recipe-file'].write_text(json.dumps(f['recipe']))
    assert invoke(f).returncode==0
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(3,)


def test_optional_current_read_without_explicit_grants_fails_closed(business_plan,admin):
    f=business_plan;assert invoke(f).returncode==0
    result=invoke(f,['--verify-current'])
    assert result.returncode==1 and result.stdout=='' and result.stderr=='Business setup unavailable\n'
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(3,)


def test_actual_foreign_executor_identity_rejected_before_business_write(business_plan,admin,tmp_path):
    f=business_plan;foreign=str(uuid.uuid4());binding,expiry,records=declared_service(foreign,['nexloop.service.request'],'-foreign-executor')
    token=secrets.token_urlsafe(48)
    manifest={'schema_version':'1.0','manifest_id':str(uuid.uuid4()),'tenant_id':foreign,'expected_revision':0,'tenant_status':'active',
        'object_types':[],'actions':[],'functions':[],'authority_facts':records,
        'service_credentials':[{'reference':'foreign-executor','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expiry.isoformat(),'status':'active'}],
        'browser_applications':[],'browser_business_applications':[],'browser_rate_policies':[],'identity_allowances':[]}
    secretfile=private(tmp_path,'foreign-executor-secret',json.dumps({'foreign-executor':token}))
    original=f['f']['paths']
    apply_manifest(manifest,database_url_file=original['dsn'],signing_key_file=original['signing'],signing_key_id='explicit-configuration',service_secrets_file=secretfile)
    f['paths']['executor-credential-file'].write_text(token)
    result=invoke(f);assert result.returncode==1 and result.stdout=='' and result.stderr=='Business setup unavailable\n'
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(0,)


def test_publisher_owner_grant_revocation_zero_business_creation(business_plan,admin):
    f=business_plan;facts=[]
    principal=next(item['binding']['subject_principal_id'] for item in f['manifest']['service_credentials'] if item['reference']=='business-owner')
    for row in f['manifest']['authority_facts']:
        if row['kind']=='grants' and row['key']==[principal,'eios:action:Consumer.create:1']:
            body={**row['payload'],'grants':[]};body.pop('snapshot_digest',None)
            row={**row,'payload':F.GrantFacts.model_validate_json(json.dumps(body)).model_dump(mode='json')}
        facts.append(row)
    manifest={**f['manifest'],'manifest_id':str(uuid.uuid4()),
        'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['f']['tenant'],)).fetchone()[0],
        'authority_facts':facts}
    p=f['f']['paths']
    apply_manifest(manifest,database_url_file=p['dsn'],signing_key_file=p['signing'],signing_key_id='explicit-configuration',service_secrets_file=p['secrets'])
    result=invoke(f);assert result.returncode==1 and result.stdout=='' and result.stderr=='Business setup unavailable\n'
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(0,)


@pytest.mark.parametrize('fault',['public_mode','symlink'])
def test_unsafe_private_recipe_preflight_zero_business_creation(business_plan,admin,fault):
    f=business_plan;path=f['paths']['recipe-file']
    if fault=='public_mode':path.chmod(0o644)
    else:
        target=path.with_name('actual-private-recipe');path.rename(target);path.symlink_to(target)
    result=invoke(f);assert result.returncode==1 and result.stdout=='' and result.stderr=='Business setup unavailable\n'
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(0,)
