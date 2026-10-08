"""Same-tenant two genuine HUMAN/cookie/Consumer ownership negative proofs."""
import json
import pytest
from multi_browser_conversation_fixture import two_browser_consumers,receipt_plan,message_plan
from nexloop_eios.conversation_runtime_bridge import MessageRuntimeUnavailable,canonical_event_id


def receipt_path(message):return '/api/v1/messages/'+message['id']+'/receipt'


def test_two_real_humans_read_own_messages_not_other_consumer_receipt(two_browser_consumers,admin):
    f=two_browser_consumers;clients=f['clients'];messages=f['messages'];conversations=f['conversations'];base=f['base']
    assert f['humans'][0]['subject'].subject_id!=f['humans'][1]['subject'].subject_id
    assert f['humans'][0]['membership'].principal_id!=f['humans'][1]['membership'].principal_id and f['consumers'][0]!=f['consumers'][1]
    for index,client in enumerate(clients):
        own=client.get('/api/v1/conversations/'+conversations[index]['id']+'/messages')
        assert own.status_code==200 and own.json()['items']==[messages[index]]
        result=client.get(receipt_path(messages[index]));assert result.status_code==200
        assert result.json()=={'message_id':messages[index]['id'],'run':None,'receipt':None}
    f['bridge'].bind_message(message_id=messages[0]['id'],run_token=f['run'].token,command=f['command'])
    queued=f['bridge'].deliver_one(run_token=f['run'].token)
    actual=base['api'].authenticate_run(f['run'].token,world='real',run_id=f['run'].run_id)
    intent=actual.submit_effect_intent(parameters={'message':'one synthetic same-tenant isolated service'})
    own_receipt=clients[0].get(receipt_path(messages[0]))
    assert own_receipt.status_code==200 and own_receipt.json()['run']['task_id']==queued['task_id']
    assert own_receipt.json()['receipt']['intent_id']==intent['intent_id'] and own_receipt.json()['receipt']['business_action_success'] is False
    other=clients[1].get(receipt_path(messages[0]));assert other.status_code==403
    assert intent['intent_id'] not in other.text and queued['run_id'] not in other.text
    assert clients[0].get(receipt_path(messages[1])).status_code==403
    assert clients[1].get('/api/v1/conversations/'+conversations[0]['id']+'/messages').status_code==403
    own_b=clients[1].get(receipt_path(messages[1]));assert own_b.status_code==200 and own_b.json()['run'] is None
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
    assert admin.execute('select count(*) from control.nexloop_consumer_owners where principal_id=any(%s)',([h['membership'].principal_id for h in f['humans']],)).fetchone()==(2,)


@pytest.mark.parametrize('consumer',['source_consumer','message_consumer'])
def test_actual_b_message_cannot_bind_a_source_run_across_consumer(two_browser_consumers,admin,consumer):
    f=two_browser_consumers;message=f['messages'][1];conversation=f['conversations'][1];base=f['base']
    event=canonical_event_id(base['tenant'],'real',conversation['id']+':'+str(message['sequence']))
    command={**f['command'],'trigger_event_id':event}
    if consumer=='message_consumer':command['consumer_ref']='consumer:'+f['consumers'][1]
    with pytest.raises(MessageRuntimeUnavailable):f['bridge'].bind_message(message_id=message['id'],run_token=f['run'].token,command=command)
    assert admin.execute('select count(*) from runtime.nexloop_message_routes where message_id=%s',(message['id'],)).fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_inbox where source_id='webchat'").fetchone()==(0,)
    own=f['clients'][1].get(receipt_path(message));assert own.status_code==200 and own.json()['run'] is None


def test_existing_run_cannot_be_rebound_to_second_actual_owned_message(two_browser_consumers,admin):
    f=two_browser_consumers;first=f['messages'][0];client=f['clients'][0];conversation=f['conversations'][0]
    f['bridge'].bind_message(message_id=first['id'],run_token=f['run'].token,command=f['command'])
    accepted=client.post('/api/v1/conversations/'+conversation['id']+'/messages',
        headers={**f['headers'][0],'Idempotency-Key':'isolation-second-actual-message'},json={'body':'second independently committed statement'})
    assert accepted.status_code==202;second=accepted.json()['message']
    assert second['id']!=first['id'] and second['sequence']==first['sequence']+1
    command={**f['command'],'trigger_event_id':canonical_event_id(f['base']['tenant'],'real',conversation['id']+':'+str(second['sequence']))}
    with pytest.raises(MessageRuntimeUnavailable):f['bridge'].bind_message(message_id=second['id'],run_token=f['run'].token,command=command)
    assert admin.execute('select message_id from runtime.nexloop_message_routes').fetchall()==[(first['id'],)]
    assert admin.execute("select count(*) from runtime.nexloop_inbox where source_id='webchat'").fetchone()==(0,)
    projection=client.get(receipt_path(second));assert projection.status_code==200 and projection.json()['run'] is None
