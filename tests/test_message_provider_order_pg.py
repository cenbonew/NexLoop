"""NX-051 (AT-014): provider order/time/reference facts and reply links, kept beside the receipt order.

Native WebChat client values are client-level evidence only (ruling 3): they never reorder anything.
Synthetic data only; no real channel (ruling 4).
"""
import uuid
import pytest
import psycopg
from nexloop_eios.native_web_inbound import NativeWebMessagePort
from nexloop_eios.conversation_messages import ConversationConflict
from test_conversation_messages import conversations,conversation,browser_business,published_action,identity,uow
from test_web_chat_http import actual_chat,chat,browser,login,ORIGIN

def port(f):return NativeWebMessagePort(f['port'].pool,f['port'].session,f['port'].signer)
def send(p,c,body,**client):
    event=client.pop('event',None) or str(uuid.uuid4())
    return p.accept_native_message(conversation_id=c['id'],provider_event_id=event,idempotency_key='t-'+uuid.uuid4().hex[:24],body=body,**client)
def links(admin,message):
    return admin.execute('select raw_kind,raw_ref,reply_to_message_id,resolution,source from runtime.nexloop_message_reply_links where message_id=%s order by event_id',(message,)).fetchall()

def test_out_of_order_client_sequence_keeps_receipt_order_and_both_orders_are_visible(conversations,admin):
    p=port(conversations);c=conversation(conversations)
    a=send(p,c,'stated second',client_sequence=2,client_sent_at='2026-10-10T01:00:02Z')['message']
    b=send(p,c,'stated first',client_sequence=1,client_sent_at='2026-10-10T01:00:01Z')['message']
    items=p.read_messages(conversation_id=c['id'])['items']
    assert [(i['id'],i['sequence']) for i in items]==[(a['id'],1),(b['id'],2)]  # receipt order, never rewritten
    assert [(i['provider']['namespace'],i['provider']['sequence'],i['provider']['trust']) for i in items]==[('native.webchat',2,'client'),('native.webchat',1,'client')]
    assert all(i['reply_to_message_id'] is None for i in items)
    assert admin.execute('select message_id,(record->>\'sequence\')::int from runtime.nexloop_conversation_messages order by 2').fetchall()==[(a['id'],1),(b['id'],2)]
    assert admin.execute('select count(*) from runtime.nexloop_message_provider_facts').fetchone()==(2,)

def test_v1_native_and_plain_api_messages_get_server_default_facts(conversations,admin):
    p=port(conversations);c=conversation(conversations);event=str(uuid.uuid4())
    native=send(p,c,'v1 native body',event=event)['message']
    plain=conversations['port'].accept_message(conversation_id=c['id'],idempotency_key='plain-api-message',body='plain api body')['message']
    view={i['id']:i for i in p.read_messages(conversation_id=c['id'])['items']}
    assert view[native['id']]['provider']=={'namespace':'native.webchat','message_ref':event,'sequence':None,'sent_at':None,'trust':'client','skewed':False}
    assert view[plain['id']]['provider']=={'namespace':'nexloop.api','message_ref':None,'sequence':None,'sent_at':None,'trust':'server','skewed':False}

def test_reply_links_resolved_pending_late_foreign_and_self(conversations,admin):
    p=port(conversations);c=conversation(conversations);other=p.create_conversation(idempotency_key='second-conversation-key')
    a=send(p,c,'original question')['message']
    b=send(p,c,'quoting the question',reply_to={'kind':'message','ref':a['id']})['message']
    assert links(admin,b['id'])==[('message',a['id'],a['id'],'resolved','provider')]
    # pending: references a provider event this conversation has not seen yet; resolved later by an appended event
    late=str(uuid.uuid4())
    pending=send(p,c,'reply to something not yet arrived',reply_to={'kind':'provider_event','ref':late})['message']
    first=links(admin,pending['id']);assert first==[('provider_ref',late,None,'pending','provider')]
    assert {i['id']:i for i in p.read_messages(conversation_id=c['id'])['items']}[pending['id']]['reply_to_message_id'] is None
    arrived=send(p,c,'the late original',event=late)['message']
    assert links(admin,pending['id'])==first+[('provider_ref',late,arrived['id'],'resolved','provider')]  # original row unchanged
    assert {i['id']:i for i in p.read_messages(conversation_id=c['id'])['items']}[pending['id']]['reply_to_message_id']==arrived['id']
    # another conversation's message: recorded as foreign, nothing exposed
    foreign=send(p,other,'cross conversation',reply_to={'kind':'message','ref':a['id']})['message']
    assert links(admin,foreign['id'])==[('message',a['id'],None,'foreign_conversation','provider')]
    assert p.read_messages(conversation_id=other['id'])['items'][0]['reply_to_message_id'] is None
    # self reference through its own provider event: unknown, never a self link
    own=str(uuid.uuid4())
    self_ref=send(p,c,'points at itself',event=own,reply_to={'kind':'provider_event','ref':own})['message']
    assert links(admin,self_ref['id'])==[('provider_ref',own,None,'unknown','provider')]

def test_skewed_client_time_is_flagged_and_message_still_accepted(conversations,admin):
    p=port(conversations);c=conversation(conversations)
    m=send(p,c,'clock far behind',client_sequence=1,client_sent_at='2020-01-01T00:00:00Z')
    assert m['created'] is True
    item=p.read_messages(conversation_id=c['id'])['items'][0]
    assert item['provider']['skewed'] is True and item['provider']['sent_at'].startswith('2020-01-01T00:00:00')
    assert item['accepted_at']==m['message']['accepted_at']  # receipt time untouched

def test_replay_records_facts_once_and_ignores_restated_client_values(conversations,admin):
    p=port(conversations);c=conversation(conversations);event=str(uuid.uuid4())
    first=send(p,c,'same native event',event=event,client_sequence=5)
    again=send(p,c,'same native event',event=event,client_sequence=9,client_sent_at='2026-10-10T00:00:00Z')
    assert again=={**first,'created':False}
    assert admin.execute('select provider_sequence,provider_sent_at from runtime.nexloop_message_provider_facts').fetchall()==[(5,None)]
    assert admin.execute('select count(*) from runtime.nexloop_native_web_events').fetchone()==(1,)

def test_invalid_client_fields_write_nothing(conversations,admin):
    p=port(conversations);c=conversation(conversations)
    for client in [dict(client_sequence=0),dict(client_sequence=True),dict(client_sent_at='yesterday'),dict(reply_to={'kind':'message','ref':'x'}),
                   dict(reply_to={'kind':'thread','ref':'a'*64}),dict(reply_to={'kind':'message','ref':'a'*64,'extra':1})]:
        with pytest.raises(Exception):send(p,c,'bad client fields',**client)
    for table in ['nexloop_conversation_messages','nexloop_native_web_events','nexloop_message_provider_facts','nexloop_message_reply_links']:
        assert admin.execute('select count(*) from runtime.'+table).fetchone()==(0,)

def test_facts_and_links_append_only_and_not_reachable_by_runtime_roles(conversations,admin):
    p=port(conversations);c=conversation(conversations)
    a=send(p,c,'first')['message'];send(p,c,'second',reply_to={'kind':'message','ref':a['id']})
    for statement in ["update runtime.nexloop_message_provider_facts set provider_sequence=1","delete from runtime.nexloop_message_provider_facts",
                      "update runtime.nexloop_message_reply_links set resolution='unknown'","delete from runtime.nexloop_message_reply_links",
                      "update control.nexloop_provider_namespaces set trust='signed'"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):admin.execute(statement)
    with p.pool.connection() as db:
        for table in ['runtime.nexloop_message_provider_facts','runtime.nexloop_message_reply_links','control.nexloop_provider_namespaces']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from '+table)
            db.rollback()
        for call in ["select runtime.nexloop_record_provider_facts('t','real',%s,'nexloop.api',null,1,null)",
                     "select runtime.nexloop_record_reply_link('t','real',%s,'message',%s,'provider')"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute(call,(a['id'],)*call.count('%s'))
            db.rollback()
        # the client-facts entry point rejects an unsigned envelope and someone else's message
        with pytest.raises(psycopg.Error):db.execute("select authz.nexloop_native_client_facts('x','real','{}','sig','{}')")

def test_http_native_v2_accepted_v1_still_accepted_bad_v2_is_4xx_without_writes(actual_chat,admin):
    client,headers,f=actual_chat
    c=client.post('/api/v1/conversations',headers={**headers,'Idempotency-Key':'nx051-http-conversation'},json={});assert c.status_code==200
    endpoint='/api/v1/conversations/'+c.json()['id']+'/native-messages'
    v1={'schema_version':'nexloop.native-message.v1','provider_event_id':str(uuid.uuid4()),'body':'v1 body'}
    a=client.post(endpoint,headers={**headers,'Idempotency-Key':'nx051-http-transport-v1'},json=v1);assert a.status_code==202
    v2={'schema_version':'nexloop.native-message.v2','provider_event_id':str(uuid.uuid4()),'body':'v2 body','client_sequence':1,'client_sent_at':'2026-10-10T01:00:00.123Z','reply_to':{'kind':'message','ref':a.json()['message']['id']}}
    b=client.post(endpoint,headers={**headers,'Idempotency-Key':'nx051-http-transport-v2'},json=v2);assert b.status_code==202
    before=admin.execute('select count(*) from runtime.nexloop_conversation_messages').fetchone()
    for index,bad in enumerate([{**v2,'client_sequence':0},{**v2,'reply_to':{'kind':'thread','ref':'x'}},{k:v for k,v in v2.items() if k!='reply_to'},
                                {**v2,'schema_version':'nexloop.native-message.v1'},{**v1,'schema_version':'nexloop.native-message.v2'},{**v2,'client_sent_at':'not a time'}]):
        r=client.post(endpoint,headers={**headers,'Idempotency-Key':'nx051-http-transport-bad-%d'%index},json={**bad,'provider_event_id':str(uuid.uuid4())})
        assert 400<=r.status_code<500,(index,r.status_code)
    assert admin.execute('select count(*) from runtime.nexloop_conversation_messages').fetchone()==before
    items=client.get('/api/v1/conversations/'+c.json()['id']+'/messages',headers=headers).json()['items']
    assert [i['sequence'] for i in items]==[1,2] and items[1]['reply_to_message_id']==items[0]['id']
    assert items[1]['provider']['sequence']==1 and items[1]['provider']['trust']=='client' and items[0]['provider']['sequence'] is None
