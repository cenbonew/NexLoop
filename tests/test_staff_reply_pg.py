"""NX-028 ruling B (0153) on clean catalog PostgreSQL: a staff reply during a takeover is a governed human Action that writes
the Message and the stream directly (native WebChat only), bound to one inbound message, under ADR-023 §2.6/§2.7 shared with
the Agent and fallback replies.

commitment_fixture (actual governed effect executor), real browser HUMAN owner session; inbound stream entries are admin
seeds of the 0046 write (the 0109 and NX-051 triggers run on them); time is injected only where a test says so.
"""
from datetime import UTC,datetime,timedelta
import hashlib,secrets

import psycopg
import pytest
from commitment_fixture import TAKEOVER_ACTIONS,TENANT,commitments,publish_request_actions  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_contact_effect_categories_pg import contact_check
from test_takeover_pg import give_back,owner,take

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)


def reply(actions,conversation,reply_to,text,key):
    return actions.send_staff_reply(action_name='nexloop.message.staff_send',action_version=1,request_id=key,conversation_id=conversation,reply_to=reply_to,text=text)


def stream(admin,conversation):
    return admin.execute("select sequence,message_id,record->>'sender_kind',record->>'actor',record->>'trigger_message_id' from runtime.nexloop_conversation_messages where conversation_id=%s order by sequence",(conversation,)).fetchall()


def setup(c,identity,uow):
    actions,service=owner(c,identity,uow)
    conversation=c.conversation(c['consumer'])
    return actions,service,conversation


def test_staff_reply_is_a_message_in_the_stream_and_settles_the_pending_reply(commitments,admin,identity,uow):
    """AT-044: the person answers; the Agent does not; the customer sees the staff reply."""
    c=commitments;actions,service,conversation=setup(c,identity,uow)
    started=take(actions,'conversation',conversation,'take-staff')
    inbound=c.inbound('我的退款到了吗？')
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='reply-due' and item_key=%s",('reply:'+inbound,)).fetchone()==(1,)
    actions,human=c.owner(identity,uow)
    staff=human.authentication.subject_principal_id
    created=reply(actions,conversation,inbound,'您好，我是人工客服，退款已在处理。','staff-reply-1')
    message=created['message'];assert created['created'] is True
    assert message['sender_kind']=='human_takeover' and message['actor']==staff and message['trigger_message_id']==inbound and 'intent_id' not in message
    assert stream(admin,conversation)[-1]==(message['sequence'],message['id'],'human_takeover',staff,inbound)
    obj=admin.execute("select properties from ontology.objects where type_name='Message' and object_id=%s",(message['id'],)).fetchone()[0]
    assert obj['actor']==staff and obj['body']=='您好，我是人工客服，退款已在处理。' and obj['sequence']==message['sequence']
    # ADR-023 §2.7: answered, so no pending reply and no fallback Run will start.
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='reply-due' and item_key=%s",('reply:'+inbound,)).fetchone()==(0,)
    # NX-051 evidence: server-level staff namespace and a server reply link; no relay planning for the person's words.
    assert admin.execute('select provider_namespace,trust from runtime.nexloop_message_provider_facts where message_id=%s',(message['id'],)).fetchone()==('nexloop.staff','server')
    assert admin.execute("select reply_to_message_id,source from runtime.nexloop_message_reply_links where message_id=%s",(message['id'],)).fetchone()==(inbound,'server')
    assert admin.execute('select count(*) from runtime.nexloop_message_outbox where message_id=%s',(message['id'],)).fetchone()==(0,)
    assert admin.execute('select runtime.nexloop_staff_message_accepted(%s,%s,%s)',(TENANT,'real',message['id'])).fetchone()==(True,)
    # Replay of the same governed request: answered from its terminal outcome, nothing new is written.
    again=reply(actions,conversation,inbound,'您好，我是人工客服，退款已在处理。','staff-reply-1')
    assert again.get('replayed') is True and len([r for r in stream(admin,conversation) if r[2]=='human_takeover'])==1


def test_only_the_author_of_an_active_takeover_replies(commitments,admin,identity,uow):
    c=commitments;actions,service,conversation=setup(c,identity,uow)
    inbound=c.inbound('在吗')
    actions,_=c.owner(identity,uow)
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='author of the active takeover'):reply(actions,conversation,inbound,'x','staff-no-takeover')
    started=take(actions,'conversation',conversation,'take-author')
    service=c.service_actions_session(service)
    with pytest.raises(Exception,match='human goal authority required'):reply(service,conversation,inbound,'x','staff-service')
    actions,_=c.owner(identity,uow)
    give_back(actions,started['takeover_id'],'handback-author')
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='author of the active takeover'):reply(actions,conversation,inbound,'x','staff-after-handback')
    with pytest.raises(ValueError):reply(actions,conversation,None,'x','staff-unbound')  # bound to one inbound message (v0.1)
    assert not [r for r in stream(admin,conversation) if r[2]=='human_takeover']


def test_non_webchat_conversation_is_refused(commitments,admin,identity,uow):
    """Ruling B is native WebChat only: a conversation with a message from another channel is refused (NXC07)."""
    c=commitments;actions,service,conversation=setup(c,identity,uow)
    take(actions,'conversation',conversation,'take-sms')
    admin.execute("insert into control.nexloop_provider_namespaces(namespace,trust,skew_seconds,published_by) values('synthetic.sms','signed',60,'synthetic channel')")
    message=hashlib.sha256(secrets.token_bytes(8)).hexdigest()
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        seq=admin.execute('update runtime.nexloop_conversations set last_sequence=last_sequence+1 where conversation_id=%s returning last_sequence',(conversation,)).fetchone()[0]
        admin.execute('''insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
            values(%s,'real',%s,%s,%s,'synthetic-sms-1',%s,%s)''',(TENANT,conversation,seq,message,'d'*64,
            psycopg.types.json.Jsonb({'body':'短信来信','accepted_at':datetime.now(UTC).isoformat(),'conversation_id':conversation,'sequence':seq,'actor':'synthetic-human-principal'})))
        admin.execute("select runtime.nexloop_record_provider_facts(%s,'real',%s,'synthetic.sms','sms-1',1,clock_timestamp())",(TENANT,message))
    actions,_=c.owner(identity,uow)
    with pytest.raises(psycopg.Error) as refused:reply(actions,conversation,message,'回复','staff-sms')
    assert refused.value.diag.sqlstate=='NXC07'


def test_restricted_customer_one_reply_shared_with_agent_window_and_fallback(commitments,admin,identity,uow):
    c=commitments;actions,service,conversation=setup(c,identity,uow)
    refusal=c.inbound('以后别再给我发消息了')  # 0109: restricted in this transaction
    second=c.inbound('退款呢')
    take(actions,'conversation',conversation,'take-restricted')
    actions,_=c.owner(identity,uow)
    reply(actions,conversation,second,'退款已处理，本次之后不再打扰。','staff-restricted-1')
    # A second reply to the same message: refused for the person, and for the Agent (shared one-reply rule).
    with pytest.raises(psycopg.Error) as twice:reply(actions,conversation,second,'再补一句','staff-restricted-2')
    assert twice.value.diag.sqlstate=='NXC05'
    agent=c.outbound('Agent 也想回复')
    with admin.transaction():  # the Agent's outbound record bound to the same message (as the message Run would have derived it)
        admin.execute('alter table runtime.nexloop_outbound_messages disable trigger nexloop_outbound_guard')
        admin.execute("update runtime.nexloop_outbound_messages set trigger_message_id=%s,delivery_state='persisted',message_id=null,sequence=null,materialized_at=null where intent_id=%s",(second,agent['intent_id']))
        admin.execute('alter table runtime.nexloop_outbound_messages enable trigger nexloop_outbound_guard')
    # (the takeover would refuse it anyway; hand back first so only the one-reply rule is left)
    actions,_=c.owner(identity,uow)
    open_takeover=admin.execute('select takeover_id from control.nexloop_takeovers where ended_at is null').fetchone()[0]
    give_back(actions,open_takeover,'handback-restricted')
    assert contact_check(c,agent['intent_id']).startswith('NXC05')
    # Outside the reply window: refused (time injection: the inbound message was accepted long ago).
    take(c.owner(identity,uow)[0],'conversation',conversation,'take-restricted-2')
    old=c.inbound('很久以前的来信')
    with admin.transaction():
        admin.execute("update runtime.nexloop_conversation_messages set record=jsonb_set(record,'{accepted_at}',to_jsonb((clock_timestamp()-interval '2 hours')::text)) where message_id=%s",(old,))
    with pytest.raises(psycopg.Error) as late:reply(c.owner(identity,uow)[0],conversation,old,'回复','staff-late')
    assert late.value.diag.sqlstate=='NXC05'
    # After the staff reply no fallback reply Run can start for that message (its pending reply is gone).
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='reply-due' and item_key=%s",('reply:'+second,)).fetchone()==(0,)


def test_commitment_in_a_staff_reply_is_registered_with_the_staff_member(commitments,admin,identity,uow):
    """NX-026: a staff reply is a delivered enterprise message; its commitment is registered, made_by the staff principal."""
    c=commitments;actions,service,conversation=setup(c,identity,uow)
    started=take(actions,'conversation',conversation,'take-commit')
    inbound=c.inbound('什么时候能修好？')
    actions,human=c.owner(identity,uow)
    created=reply(actions,conversation,inbound,'我保证明天中午前给您修好。','staff-commit')['message']
    msg={'message_id':created['id'],'conversation_id':conversation,'body':created['body'],'accepted_at':datetime.fromisoformat(created['accepted_at'])}
    claim=c.claim(msg,'我保证明天中午前给您修好',predicate='修好')
    c.tick()
    commitment=c.by_claim(claim)
    assert commitment,(admin.execute("select kind,detail from runtime.nexloop_commitment_events").fetchall(),
        admin.execute("select feed,last_code,attempts from runtime.nexloop_work_feed where feed like 'commitment%%'").fetchall())
    made_by=c.commitment(commitment)['properties']['made_by']
    assert made_by=={'sender_kind':'human_takeover','sender_principal':human.authentication.subject_principal_id,'takeover_id':str(started['takeover_id'])}
