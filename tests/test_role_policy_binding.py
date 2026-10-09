from datetime import UTC,datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
import json,threading
import pytest
from psycopg.types.json import Jsonb
from eios.authz.resources import ResourceType
from eios.authz.operations import Operation
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.role_mapping import role_mapping_schemas
from nexloop_eios.role_policies import role_policy_schemas,policy_envelope,CEILING_FIELDS,SCOPE_FIELDS
from nexloop_eios.role_runs import role_binding_envelope,ROLE_FIELDS,LINK_FIELDS,STEP_FIELDS
from nexloop_eios.effect_contexts import effect_plan_schemas
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.run_credentials import issue_run_credential
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action

@pytest.fixture
def policy_selection(published_action,admin,request):
    fault=getattr(request,"param",None)
    reader,base,capability=published_action
    schemas=(*role_mapping_schemas(),*role_policy_schemas(),*effect_plan_schemas())
    targets=[('eios:action:Consumer.create:1',ResourceType.ACTION,Operation.EXECUTE)]
    for schema in schemas:
        kind=schema.type_name
        ref=base.object_types[0].model_copy(update={'stable_name':kind,'schema_digest':schema_contract_digest(schema)})
        manage=kind in ('RoleDefinition','ConsumerRoleLink','RoleExecutionCeiling','RoleAssignmentScope')
        scopes=('ontology.roles.manage',) if manage else ()
        definition=base.model_copy(update={'stable_name':kind+'.create','contract_digest':None,'required_scopes':scopes,'object_types':(ref,),'governance':base.governance.model_copy(update={'change_scope':base.governance.change_scope.model_copy(update={'object_types':(ref,)})})})
        admin.execute('insert into ontology.object_type_versions values(%s,%s,1,%s)',('synthetic-a',kind,Jsonb(schema.model_dump(mode='json'))))
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',('synthetic-a','real','eios:action:'+kind+'.create:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_copy(update={'required_scopes':scopes}).model_dump(mode='json'))))
        targets.append(('eios:action:'+kind+'.create:1',ResourceType.ACTION,Operation.EXECUTE))
    session,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-policy-config',extra_scopes=('ontology.roles.manage',))
    creator=GovernedObjectCreator(reader.pool,session,reader.signer)
    def create(kind,intent,p):return GovernedObjectCreator(reader.pool,authenticate_service(reader.pool,token,world='real'),reader.signer).create(action_name=kind+'.create',action_version=1,intent_id=intent,type_name=kind,properties=p)['object_id']
    source_actions=[('eios:action:Consumer.create:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:Goal.create:1',ResourceType.ACTION,Operation.EXECUTE)]
    source,source_token=seed_multi_authority(admin,reader.pool,source_actions,identity_suffix='-policy-source')
    consumer=create('Consumer','policy-consumer',{})
    now=datetime.now(UTC);valid=dict(valid_from=(now-timedelta(seconds=1)).isoformat(),valid_until=(now+timedelta(minutes=5)).isoformat())
    goal=create('Goal','policy-goal',dict(consumer_id=consumer,state='active',valid_until=valid['valid_until']))
    step=create('PlanStep','policy-step',dict(consumer_id=consumer,goal_id=goal,control_id='no-effect-issued',submitter_principals=[source.authentication.subject_principal_id],action_name='Consumer.create',state='ready'))
    budget=dict(maximum_model_turns=2,maximum_tool_calls=3,active_timeout_seconds=30,maximum_cost='0.10',currency='USD')
    ceiling=create('RoleExecutionCeiling','policy-ceiling',dict(active=True,action_resources=['eios:action:Goal.create:1'] if fault=='role-deny' else ['eios:action:Consumer.create:1'],consumer_ids=[consumer],goal_ids=[goal],step_ids=[step],budget=budget,effect_units=1,**(dict(valid_from=valid['valid_from'],valid_until=(now+timedelta(seconds=4)).isoformat()) if fault=='short-ttl' else valid)))
    role=create('RoleDefinition','policy-role',dict(active=True,name='bounded',responsibility='synthetic responsibility',ceiling_ref='metadata-only' if fault=='unknown-ceiling' else ceiling,**valid))
    scope=create('RoleAssignmentScope','policy-scope',dict(active=True,role_id=role,consumer_id=consumer,goal_ids=[goal],step_ids=[step],**valid))
    link=create('ConsumerRoleLink','policy-link',dict(active=True,consumer_id=consumer,role_id=role,scope='shared-step-service' if fault=='unknown-scope' else scope,**valid))
    for kind,obj,fields in [('RoleDefinition',role,ROLE_FIELDS),('ConsumerRoleLink',link,LINK_FIELDS),('PlanStep',step,STEP_FIELDS),('RoleExecutionCeiling',ceiling,CEILING_FIELDS),('RoleAssignmentScope',scope,SCOPE_FIELDS)]:
        source_actions.append(('eios:object:'+kind+'/'+obj,ResourceType.OBJECT,Operation.READ))
        source_actions.extend(('eios:property:'+kind+'/'+obj+'/'+f,ResourceType.PROPERTY,Operation.READ) for f in fields)
    # Explicit synthetic Configurator refresh before issuance; never Role grants.
    admin.execute('delete from authz.nexloop_service_credentials where token_digest=%s',(source.token_digest,))
    source,source_token=seed_multi_authority(admin,reader.pool,source_actions,identity_suffix='-policy-source')
    holder=SimpleNamespace(_session=source,_backend=SimpleNamespace(_pool=reader.pool,_signer=reader.signer,_lock=threading.RLock(),_assert_open=lambda:None))
    run=issue_run_credential(reader.pool,source,reader.signer,action_resources=['eios:action:Consumer.create:1'])
    args=dict(run_id=run.run_id,role_id=role,link_id=link,consumer_id=consumer,step_id=step)
    policy_args=dict(**args,goal_id=goal,ceiling_id=ceiling,scope_id=scope,budget=budget)
    def bind(**changes):
        p=policy_envelope(holder,**{**policy_args,**changes});r=role_binding_envelope(holder,**args)
        with reader.pool.connection() as db,db.transaction():
            return db.execute('select authz.nexloop_role_policy_bind(%s,%s,%s,%s,%s,%s,%s,%s)',(source.token_digest,'real',*r,p['text'],p['signature'],p['payload'])).fetchone()[0]
    holder._create_policy_object=create
    holder._valid=valid
    return bind,policy_args,holder,run

def test_real_policy_binding(policy_selection,admin):
    bind,args,holder,run=policy_selection
    result=bind();assert result['grants_authority'] is False
    assert result['effect_units']==1
    assert admin.execute('select count(*) from authz.nexloop_role_policy_bindings').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)

@pytest.mark.parametrize('fault',['goal','consumer','budget'])
def test_real_scope_and_budget_denial(policy_selection,admin,fault):
    bind,args,holder,run=policy_selection
    changes={'goal_id':'outside-goal'} if fault=='goal' else {'consumer_id':'outside-consumer'} if fault=='consumer' else {'budget':{**args['budget'],'maximum_tool_calls':4}}
    with pytest.raises(Exception) as error:bind(**changes)
    assert ('selection mismatch' if fault=='consumer' else 'Goal Step mismatch' if fault=='goal' else 'budget exceeded') in str(error.value)
    assert admin.execute('select count(*) from authz.nexloop_role_policy_bindings').fetchone()==(0,)
    assert admin.execute('select count(*) from authz.nexloop_role_run_bindings').fetchone()==(0,)

@pytest.mark.parametrize('policy_selection',['role-deny','unknown-ceiling','unknown-scope'],indirect=True)
def test_source_allowed_but_actual_role_policy_denies(policy_selection,admin):
    bind,args,holder,run=policy_selection
    with pytest.raises(Exception) as error:bind()
    assert any(message in str(error.value) for message in ('ceiling or assignment exceeded','unknown Role ceiling','unknown Role scope'))
    assert admin.execute('select count(*) from authz.nexloop_role_policy_bindings').fetchone()==(0,)
    assert admin.execute('select count(*) from authz.nexloop_role_run_bindings').fetchone()==(0,)

def test_atomic_policy_run_has_real_allowed_resources(policy_selection,admin):
    bind,args,holder,run=policy_selection
    from nexloop_eios.role_policies import issue_role_run
    newrun=issue_role_run(holder,action_resources=['eios:action:Consumer.create:1'],role_parameters={k:args[k] for k in ('role_id','link_id','consumer_id','step_id')},policy_parameters={k:v for k,v in args.items() if k!='run_id'})
    resources=admin.execute('select allowed_resources from authz.nexloop_run_credentials where run_id=%s',(newrun.run_id,)).fetchone()[0]
    assert resources==['eios:action:Consumer.create:1']
    assert admin.execute('select count(*) from authz.nexloop_role_policy_bindings where run_id=%s',(newrun.run_id,)).fetchone()==(1,)


def test_two_role_definitions_do_not_stack_policy_allowlists(policy_selection,admin):
    bind,args,holder,run=policy_selection
    create=holder._create_policy_object
    second_ceiling=create('RoleExecutionCeiling','independent-ceiling',dict(active=True,action_resources=['eios:action:Goal.create:1'],consumer_ids=[args['consumer_id']],goal_ids=[args['goal_id']],step_ids=[args['step_id']],budget=args['budget'],effect_units=1,**holder._valid))
    second_role=create('RoleDefinition','independent-role',dict(active=True,name='second',responsibility='separate',ceiling_ref=second_ceiling,**holder._valid))
    second_scope=create('RoleAssignmentScope','independent-scope',dict(active=True,role_id=second_role,consumer_id=args['consumer_id'],goal_ids=[args['goal_id']],step_ids=[args['step_id']],**holder._valid))
    create('ConsumerRoleLink','independent-link',dict(active=True,consumer_id=args['consumer_id'],role_id=second_role,scope=second_scope,**holder._valid))
    # Fresh genuine Source, same identity; Configurator publication changed facts.
    from nexloop_eios.authorization import _identity
    with holder._backend._pool.connection() as db,db.transaction():holder._session=_identity(db,holder._session.token_digest,'real')
    from nexloop_eios.role_policies import issue_role_run
    before=admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]
    with pytest.raises(Exception) as error:
        issue_role_run(holder,action_resources=['eios:action:Goal.create:1'],role_parameters={k:args[k] for k in ('role_id','link_id','consumer_id','step_id')},policy_parameters={k:v for k,v in args.items() if k!='run_id'})
    assert 'ceiling or assignment exceeded' in str(error.value)
    assert admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]==before
    assert admin.execute('select count(*) from authz.nexloop_role_policy_bindings').fetchone()==(0,)
