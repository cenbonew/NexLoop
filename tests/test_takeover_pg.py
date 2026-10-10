"""NX-028 slice 3 (0152) on clean catalog PostgreSQL: human takeover, D3–D5 (AT-044 data and dispatch side).

Actual governed effect executor with the independent effect Worker and loopback provider (commitment_fixture), a real
browser HUMAN owner session for the takeover Actions, real service principals for the expiry and reply ports. Inbound
stream entries are admin seeds of the 0046 write (the 0109 trigger runs on them); time is injected only where a test
says so. The actual relay / Host / Pi path is test_takeover_e2e_pg.
"""
from datetime import UTC,datetime,timedelta
import uuid

import psycopg
import pytest
from support.effect_provider import effect_provider
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.authorization import authenticate_service
from commitment_fixture import TAKEOVER_ACTIONS,TENANT,commitments,publish_request_actions  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import active_plan,plan_triggers
from test_contact_effect_categories_pg import contact_check
from test_governed_entry_pg import predict

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)


def owner(c,identity,uow):
    publish_request_actions(c['admin'],names=TAKEOVER_ACTIONS);c['extra_human_actions']=TAKEOVER_ACTIONS
    service=c.service_actions('-takeover-service');c.owner(identity,uow)
    actions,_=c.owner(identity,uow)
    return actions,c.service_actions_session(service)


def take(actions,scope_kind,scope_ref,key,**extra):
    return actions.take_over_conversation(action_name='nexloop.conversation.takeover',action_version=1,request_id=key,scope_kind=scope_kind,
        scope_ref=scope_ref,reason='客户情绪激动，人工接手',**extra)


def give_back(actions,takeover,key):
    return actions.hand_back_conversation(action_name='nexloop.conversation.handback',action_version=1,request_id=key,takeover_id=takeover,reason='已处理完毕')


def reply_items(admin):
    return sorted(r[0] for r in admin.execute("select item_key from runtime.nexloop_work_feed where feed='reply-due'").fetchall())


def test_takeover_is_human_only_bounded_and_stops_agent_replies_in_the_conversation(commitments,admin,identity,uow):
    c=commitments;plan=active_plan(c)
    actions,service=owner(c,identity,uow)
    conversation=c.conversation(c['consumer'])
    with pytest.raises(Exception,match='human goal authority required'):take(service,'conversation',conversation,'take-service')
    with pytest.raises(Exception):take(actions,'conversation',conversation,'take-too-long',duration_seconds=86401)
    actions,_=c.owner(identity,uow)
    started=take(actions,'conversation',conversation,'take-owner')
    assert started['scope_kind']=='conversation' and started['consumer_id']==c['consumer']
    expires=datetime.fromisoformat(str(started['expires_at']))
    assert timedelta(seconds=7190)<expires-datetime.now(UTC)<=timedelta(seconds=7200)  # D3 default 2 h
    # The takeover start advances the control revision but wakes no plan (a person answers now).
    assert admin.execute("select event_kind,scope_kind,scope_ref from control.nexloop_control_events order by revision desc limit 1").fetchone()==('takeover','consumer',c['consumer'])
    assert plan_triggers(c,plan)==[]
    with pytest.raises(Exception,match='already taken over'):take(actions,'consumer',c['consumer'],'take-again')
    # An Agent reply in that conversation is refused at dispatch (NXC06); a non-message service effect is not (conversation scope).
    reply=c.outbound('好的，我来处理');service_intent=c.submit({'service':'后台退款'})['intent_id']
    assert contact_check(c,reply['intent_id']).startswith('NXC06')
    # Conversation scope: a non-message service effect of the consumer is not a reply and stays dispatchable.
    assert predict(admin,service_intent)['dispatchable'] is True


def test_consumer_takeover_refuses_every_agent_effect_with_zero_requests(commitments,admin,identity,uow,tmp_path):
    c=commitments;actions,_=owner(c,identity,uow)
    take(actions,'consumer',c['consumer'],'take-consumer')
    queued=c.submit({'service':'后台退款'})['intent_id']
    assert predict(admin,queued)['reason']=='taken_over'
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        result=c.dispatch(provider)
        assert result['status']=='admission_unavailable' and provider.control('snapshot')['effects']==0


def test_handback_resumes_only_the_latest_unanswered_message_and_reevaluates(commitments,admin,identity,uow):
    """D5: earlier unanswered messages are settled by the takeover, never replayed; the latest one is a pending reply again."""
    c=commitments;plan=active_plan(c);actions,_=owner(c,identity,uow)
    conversation=c.conversation(c['consumer'])
    before=c.inbound('之前的问题')  # accepted before the takeover: not part of it
    started=take(actions,'conversation',conversation,'take-d5')
    first=c.inbound('第一条');answered=c.inbound('第二条');last=c.inbound('第三条')
    # The person answered the second one (a channel-accepted reply bound to it).
    reply=c.outbound('人工：已为你处理第二条')
    admin.execute('select 1')
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute('alter table runtime.nexloop_outbound_messages disable trigger nexloop_outbound_guard')
        admin.execute('update runtime.nexloop_outbound_messages set trigger_message_id=%s where intent_id=%s',(answered,reply['intent_id']))
        admin.execute('alter table runtime.nexloop_outbound_messages enable trigger nexloop_outbound_guard')
    actions,_=c.owner(identity,uow)
    ended=give_back(actions,started['takeover_id'],'handback-d5')
    assert ended['end_reason']=='handback' and ended['resumed_message_id']==last and ended['settled']==1
    settled=dict(admin.execute('select message_id,disposition from control.nexloop_takeover_settlements').fetchall())
    assert settled=={last:'resumed',first:'settled_by_takeover'}
    items=reply_items(admin)
    assert 'reply:'+last in items and 'reply:'+first not in items and 'reply:'+before in items
    assert admin.execute("select event_kind from control.nexloop_control_events order by revision desc limit 1").fetchone()==('handback',)
    assert [k for k,_ in plan_triggers(c,plan)]==['handback']
    with pytest.raises(Exception):give_back(actions,started['takeover_id'],'handback-twice')


def test_expiry_ends_the_takeover_escalates_and_settles(commitments,admin,identity,uow):
    from nexloop_eios.takeovers import TakeoverExpiryWorker
    c=commitments;actions,_=owner(c,identity,uow)
    conversation=c.conversation(c['consumer'])
    started=take(actions,'conversation',conversation,'take-expire',duration_seconds=60)
    late=c.inbound('人呢？')
    _,token=seed_multi_authority(admin,c['worker'],[(r,ResourceType.ACTION,Operation.EXECUTE) for r in
        ('eios:action:NexLoop.feed.takeover-expiry:1','eios:action:nexloop.takeover.expire:1')],identity_suffix='-takeover-expiry',tenant=TENANT)
    worker=lambda:TakeoverExpiryWorker(c['worker'],authenticate_service(c['worker'],token,world='real'),c['signer'])
    assert not any(worker().run_once().values())  # not due yet
    with admin.transaction():  # explicit time injection: the takeover's end time has passed
        admin.execute('alter table control.nexloop_takeovers disable trigger nx028_takeover_guard')
        admin.execute("update control.nexloop_takeovers set started_at=started_at-interval '1 minute',expires_at=clock_timestamp()-interval '1 second' where takeover_id=%s",(started['takeover_id'],))
        admin.execute('alter table control.nexloop_takeovers enable trigger nx028_takeover_guard')
        admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp() where feed='takeover-expiry'")
    reply=c.outbound('Agent 回复')
    assert contact_check(c,reply['intent_id'])=='pass'  # already inactive before the worker runs (SQL checks the time)
    summary=worker().run_once();assert summary['expired']==1,summary
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='takeover-expiry'").fetchone()==(0,)
    row=admin.execute('select end_reason from control.nexloop_takeovers where takeover_id=%s',(started['takeover_id'],)).fetchone()
    assert row==('expired',)
    assert admin.execute('select reason,detail->>%s from control.nexloop_takeover_escalations',('resumed_message_id',)).fetchone()==('expired_without_handback',late)
    assert 'reply:'+late in reply_items(admin)


def test_reply_port_reports_takeover_and_customer_sees_only_handled_by(commitments,admin,identity,uow):
    from nexloop_eios.contact_restrictions import ReplyGuaranteeWorker,load_reply_policy
    from goal_fixture import authenticate_human
    from test_contact_refusal_pg import POLICY
    c=commitments;actions,_=owner(c,identity,uow)
    conversation=c.conversation(c['consumer'])
    message=c.inbound('你好')
    take(actions,'conversation',conversation,'take-reply')
    _,token=seed_multi_authority(admin,c['worker'],[(r,ResourceType.ACTION,Operation.EXECUTE) for r in
        ('eios:action:NexLoop.feed.reply-due:1','eios:action:nexloop.reply.guarantee:1','eios:action:nexloop.contact.read:1')],identity_suffix='-reply-takeover',tenant=TENANT)
    admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp()-interval '1 second' where feed='reply-due'")  # time injection
    summary=ReplyGuaranteeWorker(c['worker'],authenticate_service(c['worker'],token,world='real'),c['signer'],policy=load_reply_policy(POLICY)).run_once()
    assert summary['taken_over']==1 and summary['escalated']==0 and summary['fallback_started']==0,summary
    assert admin.execute('select count(*) from control.nexloop_reply_escalations').fetchone()==(0,)
    # Customer view (D4): the conversation's own Human sees handled_by only; another Human gets nothing.
    human=authenticate_human(c['plan']['backend']._pool,c['browser'])
    admin.execute('update runtime.nexloop_conversations set principal_id=%s where conversation_id=%s',(human.authentication.subject_principal_id,conversation))
    from psycopg.conninfo import make_conninfo
    with psycopg.connect(make_conninfo(c['pg'],user='nexloop_api')) as api:
        state=api.execute('select authz.nexloop_conversation_takeover_state(%s,%s,%s)',(human.token_digest,'real',conversation)).fetchone()[0]
        assert state=={'conversation_id':conversation,'handled_by':'human'}
        api.rollback()
        other=c.conversation('8'*64)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute('select authz.nexloop_conversation_takeover_state(%s,%s,%s)',(human.token_digest,'real',other))
