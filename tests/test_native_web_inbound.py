"""Actual governed native provider ID admission; legacy remains separately covered."""
import uuid
import pytest
import psycopg
from nexloop_eios.native_web_inbound import NativeWebMessagePort
from nexloop_eios.conversation_messages import ConversationConflict,ConversationDenied
from test_conversation_messages import conversations,conversation,browser_business,published_action,identity,uow

def port(f):return NativeWebMessagePort(f['port'].pool,f['port'].session,f['port'].signer)

def test_native_event_different_transport_keys_replay_one_real_message(conversations,admin):
    p=port(conversations);c=conversation(conversations);event=str(uuid.uuid4());arguments=dict(conversation_id=c['id'],provider_event_id=event,body='explicit native event body')
    a=p.accept_native_message(idempotency_key='native-first-transport',**arguments)
    b=p.accept_native_message(idempotency_key='native-second-transport',**arguments)
    assert a['created'] is True and b=={**a,'created':False}
    assert admin.execute('select provider_namespace,provider_event_id::text,message_id from runtime.nexloop_native_web_events').fetchone()==('native.webchat',event,a['message']['id'])
    for table in ['nexloop_conversation_messages','nexloop_message_inbox','nexloop_message_outbox','nexloop_native_web_events']:
        assert admin.execute('select count(*) from runtime.'+table).fetchone()==(1,)
    with p.pool.connection() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from runtime.nexloop_native_web_events')

def test_native_event_changed_body_or_conversation_is409_keeps_original(conversations,admin):
    p=port(conversations);c=conversation(conversations);event=str(uuid.uuid4())
    original=p.accept_native_message(conversation_id=c['id'],provider_event_id=event,idempotency_key='transport-original-key',body='original')
    for conversation_id,body in [(c['id'],'different'),(p.create_conversation(idempotency_key='new-conversation-key')['id'],'original')]:
        with pytest.raises(ConversationConflict):p.accept_native_message(conversation_id=conversation_id,provider_event_id=event,idempotency_key='different-transport-key',body=body)
    # NX-051 read projection: the native event is the provider reference (client trust), no reply link.
    projection={'reply_to_message_id':None,'provider':{'namespace':'native.webchat','message_ref':event,'sequence':None,'sent_at':None,'trust':'client','skewed':False}}
    assert p.read_messages(conversation_id=c['id'])['items']==[{**original['message'],**projection}]
    assert admin.execute('select count(*) from runtime.nexloop_native_web_events').fetchone()==(1,)

from test_web_chat_http import actual_chat,chat,browser,login,ORIGIN
from multi_browser_conversation_fixture import two_browser_consumers,receipt_plan,message_plan

def test_actual_http_native_event_id_replay_conflict_and_namespace_rejected(actual_chat):
    client,headers,f=actual_chat
    c=client.post('/api/v1/conversations',headers={**headers,'Idempotency-Key':'native-http-conversation'},json={});assert c.status_code==200
    endpoint='/api/v1/conversations/'+c.json()['id']+'/native-messages';body={'schema_version':'nexloop.native-message.v1','provider_event_id':str(uuid.uuid4()),'body':'actual native provider event'}
    a=client.post(endpoint,headers={**headers,'Idempotency-Key':'native-transport-first'},json=body);assert a.status_code==202
    b=client.post(endpoint,headers={**headers,'Idempotency-Key':'native-transport-second'},json=body);assert b.status_code==202 and b.json()=={**a.json(),'created':False}
    conflict=client.post(endpoint,headers={**headers,'Idempotency-Key':'native-transport-third'},json={**body,'body':'replacement'});assert conflict.status_code==409
    for field in ['provider_namespace','actor','tenant_id']:
        assert client.post(endpoint,headers={**headers,'Idempotency-Key':'native-transport-fourth'},json={**body,field:'forged'}).status_code==422

def test_two_actual_humans_same_provider_id_isolated_and_foreign_conversation_denied(two_browser_consumers,admin):
    f=two_browser_consumers;event=str(uuid.uuid4());results=[]
    for index,client in enumerate(f['clients']):
        endpoint='/api/v1/conversations/'+f['conversations'][index]['id']+'/native-messages'
        result=client.post(endpoint,headers={**f['headers'][index],'Idempotency-Key':'same-native-transport'},json={'schema_version':'nexloop.native-message.v1','provider_event_id':event,'body':'same provider identifier independent actor'})
        assert result.status_code==202;results.append(result.json())
    assert results[0]['message']['id']!=results[1]['message']['id']
    assert admin.execute('select count(distinct principal_id) from runtime.nexloop_native_web_events where provider_event_id=%s',(event,)).fetchone()==(2,)
    denied=f['clients'][1].post('/api/v1/conversations/'+f['conversations'][0]['id']+'/native-messages',headers={**f['headers'][1],'Idempotency-Key':'native-foreign-conversation'},json={'schema_version':'nexloop.native-message.v1','provider_event_id':event,'body':'same provider identifier independent actor'})
    assert denied.status_code in (403,409)

from contextlib import contextmanager
import multiprocessing,signal,time
from psycopg.conninfo import make_conninfo
from nexloop_eios.assembly import open_core
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.postgres_artifacts import AuthoritySigner

def _native_child(dsn,identity_dsn,token,tenant,application,material,key_id,arguments,pipe):
    with open_core(dsn) as pool:
        from nexloop_eios.browser_identity import open_browser_identity
        from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
        from eios.identity.sessions import BrowserSessionService
        from eios.identity.ports import TrustedIdentityOperator
        with open_browser_identity(identity_dsn) as identity_pool:
            store=PostgresBrowserSessionUnitOfWork(identity_pool,tenant_id=tenant,application_id=application)
            operator=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id=str(uuid.uuid4()),trace_id=str(uuid.uuid4()))
            inspected=BrowserSessionService(store,store,application_id=application,operator=operator).inspect(token)
        human=authenticate_browser_business(pool,inspected,world='real')
        result=NativeWebMessagePort(pool,human,AuthoritySigner(key_id,material)).accept_native_message(**arguments)
        pipe.send(('committed',result));time.sleep(60)

@contextmanager
def native_process(f,arguments):
    ctx=multiprocessing.get_context('spawn');parent,child=ctx.Pipe();p=ctx.Process(target=_native_child,args=(make_conninfo(f['pg'],user='nexloop_api'),f['base']['identity'][0],f['base']['issued'].session_token.get_secret_value(),f['base']['issued'].session.tenant_id,f['base']['issued'].session.application_id,f['reader'].signer.material,f['reader'].signer.key_id,arguments,child));p.start();child.close()
    try:yield p,parent
    finally:
        if p.is_alive():p.kill()
        p.join(10);assert not p.is_alive();parent.close()

@pytest.mark.parametrize('window',['before_commit','after_commit'])
def test_actual_native_accept_sigkill_exact_provider_id_recovery(conversations,admin,window):
    f=conversations;p=port(f);c=conversation(f);event=str(uuid.uuid4());arguments=dict(conversation_id=c['id'],provider_event_id=event,idempotency_key='native-crash-original',body='stable native crash input')
    if window=='before_commit':
        admin.execute("create function public.native_owned_pause() returns trigger language plpgsql as $$begin perform pg_sleep(3);return new;end$$")
        admin.execute('grant execute on function public.native_owned_pause() to nexloop_owner')
        admin.execute('create trigger native_owned_pause before insert on runtime.nexloop_native_web_events for each row execute function public.native_owned_pause()')
    with native_process(f,arguments) as (process,pipe):
        if window=='before_commit':
            until=time.monotonic()+10
            while not admin.execute("select exists(select 1 from pg_stat_activity where usename='nexloop_api' and wait_event='PgSleep')").fetchone()[0]:
                assert time.monotonic()<until;time.sleep(.02)
            assert not pipe.poll(),'uncommitted provider acceptance acknowledged'
        else:
            assert pipe.poll(10);state,original=pipe.recv();assert state=='committed'
        process.kill();process.join(10);assert process.exitcode==-signal.SIGKILL
    if window=='before_commit':
        # The INSERT trigger paused before COMMIT; transaction abort must remove all formal/technical records.
        admin.execute('drop trigger native_owned_pause on runtime.nexloop_native_web_events')
        assert admin.execute("select count(*) from ontology.objects where type_name='Message'").fetchone()==(0,)
    recovered=p.accept_native_message(**{**arguments,'idempotency_key':'native-crash-new-transport'})
    assert recovered['created'] is (window=='before_commit')
    if window=='after_commit':assert recovered['message']==original['message']
    for table in ['nexloop_native_web_events','nexloop_message_inbox','nexloop_message_outbox','nexloop_conversation_messages']:
        assert admin.execute('select count(*) from runtime.'+table).fetchone()==(1,)


def test_legacy_derived_key_cannot_ack_unregistered_native_event(conversations,admin):
    import hashlib
    from nexloop_eios.postgres_artifacts import canonical_payload
    f=conversations;p=port(f);c=conversation(f);event=str(uuid.uuid4());auth=p.session.authentication
    name=canonical_payload([auth.tenant_id,'real','native.webchat',auth.subject_principal_id,event]);key='native-'+hashlib.sha256(name.encode()).hexdigest()
    f['port'].accept_message(conversation_id=c['id'],idempotency_key=key,body='legacy occupied canonical key')
    with pytest.raises(ConversationDenied):p.accept_native_message(conversation_id=c['id'],idempotency_key='new-native-transport',provider_event_id=event,body='legacy occupied canonical key')
    assert admin.execute('select count(*) from runtime.nexloop_native_web_events').fetchone()==(0,)


def test_transport_key_itself_remains_payload_bound(conversations,admin):
    f=conversations;p=port(f);c=conversation(f);event=str(uuid.uuid4());args=dict(conversation_id=c['id'],idempotency_key='transport-fixed-event',provider_event_id=event,body='original transport')
    original=p.accept_native_message(**args)
    with pytest.raises(ConversationConflict):p.accept_native_message(**{**args,'provider_event_id':str(uuid.uuid4())})
    # NX-051 read projection: the native event is the provider reference (client trust), no reply link.
    projection={'reply_to_message_id':None,'provider':{'namespace':'native.webchat','message_ref':event,'sequence':None,'sent_at':None,'trust':'client','skewed':False}}
    assert p.read_messages(conversation_id=c['id'])['items']==[{**original['message'],**projection}]
