"""NX-051 (AT-014): extraction over a SYNTHETIC signed channel namespace (dispatcher ruling 5).

No real channel is connected (ruling 4). The signed adapter is stood in for by the extension point
runtime.nexloop_record_provider_facts, called inside the governed Message commit transaction (the only place
an adapter may record it: the deferred default trigger then finds the adapter's row and adds nothing). The test
grants that EXECUTE (and runtime schema USAGE) to the API role for the duration of the test only; production keeps it owner-only.
Real-channel evidence is added when a real channel is connected.
"""
import hashlib
import json
from datetime import UTC,datetime,timedelta

import pytest
from nexloop_eios import claim_store
from nexloop_eios.conversation_extraction import build_user_payload
from test_conversation_messages import conversations
from test_browser_business_authorization import browser_business
from test_action_definitions import published_action
from test_browser_identity_reads import identity
from test_browser_session_creation import uow
from test_claim_store_pg import extractor_for,registered

NAMESPACE='synthetic.signed'


@pytest.fixture
def signed_channel(conversations,admin,monkeypatch):
    admin.execute("insert into control.nexloop_provider_namespaces(namespace,trust,skew_seconds,published_by) values(%s,'signed',300,'nx051 synthetic test') on conflict do nothing",(NAMESPACE,))
    admin.execute('grant usage on schema runtime to nexloop_api')
    admin.execute('grant execute on function runtime.nexloop_record_provider_facts(text,text,text,text,text,bigint,timestamptz) to nexloop_api')
    port=conversations['port'];original=port._call;channel={}
    def adapter(db,verb,action,**arguments):
        result=original(db,verb,action,**arguments)
        if verb=='commit_message' and arguments['idempotency_key'] in channel:
            seq,sent,ref=channel[arguments['idempotency_key']]
            db.execute('select runtime.nexloop_record_provider_facts(%s,%s,%s,%s,%s,%s,%s)',
                (port.session.authentication.tenant_id,port.session.world,result['message']['id'],NAMESPACE,ref,seq,sent))
        return result
    monkeypatch.setattr(port,'_call',adapter)
    def send(conversation_id,key,body,seq,sent=None,ref=None):
        channel[key]=(seq,sent,ref or 'sig-'+key)
        return port.accept_message(conversation_id=conversation_id,idempotency_key=key,body=body)['message']
    yield port,send
    admin.execute('revoke execute on function runtime.nexloop_record_provider_facts(text,text,text,text,text,bigint,timestamptz) from nexloop_api')
    admin.execute('revoke usage on schema runtime from nexloop_api')


CORRECTION={'topics':[{'topic':'预算','conversation_summary':'预算更正','user_valid_reply':True,'message_refs':[1,2,3]}],
  'claims':[{'topic_index':0,'message_ref':3,'quote':'预算两千左右','kind':'constraint','predicate':'预算上限','value':{'type':'money','value':{'amount':2000,'currency':'CNY'}}},
            {'topic_index':0,'message_ref':2,'quote':'说错了，预算是三千','kind':'correction','predicate':'预算上限','value':{'type':'money','value':{'amount':3000,'currency':'CNY'}},'corrects':0},
            {'topic_index':0,'message_ref':1,'quote':'明天下午三点给我回电话','kind':'intent','predicate':'回电时间','value':{'type':'string','value':'回电'},'time_expression':'明天下午三点'}]}


def test_signed_channel_orders_payload_marks_late_and_drives_correction_and_anchor(signed_channel,conversations,admin):
    port,send=signed_channel
    conversation_id=port.create_conversation(idempotency_key='nx051-signed-conversation')['id']
    sent=datetime.now(UTC)-timedelta(seconds=120)
    # Receipt order r1,r2,r3; the channel says r3 was sent before r2 (it arrived late).
    r1=send(conversation_id,'nx051-signed-message-1','明天下午三点给我回电话。',1,sent)
    r2=send(conversation_id,'nx051-signed-message-2','说错了，预算是三千。',3,sent+timedelta(seconds=20))
    r3=send(conversation_id,'nx051-signed-message-3','预算两千左右。',2,sent+timedelta(seconds=10))
    ids=[r1['id'],r2['id'],r3['id']]
    assert [m['sequence'] for m in (r1,r2,r3)]==[1,2,3]
    # The adapter's row won; the deferred default added nothing; receipt order is not rewritten.
    assert admin.execute('select message_id,provider_namespace,trust,provider_sequence,skewed from runtime.nexloop_message_provider_facts order by provider_sequence').fetchall()==[
        (r1['id'],NAMESPACE,'signed',1,False),(r3['id'],NAMESPACE,'signed',2,False),(r2['id'],NAMESPACE,'signed',3,False)]
    assert admin.execute("select message_id,(record->>'sequence')::int from runtime.nexloop_conversation_messages order by 2").fetchall()==[(r1['id'],1),(r2['id'],2),(r3['id'],3)]
    items=port.read_messages(conversation_id=conversation_id)['items']
    assert [i['sequence'] for i in items]==[1,2,3] and [i['provider']['sequence'] for i in items]==[1,3,2]
    extractor,_=extractor_for(conversations,admin,conversation_id,ids,None,suffix='-nx051-signed')
    context,window=extractor.load_window(conversation_id,ids)
    payload=json.loads(build_user_payload(window,context))['conversation_data']
    assert [(row['ref'],row['channel_order'],row['late']) for row in payload]==[(1,1,False),(3,2,True),(2,3,False)]
    assert payload[0]['time']==sent.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).isoformat(timespec='minutes')
    registered(extractor,conversation_id,ids,CORRECTION)
    result=extractor.extract(conversation_id=conversation_id,message_ids=ids)
    assert result['replay'] is False
    rows={kind:(claim_id,corrects,flags) for claim_id,kind,corrects,flags in admin.execute('select claim_id,epistemic_kind,corrects_claim_id,guard_flags from ontology.nexloop_claims').fetchall()}
    # The correction is earlier in receipt order than its target, later in the signed channel order: accepted by both checks.
    assert rows['correction'][1]==rows['constraint'][0] and 'correction_order_signed_channel' in rows['correction'][2]
    assert 'anchor_signed_channel_time' in rows['intent'][2]
    run=admin.execute('select rejected from ontology.nexloop_extraction_runs').fetchone()[0]
    assert not any(r.get('reason')=='correction_target_unavailable' for r in run)


def test_server_rejects_correction_against_effective_order_even_if_client_skips_check(signed_channel,conversations,admin,monkeypatch):
    """0121: same rule in SQL. With receipt order only (no signed facts) a forged earlier-target correction is refused."""
    port=conversations['port']
    conversation_id=port.create_conversation(idempotency_key='nx051-receipt-conversation')['id']
    ids=[port.accept_message(conversation_id=conversation_id,idempotency_key=f'nx051-receipt-message-{i}',body=b)['message']['id']
         for i,b in enumerate(['说错了，预算是三千。','预算两千左右。'])]
    extractor,_=extractor_for(conversations,admin,conversation_id,ids,None,suffix='-nx051-receipt')
    body={'topics':[{'topic':'预算','conversation_summary':'预算','user_valid_reply':True,'message_refs':[1,2]}],
          'claims':[{'topic_index':0,'message_ref':2,'quote':'预算两千左右','kind':'constraint','predicate':'预算上限','value':{'type':'money','value':{'amount':2000,'currency':'CNY'}}},
                    {'topic_index':0,'message_ref':1,'quote':'说错了，预算是三千','kind':'correction','predicate':'预算上限','value':{'type':'money','value':{'amount':3000,'currency':'CNY'}}}]}
    registered(extractor,conversation_id,ids,body)
    from test_claim_store_pg import tampered
    def forged(claims):
        target=next(c for c in claims if c['epistemic_kind']=='constraint')
        for c in claims:
            if c['epistemic_kind']=='correction':c['corrects_claim_id']=target['claim_id']
        # list the target first so only the effective-order rule (not "recorded earlier in the run") can refuse it
        return sorted(claims,key=lambda c:c['epistemic_kind']!='constraint')
    forged=tampered(forged)
    monkeypatch.setattr(claim_store,'normalize',forged)
    with pytest.raises(claim_store.ClaimExtractionDenied):extractor.extract(conversation_id=conversation_id,message_ids=ids)
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone()==(0,)


def test_late_reply_resolution_is_a_new_input_version_without_duplicate_claims(signed_channel,conversations,admin):
    """AT-020 regression: a reply link resolved later changes the evidence, so a new input version; Claim identity is unchanged."""
    port,send=signed_channel
    conversation_id=port.create_conversation(idempotency_key='nx051-late-conversation')['id']
    first=port.accept_message(conversation_id=conversation_id,idempotency_key='nx051-late-message-1',body='预算两千左右。')['message']
    # A pending provider-ref reply on the first message, resolved when the referenced message is recorded.
    admin.execute("select runtime.nexloop_record_reply_link(%s,'real',%s,'provider_ref','sig-late-target','provider')",(port.session.authentication.tenant_id,first['id']))
    ids=[first['id']]
    extractor,_=extractor_for(conversations,admin,conversation_id,ids,None,suffix='-nx051-late')
    body={'topics':[{'topic':'预算','conversation_summary':'预算','user_valid_reply':True,'message_refs':[1]}],
          'claims':[{'topic_index':0,'message_ref':1,'quote':'预算两千左右','kind':'constraint','predicate':'预算上限','value':{'type':'money','value':{'amount':2000,'currency':'CNY'}}}]}
    registered(extractor,conversation_id,ids,body)
    before=extractor.extract(conversation_id=conversation_id,message_ids=ids)
    # Seeding the extractor's authority republished application facts: the browser Human re-authenticates (as after any login).
    from nexloop_eios.browser_authorization import authenticate_browser_business
    port.session=authenticate_browser_business(conversations['reader'].pool,conversations['base']['issued'].session,world='real')
    target=send(conversation_id,'nx051-late-message-2','这是被引用的那条。',1,ref='sig-late-target')
    assert admin.execute('select resolution,reply_to_message_id from runtime.nexloop_message_reply_links where message_id=%s order by event_id',(first['id'],)).fetchall()==[('pending',None),('resolved',target['id'])]
    context,window=extractor.load_window(conversation_id,ids)
    assert window[0].reply_to==target['id'] and json.loads(build_user_payload(window,context))['conversation_data'][0]['reply_to_ref']=='outside_window'
    registered(extractor,conversation_id,ids,body)
    after=extractor.extract(conversation_id=conversation_id,message_ids=ids)
    assert after['replay'] is False and after['input_digest']!=before['input_digest'] and after['claim_ids']==before['claim_ids']
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone()==(1,)
    assert admin.execute('select count(*) from ontology.nexloop_extraction_runs').fetchone()==(2,)
