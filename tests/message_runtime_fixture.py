"""Private UUID-tenant message+Plan configuration; actual Actions/authentication.
No direct SQL formal-object writes, invented Human credentials, or mock ports.
"""
from contextlib import ExitStack
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,uuid
import pytest
from argon2 import PasswordHasher
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.applications import ResourceRestriction
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.identity.models import Subject,SubjectKind,TenantMembership,MembershipKind,LocalAccount,EncodedPasswordHash
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_contexts import CONFIGURE,BIND,EFFECT,effect_plan_schemas,registrar_schema
from nexloop_eios.browser_identity import open_browser_identity
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.conversation_messages import conversation_schemas,ConversationOwnershipRegistrar,ConversationMessagePort,OWNERSHIP,CREATE,MESSAGE,READ
from nexloop_eios.conversation_runtime_bridge import ConversationRuntimeBridge,ConversationRunReader,canonical_event_id,ROUTE
from authority_fixture import authority_records,seed_authority
from runtime_effect_fixture import seed_multi_uuid,PrivatePlan,QUEUE
from test_postgres_action_claims import governance_inputs
from test_browser_evidence import authenticate
from test_browser_session_creation import create

@pytest.fixture
def message_plan(admin,pg,tmp_path):
    bootstrap(admin);tenant=str(uuid.uuid4());material=secrets.token_bytes(32)
    key=tmp_path/'signer';key.write_bytes(material);key.chmod(0o600)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',('runtime-effect',material))
    seed_authority(admin,tenant,'eios:action:Consumer.create:1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-seed')
    schemas={schema.type_name:schema for schema in effect_plan_schemas()}
    schemas.update({schema.type_name:schema for schema in conversation_schemas()})
    schemas['Consumer']=ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True)
    base=governance_inputs();definition=base['action_definition'];capability=base['capability_snapshot']
    refs={name:definition.object_types[0].model_copy(update={'tenant_id':tenant,'stable_name':name,'schema_digest':schema_contract_digest(schema)}) for name,schema in schemas.items()}
    for name,schema in schemas.items():admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',(tenant,name,Jsonb(schema.model_dump(mode='json'))))
    actions={name+'.create':(name,) for name in schemas};actions.update({CONFIGURE:('EffectControl',),BIND:tuple(schemas),EFFECT:('Consumer',),'nexloop.service.query':('Consumer',),READ:('Conversation',),ROUTE:('Conversation',)})
    for name,types in actions.items():
        body=json.loads(json.dumps(definition.model_dump(mode='json')).replace('synthetic-a',tenant));body.pop('contract_digest',None)
        body['stable_name']=name;body['object_types']=[refs[k].model_dump(mode='json') for k in types]
        body['governance']['change_scope']['object_types']=body['object_types']
        capname='ontology.object.create' if name.endswith('.create') else name;body['capability_binding']['capability_name']=capname
        if name in (CONFIGURE,BIND):body['input_schema']=registrar_schema('configure' if name==CONFIGURE else 'bind')
        if name==EFFECT:
            body['governance']['change_scope']['target_systems']=['service']
            body['input_schema']={'type':'object','properties':{'message':{'type':'string','minLength':1}},'required':['message'],'additionalProperties':False}
        published=type(definition).model_validate_json(json.dumps(body));cap=capability.model_copy(update={'capability_name':capname,'has_side_effects':True})
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (tenant,'real','eios:action:'+name+':1',Jsonb(published.model_dump(mode='json')),Jsonb(cap.model_dump(mode='json'))))
    def targets(names):return [('eios:action:'+name+':1',ResourceType.ACTION,Operation.EXECUTE) for name in names]
    owner_token=seed_multi_uuid(admin,tenant,targets(['Consumer.create','EffectControl.create',CONFIGURE,QUEUE,OWNERSHIP,ROUTE]),suffix='-owner')
    planner_token=seed_multi_uuid(admin,tenant,targets(['Goal.create','PlanStep.create',BIND]),suffix='-planner')
    executor_token=seed_multi_uuid(admin,tenant,targets([EFFECT,'nexloop.service.query']),suffix='-executor')
    source_tokens=[seed_multi_uuid(admin,tenant,targets([EFFECT]),suffix='-source-'+letter) for letter in ('A','B')]
    worker_token=seed_multi_uuid(admin,tenant,targets([QUEUE]),suffix='-runtime-worker')
    with ExitStack() as stack:
        config=dict(signing_key_file=key,signing_key_id='runtime-effect')
        api=stack.enter_context(open_backend(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'api-artifacts',**config))
        backend_worker=stack.enter_context(open_backend(database_url=make_conninfo(pg,user='nexloop_domain_worker'),artifact_root=tmp_path/'worker-artifacts',**config))
        owner=api.authenticate(owner_token,world='real');planner=api.authenticate(planner_token,world='real');executor=api.authenticate(executor_token,world='real')
        sources=[api.authenticate(token,world='real') for token in source_tokens]
        def create_object(service,name,properties):return service.create_object(action_name=name+'.create',action_version=1,intent_id='runtime-effect-'+name,type_name=name,properties=properties)['object_id']
        expiry=(datetime.now(UTC)+timedelta(minutes=3)).isoformat()
        consumer=create_object(owner,'Consumer',{})
        control=create_object(owner,'EffectControl',{'consumer_id':consumer,'owner_principal':owner._session.authentication.subject_principal_id,
            'executor_principal':executor._session.authentication.subject_principal_id,'budget_units':1,'allow_effect':True,'valid_until':expiry})
        goal=create_object(planner,'Goal',{'consumer_id':consumer,'state':'active','valid_until':expiry})
        step=create_object(planner,'PlanStep',{'consumer_id':consumer,'goal_id':goal,'control_id':control,'submitter_principals':sorted(source._session.authentication.subject_principal_id for source in sources),
            'action_name':EFFECT,'state':'ready'})
        owner.configure_effect_control(control_id=control,control_revision=1,executor_token=executor_token)
        # Canonical Human setup only; no formal object direct SQL.
        now=datetime.now(UTC);password=secrets.token_urlsafe(32);application='synthetic-webchat'
        subject=Subject(subject_id='webchat-human-'+str(uuid.uuid4()),kind=SubjectKind.HUMAN,status='active',created_at=now,updated_at=now,revision=1)
        membership=TenantMembership(tenant_id=tenant,principal_id='webchat-principal-'+str(uuid.uuid4()),subject_id=subject.subject_id,kind=MembershipKind.HOME,status='active',valid_from=now,valid_until=None,revision=1)
        account=LocalAccount(local_account_id='webchat-account-'+str(uuid.uuid4()),tenant_id=tenant,subject_id=subject.subject_id,username='synthetic-user',verified_email='synthetic@example.invalid',password_hash=EncodedPasswordHash(PasswordHasher().hash(password)),status='active',failed_attempts=0,lockout_level=0,locked_until=None,must_change_password=False,session_epoch=1,created_at=now,updated_at=now,revision=1)
        admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)',(tenant,application))
        admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)',(subject.subject_id,Jsonb(subject.model_dump(mode='json'))))
        admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)',(tenant,membership.principal_id,subject.subject_id,Jsonb(membership.model_dump(mode='json'))))
        payload=account.model_dump(mode='json');payload.pop('password_hash');payload.pop('password_history')
        admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',(tenant,account.local_account_id,subject.subject_id,account.username,account.password_hash.get_secret_value(),[],Jsonb(payload)))
        auth,_,_=authority_records(tenant,'eios:action:'+CREATE+':1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-human')
        def convert(value):
            if isinstance(value,dict):return {k:convert(v) for k,v in value.items()}
            if isinstance(value,list):return [convert(v) for v in value]
            if value==auth.subject_id:return subject.subject_id
            if value==auth.subject_principal_id:return membership.principal_id
            if value=='service':return 'human'
            return value
        records={};names=[CREATE,MESSAGE,READ]
        for name in names:
            _,_,rows=authority_records(tenant,'eios:action:'+name+':1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-human')
            for kind,key,fact in rows:
                if kind in ('subject','membership','authentication'):continue
                body=convert(fact.model_dump(mode='json'));body.pop('snapshot_digest',None)
                records[(kind,tuple(convert(key)))]=(kind,convert(key),type(fact).model_validate_json(json.dumps(body)))
        for index,(kind,key,fact) in list(records.items()):
            if kind=='application':
                resources=tuple(ResourceRestriction(tenant_id=tenant,resource_type='action',resource_id='eios:action:'+name+':1') for name in names)
                digest=F.canonical_authority_digest({'application':auth.caller_application_id,'resources':[r.model_dump(mode='json') for r in resources]})
                body=fact.model_dump(mode='json');body.pop('snapshot_digest',None)
                body.update(resources=[r.model_dump(mode='json') for r in resources],version_digest=digest,record_digest=digest)
                records[index]=(kind,key,F.ApplicationFacts.model_validate_json(json.dumps(body)))
        for kind,key,fact in records.values():admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
            (tenant,kind,key,Jsonb(fact.model_dump(mode='json'))))
        admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)',(tenant,application,auth.caller_application_id,'1',Jsonb(['action.execute'])))
        identity=(make_conninfo(pg,user='nexloop_identity'),subject,membership,account,password)
        identity_pool=stack.enter_context(open_browser_identity(identity[0]))
        uow=PostgresBrowserSessionUnitOfWork(identity_pool,tenant_id=tenant,application_id=application)
        issued=create(uow,identity,authenticate(uow,identity).evidence)
        # Real identity/grant configuration advanced authority epoch. Refresh
        # all services before genuine Run issuance and current Plan binding.
        owner=api.authenticate(owner_token,world='real');planner=api.authenticate(planner_token,world='real')
        source=api.authenticate(source_tokens[0],world='real');worker=backend_worker.authenticate(worker_token,world='real')
        ownership=ConversationOwnershipRegistrar(api._pool,owner._session,api._signer).register_consumer_owner(
            consumer_id=consumer,principal_id=membership.principal_id,idempotency_key='synthetic-message-owner-key')
        human=authenticate_browser_business(api._pool,issued.session,world='real')
        port=ConversationMessagePort(api._pool,human,api._signer)
        conversation=port.create_conversation(idempotency_key='synthetic-message-conversation-key')
        message=port.accept_message(conversation_id=conversation['id'],idempotency_key='synthetic-message-payload-key',body='synthetic bounded user statement')['message']
        run=source.issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])
        planner.bind_effect_context(step_id=step,step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,run_id=run.run_id,run_token=run.token,executor_token=executor_token)
        source_event_id=conversation['id']+':'+str(message['sequence'])
        command={'schema_version':'1.0','run_id':run.run_id,'tenant_id':tenant,'world_id':'real','mode':'real',
            'request_id':'message-runtime-'+str(uuid.uuid4()),'trigger_event_id':canonical_event_id(tenant,'real',source_event_id),
            'role_ref':'role:source','consumer_ref':'consumer:'+consumer,'goal_version_ref':'goal:'+goal+':revision:1:step:1:control:1',
            'context_manifest_ref':'artifact:synthetic-message','runtime_profile':'deterministic-test','credential_ref':'run_credential:'+run.run_id,
            'budget':{'maximum_model_turns':8,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'},
            'not_after':run.expires_at.isoformat(),'runtime_owner_epoch':1}
        yield PrivatePlan(api=api,owner=owner,owner_token=owner_token,planner_token=planner_token,source_token=source_tokens[0],worker=worker,worker_token=worker_token,
            command=command,run=run,bridge=ConversationRuntimeBridge(owner),port=port,reader=ConversationRunReader(api._pool,human,api._signer),
            consumer=consumer,tenant=tenant,conversation=conversation,message=message,issued=issued,identity=identity,uow=uow,pg=pg,step=step,goal=goal,control=control,signing_key=config['signing_key_file'],executor_token=executor_token)
