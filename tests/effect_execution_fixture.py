"""Actual governed effect executor fixtures over published schema and Action ports.

Admin seeds only synthetic permission/schema/publication configuration. Every
Consumer/Goal/Step/Control business object uses real governed object Actions.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC,datetime,timedelta
import copy
import json
import secrets
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
from eios.ontology.semantics import schema_contract_digest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_contexts import EffectContextRegistrar,EffectContextUnavailable,CONFIGURE,BIND,EFFECT,registrar_schema,effect_plan_schemas
from nexloop_eios.effect_intents import EffectIntentUnavailable
from authority_fixture import seed_authority
from multi_authority_fixture import seed_multi_authority
from test_postgres_action_claims import governance_inputs
from generic_offering_fixture import install_generic_catalog

# Optional indirect parameter: 'commitment-service' publishes the effect Action as a non-message service delivery that
# may name the commitment it fulfils (NX-026) and carries an optional customer notification parameter.
SERVICE_SCHEMAS={'commitment-service':{'type':'object','properties':{'service':{'type':'string','minLength':1},'notice':{'type':'string'},
    'message':{'type':'string'},'commitment_ref':{'type':'string','minLength':1}},'required':['service'],'additionalProperties':False}}


@pytest.fixture
def execution_plan(admin,pg,tmp_path,request):
    bootstrap(admin)
    assert admin.execute("select to_regclass('control.nexloop_effect_control_ledger')").fetchone()[0]
    tenant='synthetic-a';material=secrets.token_bytes(32);key=tmp_path/'key';key.write_bytes(material);key.chmod(0o600)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',('synthetic-plan',material))
    seed_authority(admin,tenant,'eios:action:Consumer.create:1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-seed')
    schemas={schema.type_name:schema for schema in effect_plan_schemas()}
    schemas['Consumer']=ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True)
    base=governance_inputs();base_definition=base['action_definition'];base_capability=base['capability_snapshot']
    refs={name:base_definition.object_types[0].model_copy(update={'stable_name':name,'schema_digest':schema_contract_digest(schema)}) for name,schema in schemas.items()}
    for name,schema in schemas.items():admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',(tenant,name,Jsonb(schema.model_dump(mode='json'))))
    actions={name+'.create':(name,) for name in schemas}
    actions.update({'Goal.edit':('Goal',),'EffectControl.edit':('EffectControl',),'nexloop.service.query':('Consumer',),CONFIGURE:('EffectControl',),BIND:tuple(schemas),EFFECT:('Consumer',),
        # 0065: fulfilled receipts reconcile only through this independent governed Action.
        'nexloop.service.receipt_reconcile':('Consumer',)})
    for name,types in actions.items():
        body=base_definition.model_dump(mode='json');body.pop('contract_digest',None)
        body['stable_name']=name;body['object_types']=[refs[t].model_dump(mode='json') for t in types]
        body['governance']['change_scope']['object_types']=body['object_types']
        capname='ontology.object.create' if name.endswith('.create') else ('ontology.object.edit' if name.endswith('.edit') else name)
        body['capability_binding']['capability_name']=capname
        if name in (CONFIGURE,BIND):body['input_schema']=registrar_schema('configure' if name==CONFIGURE else 'bind')
        if name==EFFECT:body['governance']['change_scope']['target_systems']=['service']
        if name==EFFECT:body['input_schema']={'type':'object','properties':{'message':{'type':'string','minLength':1}},'required':['message'],'additionalProperties':False}
        if name==EFFECT and getattr(request,'param',None) in SERVICE_SCHEMAS:body['input_schema']=copy.deepcopy(SERVICE_SCHEMAS[request.param])
        if name=='nexloop.service.receipt_reconcile':
            body['governance']['change_scope']['target_systems']=['service']
            body['governance']['idempotency']['key_fields']=['intent_id']
            body['input_schema']={'type':'object','properties':{'intent_id':{'type':'string'},'effect_fence':{'type':'integer'},'query_id':{'type':'string'}},'required':['intent_id','effect_fence','query_id'],'additionalProperties':False}
        definition=type(base_definition).model_validate_json(json.dumps(body))
        capability=base_capability.model_copy(update={'capability_name':capname,'has_side_effects':True})
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (tenant,'real','eios:action:'+name+':1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
    def targets(names):return [('eios:action:'+name+':1',ResourceType.ACTION,Operation.EXECUTE) for name in names]
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'artifacts',signing_key_file=key,signing_key_id='synthetic-plan'))
        owner_session,owner_token=seed_multi_authority(admin,backend._pool,targets(['Consumer.create','EffectControl.create',CONFIGURE]),identity_suffix='-owner')
        planner_session,planner_token=seed_multi_authority(admin,backend._pool,targets(['Goal.create','PlanStep.create',BIND]),identity_suffix='-planner')
        executor_session,executor_token=seed_multi_authority(admin,backend._pool,targets([EFFECT,'nexloop.service.query','nexloop.service.receipt_reconcile']),identity_suffix='-executor')
        submitter_session,submitter_token=seed_multi_authority(admin,backend._pool,targets([EFFECT]),identity_suffix='-submitter')
        second_session,second_token=seed_multi_authority(admin,backend._pool,targets([EFFECT]),identity_suffix='-submitter-B')
        owner=backend.authenticate(owner_token,world='real');planner=backend.authenticate(planner_token,world='real');submitter=backend.authenticate(submitter_token,world='real')
        def create(service,name,request,properties):return service.create_object(action_name=name+'.create',action_version=1,intent_id=request,type_name=name,properties=properties)['object_id']
        expiry=(datetime.now(UTC)+timedelta(minutes=3)).isoformat()
        consumer=create(owner,'Consumer','real-plan-consumer',{})
        control=create(owner,'EffectControl','real-plan-control',{'consumer_id':consumer,'owner_principal':owner_session.authentication.subject_principal_id,
            'executor_principal':executor_session.authentication.subject_principal_id,'budget_units':8 if getattr(request,'param',None) in SERVICE_SCHEMAS else 1,'allow_effect':True,'valid_until':expiry})
        goal=create(planner,'Goal','real-plan-goal',{'consumer_id':consumer,'state':'active','valid_until':expiry})
        _,goal_editor_token=seed_multi_authority(admin,backend._pool,[
            ('eios:action:Goal.edit:1',ResourceType.ACTION,Operation.EXECUTE),
            ('eios:object:Goal/'+goal,ResourceType.OBJECT,Operation.EDIT),
            ('eios:property:Goal/'+goal+'/state',ResourceType.PROPERTY,Operation.EDIT)],identity_suffix='-planner-editor')
        owner=backend.authenticate(owner_token,world='real');planner=backend.authenticate(planner_token,world='real')
        submitter=backend.authenticate(submitter_token,world='real')
        goal_editor=backend.authenticate(goal_editor_token,world='real')
        def new_step(number):return create(planner,'PlanStep','real-plan-step-'+str(number),{'consumer_id':consumer,'goal_id':goal,'control_id':control,
            'submitter_principals':sorted([submitter_session.authentication.subject_principal_id,second_session.authentication.subject_principal_id]),'action_name':EFFECT,'state':'ready'})
        _,control_editor_token=seed_multi_authority(admin,backend._pool,[
            ('eios:action:EffectControl.edit:1',ResourceType.ACTION,Operation.EXECUTE),
            ('eios:object:EffectControl/'+control,ResourceType.OBJECT,Operation.EDIT),
            ('eios:property:EffectControl/'+control+'/allow_effect',ResourceType.PROPERTY,Operation.EDIT)],identity_suffix='-control-editor')
        owner=backend.authenticate(owner_token,world='real');planner=backend.authenticate(planner_token,world='real')
        submitter=backend.authenticate(submitter_token,world='real');goal_editor=backend.authenticate(goal_editor_token,world='real')
        control_editor=backend.authenticate(control_editor_token,world='real')
        step=new_step(1)
        catalog=install_generic_catalog(admin,backend,consumer_id=consumer,valid_until=expiry,submitter=submitter,second_submitter=backend.authenticate(second_token,world='real'))
        submitter=catalog['submitter'];submitter_token=catalog['submitter_token'];second_token=catalog['second_token']
        owner=backend.authenticate(owner_token,world='real');planner=backend.authenticate(planner_token,world='real')
        goal_editor=backend.authenticate(goal_editor_token,world='real')
        control_editor=backend.authenticate(control_editor_token,world='real')
        issued=submitter.issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])
        run=backend.authenticate_run(issued.token,world='real',run_id=issued.run_id)
        yield PrivateConfiguration(backend=backend,owner=owner,planner=planner,executor=executor_session,executor_token=executor_token,run=run,control=control,goal=goal,
            step=step,consumer=consumer,new_step=new_step,submitter=submitter,issued=issued,second_submitter=backend.authenticate(second_token,world='real'),goal_editor=goal_editor,control_editor=control_editor,
            key=key,owner_token=owner_token,planner_token=planner_token,submitter_token=submitter_token,second_token=second_token)


from contextlib import contextmanager
from authority_fixture import replace_fact
from eios.authz import facts as F

class PrivateConfiguration(dict):
    def __repr__(self):return '<synthetic private executor configuration>'

class GovernedEffectExecutor:
    def __init__(self,plan,pg,tmp_path,admin):
        self.plan,self.pg,self.tmp_path,self.admin=plan,pg,tmp_path,admin
        self.tenant='synthetic-a';self.executor_principal=plan['executor'].authentication.subject_principal_id
        configure_args=dict(control_id=plan['control'],control_revision=1,executor_token=plan['executor_token'])
        plan['owner'].configure_effect_control(**configure_args)
        self.runs=[plan['issued'],plan['second_submitter'].issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])]
        self.ports=[]
        for run in self.runs:
            bound=plan['planner'].bind_effect_context(step_id=plan['step'],step_revision=1,goal_revision=1,
                consumer_revision=1,control_revision=1,run_id=run.run_id,run_token=run.token,executor_token=plan['executor_token'])
            self.context_id=bound['context_ref']
            self.ports.append(plan['backend'].authenticate_run(run.token,world='real',run_id=run.run_id))
    def __repr__(self):return '<actual synthetic governed effect executor>'
    def executor_configuration(self):
        return PrivateConfiguration(database_url=make_conninfo(self.pg,user='nexloop_action_worker'),
            artifact_root=self.tmp_path/'executor-artifacts',signing_key_file=self.plan['key'],
            signing_key_id='synthetic-plan',service_token=self.plan['executor_token'],world='real')
    @contextmanager
    def current_executor(self):
        config=self.executor_configuration();token=config.pop('service_token');world=config.pop('world')
        with open_backend(**config) as backend:yield backend.authenticate(token,world=world)
    @contextmanager
    def current_submitter(self,index):
        run=self.runs[index]
        with open_backend(database_url=make_conninfo(self.pg,user='nexloop_api'),
            artifact_root=self.tmp_path/('submitter-artifacts-'+str(index)),
            signing_key_file=self.plan['key'],signing_key_id='synthetic-plan') as backend:
            yield backend.authenticate_run(run.token,world='real',run_id=run.run_id)
    def revoke_all_source_grants(self):
        for port in self.ports:
            replace_fact(self.admin,self.tenant,'grants',[port._session.authentication.subject_principal_id,'eios:action:'+EFFECT+':1'],F.GrantFacts,grants=[])
    def revoke_executor_recovery_grant(self):
        replace_fact(self.admin,self.tenant,'grants',[self.executor_principal,'eios:action:nexloop.service.receipt_reconcile:1'],F.GrantFacts,grants=[])
    def revoke_executor_query_grant(self):
        replace_fact(self.admin,self.tenant,'grants',[self.executor_principal,'eios:action:nexloop.service.query:1'],F.GrantFacts,grants=[])
    def revoke_control(self):
        return self.plan['control_editor'].edit_object(action_name='EffectControl.edit',action_version=1,
            intent_id='synthetic-control-stop',type_name='EffectControl',object_id=self.plan['control'],
            expected_revision=1,properties={'allow_effect':False})

@pytest.fixture
def governed_effect_executor(execution_plan,pg,tmp_path,admin):
    return GovernedEffectExecutor(execution_plan,pg,tmp_path,admin)
