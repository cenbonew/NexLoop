"""DRAFT executable PG tests, not collected until authorized head41 assembly.

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

@pytest.fixture
def plan(admin,pg,tmp_path):
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
    actions.update({'Goal.edit':('Goal',),CONFIGURE:('EffectControl',),BIND:tuple(schemas),EFFECT:('Consumer',)})
    for name,types in actions.items():
        body=base_definition.model_dump(mode='json');body.pop('contract_digest',None)
        body['stable_name']=name;body['object_types']=[refs[t].model_dump(mode='json') for t in types]
        body['governance']['change_scope']['object_types']=body['object_types']
        capname='ontology.object.create' if name.endswith('.create') else ('ontology.object.edit' if name.endswith('.edit') else name)
        body['capability_binding']['capability_name']=capname
        if name in (CONFIGURE,BIND):body['input_schema']=registrar_schema('configure' if name==CONFIGURE else 'bind')
        if name==EFFECT:body['input_schema']={'type':'object','properties':{'message':{'type':'string','minLength':1}},'required':['message'],'additionalProperties':False}
        definition=type(base_definition).model_validate_json(json.dumps(body))
        capability=base_capability.model_copy(update={'capability_name':capname})
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (tenant,'real','eios:action:'+name+':1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
    def targets(names):return [('eios:action:'+name+':1',ResourceType.ACTION,Operation.EXECUTE) for name in names]
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'artifacts',signing_key_file=key,signing_key_id='synthetic-plan'))
        owner_session,owner_token=seed_multi_authority(admin,backend._pool,targets(['Consumer.create','EffectControl.create',CONFIGURE]),identity_suffix='-owner')
        planner_session,planner_token=seed_multi_authority(admin,backend._pool,targets(['Goal.create','PlanStep.create',BIND]),identity_suffix='-planner')
        executor_session,executor_token=seed_multi_authority(admin,backend._pool,targets([EFFECT]),identity_suffix='-executor')
        submitter_session,submitter_token=seed_multi_authority(admin,backend._pool,targets([EFFECT]),identity_suffix='-submitter')
        second_session,second_token=seed_multi_authority(admin,backend._pool,targets([EFFECT]),identity_suffix='-submitter-B')
        owner=backend.authenticate(owner_token,world='real');planner=backend.authenticate(planner_token,world='real');submitter=backend.authenticate(submitter_token,world='real')
        def create(service,name,request,properties):return service.create_object(action_name=name+'.create',action_version=1,intent_id=request,type_name=name,properties=properties)['object_id']
        expiry=(datetime.now(UTC)+timedelta(minutes=3)).isoformat()
        consumer=create(owner,'Consumer','real-plan-consumer',{})
        control=create(owner,'EffectControl','real-plan-control',{'consumer_id':consumer,'owner_principal':owner_session.authentication.subject_principal_id,
            'executor_principal':executor_session.authentication.subject_principal_id,'budget_units':1,'allow_effect':True,'valid_until':expiry})
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
        step=new_step(1)
        catalog=install_generic_catalog(admin,backend,consumer_id=consumer,valid_until=expiry,submitter=submitter,second_submitter=backend.authenticate(second_token,world='real'))
        submitter=catalog['submitter'];submitter_token=catalog['submitter_token'];second_token=catalog['second_token']
        owner=backend.authenticate(owner_token,world='real');planner=backend.authenticate(planner_token,world='real')
        goal_editor=backend.authenticate(goal_editor_token,world='real')
        issued=submitter.issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])
        run=backend.authenticate_run(issued.token,world='real',run_id=issued.run_id)
        yield dict(backend=backend,owner=owner,planner=planner,executor=executor_session,executor_token=executor_token,run=run,control=control,goal=goal,
            step=step,consumer=consumer,new_step=new_step,submitter=submitter,issued=issued,second_submitter=backend.authenticate(second_token,world='real'),goal_editor=goal_editor)


def configure(plan):return plan['owner'].configure_effect_control(control_id=plan['control'],control_revision=1,executor_token=plan['executor_token'])
def bind(plan,step=None,run=None):return plan['planner'].bind_effect_context(step_id=step or plan['step'],step_revision=1,goal_revision=1,
    consumer_revision=1,control_revision=1,run_id=(run or plan['issued']).run_id,
    run_token=(run or plan['issued']).token,executor_token=plan['executor_token'])


def test_governed_objects_real_run_registrar_and_atomic_claim(plan,admin):
    configure(plan);receipt=bind(plan)
    assert bind(plan)==receipt and receipt['scope']=='effect_context'
    submitted=plan['run'].submit_effect_intent(parameters={'message':'service'})
    assert submitted['state']=='accepted' and submitted['business_action_success'] is False
    assert admin.execute('select count(*) from control.nexloop_effect_plan_bindings').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.nexloop_effect_control_reservations').fetchone()[0]==1
    assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()[0]==1
    assert admin.execute("select claim->>'state' from runtime.nexloop_action_claims where action_name=%s",(BIND,)).fetchone()[0]=='terminal'
    assert admin.execute("select slot_identity from control.nexloop_effect_contexts").fetchone()[0]=='service.request:primary'


def test_missing_owner_registration_denies_without_context_or_claim(plan,admin):
    with pytest.raises(EffectContextUnavailable):bind(plan)
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone()[0]==0
    assert admin.execute('select count(*) from runtime.nexloop_action_claims where action_name=%s',(BIND,)).fetchone()[0]==0


def test_shared_budget_across_actual_plan_steps(plan,admin):
    configure(plan);bind(plan);plan['run'].submit_effect_intent(parameters={'message':'first'})
    second=plan['submitter'].issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])
    run=plan['backend'].authenticate_run(second.token,world='real',run_id=second.run_id)
    bind(plan,plan['new_step'](2),second)
    with pytest.raises(EffectIntentUnavailable):run.submit_effect_intent(parameters={'message':'second'})
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==1
    assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()[0]==1


def test_run_cannot_become_planner_or_inject_slot(plan):
    with pytest.raises(EffectContextUnavailable):EffectContextRegistrar(plan['backend']._pool,plan['run']._session,plan['backend']._signer)
    with pytest.raises(TypeError):plan['planner'].bind_effect_context(slot_identity='fresh',step_id=plan['step'])


def test_source_revocation_denies_bind_and_rolls_back_claim(plan,admin):
    configure(plan)
    admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s",(plan['issued'].run_id,))
    with pytest.raises(EffectContextUnavailable):bind(plan)
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone()[0]==0
    assert admin.execute('select count(*) from runtime.nexloop_action_claims where action_name=%s',(BIND,)).fetchone()[0]==0


def test_post_context_failure_rolls_back_context_runlink_and_terminal_claim(plan,admin):
    configure(plan)
    admin.execute("create function public.synthetic_link_failure() returns trigger language plpgsql as $$ begin raise exception 'synthetic';end $$")
    admin.execute('create trigger synthetic_link_failure before insert on control.nexloop_effect_run_contexts for each row execute function public.synthetic_link_failure()')
    with pytest.raises(EffectContextUnavailable):bind(plan)
    for table in ('nexloop_effect_contexts','nexloop_effect_run_contexts','nexloop_effect_plan_bindings'):
        assert admin.execute('select count(*) from control.'+table).fetchone()[0]==0
    assert admin.execute('select count(*) from runtime.nexloop_action_claims where action_name=%s',(BIND,)).fetchone()[0]==0


def test_application_cannot_read_or_call_inner(plan):
    with plan['backend']._pool.connection() as connection:
        for table in ('control.nexloop_effect_control_ledger','control.nexloop_effect_plan_bindings','runtime.nexloop_effect_control_reservations'):
            with connection.transaction(),pytest.raises(psycopg.errors.InsufficientPrivilege):connection.execute('select * from '+table)
        with connection.transaction(),pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('select authz.nexloop_effect_intent_command_v0040(%s,%s,%s,%s,%s)',('','real','{}','','{}'))


def test_actual_other_run_principal_cannot_associate_to_step(plan,admin):
    configure(plan)
    # A real independent executor Run has valid Action authority; it still must
    # not inherit the Step's specifically delegated submitter identity.
    backend=plan['backend']
    executor=backend.authenticate(plan['executor_token'],world='real')
    issued=executor.issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])
    other=backend.authenticate_run(issued.token,world='real',run_id=issued.run_id)
    with pytest.raises(EffectContextUnavailable):bind(plan,run=issued)
    assert admin.execute('select count(*) from control.nexloop_effect_run_contexts').fetchone()[0]==0


def test_final_failure_rolls_back_real_claim_and_shared_ledger(plan,admin):
    configure(plan);bind(plan)
    admin.execute("create function public.synthetic_reserve_fail() returns trigger language plpgsql as $$ begin raise exception 'synthetic';end $$")
    admin.execute('create trigger synthetic_reserve_failure before insert on runtime.nexloop_effect_control_reservations for each row execute function public.synthetic_reserve_fail()')
    with pytest.raises(EffectIntentUnavailable):plan['run'].submit_effect_intent(parameters={'message':'service'})
    for table in ('nexloop_effect_intents','nexloop_effect_outbox','nexloop_effect_control_reservations'):
        assert admin.execute('select count(*) from runtime.'+table).fetchone()[0]==0
    assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()[0]==0
    assert admin.execute('select reserved_units from control.nexloop_effect_contexts').fetchone()[0]==0


def test_distinct_authorized_roles_share_actual_step_intent_and_outbox(plan,admin):
    configure(plan);first=bind(plan)
    second=plan['second_submitter'].issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])
    other=plan['backend'].authenticate_run(second.token,world='real',run_id=second.run_id)
    assert plan['run']._session.authentication.subject_principal_id!=other._session.authentication.subject_principal_id
    assert bind(plan,run=second)==first
    a=plan['run'].submit_effect_intent(parameters={'message':'one service'})
    b=other.submit_effect_intent(parameters={'message':'one service'})
    assert a==b and a['state']=='accepted'
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.nexloop_effect_submissions').fetchone()[0]==2
    assert admin.execute('select count(*) from control.nexloop_effect_run_contexts').fetchone()[0]==2
    assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()[0]==1




def test_actual_governed_goal_revision_change_blocks_original_context(plan,admin):
    configure(plan);bind(plan)
    receipt=plan['goal_editor'].edit_object(action_name='Goal.edit',action_version=1,intent_id='real-goal-close',
        type_name='Goal',object_id=plan['goal'],expected_revision=1,properties={'state':'closed'})
    assert receipt['revision']==2
    assert admin.execute("select nexloop_revision from ontology.objects where object_id=%s",(plan['goal'],)).fetchone()[0]==2
    with pytest.raises(EffectIntentUnavailable):plan['run'].submit_effect_intent(parameters={'message':'service'})
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==0


def test_lock_wait_expired_real_run_proof_rolls_back_registration_and_claim(plan,admin,pg,monkeypatch):
    import threading
    import time
    configure(plan)
    ready=threading.Event()
    original=EffectContextRegistrar._proof
    def shorter_proof(self,session,target):
        proof=original(self,session,target)
        if session.run_context is not None:
            # Genuine current EIOS facts, with a deliberately shorter trusted
            # proof lifetime. Authorization is not stubbed or defaulted true.
            proof['expires_at']=(datetime.now(UTC)+timedelta(milliseconds=150)).isoformat()
            ready.set()
        return proof
    monkeypatch.setattr(EffectContextRegistrar,'_proof',shorter_proof)
    with psycopg.connect(pg) as blocker,ThreadPoolExecutor(1) as executor:
        blocker.execute('select control_id from control.nexloop_effect_control_ledger where control_id=%s for update',(plan['control'],))
        future=executor.submit(bind,plan)
        try:
            assert ready.wait(3)
            time.sleep(.3)
        finally:blocker.commit()
        with pytest.raises(EffectContextUnavailable):future.result(timeout=4)
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone()[0]==0
    assert admin.execute('select count(*) from control.nexloop_effect_run_contexts').fetchone()[0]==0
    assert admin.execute('select count(*) from runtime.nexloop_action_claims where action_name=%s',(BIND,)).fetchone()[0]==0
