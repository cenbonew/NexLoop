from test_role_mapping import roles
from test_action_definitions import published_action
from multi_authority_fixture import seed_multi_authority
from pathlib import Path
from types import SimpleNamespace
import pytest
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.run_credentials import issue_run_credential
from nexloop_eios.role_runs import bind_role_run,ROLE_FIELDS,LINK_FIELDS,STEP_FIELDS

@pytest.fixture
def selected(roles,admin):
    create,valid,reader=roles
    schema=ObjectTypeDefinition(type_name='PlanStep',version=1,only_edit_via_actions=True,properties=tuple(PropertyDefinition(property_name=n,value_type=PropertyValueType.JSON if n=='submitter_principals' else PropertyValueType.STRING,required=True) for n in STEP_FIELDS))
    # Original reader predates role publication; refresh genuine Source first.
    from nexloop_eios.action_definitions import PostgresActionDefinitionReader
    reader=PostgresActionDefinitionReader(reader.pool,authenticate_service(reader.pool,create.token,world='real'),reader.signer)
    base,cap=reader.get('Consumer.create',1)
    ref=base.object_types[0].model_copy(update={'stable_name':'PlanStep','schema_digest':schema_contract_digest(schema)})
    definition=base.model_copy(update={'stable_name':'PlanStep.create','contract_digest':None,'object_types':(ref,),'governance':base.governance.model_copy(update={'change_scope':base.governance.change_scope.model_copy(update={'object_types':(ref,)})})})
    admin.execute('insert into ontology.object_type_versions values(%s,%s,1,%s)',('synthetic-a','PlanStep',Jsonb(schema.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',('synthetic-a','real','eios:action:PlanStep.create:1',Jsonb(definition.model_dump(mode='json')),Jsonb(cap.model_dump(mode='json'))))
    creator=GovernedObjectCreator(reader.pool,authenticate_service(reader.pool,create.token,world='real'),reader.signer)
    def c(kind,intent,props):return creator.create(action_name=kind+'.create',action_version=1,intent_id=intent,type_name=kind,properties=props)['object_id']
    consumer=c('Consumer','selected-consumer',{})
    role=c('RoleDefinition','selected-role',dict(name='selected',responsibility='synthetic',ceiling_ref='metadata-only',**valid))
    link=c('ConsumerRoleLink','selected-link',dict(consumer_id=consumer,role_id=role,scope='local-service',**valid))
    targets=[('eios:action:Consumer.create:1',ResourceType.ACTION,Operation.EXECUTE)]
    for kind,obj,fields in [('RoleDefinition',role,ROLE_FIELDS),('ConsumerRoleLink',link,LINK_FIELDS)]:
        targets.append(('eios:object:'+kind+'/'+obj,ResourceType.OBJECT,Operation.READ))
        targets.extend(('eios:property:'+kind+'/'+obj+'/'+field,ResourceType.PROPERTY,Operation.READ) for field in fields)
    first,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-selected-source')
    planner,_=seed_multi_authority(admin,reader.pool,[('eios:action:PlanStep.create:1',ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-selected-planner')
    step=GovernedObjectCreator(reader.pool,planner,reader.signer).create(action_name='PlanStep.create',action_version=1,intent_id='selected-step',type_name='PlanStep',properties=dict(consumer_id=consumer,state='ready',submitter_principals=[first.authentication.subject_principal_id]))['object_id']
    targets.append(('eios:object:PlanStep/'+step,ResourceType.OBJECT,Operation.READ))
    targets.extend(('eios:property:PlanStep/'+step+'/'+field,ResourceType.PROPERTY,Operation.READ) for field in STEP_FIELDS)
    admin.execute('delete from authz.nexloop_service_credentials where token_digest=%s',(first.token_digest,)) # synthetic pre-Run configuration refresh
    session,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-selected-source')
    holder=SimpleNamespace(_session=session,_backend=SimpleNamespace(_pool=reader.pool,_signer=reader.signer,_lock=__import__('threading').RLock(),_assert_open=lambda:None))
    run=issue_run_credential(reader.pool,session,reader.signer,action_resources=['eios:action:Consumer.create:1'])
    return holder,dict(run_id=run.run_id,consumer_id=consumer,link_id=link,role_id=role,step_id=step),run

def test_genuine_source_run_selected_role_current_read(selected,admin):
    source,args,run=selected
    result=bind_role_run(source,**args)
    assert result['run_id']==run.run_id
    assert result['role_ref']=='role:'+args['role_id']+':mapping:'+args['link_id']
    assert result['grants_authority'] is False and result['dispatch_permit'] is False
    assert 'source_digest' not in result
    assert bind_role_run(source,**args)==result
    assert admin.execute('select count(*) from authz.nexloop_role_run_bindings').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)

@pytest.mark.parametrize('fault',['foreign_consumer','role_mismatch','source_revoked','read_revoked','expired','ended'])
def test_selection_failclosed_without_admission_or_ledger(selected,admin,fault):
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    source,args,run=selected
    if fault=='foreign_consumer':args={**args,'consumer_id':'f'*64}
    elif fault=='role_mismatch':args={**args,'role_id':args['link_id']}
    elif fault=='source_revoked':admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",(source._session.token_digest,))
    elif fault=='read_revoked':replace_fact(admin,'synthetic-a','grants',[source._session.authentication.subject_principal_id,'eios:property:ConsumerRoleLink/'+args['link_id']+'/scope'],F.GrantFacts,grants=[])
    elif fault=='expired':admin.execute("update authz.nexloop_run_credentials set expires_at=issued_at+interval '1 microsecond' where run_id=%s",(run.run_id,))
    elif fault=='ended':admin.execute("update ontology.objects set properties=jsonb_set(properties,'{active}','false') where object_id=%s",(args['link_id'],)) # isolated fault injection
    with pytest.raises(Exception):bind_role_run(source,**args)
    assert admin.execute('select count(*) from authz.nexloop_role_run_bindings').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)
