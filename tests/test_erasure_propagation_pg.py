"""NX-029 final (AT-026): a Consumer erasure reaches every copy - plans and strategies, formal objects that name the
Consumer (RelationshipAssessment, ConsumerRoleLink, Goal, PlanStep, ...), the effect ledger - and erased messages leave
the reads that show or assemble conversations. Synthetic data only; the end-to-end check scans every table."""
from datetime import UTC,datetime
import json,secrets,uuid

import pytest
from psycopg.types.json import Jsonb
from commercial_fixture import TENANT
from test_erasure_pg import ACTIONS,KEEPER,erasing,keeper,act,request  # noqa: F401
from test_erasure_pg import pytestmark  # noqa: F401  (execution_plan parametrization)
from test_commitments_pg import active_plan,fresh
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

TEXTS=('13800000000','红色款','已为您登记','下周再联系')


def scan(admin,needles):
    """Every row of every application table whose JSON form contains one of the needles."""
    tables=admin.execute("""select n.nspname||'.'||c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
      where c.relkind in ('r','p') and n.nspname in ('ontology','runtime','control','authz','public')""").fetchall()
    hits={}
    for (table,) in tables:
        for needle in needles:
            n=admin.execute(f'select count(*) from {table} x where to_jsonb(x)::text like %s',('%'+needle+'%',)).fetchone()[0]
            if n:hits.setdefault(needle,{})[table]=n
    return hits


def seed_references(c,admin,plan):
    """Objects and working state that name the Consumer, seeded as their writers leave them (guards bypassed for the
    seed only; the erasure under test runs with every trigger on)."""
    consumer=c['consumer'];now=datetime.now(UTC)
    assessment,link,role=(secrets.token_hex(32) for _ in range(3))
    props={'relation_name':'prefers','relation_version':0,'source_type':'Consumer','source_id':consumer,'target_type':'Product','target_id':'f'*64,
        'conclusion':'顾客偏好红色款','epistemic_kind':'user_statement','resolution_state':'awaiting_definition','valid_from':now.isoformat(),'valid_to':'',
        'evidence_message_id':None,'evidence_content_hash':None,'corrects_revision':0}
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for type_name,object_id,properties in (('RelationshipAssessment',assessment,props),
                ('ConsumerRoleLink',link,{'consumer_id':consumer,'role_id':role,'scope':'红色款售后','active':True,'valid_from':'2026-01-01T00:00:00Z','valid_until':'2999-01-01T00:00:00Z'}),
                ('RoleDefinition',role,{'name':'售后','responsibility':'跟进','ceiling_ref':'ceiling','active':True,'valid_from':'2026-01-01T00:00:00Z','valid_until':'2999-01-01T00:00:00Z'})):
            admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                values(%s,'real',%s,%s,1,%s,'synthetic','',clock_timestamp(),clock_timestamp())''',(TENANT,type_name,object_id,Jsonb(properties)))
        admin.execute('''insert into ontology.nexloop_assessment_revisions(tenant_id,world,assessment_id,revision,prior_properties,new_properties,principal_id,action_resource,intent_id,transaction_id)
            values(%s,'real',%s,1,null,%s,'synthetic','eios:action:RelationshipAssessment.create:1','i',txid_current())''',(TENANT,assessment,Jsonb(props)))
        admin.execute('''insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,created_at)
            values(%s,%s,'real','grants',1,'RoleDefinition',%s,'ConsumerRoleLink',%s,clock_timestamp())''',(secrets.token_hex(16),TENANT,role,link))
        admin.execute('''insert into ontology.events(event_id,tenant_id,event_type,object_type,object_id,payload,actor_id,sequence,created_at)
            values(%s,%s,'created','ConsumerRoleLink',%s,%s,'synthetic',1,clock_timestamp())''',(secrets.token_hex(16),TENANT,link,Jsonb({'scope':'红色款售后'})))
    run=uuid.uuid4()
    admin.execute("""insert into runtime.nexloop_strategies(tenant_id,world,strategy_id,version,consumer_id,goal_version_ref,content,created_by)
        values(%s,'real',%s,1,%s,'goal:synthetic-goal@1','顾客想要红色款，手机号 13800000000','synthetic')""",(TENANT,uuid.uuid4(),consumer))
    admin.execute("""insert into runtime.nexloop_plan_steps(tenant_id,world,plan_id,version,step_key,prerequisites,expected_result,stop_if,budget)
        values(%s,'real',%s,1,'confirm',%s,'联系顾客确认红色款','[]','{}')""",(TENANT,plan,Jsonb(['等顾客回复红色款'])))
    admin.execute("insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason) values(%s,'real',%s,1,'stale','顾客说下周再联系')",(TENANT,plan))
    admin.execute("insert into runtime.nexloop_plan_runs(tenant_id,world,run_id,plan_id,version,triggers) values(%s,'real',%s,%s,1,'[]')",(TENANT,run,plan))
    admin.execute("""insert into runtime.nexloop_plan_outcomes(tenant_id,world,run_id,plan_id,version,kind,outcome,outcome_digest)
        values(%s,'real',%s,%s,1,'no_action',%s,%s)""",(TENANT,run,plan,Jsonb({'note':'顾客说红色款太贵'}),'f'*64))
    return {'assessment':assessment,'link':link,'role':role}


def run_erasure(c,admin,consumer):
    for _ in range(20):
        keeper(c,host=lambda run_id:'purged').run_once()
        if request(admin,consumer)[1]=='completed':return
    raise AssertionError(request(admin,consumer))


def test_consumer_erasure_reaches_plans_formal_objects_and_the_effect_ledger(erasing,admin):
    c=erasing;consumer=c['consumer']
    plan=active_plan(c);commitment,_=fresh(c)
    c.inbound('我的手机号是 13800000000，请删除我的资料')
    reply=c.outbound('好的，已为您登记');c.claim(reply,'已为您登记')
    # A conditional statement keeps a condition while it lives; the erasure clears it too.
    c.claim(c.outbound('如果红色款到货就通知您'),'红色款到货就通知您',modality='conditional',condition='红色款到货')
    seeded=seed_references(c,admin,plan)
    before=scan(admin,TEXTS)
    assert {'runtime.nexloop_strategies','runtime.nexloop_plan_steps','runtime.nexloop_plan_events','runtime.nexloop_plan_outcomes',
            'ontology.objects','ontology.nexloop_assessment_revisions','runtime.nexloop_effect_intents'}<=set().union(*before.values())
    act(c['owner_actions'],'erase_consumer','erase-1',consumer_id=consumer,reason='顾客要求删除')
    run_erasure(c,admin,consumer)
    # The original text is nowhere: no table holds it any more.
    assert scan(admin,TEXTS)=={}
    # No formal object, relation, event or assessment revision names the Consumer; unrelated objects stay.
    assert admin.execute('select count(*) from ontology.objects where properties::text like %s or object_id=%s',('%'+consumer+'%',consumer)).fetchone()==(0,)
    assert admin.execute('select count(*) from ontology.relations where %s in (source_object_id,target_object_id) or source_object_id=any(%s) or target_object_id=any(%s)',
        (consumer,[seeded['assessment'],seeded['link']],[seeded['assessment'],seeded['link']])).fetchone()==(0,)
    assert admin.execute('select count(*) from ontology.events where object_id=any(%s)',([consumer,seeded['assessment'],seeded['link']],)).fetchone()==(0,)
    assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions where assessment_id=%s',(seeded['assessment'],)).fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition' and object_id=%s",(seeded['role'],)).fetchone()==(1,)
    deleted={k for (k,) in admin.execute("select split_part(subject_key,'/',1) from runtime.nexloop_erasure_tombstones where item_class='formal_object' and action='deleted'").fetchall()}
    assert {'RelationshipAssessment','ConsumerRoleLink','Goal','PlanStep','EffectControl','ConsumerServiceOffering'}<=deleted
    # Plans keep their status chain (codes, versions, digests) without text; the Commitment keeps D4 handling.
    assert admin.execute('select status,reason from runtime.nexloop_plan_events where plan_id=%s order by event_id',(plan,)).fetchall()==[
        ('active','established'),('stale','')]
    assert admin.execute('select expected_result,prerequisites from runtime.nexloop_plan_steps where plan_id=%s',(plan,)).fetchone()==('',[])
    assert admin.execute('select outcome,outcome_digest from runtime.nexloop_plan_outcomes where plan_id=%s',(plan,)).fetchone()==({'erased':True},'f'*64)
    assert admin.execute("select properties->>'made_to' from ontology.objects where type_name='Commitment' and object_id=%s",(commitment,)).fetchone()[0].startswith('erasure:')
    # The effect ledger keeps states and receipts, not the request text.
    assert admin.execute('select state,frozen_request from runtime.nexloop_effect_intents where intent_id=%s',(reply['intent_id'],)).fetchone()==('fulfilled',{'erased':True})
    counts=request(admin,consumer)[3]
    assert counts['strategies']==1 and counts['plan_steps']==1 and counts['plan_reasons']==1 and counts['plan_outcomes']==1 and 'effect_requests' in counts
    assert admin.execute("select count(*) from runtime.nexloop_effect_intents where consumer_id=%s and not frozen_request ? 'erased'",(consumer,)).fetchone()==(0,)
    # Plan text is append-only for everyone else: no session setting or ordinary update can clear it.
    with pytest.raises(Exception,match='append-only'),admin.transaction():
        admin.execute("update runtime.nexloop_plan_events set reason='x' where plan_id=%s",(plan,))


def test_assessment_guards_still_refuse_outside_an_erasure(erasing,admin):
    c=erasing;plan=active_plan(c);seeded=seed_references(c,admin,plan)
    with pytest.raises(Exception,match='assessment deletion unsupported'),admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("delete from ontology.objects where type_name='RelationshipAssessment' and object_id=%s",(seeded['assessment'],))
    with pytest.raises(Exception,match='assessment audit immutable'),admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute('delete from ontology.nexloop_assessment_revisions where assessment_id=%s',(seeded['assessment'],))


def test_message_erasure_clears_the_intent_that_sent_it(erasing,admin):
    c=erasing;owner=c['owner_actions']
    sent=c.outbound('好的，已为您登记');other=c.outbound('另一条回复')
    out=act(owner,'erase_message','erase-msg-1',message_id=sent['message_id'],reason='顾客要求删除这条消息')
    assert out['intents']==1
    assert admin.execute('select state,frozen_request from runtime.nexloop_effect_intents where intent_id=%s',(sent['intent_id'],)).fetchone()==('fulfilled',{'erased':True})
    assert admin.execute("select frozen_request->'parameters'->>'message' from runtime.nexloop_effect_intents where intent_id=%s",(other['intent_id'],)).fetchone()==('另一条回复',)
    assert scan(admin,['已为您登记'])=={}
