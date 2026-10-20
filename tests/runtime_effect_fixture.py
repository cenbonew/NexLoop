"""UUID-tenant real PG configuration for Runtime→governedIntent integration.

Only authority/schema/publication configuration is synthetic. Formal objects use
actual EIOS Actions; no authorize callback, stored credential or production read.
"""
from contextlib import ExitStack
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,uuid
import pytest
from system_catalog_fixture import publish_system_catalog
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.applications import ResourceRestriction,OperationRestriction
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.backend import open_backend
from nexloop_eios.service_offerings import offering_schemas,json_export_example,OFFERING_FIELDS,BINDING_FIELDS
from nexloop_eios.effect_contexts import CONFIGURE,BIND,EFFECT
from authority_fixture import authority_records,seed_authority
QUEUE='NexLoop.queue.operations'


class PrivatePlan(dict):
    def __repr__(self):return '<synthetic UUID-tenant runtime effect configuration>'
    __str__=__repr__


# Shared EffectControl budget; Role policy tests raise it to isolate Role effect_units.
EFFECT_BUDGET_UNITS=1

def seed_multi_uuid(admin,tenant,targets,*,suffix,extra_scopes=(),world='real',sim_use=None):
    """Multi-resource genuine stored EIOS service authority for this UUID tenant."""
    scopes=frozenset(kind.value+'.'+op.value for _,kind,op in targets)|frozenset(extra_scopes);records={};apps=[]
    for target,kind,op in targets:
        auth,expiry,rows=authority_records(tenant,target,world=world,resource_type=kind,operation=op,operations=tuple(operation for resource,resource_kind,operation in targets if resource==target and resource_kind==kind),identity_suffix=suffix)
        for name,key,fact in rows:
            if name=='application':apps.append(fact)
            records[(name,tuple(key))]=(name,key,fact)
    app=apps[0].model_copy(update={'resources':tuple(ResourceRestriction(tenant_id=tenant,resource_type=kind.value,resource_id=target) for target,kind in sorted({(target,kind) for target,kind,op in targets},key=lambda pair:pair[0])),
        'operations':tuple(OperationRestriction(operation=op) for op in sorted({op for _,_,op in targets},key=lambda op:op.value))})
    auth=auth.model_copy(update={'requested_scopes':scopes,'caller_application_digest':app.version_digest})
    for index,(name,key,fact) in list(records.items()):
        if name=='application':fact=app
        if name=='scope':fact=fact.model_copy(update={'catalog_scopes':scopes,'authorized_scopes':scopes})
        if name=='authentication':fact=F.CredentialAuthenticationFacts(**auth.model_dump(),status='active',expires_at=expiry,repository_witness=fact.repository_witness)
        if name=='grants':
            role=next(row[2].roles[0] for row in records.values() if row[0]=='subject_authority')
            fact=fact.model_copy(update={'grants':tuple(g.model_copy(update={'role_digest':role.digest}) for g in fact.grants)})
        body=fact.model_dump(mode='json');body.pop('snapshot_digest',None)
        records[index]=(name,key,type(fact).model_validate_json(json.dumps(body)))
    token=secrets.token_urlsafe(48)
    # NX-041 D10: a simulation world's credential names its use (api, worker, agent); real ones carry none.
    admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at,sim_use) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (hashlib.sha256(token.encode()).hexdigest(),tenant,auth.credential_id,Jsonb(auth.model_dump(mode='json')),[world],'nexloop-core','active',expiry,sim_use))
    for name,key,fact in records.values():admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
        (tenant,name,key,Jsonb(fact.model_dump(mode='json'))))
    return token


# Extra Source authority a test module may add (monkeypatched per test, empty by default), e.g. AT-043's incentive Action.
SOURCE_EXTRA_TARGETS=()


def install_runtime_catalog(admin,tenant,api,consumer,sources,expiry):
    """Config-only publication followed by genuine maintainer governed CREATE.

    Source gets exact directory READ plus original EFFECT, never CREATE/EDIT.
    Replace only synthetic pre-Run technical credentials, then real fresh auth.
    """
    schemas=offering_schemas()
    # nexloop_eios.system_catalog builds the offering types and their create Actions.
    publish_system_catalog(admin,tenant,[schema.type_name for schema in schemas],[schema.type_name+'.create' for schema in schemas])
    keeper_token=seed_multi_uuid(admin,tenant,[('eios:action:'+schema.type_name+'.create:1',ResourceType.ACTION,Operation.EXECUTE) for schema in schemas],suffix='-catalog-maintainer')
    offered=api.authenticate(keeper_token,world='real').create_object(action_name='ServiceOffering.create',action_version=1,intent_id='runtime-effect-catalog-offering',type_name='ServiceOffering',properties=json_export_example(valid_until=expiry))
    tokens=[]
    for letter,source in zip(('A','B'),sources,strict=True):
        keeper=api.authenticate(keeper_token,world='real')
        binding=keeper.create_object(action_name='ConsumerServiceOffering.create',action_version=1,intent_id='runtime-effect-catalog-binding-'+letter,type_name='ConsumerServiceOffering',properties={'consumer_id':consumer,'offering_id':offered['object_id'],'offering_revision':1,'source_principal':source._session.authentication.subject_principal_id,'active':True})
        targets=[('eios:action:'+EFFECT+':1',ResourceType.ACTION,Operation.EXECUTE),*SOURCE_EXTRA_TARGETS]
        for receipt,fields in ((offered,OFFERING_FIELDS),(binding,BINDING_FIELDS)):
            target=receipt['type_name']+'/'+receipt['object_id'];targets.append(('eios:object:'+target,ResourceType.OBJECT,Operation.READ))
            targets.extend(('eios:property:'+target+'/'+field,ResourceType.PROPERTY,Operation.READ) for field in fields)
        admin.execute('delete from authz.nexloop_service_credentials where tenant_id=%s and credential_id=%s',(tenant,source._session.authentication.credential_id))
        tokens.append(seed_multi_uuid(admin,tenant,targets,suffix='-source-'+letter))
    return tokens,[api.authenticate(token,world='real') for token in tokens]


@pytest.fixture
def runtime_effect_plan(admin,pg,tmp_path):
    bootstrap(admin);tenant=str(uuid.uuid4());material=secrets.token_bytes(32)
    key=tmp_path/'signer';key.write_bytes(material);key.chmod(0o600)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',('runtime-effect',material))
    seed_authority(admin,tenant,'eios:action:Consumer.create:1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-seed')
    # nexloop_eios.system_catalog builds the types and Actions. _INITIAL_QUERY_CONSTRAINTS (set by test_receipt_tail) is a deliberate
    # variant of nexloop.service.query for that test only.
    constraints=globals().get('_INITIAL_QUERY_CONSTRAINTS',{})
    def query_variant(d):
        d['governance'].update(constraints.get('governance',{}))
        d.update(constraints.get('definition',{}))
    publish_system_catalog(admin,tenant,('Consumer','Goal','PlanStep','EffectControl'),
        ('Consumer.create','Goal.create','PlanStep.create','EffectControl.create',CONFIGURE,BIND,EFFECT,'nexloop.service.query','nexloop.service.receipt_reconcile'),
        variants={'nexloop.service.query':query_variant} if constraints else None)
    def targets(names):return [('eios:action:'+name+':1',ResourceType.ACTION,Operation.EXECUTE) for name in names]
    owner_token=seed_multi_uuid(admin,tenant,targets(['Consumer.create','EffectControl.create',CONFIGURE,QUEUE]),suffix='-owner')
    planner_token=seed_multi_uuid(admin,tenant,targets(['Goal.create','PlanStep.create',BIND]),suffix='-planner')
    executor_token=seed_multi_uuid(admin,tenant,targets([EFFECT,'nexloop.service.query','nexloop.service.receipt_reconcile']),suffix='-executor')
    source_tokens=[seed_multi_uuid(admin,tenant,targets([EFFECT]),suffix='-source-'+letter) for letter in ('A','B')]
    worker_token=seed_multi_uuid(admin,tenant,targets([QUEUE]),suffix='-runtime-worker')
    with ExitStack() as stack:
        config=dict(signing_key_file=key,signing_key_id='runtime-effect')
        api=stack.enter_context(open_backend(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'api-artifacts',**config))
        backend_worker=stack.enter_context(open_backend(database_url=make_conninfo(pg,user='nexloop_domain_worker'),artifact_root=tmp_path/'worker-artifacts',**config))
        owner=api.authenticate(owner_token,world='real');planner=api.authenticate(planner_token,world='real');executor=api.authenticate(executor_token,world='real')
        sources=[api.authenticate(token,world='real') for token in source_tokens]
        def create(service,name,properties):return service.create_object(action_name=name+'.create',action_version=1,intent_id='runtime-effect-'+name,type_name=name,properties=properties)['object_id']
        expiry=(datetime.now(UTC)+timedelta(minutes=3)).isoformat()
        consumer=create(owner,'Consumer',{})
        control=create(owner,'EffectControl',{'consumer_id':consumer,'owner_principal':owner._session.authentication.subject_principal_id,
            'executor_principal':executor._session.authentication.subject_principal_id,'budget_units':EFFECT_BUDGET_UNITS,'allow_effect':True,'valid_until':expiry})
        goal=create(planner,'Goal',{'consumer_id':consumer,'state':'active','valid_until':expiry})
        step=create(planner,'PlanStep',{'consumer_id':consumer,'goal_id':goal,'control_id':control,'submitter_principals':sorted(source._session.authentication.subject_principal_id for source in sources),
            'action_name':EFFECT,'state':'ready'})
        source_tokens,sources=install_runtime_catalog(admin,tenant,api,consumer,sources,expiry)
        owner=api.authenticate(owner_token,world='real');planner=api.authenticate(planner_token,world='real');executor=api.authenticate(executor_token,world='real')
        owner.configure_effect_control(control_id=control,control_revision=1,executor_token=executor_token)
        runs=[source.issue_run_credential(action_resources=['eios:action:'+EFFECT+':1']) for source in sources]
        for run in runs:planner.bind_effect_context(step_id=step,step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,run_id=run.run_id,run_token=run.token,executor_token=executor_token)
        worker=backend_worker.authenticate(worker_token,world='real')
        worker_spawn=dict(database_url=make_conninfo(pg,user='nexloop_domain_worker'),artifact_root=tmp_path/'worker-artifacts',token=worker_token,world='real',**config)
        commands=[];activations=[];jobs=[]
        for index,run in enumerate(runs):
            command={'schema_version':'1.0','run_id':run.run_id,'tenant_id':tenant,'world_id':'real','mode':'real','request_id':'runtime-effect-'+str(uuid.uuid4()),'trigger_event_id':str(uuid.uuid4()),
                'role_ref':'role:source-'+str(index),'consumer_ref':'consumer:'+consumer,'goal_version_ref':'goal:'+goal+':revision:1:step:1:control:1',
                'context_manifest_ref':'artifact:synthetic-effect','runtime_profile':'deterministic-test','credential_ref':'run_credential:'+run.run_id,
                'budget':{'maximum_model_turns':8,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'},
                'not_after':run.expires_at.isoformat(),'runtime_owner_epoch':1}
            accepted=owner.accept_runtime_event(queue='operations',source_id='runtime-effect',event_id='event-'+str(index),run_token=run.token,command=command,input='persist one service intent')
            job=worker.claim_task(queue='operations',lease_seconds=60)
            assert job['task_id']==accepted['task_id']
            activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=run.run_id,command=command,input='persist one service intent',owner_epoch=1)
            commands.append(command);activations.append(activation['activation_ref']);jobs.append(job)
        yield PrivatePlan(api=api,backend_worker=backend_worker,owner=owner,planner=planner,owner_token=owner_token,planner_token=planner_token,sources=sources,source_tokens=source_tokens,worker=worker,worker_token=worker_token,worker_spawn=worker_spawn,commands=commands,activations=activations,runs=runs,
            consumer=consumer,goal=goal,step=step,control=control,tenant=tenant,executor_token=executor_token,signing_key=key,signing_key_id='runtime-effect',pg=pg,jobs=jobs)
