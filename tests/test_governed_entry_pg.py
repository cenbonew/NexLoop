"""NX-028 slice 2 (0150) on clean catalog PostgreSQL: the governed human entry's capability registry (D9), the dispatch
prediction (AT-006 UI part) and the two human requests (manual reevaluation, D7 query of an effect result).

Actual governed effect executor with the independent effect Worker and loopback provider (commitment_fixture); real
browser HUMAN owner session; admin seeds fixtures, probes, and where a test says so injects a ledger state.
"""
from datetime import UTC,datetime,timedelta
import time,uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb
from support.effect_provider import effect_provider
from commitment_fixture import REQUEST_ACTIONS,TENANT,commitments,publish_request_actions  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import active_plan,plan_triggers
from test_nx022_dispatch_e2e import pause

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)


def predict(admin,intent):
    return admin.execute('select control.nexloop_intent_dispatch_prediction(%s,%s,%s)',(TENANT,'real',intent)).fetchone()[0]


def requests(c,identity,uow):
    publish_request_actions(c['admin']);c['extra_human_actions']=REQUEST_ACTIONS
    service=c.service_actions('-request-service');c.owner(identity,uow)
    owner,_=c.owner(identity,uow)
    return owner,c.service_actions_session(service)


def test_registry_is_the_single_capability_source_and_refuses_bad_handlers(commitments,admin):
    rows=dict(admin.execute('select capability,operation from control.nexloop_governed_capabilities').fetchall())
    assert rows=={'goals.metric.approve':'approve_metric','goals.version.publish':'publish_goal','goals.agent.propose':'propose_agent_goal',
        'goals.control.set':'set_control','goals.budget.set':'set_budget','goals.contact.release':'release_contact_restriction',
        'commitment.cancel':'cancel_commitment','commitment.extend':'extend_commitment','commitment.attest':'attest_commitment',
        'commitment.condition_met':'commitment_condition_met','commitment.mark_communication':'mark_commitment_communication',
        'plan.request_reevaluation':'request_plan_reevaluation','service.query_request':'request_effect_query',
        'conversation.takeover':'take_over_conversation','conversation.handback':'hand_back_conversation',  # 0152 (slice 3)
        'message.staff_send':'send_staff_reply',  # 0153 (ruling B)
        'commitment.bind_commercial':'bind_commitment_commercial','cost.record':'record_cost',  # NX-027 (0140)
        'consumer.erase':'erase_consumer','consumer.erase_on_request':'erase_consumer_on_request','message.erase':'erase_message','retention.hold':'hold_retention',
        'retention.release_hold':'release_retention_hold',  # NX-029 (0142)
        'consumer.export':'export_consumer',  # NX-029 slice 3
        'alert.silence':'silence_alert',  # NX-030 (0150)
        'incentive.issue':'issue_incentive',  # AT-043 (0145)
        'restore.open_scope':'open_restore_scope',  # NX-035 (0147)
        'real_dispatch.set':'set_real_dispatch',  # 0153 (NX-037 prerequisite)
        'contact_key.revoke':'revoke_contact_key',  # NX-053 (0154)
        'service.confirm_not_delivered':'confirm_effect_not_delivered',  # 0210
        'consumer.set_persona':'set_consumer_persona','consumer.erase_synthetic':'erase_synthetic_consumer',  # 0220
        'operator_persona.publish':'publish_operator_persona','operator_persona.assign':'assign_operator_persona',
        'operator_persona.rules_publish':'publish_persona_rules','ai_disclosure.set':'set_ai_disclosure',  # 0227
        'contact_preference.dismiss':'dismiss_contact_preference',
        'outreach_controls.set':'set_outreach_controls','outreach_purposes.set':'set_outreach_purpose',
        'outreach.send':'send_outreach','outreach.approve':'decide_outreach_approval','approvals.decide':'decide_approval','approvals.cancel':'cancel_approval','approvals.solo_mode.set':'set_approval_solo_mode','marketing_consent.record':'record_marketing_consent','object_set.publish':'publish_object_set',
        'semantic_release.publish':'publish_semantic_release','semantic_release.rollback':'rollback_semantic_release','semantic_release.stop':'set_evolution_stop','object_view.publish':'publish_object_view',
        'sim_world.create':'create_sim_world','sim_world.delete':'delete_sim_world','sim_world.generate':'generate_sim_world_data','sim_clock.control':'control_sim_clock','consumer_twin.publish':'publish_consumer_twin','consumer_twin.retire':'retire_consumer_twin','consumer_twin.erase_text':'erase_consumer_twin_text','consumer_twin.publish':'publish_consumer_twin','consumer_twin.retire':'retire_consumer_twin','consumer_twin.erase_text':'erase_consumer_twin_text','consumer_twin.calibrate':'calibrate_consumer_twin',
        'strategy.publish':'publish_strategy','strategy.retire':'retire_strategy','experiment.register':'register_experiment',
        'experiment.start':'start_experiment','experiment.stop':'stop_experiment','shadow_copy.set':'set_shadow_copy','sim_world.publish_catalog':'publish_sim_catalog','experiment.report':'read_experiment_report','workbench_member.issue_reset':'issue_workbench_reset','workbench_member.invite':'invite_workbench_member','workbench_member.revoke_invitation':'revoke_workbench_invitation','workbench_member.change_role':'change_workbench_member_role','workbench_member.suspend':'suspend_workbench_member','workbench_member.resume':'resume_workbench_member','workbench_member.revoke':'revoke_workbench_member',  # 0228, NX-058 slice C
        'remediation.resolve':'resolve_remediation','remediation.switch':'switch_remediation',  # NX-057 R1
        'remediation.rule_release':'release_remediation_rule',  # NX-057 R2
        'workbench_member.promote_owner':'promote_workbench_member','workbench_member.demote_owner':'demote_workbench_owner','workbench_member.suspend_owner':'suspend_workbench_owner','workbench_member.resume_owner':'resume_workbench_owner','workbench_member.revoke_owner':'revoke_workbench_owner',
        'workbench_member.issue_owner_reset':'issue_workbench_owner_reset','workbench_member.invite_owner':'invite_workbench_owner','workbench_member.erase_member':'erase_workbench_member',
        'workbench_member.link_external':'link_workbench_external','workbench_member.unlink_external':'unlink_workbench_external',
        'claim_extraction.dead_letter_resolve':'resolve_extraction_dead_letter'}  # NX-058 slices D2, D3, E, F; extraction dead letters
    # Append-only; a handler must be an owner function in control/runtime with the one signature.
    with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():admin.execute("update control.nexloop_governed_capabilities set subject_rule='agent_or_human'")
    for handler in ('pg_catalog.lower(text)','control.nexloop_nx022_owner_change(text,text,text,text,jsonb)'):
        with pytest.raises(psycopg.Error,match='handler invalid'),admin.transaction():
            admin.execute("insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values('x.y','x_y','human',%s,'NX-028')",(handler,))
    # The old entry is kept, without any grant.
    for role in ('nexloop_api','nexloop_domain_worker'):
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_goal_governed_action_before_registry_v0115(text,text,text,text,text)','execute')",(role,)).fetchone()==(False,)
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_goal_governed_action(text,text,text,text,text)','execute')",(role,)).fetchone()==(True,)


def test_prediction_matches_real_dispatch_for_pause_resume_and_contact(commitments,admin,tmp_path):
    """AT-006 UI part: what the workbench predicts is what dispatch then does (zero provider requests when refused)."""
    c=commitments;c.configure_categories()
    queued=c.submit({'service':'后台退款'})['intent_id']
    assert predict(admin,queued)=={'dispatchable':True,'reason':None,'detail':{'snapshot_revision':predict(admin,queued)['detail']['snapshot_revision']}}
    pause(admin,TENANT,'consumer',c['consumer'])
    assert predict(admin,queued)['reason']=='control_paused'
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        result=c.dispatch(provider);assert result['status']=='admission_unavailable' and provider.control('snapshot')['effects']==0
        pause(admin,TENANT,'consumer',c['consumer'],paused=False)
        # Resume is not replay: the old intent stays refused (stale snapshot), exactly as predicted.
        time.sleep(3.1)  # the refused worker's 3 s leases expire naturally
        assert predict(admin,queued)['reason']=='control_revision_stale'
        assert c.dispatch(provider)['status']=='admission_unavailable' and provider.control('snapshot')['effects']==0
        # A new intent after resume (reevaluation) is predicted dispatchable and dispatches once.
        fresh=c.submit({'service':'后台退款（复评后）'})['intent_id']
        assert predict(admin,fresh)['dispatchable'] is True
        outcome=[c.dispatch(provider) for _ in range(2)]
        assert provider.control('snapshot')['effects']==1 and any(r['claimed'] and r['status']!='admission_unavailable' for r in outcome)
        assert predict(admin,fresh)['reason']=='not_queued'
    # Contact restriction: an attached notification and an undeclared contact are predicted with their reasons.
    c.inbound('以后别再给我发消息了')
    noted=c.submit({'service':'退款','notice':'已退款'})['intent_id'];plain=c.submit({'service':'退款'})['intent_id']
    assert predict(admin,noted)['reason']=='attached_notification' and predict(admin,plain)['dispatchable'] is True
    # The prediction writes nothing and leaves no lock or setting behind.
    assert admin.execute("select count(*) from pg_locks where locktype='advisory' and granted and pid=pg_backend_pid()").fetchone()==(0,)
    assert predict(admin,str(uuid.uuid4()))=={'dispatchable':False,'reason':'unavailable','detail':{'intent':'not_found'}}


def test_manual_reevaluation_is_a_human_request_and_marks_the_plan(commitments,admin,identity,uow):
    c=commitments;plan=active_plan(c)
    owner,service=requests(c,identity,uow)
    with pytest.raises(Exception,match='human goal authority required'):
        service.request_plan_reevaluation(action_name='nexloop.plan.request_reevaluation',action_version=1,request_id='reeval-service',plan_id=plan,reason='x')
    assert plan_triggers(c,plan)==[]
    owner,_=c.owner(identity,uow)
    result=owner.request_plan_reevaluation(action_name='nexloop.plan.request_reevaluation',action_version=1,request_id='reeval-owner',plan_id=plan,reason='客户情况有变，请复评')
    assert result['requested'] is True and result['operation']=='request_plan_reevaluation'
    (payload,),=admin.execute("select payload from runtime.nexloop_work_feed where feed='plan-reevaluate' and item_key=%s",('plan:'+str(plan),)).fetchall()
    assert [(t['kind'],t['cause']) for t in payload['triggers']]==[('manual','human')]
    assert admin.execute("select kind,target,reason from runtime.nexloop_human_requests").fetchall()==[('plan_reevaluation','plan:'+str(plan),'客户情况有变，请复评')]
    # Replay of the same governed intent changes nothing; an inactive plan is refused.
    owner.request_plan_reevaluation(action_name='nexloop.plan.request_reevaluation',action_version=1,request_id='reeval-owner',plan_id=plan,reason='客户情况有变，请复评')
    assert admin.execute('select count(*) from runtime.nexloop_human_requests').fetchone()==(1,)
    with pytest.raises(Exception):
        owner.request_plan_reevaluation(action_name='nexloop.plan.request_reevaluation',action_version=1,request_id='reeval-missing',plan_id=str(uuid.uuid4()),reason='x')


def test_effect_query_request_only_brings_an_unknown_result_forward(commitments,admin,identity,uow):
    """D7: the human asks; the executor service queries once. No resend, same outbox row, same idempotency key."""
    c=commitments;receipt=c.submit({'service':'后台退款'});intent=receipt['intent_id']
    owner,_=requests(c,identity,uow)
    with pytest.raises(Exception):  # a queued (not unknown) effect is not awaiting a query
        owner.request_effect_query(action_name='nexloop.service.query_request',action_version=1,request_id='query-pending',intent_id=intent,reason='x')
    with admin.transaction():  # ledger state injection: the provider outcome became unknown an hour from now
        admin.execute("update runtime.nexloop_effect_outbox set state='unknown',available_at=clock_timestamp()+interval '1 hour' where intent_id=%s",(intent,))
    before=admin.execute('select state,fence,available_at from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()
    owner,_=c.owner(identity,uow)
    result=owner.request_effect_query(action_name='nexloop.service.query_request',action_version=1,request_id='query-owner',intent_id=intent,reason='客户说没收到退款')
    assert result['outbox_state']=='unknown' and result['requested'] is True
    after=admin.execute('select state,fence,available_at<=clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()
    assert after==('unknown',before[1],True)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
    assert admin.execute("select kind,target from runtime.nexloop_human_requests").fetchall()==[('effect_query','intent:'+intent)]
