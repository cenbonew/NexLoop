"""Two independent typed HUMANs in one owned PG tenant, two governed Consumers.

Canonical identity rows are isolated authentication-directory configuration.
There is no product registration API claim. Password/Evidence/cookie sessions,
Human proofs, ownership, Conversation, Message, and Run admission are actual.
"""
from contextlib import ExitStack
from datetime import UTC,datetime
import hashlib,json,secrets,uuid
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.identity.models import Subject,SubjectKind,TenantMembership,MembershipKind,LocalAccount,EncodedPasswordHash
from nexloop_eios.browser_http import BrowserConfiguration,COOKIE
from nexloop_eios.http_api import ApiConfiguration,create_app
from nexloop_eios.conversation_messages import ConversationOwnershipRegistrar,CREATE,MESSAGE,READ
from nexloop_eios.conversation_runtime_bridge import ConversationRuntimeBridge,canonical_event_id
from nexloop_eios.browser_identity import PostgresBrowserIdentityReader
from authority_fixture import authority_records
from runtime_effect_fixture import PrivatePlan
from message_runtime_fixture import message_plan
from test_conversation_effect_receipts import receipt_plan,FUNCTION

ORIGIN='https://synthetic.example'


def _private(tmp_path,name,value):
    path=tmp_path/name;path.write_bytes(value if type(value) is bytes else value.encode());path.chmod(0o600);return path


def _human(admin,tenant,application,index):
    now=datetime.now(UTC);password=secrets.token_urlsafe(32);suffix=str(uuid.uuid4())
    subject=Subject(subject_id='isolation-subject-'+suffix,kind=SubjectKind.HUMAN,status='active',created_at=now,updated_at=now,revision=1)
    membership=TenantMembership(tenant_id=tenant,principal_id='isolation-principal-'+suffix,subject_id=subject.subject_id,kind=MembershipKind.HOME,status='active',valid_from=now,valid_until=None,revision=1)
    account=LocalAccount(local_account_id='isolation-account-'+suffix,tenant_id=tenant,subject_id=subject.subject_id,username='isolation-user-'+str(index),verified_email='isolation-'+str(index)+'@example.invalid',password_hash=EncodedPasswordHash(PasswordHasher().hash(password)),status='active',failed_attempts=0,lockout_level=0,locked_until=None,must_change_password=False,session_epoch=1,created_at=now,updated_at=now,revision=1)
    admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)',(subject.subject_id,Jsonb(subject.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)',(tenant,membership.principal_id,subject.subject_id,Jsonb(membership.model_dump(mode='json'))))
    body=account.model_dump(mode='json');body.pop('password_hash');body.pop('password_history')
    admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',(tenant,account.local_account_id,subject.subject_id,account.username,account.password_hash.get_secret_value(),[],Jsonb(body)))
    # Configure genuine canonical Human authority facts. Application/resource
    # publication is shared; principal-specific grants are independent.
    targets=[('eios:action:'+name+':1',ResourceType.ACTION) for name in (CREATE,MESSAGE,READ)]+[('eios:function:'+FUNCTION+':1',ResourceType.FUNCTION)]
    for target,kind in targets:
        auth,_,records=authority_records(tenant,target,operation=Operation.EXECUTE,resource_type=kind,identity_suffix='-human')
        def convert(value):
            if isinstance(value,dict):return {k:convert(v) for k,v in value.items()}
            if isinstance(value,list):return [convert(v) for v in value]
            if value==auth.subject_id:return subject.subject_id
            if value==auth.subject_principal_id:return membership.principal_id
            if value=='service':return 'human'
            return value
        for fact_kind,key,fact in records:
            if fact_kind in ('authentication','subject','membership','application','resource_graph'):continue
            body=convert(fact.model_dump(mode='json'));body.pop('snapshot_digest',None)
            if fact_kind=='scope':
                body['catalog_scopes']=['action.execute','function.execute'];body['authorized_scopes']=body['catalog_scopes']
                body['catalog_digest']=F.canonical_authority_digest({'scopes':body['catalog_scopes']})
            actual=type(fact).model_validate_json(json.dumps(body))
            admin.execute('insert into authz.nexloop_authority_facts values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
                (tenant,fact_kind,convert(key),Jsonb(actual.model_dump(mode='json'))))
    return PrivatePlan(subject=subject,membership=membership,account=account,password=password,application=application)


@pytest.fixture
def two_browser_consumers(receipt_plan,admin,tmp_path):
    f=receipt_plan;tenant=f['tenant'];application=f['uow'].application_id
    humans=[_human(admin,tenant,application,index) for index in range(2)]
    # All formal objects use real governed EIOS operations, never fixture SQL.
    owner=f['api'].authenticate(f['owner_token'],world='real')
    consumer_b=owner.create_object(action_name='Consumer.create',action_version=1,intent_id='isolation-consumer-'+str(uuid.uuid4()),type_name='Consumer',properties={})['object_id']
    consumers=[f['consumer'],consumer_b]
    registrar=ConversationOwnershipRegistrar(f['api']._pool,owner._session,f['api']._signer)
    for human,consumer in zip(humans,consumers):
        registrar.register_consumer_owner(consumer_id=consumer,principal_id=human['membership'].principal_id,idempotency_key='isolation-owner-'+str(uuid.uuid4()))
    admin.execute('insert into control.nexloop_browser_rate_policies values(%s,%s,%s,20,60,true)',(tenant,'local_login','nexloop_identity'))
    identity_dsn=_private(tmp_path,'identity-dsn',make_conninfo(f['pg'],user='nexloop_identity'))
    database_dsn=_private(tmp_path,'api-dsn',make_conninfo(f['pg'],user='nexloop_api'))
    rate_key=_private(tmp_path,'rate-key',secrets.token_hex(32))
    browser_config=BrowserConfiguration(identity_dsn,rate_key,tenant,application,ORIGIN)
    with ExitStack() as stack:
        clients=[];headers=[];messages=[];conversations=[]
        for index,human in enumerate(humans):
            config=ApiConfiguration(database_dsn,f['signing_key'],tmp_path/('api-artifacts-'+str(index)),'runtime-effect',browser_config,
                execution_profile='deterministic-test',conversation_stream_seconds=1)
            client=stack.enter_context(TestClient(create_app(config),base_url=ORIGIN))
            store=client.app.state.browser_store
            assert PostgresBrowserIdentityReader(store.pool,tenant_id=tenant,application_id=application).get_subject(human['subject'].subject_id)==human['subject']
            signed_in=client.post('/api/v1/auth/login',headers={'Origin':ORIGIN},json={'username':human['account'].username,'password':human['password']})
            assert signed_in.status_code==200 and human['password'] not in signed_in.text
            assert signed_in.json()['principal_id']==human['membership'].principal_id
            assert client.cookies.get(COOKIE) and 'HttpOnly' in signed_in.headers['set-cookie']
            csrf={'Origin':ORIGIN,'X-CSRF-Token':signed_in.json()['csrf_token']}
            created=client.post('/api/v1/conversations',headers={**csrf,'Idempotency-Key':'isolation-conversation-'+str(index)},json={})
            assert created.status_code==200,created.status_code
            conversation=created.json();assert conversation['consumer_id']==consumers[index]
            accepted=client.post('/api/v1/conversations/'+conversation['id']+'/messages',headers={**csrf,'Idempotency-Key':'isolation-message-'+str(index)},json={'body':'synthetic statement from person '+str(index)})
            assert accepted.status_code==202
            clients.append(client);headers.append(csrf);conversations.append(conversation);messages.append(accepted.json()['message'])
        assert clients[0].cookies.get(COOKIE)!=clients[1].cookies.get(COOKIE)
        # Current Source Run is genuinely reissued after canonical directory
        # changes, then rebound by the actual planner to the existing Plan A.
        source=f['api'].authenticate(f['source_token'],world='real')
        run=source.issue_run_credential(action_resources=['eios:action:nexloop.service.request:1'])
        planner=f['api'].authenticate(f['planner_token'],world='real')
        planner.bind_effect_context(step_id=f['step'],step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,
            run_id=run.run_id,run_token=run.token,executor_token=f['executor_token'])
        source_event_id=conversations[0]['id']+':'+str(messages[0]['sequence'])
        command={**f['command'],'run_id':run.run_id,'credential_ref':'run_credential:'+run.run_id,'not_after':run.expires_at.isoformat(),
            'trigger_event_id':canonical_event_id(tenant,'real',source_event_id),'request_id':'isolation-message-run-'+str(uuid.uuid4())}
        bridge=ConversationRuntimeBridge(f['api'].authenticate(f['owner_token'],world='real'))
        yield PrivatePlan(base=f,humans=humans,clients=clients,headers=headers,consumers=consumers,conversations=conversations,messages=messages,
            run=run,command=command,bridge=bridge)
