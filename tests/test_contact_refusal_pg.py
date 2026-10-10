"""ADR-023 on clean catalog PostgreSQL: the deterministic protective check, the contact restriction and its release.

* Rule sets: positive (refuse / uncertain → restricted), counter-examples (not restricted), misjudgment negatives
  (look like refusals, excluded on purpose), and the accepted over-blocking cases (when in doubt, restrict).
* Inbound: a real governed Human Message runs the check in its own transaction; a hit writes the consumer-level
  restriction through the NX-022 control writer (control revision advanced, evidence recorded) and the message is
  persisted all the same; every inbound message registers a pending reply.
* Release: only a human owner's governed Action; an Agent or a service holding the very same grant is refused.
Synthetic data only; admin seeds fixtures and probes (and calls the owner-only matcher directly for the rule sets).
"""
import json
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from nexloop_eios.contact_restrictions import ContactReadPort,load_refusal_rules,load_reply_policy
from nexloop_eios.goal_controls import GoalGovernedActions

ROOT=Path(__file__).resolve().parents[1]
RULES=ROOT/'deploy/configuration/contact-refusal-rules.v1.json'
POLICY=ROOT/'deploy/configuration/reply-guarantee.v1.json'

REFUSE=['以后别再给我发消息了','不要再联系我了','请勿打扰','别给我打电话','退订','TD','STOP','取消订阅，谢谢','把我拉黑吧，别发了',
    '再发我就投诉你们','不用再推送了','甭找我','不要再给我发促销短信','请不要再打扰我。','unsubscribe please','别再骚扰我']
UNCERTAIN=['少给我发点消息','烦死了','别老给我打电话','不要一直推']
COUNTER=['你好，我想问一下续费价格','好的，谢谢','明天下午有空吗','我想要防水的登山鞋','可以发我一下发票吗','麻烦发一下物流单号',
    '请联系我的同事张先生','这个能打九折吗','我已经付款了']
# Look like refusals but are about goods, shipping, discounts or change: excluded on purpose (must not restrict).
MISJUDGMENT=['别发错货了','不要发顺丰，发中通','别发漏了配件','不要寄到公司','能不能别打折了，直接原价也行','不用找零了','别打包太紧']
# Accepted over-blocking (ADR-023 §3, when in doubt restrict): documented, owner releases them.
OVERBLOCK=['不用找了','别找借口']


def match(admin,body):
    return admin.execute('select control.nexloop_contact_refusal_match(%s)',(body,)).fetchone()[0]


def test_configuration_files_are_the_seeded_v1(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    rules=load_refusal_rules(RULES);policy=load_reply_policy(POLICY)
    assert admin.execute('select definition from control.nexloop_contact_refusal_rules where version=1').fetchone()[0]==rules
    assert admin.execute('select definition from control.nexloop_reply_policies where version=1').fetchone()[0]==policy
    # Append-only: a published rule version is never edited.
    with pytest.raises(Exception,match='append-only'):admin.execute("update control.nexloop_contact_refusal_rules set published_by='x'")


def test_rule_sets_positive_counter_misjudgment_and_overblocking(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    for body in REFUSE:
        hit=match(admin,body);assert hit['matched'] and hit['certainty']=='refuse' and hit['rule_version']==1 and hit['matched_text'],(body,hit)
    for body in UNCERTAIN:
        hit=match(admin,body);assert hit['matched'] and hit['certainty']=='uncertain',(body,hit)
    for body in COUNTER+MISJUDGMENT:
        assert match(admin,body)=={'matched':False,'rule_version':1},body
    for body in OVERBLOCK:
        assert match(admin,body)['matched'],body
    # A refusal next to an excluded phrase is still a refusal.
    assert match(admin,'上次发错货了，以后别再给我发消息')['certainty']=='refuse'


@pytest.fixture
def inbound(conversations,admin):
    yield conversations


def restriction(admin,consumer):
    return admin.execute('select active,rule_version,rule_id,certainty,matched_text,control_revision from control.nexloop_contact_restrictions where consumer_id=%s',(consumer,)).fetchone()


def reply_items(admin):
    return admin.execute("select item_key,payload,available_at-clock_timestamp()>interval '200 seconds' from runtime.nexloop_work_feed where feed='reply-due' order by item_key").fetchall()


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_inbound_refusal_restricts_in_the_same_transaction_and_keeps_the_message(inbound,admin):
    from test_claim_extraction_jobs_pg import make_window
    f=inbound;consumer=f['consumer']
    conversation,ids=make_window(f,['你好，续费多少钱？'],'normal')
    assert restriction(admin,consumer) is None
    head=admin.execute('select revision from control.nexloop_control_heads').fetchone()
    conversation,ids2=make_window(f,['以后别再给我发消息了'],'refusal')
    active,version,rule,certainty,text,revision=restriction(admin,consumer)
    assert (active,version,rule,certainty)==(True,1,'stop-contact','refuse') and '别再给我发' in text
    event=admin.execute("select event_kind,scope_kind,scope_ref,detail->>'message_id',principal_id from control.nexloop_control_events where revision=%s",(revision,)).fetchone()
    assert event==('contact_restricted','consumer',consumer,ids2[0],'system:contact-refusal-rules')
    assert revision>((head or (0,))[0] or 0)
    # The refusing message itself is persisted (and goes to the relay) like any inbound message.
    assert admin.execute('select count(*) from runtime.nexloop_conversation_messages where message_id=%s',(ids2[0],)).fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_message_outbox where message_id=%s',(ids2[0],)).fetchone()==(1,)
    # Every inbound message is a pending reply due at the end of the configured window.
    items=reply_items(admin)
    assert [i[0] for i in items]==sorted('reply:'+m for m in ids+ids2) and all(i[2] for i in items)
    # A second refusal while restricted adds evidence, not another control revision.
    make_window(f,['TD'],'refusal-2')
    assert restriction(admin,consumer)[5]==revision
    assert admin.execute('select count(*) from control.nexloop_contact_refusal_hits').fetchone()==(2,)
from test_conversation_messages import conversations  # noqa: F401,E402
from test_browser_business_authorization import browser_business  # noqa: F401,E402
from test_action_definitions import published_action  # noqa: F401,E402
from test_browser_identity_reads import identity  # noqa: F401,E402
from test_browser_session_creation import uow  # noqa: F401,E402


def owner_env(identity,uow,published_action,admin):
    """NX-022 governance (human owner / Agent / service, governed goal Actions incl. Contact.release), set up after the
    Human's messages: it takes over the same synthetic browser identity's application facts."""
    from test_goal_controls import env as goal_env
    return goal_env.__wrapped__(identity,uow,published_action,admin)


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_only_a_human_owner_action_releases_the_restriction(inbound,admin,identity,uow,published_action):
    from goal_fixture import authenticate_human,seed_agent_author,seed_service
    from nexloop_eios.authorization import authenticate_service
    from test_claim_extraction_jobs_pg import make_window
    f=inbound;consumer=f['consumer']
    make_window(f,['不要再联系我了'],'release')
    assert restriction(admin,consumer)[0] is True
    e=owner_env(identity,uow,published_action,admin)
    pool=e['reader'].pool;signer=e['reader'].signer
    agent_token=seed_agent_author(admin,pool,['Contact.release'],suffix='-contact-agent')
    service_token=seed_service(admin,pool,['Contact.release'],suffix='-contact-service')
    e.reauthenticate()
    body=dict(action_name='Contact.release',action_version=1,consumer_id=consumer,reason='客户电话确认可以联系')
    # An Agent and a service principal holding the very same EXECUTE grant: refused, nothing changes.
    for token,label in ((agent_token,'agent'),(service_token,'service')):
        actions=GoalGovernedActions(pool,authenticate_service(pool,token,world='real'),signer)
        with pytest.raises(Exception,match='human goal authority required|unavailable|denied'):
            actions.release_contact_restriction(request_id='release-'+label,**body)
        assert restriction(admin,consumer)[0] is True
    # The human owner's governed Action releases it and leaves evidence; a second release has nothing to release.
    result=e.owner.release_contact_restriction(request_id='release-owner',**body)
    assert result['released'] is True and result['operation']=='release_contact_restriction'
    row=admin.execute('select active,released_by,release_reason from control.nexloop_contact_restrictions where consumer_id=%s',(consumer,)).fetchone()
    assert row[0] is False and row[2]=='客户电话确认可以联系' and row[1]==e.human.authentication.subject_principal_id
    assert admin.execute("select event_kind from control.nexloop_control_events order by revision desc limit 1").fetchone()==('contact_released',)
    with pytest.raises(Exception):e.owner.release_contact_restriction(request_id='release-owner-2',**body)


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_query_port_and_due_reply_escalated_when_no_fallback_can_run(inbound,admin):
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios.assembly import open_core
    from nexloop_eios.authorization import authenticate_service
    from nexloop_eios.contact_restrictions import ReplyGuaranteeWorker
    from psycopg.conninfo import make_conninfo
    from test_claim_extraction_jobs_pg import make_window
    f=inbound;consumer=f['consumer']
    _,ids=make_window(f,['请勿打扰'],'escalate')
    targets=[(a,ResourceType.ACTION,Operation.EXECUTE) for a in ('eios:action:NexLoop.feed.reply-due:1','eios:action:nexloop.reply.guarantee:1','eios:action:nexloop.contact.read:1')]
    with open_core(make_conninfo(f['pg'],user='nexloop_domain_worker')) as worker:
        _,token=seed_multi_authority(admin,worker,targets,identity_suffix='-reply-guarantor')
        session=lambda:authenticate_service(worker,token,world='real')
        signer=f['reader'].signer
        (row,)=ContactReadPort(worker,session(),signer).restrictions()
        assert row['consumer_id']==consumer and row['active'] and row['rule_id']=='stop-contact' and row['hits'][0]['message_id']==ids[0]
        policy=load_reply_policy(POLICY)
        # Not due yet: nothing happens.
        assert not any(ReplyGuaranteeWorker(worker,session(),signer,policy=policy).run_once().values())
        admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp()-interval '1 second' where feed='reply-due'")  # time injection
        summary=ReplyGuaranteeWorker(worker,session(),signer,policy=policy).run_once()
        assert summary['escalated']==1,summary
        (escalation,)=ContactReadPort(worker,session(),signer).escalations()
        assert escalation['message_id']==ids[0] and escalation['reason']=='fallback_unavailable' and escalation['detail']['restricted'] is True
        assert reply_items(admin)==[]
        # The query port needs its own grant: a principal without nexloop.contact.read gets nothing.
        _,other=seed_multi_authority(admin,worker,targets[:2],identity_suffix='-no-contact-read')
        from nexloop_eios.plan_reevaluation import PlanUnavailable
        with pytest.raises(PlanUnavailable):ContactReadPort(worker,authenticate_service(worker,other,world='real'),signer).restrictions()
