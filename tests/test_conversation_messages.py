"""Actual Human/session/permission/governed Message PG contract; synthetic only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import secrets
import threading

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.applications import ResourceRestriction
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.conversation_messages import (
    ConversationMessagePort,ConversationOwnershipRegistrar,ConversationConflict,ConversationUnavailable,
    conversation_schemas,OWNERSHIP,CREATE,MESSAGE,READ)
from nexloop_eios.object_actions import GovernedObjectCreator
from authority_fixture import authority_records,replace_fact
from multi_authority_fixture import seed_multi_authority
from test_browser_business_authorization import browser_business
from test_action_definitions import published_action
from test_browser_identity_reads import identity
from test_browser_session_creation import uow


class ConversationFixture(dict):
    def __repr__(self):return '<synthetic governed Human conversation fixture>'
    __str__=__repr__


@pytest.fixture
def conversations(browser_business,admin,pg,tmp_path):
    base=browser_business;reader=base['reader'];tenant='synthetic-a'
    auth,_,_=authority_records(tenant,'eios:action:Consumer.create:1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
    subject=base['identity'][1].subject_id;principal=base['identity'][2].principal_id
    names=[CREATE,MESSAGE,READ]
    def convert(value):
        if isinstance(value,dict):return {key:convert(item) for key,item in value.items()}
        if isinstance(value,list):return [convert(item) for item in value]
        if value==auth.subject_id:return subject
        if value==auth.subject_principal_id:return principal
        if value=='service':return 'human'
        return value
    for name in names:
        _,_,rows=authority_records(tenant,'eios:action:'+name+':1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
        for kind,key,fact in rows:
            if kind in ('subject','membership','authentication','application'):continue
            body=convert(fact.model_dump(mode='json'));body.pop('snapshot_digest',None)
            actual=type(fact).model_validate_json(json.dumps(body))
            admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
                (tenant,kind,convert(key),Jsonb(actual.model_dump(mode='json'))))
    original=admin.execute("select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='application' and entity_key=%s",(tenant,[auth.caller_application_id,'1'])).fetchone()[0]
    app=F.ApplicationFacts.model_validate_json(json.dumps(original))
    resources=tuple(ResourceRestriction(tenant_id=tenant,resource_type='action',resource_id='eios:action:'+name+':1') for name in ['Consumer.create']+names)
    digest=F.canonical_authority_digest({'application':auth.caller_application_id,'resources':[item.model_dump(mode='json') for item in resources]})
    body=app.model_dump(mode='json');body.pop('snapshot_digest',None)
    body.update(resources=[item.model_dump(mode='json') for item in resources],version_digest=digest,record_digest=digest)
    app=F.ApplicationFacts.model_validate_json(json.dumps(body))
    admin.execute("update authz.nexloop_authority_facts set payload=%s where tenant_id=%s and fact_kind='application' and entity_key=%s",(Jsonb(app.model_dump(mode='json')),tenant,[auth.caller_application_id,'1']))
    schemas={schema.type_name:schema for schema in conversation_schemas()}
    definition=base['definition'];capability=base['capability']
    for name,schema in schemas.items():
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',(tenant,name,Jsonb(schema.model_dump(mode='json'))))
    for action,type_name in [(OWNERSHIP,'ConsumerOwnership'),(CREATE,'Conversation'),(MESSAGE,'Message'),(READ,'Conversation')]:
        reference=definition.object_types[0].model_copy(update={'stable_name':type_name,'schema_digest':schema_contract_digest(schemas[type_name])})
        body=definition.model_dump(mode='json');body.pop('contract_digest',None)
        body['stable_name']=action;body['object_types']=[reference.model_dump(mode='json')]
        body['governance']['change_scope']['object_types']=body['object_types']
        body['capability_binding']['capability_name']='ontology.object.create'
        published=type(definition).model_validate_json(json.dumps(body))
        cap=capability.model_copy(update={'capability_name':'ontology.object.create','has_side_effects':True})
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (tenant,'real','eios:action:'+action+':1',Jsonb(published.model_dump(mode='json')),Jsonb(cap.model_dump(mode='json'))))
    _,token=seed_multi_authority(admin,reader.pool,[('eios:action:'+name+':1',ResourceType.ACTION,Operation.EXECUTE) for name in ('Consumer.create',OWNERSHIP)],identity_suffix='-consumer-registrar')
    registrar_session=authenticate_service(reader.pool,token,world='real')
    consumer=GovernedObjectCreator(reader.pool,registrar_session,reader.signer).create(action_name='Consumer.create',action_version=1,intent_id='conversation-consumer-'+secrets.token_hex(12),type_name='Consumer',properties={})['object_id']
    registrar=ConversationOwnershipRegistrar(reader.pool,registrar_session,reader.signer)
    ownership=registrar.register_consumer_owner(consumer_id=consumer,principal_id=principal,idempotency_key='synthetic-consumer-owner-key')
    human=authenticate_browser_business(reader.pool,base['issued'].session,world='real')
    port=ConversationMessagePort(reader.pool,human,reader.signer)
    yield ConversationFixture(port=port,base=base,reader=reader,registrar=registrar,ownership=ownership,consumer=consumer,principal=principal,human=human,pg=pg)


def conversation(fixture):return fixture['port'].create_conversation(idempotency_key='synthetic-conversation-key')


def test_governed_owner_conversation_message_commit_before_ack_replay(conversations,admin):
    fixture=conversations;port=fixture['port'];created=conversation(fixture)
    assert created['consumer_id']==fixture['consumer']
    assert conversation(fixture)==created
    accepted=port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-message-key',body='consumer statement')
    assert accepted['created'] is True and accepted['message']['sequence']==1
    replay=port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-message-key',body='consumer statement')
    assert replay=={**accepted,'created':False}
    for table in ('nexloop_conversation_messages','nexloop_message_inbox','nexloop_message_outbox'):
        assert admin.execute('select count(*) from runtime.'+table).fetchone()==(1,)
    assert admin.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()==(1,)
    assert admin.execute("select claim->>'state' from runtime.nexloop_action_claims where action_name='Message.create'").fetchone()==('terminal',)
    assert not admin.execute('select 1 from authz.nexloop_service_credentials where token_digest=%s',(fixture['human'].token_digest,)).fetchone()


def test_payload_conflict_is409_original_and_event_preserved(conversations,admin):
    port=conversations['port'];created=conversation(conversations)
    original=port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-conflict-key',body='original')
    with pytest.raises(ConversationConflict) as conflict:
        port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-conflict-key',body='replacement')
    assert conflict.value.http_status==409
    assert port.read_messages(conversation_id=created['id'])['items']==[original['message']]
    assert admin.execute('select count(*) from runtime.nexloop_message_outbox').fetchone()==(1,)


def test_last_event_id_replay_only_committed_messages_and_pg_sequence(conversations):
    port=conversations['port'];created=conversation(conversations)
    receipts=[port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-sequence-key-'+str(index),body='statement '+str(index)) for index in range(3)]
    first=port.read_events(conversation_id=created['id'],limit=2)
    assert [item['id'] for item in first['items']]==['1','2'] and first['next_cursor']=='2'
    resumed=port.read_events(conversation_id=created['id'],after_sequence=int(first['next_cursor']))
    assert resumed['items']==[{'id':'3','type':'message.accepted','data':receipts[2]['message']}]
    assert port.read_events(conversation_id=created['id'],after_sequence=3)['items']==[]


def test_outbox_insert_failure_rolls_back_message_claim_sequence(conversations,admin):
    port=conversations['port'];created=conversation(conversations)
    admin.execute("create function public.synthetic_message_outbox_fail() returns trigger language plpgsql as $$begin raise exception 'synthetic outbox unavailable';end$$")
    admin.execute('create trigger synthetic_message_outbox_failure before insert on runtime.nexloop_message_outbox for each row execute function public.synthetic_message_outbox_fail()')
    with pytest.raises(ConversationUnavailable):port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-failure-key',body='not acknowledged')
    assert admin.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='Message.create'").fetchone()==(0,)
    assert admin.execute('select last_sequence from runtime.nexloop_conversations').fetchone()==(0,)
    assert port.read_events(conversation_id=created['id'])['items']==[]


def test_current_grant_revoke_blocks_write_read_and_preserves_commits(conversations,admin):
    fixture=conversations;created=conversation(fixture);port=fixture['port']
    port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-before-revoke',body='kept evidence')
    for action in (MESSAGE,READ):
        replace_fact(admin,'synthetic-a','grants',[fixture['principal'],'eios:action:'+action+':1'],F.GrantFacts,grants=[])
    with pytest.raises(ConversationUnavailable):port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-after-revoke',body='denied')
    with pytest.raises(ConversationUnavailable):port.read_events(conversation_id=created['id'])
    assert admin.execute('select count(*) from runtime.nexloop_conversation_messages').fetchone()==(1,)


def test_restricted_role_direct_tables_denied_and_service_cannot_be_human(conversations):
    fixture=conversations
    with psycopg.connect(make_conninfo(fixture['pg'],user='nexloop_api')) as db:
        for table in ('control.nexloop_consumer_owners','runtime.nexloop_conversations','runtime.nexloop_conversation_messages','runtime.nexloop_message_inbox','runtime.nexloop_message_outbox'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():db.execute('select * from '+table)
    with pytest.raises(ConversationUnavailable):ConversationMessagePort(fixture['reader'].pool,fixture['registrar'].session,fixture['reader'].signer)
    with pytest.raises(ConversationUnavailable):fixture['port'].read_messages(conversation_id='f'*64)


def test_concurrent_same_key_commits_single_governed_message(conversations,admin):
    fixture=conversations;created=conversation(fixture);barrier=threading.Barrier(2)
    def submit(_):
        # Independently authenticated actual Human contexts; SQL row ownership
        # and sequence serialization enforce dedup rather than an in-memory lock.
        human=authenticate_browser_business(fixture['reader'].pool,fixture['base']['issued'].session,world='real')
        port=ConversationMessagePort(fixture['reader'].pool,human,fixture['reader'].signer)
        barrier.wait(timeout=10)
        return port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-parallel-key',body='one statement')
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(submit,range(2)))
    assert results[0]['message']==results[1]['message'] and sorted(result['created'] for result in results)==[False,True]
    assert admin.execute('select last_sequence from runtime.nexloop_conversations').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_message_outbox').fetchone()==(1,)


def test_outbox_delay_past_human_deadline_rolls_back_all(conversations,admin):
    fixture=conversations;created=conversation(fixture)
    from datetime import UTC,datetime,timedelta
    session_id=fixture['base']['issued'].session.session_id
    original=admin.execute('select payload from control.nexloop_browser_sessions where session_id=%s',(session_id,)).fetchone()[0]
    admin.execute('update control.nexloop_browser_sessions set payload=%s where session_id=%s',
        (Jsonb({**original,'idle_expires_at':(datetime.now(UTC)+timedelta(seconds=2)).isoformat()}),session_id))
    human=authenticate_browser_business(fixture['reader'].pool,fixture['base']['issued'].session,world='real')
    port=ConversationMessagePort(fixture['reader'].pool,human,fixture['reader'].signer)
    admin.execute("create function public.synthetic_message_outbox_delay() returns trigger language plpgsql as $$begin perform pg_sleep(3);return new;end$$")
    admin.execute('create trigger synthetic_message_outbox_delay before insert on runtime.nexloop_message_outbox for each row execute function public.synthetic_message_outbox_delay()')
    with pytest.raises(ConversationUnavailable):port.accept_message(conversation_id=created['id'],idempotency_key='synthetic-delay-key',body='deadline crossed before ACK')
    for table in ('nexloop_conversation_messages','nexloop_message_inbox','nexloop_message_outbox'):
        assert admin.execute('select count(*) from runtime.'+table).fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='Message.create'").fetchone()==(0,)
    assert admin.execute('select last_sequence from runtime.nexloop_conversations').fetchone()==(0,)


@pytest.mark.parametrize('verb,arguments',[
    ('conversations',{'limit':None,'after':''}),
    ('messages',{'limit':None,'after_sequence':0}),
    ('events',{'limit':100,'after_sequence':None}),
    ('events',{'limit':100.5,'after_sequence':0}),
    ('events',{'limit':100,'after_sequence':9223372036854775808}),
])
def test_actual_signed_sql_rejects_null_or_unbounded_replay(conversations,verb,arguments):
    fixture=conversations;created=conversation(fixture)
    if verb!='conversations':arguments={**arguments,'conversation_id':created['id']}
    with fixture['reader'].pool.connection() as db,pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():
        fixture['port']._call(db,verb,READ,**arguments)
