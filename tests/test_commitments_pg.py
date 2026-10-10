"""NX-026 commitments on clean catalog PostgreSQL (docs/implementation/NX-026-design.md §10).

Registration from enterprise commitment Claims of delivered outbound Messages, the ledger-only state machine, due /
breach / plan marking (AT-041), AT-040 (delivery, the customer's words and the Agent's report never fulfil),
idempotency (AT-020 at commitment level), human-only Actions and the query port. Synthetic data; admin seeds the
NX-047 ledger rows and Claims (commitment_fixture) and injects time explicitly where a test says so.
"""
from datetime import UTC,datetime,timedelta
import uuid

import psycopg
import pytest
from nexloop_eios.effect_intents import EffectIntentUnavailable
from psycopg.types.json import Jsonb
from commitment_fixture import TENANT,commitments,deadline,ts  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)
REPLY='好的，我会在明天下午前给你处理进展。'
QUOTE='我会在明天下午前给你处理进展'


def registered(c,claim_id):
    c.tick()
    commitment=c.by_claim(claim_id)
    assert commitment,c['admin'].execute('select * from runtime.nexloop_work_feed').fetchall()
    return commitment,c.commitment(commitment)


def test_registration_from_a_delivered_commitment_claim(commitments,admin):
    c=commitments;now=datetime.now(UTC).replace(microsecond=0)
    message=c.outbound(REPLY,accepted_at=now)
    window=(now+timedelta(hours=20),now+timedelta(hours=30))
    claim=c.claim(message,QUOTE,valid_time=deadline(message,window[1],status='ambiguous',window=window))
    commitment,row=registered(c,claim)
    p=row['properties']
    assert p['made_to']==c['consumer'] and p['status']=='open' and p['late'] is False and p['fulfillment_basis']=='undetermined'
    assert p['due_precision']=='latest_bound' and datetime.fromisoformat(p['due_at'])==window[1]
    assert datetime.fromisoformat(p['promised_at'])==now and p['condition'] is None and p['fulfillment_evidence']==[]
    assert p['made_by']['sender_kind']=='agent' and p['made_by']['intent_id']==message['intent_id']
    assert p['content_ref']=={'claim_id':claim,'message_id':message['message_id'],'span':[REPLY.index(QUOTE),REPLY.index(QUOTE)+len(QUOTE)],
        'content_hash':p['content_ref']['content_hash']}
    assert admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s',(claim,)).fetchone()==('resolved',)
    view=c.reader().commitment(commitment)
    assert view['quote']==QUOTE and view['evidence']['problem_resolved']=='unavailable' and [e['kind'] for e in view['events']]==['registered']


def fresh(c,*,due_in=timedelta(days=2),quote=QUOTE,reply=REPLY,**claim):
    now=datetime.now(UTC).replace(microsecond=0);message=c.outbound(reply,accepted_at=now)
    vt=deadline(message,now+due_in) if due_in is not None else None
    commitment,_=registered(c,c.claim(message,quote,valid_time=vt,**claim))
    return commitment,message


def evidence_count(admin,commitment):
    return admin.execute('select count(*) from runtime.nexloop_commitment_evidence where commitment_id=%s',(commitment,)).fetchone()[0]


def statuses(c,commitment):
    return [e['detail'].get('status') for e in c.reader().commitment(commitment)['events'] if e['kind'] in ('registered','status')]


def test_effect_receipt_of_a_service_delivery_fulfils_and_is_the_evidence(commitments,admin,tmp_path):
    from support.effect_provider import effect_provider
    c=commitments;c.configure_categories();commitment,_=fresh(c)
    receipt=c.submit({'service':'退款处理','commitment_ref':'commitment:'+commitment})
    c.tick()
    assert c.commitment(commitment)['properties']['status']=='in_progress'  # requested is not delivered
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        c.deliver(provider,receipt['intent_id'])
        assert provider.control('snapshot')['effects']==1
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(receipt['intent_id'],)).fetchone()[0] in ('fulfilled','confirmed')
    c.tick()
    p=c.commitment(commitment)['properties']
    assert p['status']=='fulfilled' and p['late'] is False and [e['kind'] for e in p['fulfillment_evidence']]==['effect_fulfilled']
    assert p['fulfillment_evidence'][0]['ref']=='intent:'+receipt['intent_id']
    view=c.reader().commitment(commitment)
    assert [e['kind'] for e in view['evidence']['requested']]==['requested'] and [e['kind'] for e in view['evidence']['delivered']]==['effect_fulfilled']
    assert statuses(c,commitment)==['open','in_progress','fulfilled']
    # A fulfilled commitment takes no further reference (SQL 22023, refused before anything is written).
    with pytest.raises(EffectIntentUnavailable):c.submit({'service':'再次处理','commitment_ref':'commitment:'+commitment})
    assert evidence_count(admin,commitment)==2


def test_at040_delivered_message_customer_words_and_agent_report_never_fulfil(commitments,admin,tmp_path):
    """AT-040: sending succeeded but the problem is not solved: the commitment is not fulfilled."""
    from support.effect_provider import effect_provider
    c=commitments;c.configure_categories();commitment,message=fresh(c)
    # The progress message itself, bound to the commitment and delivered (a notification parameter: a message).
    receipt=c.submit({'service':'进展通知','message':'您的问题我们还在处理中','commitment_ref':'commitment:'+commitment})
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        c.deliver(provider,receipt['intent_id'])
    # The customer's own words and the Agent's report are only shown, never qualifying.
    admin.execute('''insert into runtime.nexloop_commitment_evidence(tenant_id,world,commitment_id,kind,ref,occurred_at,recorded_by)
        values(%s,'real',%s,'consumer_confirmation','claim:synthetic-customer-said-ok',clock_timestamp(),'synthetic'),
              (%s,'real',%s,'agent_report','run:synthetic',clock_timestamp(),'synthetic')''',(TENANT,commitment,TENANT,commitment))
    c.tick()
    p=c.commitment(commitment)['properties']
    assert p['status']=='in_progress' and p['fulfillment_evidence']==[]
    view=c.reader().commitment(commitment)
    assert [e['kind'] for e in view['evidence']['delivered']]==['delivered_message','agent_report']
    assert [e['ref'] for e in view['evidence']['delivered'] if e['kind']=='delivered_message']==['intent:'+receipt['intent_id']]
    assert [e['kind'] for e in view['evidence']['customer_confirmed']]==['consumer_confirmation'] and view['evidence']['problem_resolved']=='unavailable'
    # Nothing is resolved either: the Claim behind it is resolved as registered, never as fulfilled.
    assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Commitment' and properties->>'status'='fulfilled'",(TENANT,)).fetchone()==(0,)


def test_d3_communication_marking_by_a_human_qualifies_only_later_deliveries(commitments,admin,identity,uow,tmp_path):
    from support.effect_provider import effect_provider
    c=commitments;c.configure_categories();commitment,_=fresh(c)
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        first=c.submit({'service':'进展通知','message':'第一次进展：已登记','commitment_ref':'commitment:'+commitment})
        c.deliver(provider,first['intent_id'])
        c.tick();assert c.commitment(commitment)['properties']['status']=='in_progress'
        # A service principal with the very same grant cannot mark it (human only).
        service=c.service_actions();c.owner(identity,uow)  # seed both first: seeding moves the directory hash
        service=c.service_actions_session(service)
        with pytest.raises(Exception,match='human goal authority required'):
            service.mark_commitment_communication(action_name='Commitment.mark_communication',action_version=1,request_id='mark-service',
                commitment_id=commitment,reason='service tries')
        owner,_=c.owner(identity,uow)
        owner.mark_commitment_communication(action_name='Commitment.mark_communication',action_version=1,request_id='mark-owner',
            commitment_id=commitment,reason='这是一个沟通类承诺：告知进展即兑现')
        c.tick()
        p=c.commitment(commitment)['properties']
        # The delivery before the marking is not taken back as evidence.
        assert p['fulfillment_basis']=='communication' and p['status']=='in_progress'
        later=c.submit({'service':'进展通知','message':'第二次进展：已经处理完毕','commitment_ref':'commitment:'+commitment})
        c.deliver(provider,later['intent_id'])
    c.tick()
    p=c.commitment(commitment)['properties']
    assert p['status']=='fulfilled' and [e['ref'] for e in p['fulfillment_evidence']]==['intent:'+later['intent_id']]
    marks=[e for e in c.reader().commitment(commitment)['events'] if e['kind']=='marked_communication']
    assert len(marks)==1 and marks[0]['principal_id'] and marks[0]['detail']['reason'].startswith('这是一个沟通类承诺')


def active_plan(c,consumer=None):
    """An active NX-024 plan of the Consumer (plan rows seeded by admin; the plan port itself is covered by NX-024 tests)."""
    plan=uuid.uuid4();a=c['admin']
    a.execute('''insert into runtime.nexloop_plans(tenant_id,world,plan_id,version,consumer_id,goal_version_ref,context_strategy_ref,recipe,settings,control_snapshot,created_by)
        values(%s,'real',%s,1,%s,'goal:synthetic-goal@1','context-strategy:recent_plus_required@1','{}',%s,%s,'synthetic')''',
        (TENANT,plan,consumer or c['consumer'],Jsonb({'self_window_seconds':600,'self_trigger_cap':2,'min_reassess_seconds':0}),
         Jsonb({'scopes':[{'kind':'consumer','ref':consumer or c['consumer']}]})))  # as 0106 establishes it
    a.execute("insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason) values(%s,'real',%s,1,'active','established')",(TENANT,plan))
    return plan


def plan_triggers(c,plan):
    row=c['admin'].execute("select payload from runtime.nexloop_work_feed where feed='plan-reevaluate' and item_key=%s",('plan:'+str(plan),)).fetchone()
    return [] if row is None else [(t['kind'],t.get('ref')) for t in row[0]['triggers']]


def test_at041_due_soon_marks_plans_breach_is_owner_visible_and_late_evidence_fulfils(commitments,admin,identity,uow):
    """AT-041: due and not done → reevaluation (NX-024 T7) and an exception the owner sees; a paused Consumer hides nothing."""
    import time
    c=commitments;plan=active_plan(c)
    commitment,_=fresh(c,due_in=timedelta(seconds=3))
    # lead_seconds (3600) already reached at registration: the Consumer's active plan is marked before the due date.
    assert plan_triggers(c,plan)==[('commitment_due','commitment:'+commitment)]
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")  # the reevaluator took it
    from test_nx022_dispatch_e2e import pause
    pause(admin,TENANT,'consumer',c['consumer'])
    time.sleep(3.2);c.tick()
    p=c.commitment(commitment)['properties']
    assert p['status']=='breached'
    (_,reason,detail),=[e for e in c.exceptions(commitment) if e[1]=='breached']
    assert detail['paused'] is True and detail['contact_restricted'] is False
    # The pause itself also marks the plan (its snapshot names the consumer); the breach marks it again (T7).
    assert ('commitment_due','commitment:'+commitment) in plan_triggers(c,plan)
    assert [x['subject_ref'] for x in c.reader().exceptions() if x['reason']=='breached']==['commitment:'+commitment]
    assert [x['commitment_id'] for x in c.reader().commitments(c['consumer'])]==[commitment]  # breached is still open work
    # Late human attestation: fulfilled, late; the breach stays in the history.
    owner,_=c.owner(identity,uow)
    owner.attest_commitment(action_name='Commitment.attest',action_version=1,request_id='attest-late',commitment_id=commitment,
        occurred_at=datetime.now(UTC).replace(microsecond=0),reason='运营电话确认已经处理完毕')
    c.tick()
    p=c.commitment(commitment)['properties']
    assert p['status']=='fulfilled' and p['late'] is True and [e['kind'] for e in p['fulfillment_evidence']]==['operator_attestation']
    assert statuses(c,commitment)==['open','breached','fulfilled']
    assert c.reader().commitments(c['consumer'])==[]


def test_conditional_needs_a_human_condition_met_and_is_never_breached(commitments,admin,identity,uow):
    import time
    c=commitments;plan=active_plan(c)
    commitment,_=fresh(c,due_in=timedelta(seconds=2),reply='您确认地址后，我们明天就给您补发。',quote='您确认地址后，我们明天就给您补发',
        modality='conditional',condition='您确认地址后',predicate='补发')
    p=c.commitment(commitment)['properties'];assert p['status']=='conditional' and p['condition']=='您确认地址后'
    time.sleep(2.2);c.tick()
    assert c.commitment(commitment)['properties']['status']=='conditional'
    assert [e[1] for e in c.exceptions(commitment)]==['condition_unresolved_at_due']
    assert ('commitment_due','commitment:'+commitment) in plan_triggers(c,plan)
    service=c.service_actions();c.owner(identity,uow);service=c.service_actions_session(service)
    with pytest.raises(Exception,match='human goal authority required'):
        service.commitment_condition_met(action_name='Commitment.condition_met',action_version=1,request_id='cond-service',commitment_id=commitment,reason='x')
    owner,_=c.owner(identity,uow)
    owner.commitment_condition_met(action_name='Commitment.condition_met',action_version=1,request_id='cond-owner',commitment_id=commitment,reason='客户已确认地址')
    c.tick()
    # Condition met after the due date: open, then breached on the same evidence (due already passed).
    assert statuses(c,commitment)==['conditional','breached']


def test_without_due_date_plans_are_marked_to_clarify_and_no_due_date_is_raised(commitments,admin):
    c=commitments;plan=active_plan(c)
    commitment,_=fresh(c,due_in=None)
    p=c.commitment(commitment)['properties'];assert p['due_at'] is None and p['due_precision']=='unspecified'
    assert plan_triggers(c,plan)==[('commitment_due','commitment:'+commitment)]
    assert c.exceptions(commitment)==[]
    # Explicit time injection: registered long ago (registry guard disabled for this one statement only).
    with admin.transaction():
        admin.execute('alter table runtime.nexloop_commitments disable trigger nx026_registry_guard')
        admin.execute("update runtime.nexloop_commitments set registered_at=registered_at-interval '8 days' where commitment_id=%s",(commitment,))
        admin.execute('alter table runtime.nexloop_commitments enable trigger nx026_registry_guard')
    c.due_now(commitment);c.tick()
    assert [e[1] for e in c.exceptions(commitment)]==['no_due_date'] and c.commitment(commitment)['properties']['status']=='open'


def test_no_active_plan_is_an_exception(commitments,admin):
    c=commitments;commitment,_=fresh(c,due_in=timedelta(minutes=30))  # inside lead_seconds (3600 s): due_soon now
    assert [e[1] for e in c.exceptions(commitment)]==['no_active_plan']


def test_reaffirmed_promise_and_a_new_due_date_supersede_keeping_the_old(commitments,admin):
    c=commitments;now=datetime.now(UTC).replace(microsecond=0);due=now+timedelta(days=1)
    first=c.outbound(REPLY,accepted_at=now);a=c.claim(first,QUOTE,valid_time=deadline(first,due))
    commitment,_=registered(c,a)
    # The same promise and due date in a later delivered message: reaffirmed, no new commitment.
    second=c.outbound('再说一次：'+REPLY,accepted_at=now+timedelta(minutes=1));b=c.claim(second,QUOTE,valid_time=deadline(second,due))
    c.tick()
    assert c.by_claim(b) is None and admin.execute("select count(*) from ontology.objects where type_name='Commitment'").fetchone()==(1,)
    assert admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s',(b,)).fetchone()==('resolved',)
    assert [e['kind'] for e in c.reader().commitment(commitment)['events']]==['registered','reaffirmed']
    # Same promise, later due date: a new commitment supersedes it; the old one is cancelled and kept.
    third=c.outbound('改到后天：'+REPLY,accepted_at=now+timedelta(minutes=2));d=c.claim(third,QUOTE,valid_time=deadline(third,due+timedelta(days=1)))
    newer,row=registered(c,d);c.tick()
    assert newer!=commitment and row['properties']['supersedes']==commitment
    assert c.commitment(commitment)['properties']['status']=='cancelled'
    assert [e['kind'] for e in c.reader().commitment(commitment)['events']]==['registered','reaffirmed','superseded','status']


def test_human_extension_and_cancellation_and_their_limits(commitments,admin,identity,uow):
    import time
    c=commitments;commitment,_=fresh(c)
    service=c.service_actions();c.owner(identity,uow);service=c.service_actions_session(service)
    for method,extra in (('cancel_commitment',{}),('extend_commitment',{'due_at':datetime.now(UTC)+timedelta(days=5)})):
        with pytest.raises(Exception,match='human goal authority required'):
            getattr(service,method)(action_name='Commitment.'+method.split('_')[0],action_version=1,request_id=method+'-service',commitment_id=commitment,reason='x',**extra)
    owner,_=c.owner(identity,uow)
    new_due=(datetime.now(UTC)+timedelta(days=5)).replace(microsecond=0)
    result=owner.extend_commitment(action_name='Commitment.extend',action_version=1,request_id='extend-owner',commitment_id=commitment,due_at=new_due,reason='客户同意延期')
    c.tick()
    newer=result['new_commitment_id'];p=c.commitment(newer)['properties']
    assert p['supersedes']==commitment and datetime.fromisoformat(p['due_at'])==new_due and p['due_precision']=='exact' and p['status']=='open'
    assert c.commitment(commitment)['properties']['status']=='cancelled'
    owner.cancel_commitment(action_name='Commitment.cancel',action_version=1,request_id='cancel-owner',commitment_id=newer,reason='客户取消了需求')
    c.tick()
    assert c.commitment(newer)['properties']['status']=='cancelled'
    # A breached commitment is neither cancelled nor extended away.
    breached,_=fresh(c,due_in=timedelta(seconds=2),reply='我今天给你回电。',quote='我今天给你回电',predicate='回电')
    time.sleep(2.2);c.tick();assert c.commitment(breached)['properties']['status']=='breached'
    owner,_=c.owner(identity,uow)
    for method,extra in (('cancel_commitment',{}),('extend_commitment',{'due_at':datetime.now(UTC)+timedelta(days=5)})):
        with pytest.raises(Exception,match='cannot be'):
            getattr(owner,method)(action_name='Commitment.'+method.split('_')[0],action_version=1,request_id=method+'-breached',commitment_id=breached,reason='x',**extra)
    assert c.commitment(breached)['properties']['status']=='breached'


def test_at020_reextraction_replay_and_crash_between_create_and_record(commitments,admin,monkeypatch):
    """AT-020 at commitment level: re-extraction and replays never duplicate; a crash after the governed create recovers."""
    from nexloop_eios.commitments import CommitmentPort
    c=commitments;now=datetime.now(UTC).replace(microsecond=0)
    message=c.outbound(REPLY,accepted_at=now);vt=deadline(message,now+timedelta(days=1))
    calls={'n':0};original=CommitmentPort.registered
    def crash(self,commitment_id):
        calls['n']+=1
        if calls['n']==1:raise RuntimeError('synthetic crash after the governed create')
        return original(self,commitment_id)
    monkeypatch.setattr(CommitmentPort,'registered',crash)
    first=c.claim(message,QUOTE,valid_time=vt)
    summary=c.keeper().run_once();assert summary['retry']==1 and summary['registered']==0
    assert admin.execute("select count(*) from ontology.objects where type_name='Commitment'").fetchone()==(1,)
    admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp() where feed='commitment-register'")  # retry now (time injection)
    c.tick()
    commitment=c.by_claim(first);assert commitment and calls['n']==2
    # Re-extraction (new extractor version, same message and promise): the same commitment.
    again=c.claim(message,QUOTE,valid_time=vt,correlation=admin.execute('select correlation_key from ontology.nexloop_claims where claim_id=%s',(first,)).fetchone()[0])
    c.tick()
    assert admin.execute("select count(*) from ontology.objects where type_name='Commitment'").fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_commitments').fetchone()==(1,)
    assert admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s',(again,)).fetchone()==('resolved',)
    # A replayed mark of the same Claim changes nothing.
    admin.execute("select authz.nexloop_work_feed_touch(%s,'real','commitment-register',%s,%s)",(TENANT,'claim:'+first,Jsonb({'claim_id':first})))
    c.tick()
    assert admin.execute('select count(*) from runtime.nexloop_commitments').fetchone()==(1,)
    assert [e['kind'] for e in c.reader().commitment(commitment)['events']]==['registered']
