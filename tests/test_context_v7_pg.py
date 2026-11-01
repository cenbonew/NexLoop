"""K4c (0398): context-pack v7 on Role (plan) Runs on real PostgreSQL - the v6 Role fixture with a strategy that declares v7 and the
deployment switch on. The latest revision of each answered question of the Consumer's plans is in evidence.human_answers as a staff
member's statement (never a formal section); SQL re-derives every item and refuses a forged, stale, withdrawn, erased, foreign or
misplaced one, a missing one and a v7 pack under a v6 strategy; the quota keeps 8 (section_quota, the v6 reasons); the switch off or a
v6 strategy keeps the v6 bytes. Questions, answers and the plan row are seeded as admin (session_replication_role replica, as the K4b
handler would leave them); everything else is the fixture's governed path. Synthetic data only."""
import copy,json,uuid
from datetime import UTC,datetime,timedelta

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.context_artifacts import ContextArtifactUnavailable
from nexloop_eios.contracts import ContextPackV6,ContextPackV7
from nexloop_eios.role_context_artifacts import RoleContextV6ArtifactProducer
from test_context_v6_pg import STRATEGY,_recount,_rehash,owner,seed_strategy,sources
from test_role_context_v6_pg import STRATEGY_ID,new_role_run,role_row,role_v6  # noqa: F401
from role_run_fixture import role_runtime_plan  # noqa: F401
from test_context_input import run_parser
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401

V7=dict(STRATEGY,version=STRATEGY['version']+1,protocol='nexloop.context-pack.v7')


def seed_question(admin,tenant,consumer,*,answers,state='answered',kind='customer_fact',text='客户说下周二在家吗？',options=('在','不在'),plan=None):
    """One plan row of the Consumer (no plan event: not an active plan), its outcome, one question and its answer revisions."""
    plan=plan or str(uuid.uuid4());run=str(uuid.uuid4());question=str(uuid.uuid4())
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into runtime.nexloop_plans(tenant_id,world,plan_id,version,consumer_id,goal_version_ref,context_strategy_ref,recipe,settings,control_snapshot,created_by)"
                      " values(%s,'real',%s,1,%s,'goal:k4c@1','context-strategy:recent_plus_required@1','{}','{}','{}','test') on conflict do nothing",(tenant,plan,consumer))
        admin.execute("insert into runtime.nexloop_plan_outcomes(tenant_id,world,run_id,plan_id,version,kind,outcome,outcome_digest) values(%s,'real',%s,%s,1,'escalate','{}',%s)",
                      (tenant,run,plan,'b'*64))
        admin.execute("insert into runtime.nexloop_human_questions(tenant_id,world,question_id,run_id,plan_id,plan_version,audience,question_kind,text,options,subject_ref,"
                      "withdrawable,kinds_version,state,revision,due_by) values(%s,'real',%s,%s,%s,1,'operator',%s,%s,%s,null,true,1,%s,%s,clock_timestamp()+interval '7 days')",
                      (tenant,question,run,plan,kind,text,Jsonb(list(options)),state,1+len(answers)))
        for revision,(answer_kind,value,role) in enumerate(answers,start=2):
            admin.execute("insert into runtime.nexloop_human_answers(tenant_id,world,question_id,revision,answer_kind,option_index,text,principal_id,role,request_intent,recorded_at)"
                          " values(%s,'real',%s,%s,%s,%s,%s,'principal-k4c',%s,%s,clock_timestamp()+make_interval(secs=>%s))",
                          (tenant,question,revision,answer_kind,value if answer_kind=='option' else None,value if answer_kind=='text' else None,role,'intent:'+str(uuid.uuid4()),revision))
    return question


@pytest.fixture
def v7(role_v6,admin,monkeypatch):  # noqa: F811
    plan=role_v6
    seed_strategy(admin,plan['tenant'],V7)
    monkeypatch.setenv('NEXLOOP_CONTEXT_V7','plan')
    return plan


def prepare(plan,admin,**kw):
    source,run,command,ids=new_role_run(plan,admin)
    producer=RoleContextV6ArtifactProducer(source,STRATEGY_ID,**kw)
    return producer,producer.prepare(run_token=run.token,command=command,body='后续角色触发',**ids),run


def answers(pack):return [i for i in pack['evidence'] if i['subsection']=='human_answers']


def test_the_latest_answer_of_each_question_is_a_v7_staff_statement(v7,admin):
    plan,tenant=v7,v7['tenant']
    corrected=seed_question(admin,tenant,plan['consumer'],answers=[('option',0,'operator'),('text','他说下周三才在家','owner')])
    withdrawn=seed_question(admin,tenant,plan['consumer'],answers=[('option',1,'operator'),('withdraw',None,'operator')],state='withdrawn')
    seed_question(admin,tenant,plan['consumer'],answers=[('option',1,'operator')],state='superseded')
    seed_question(admin,tenant,'f'*64,answers=[('option',0,'operator')])  # another Consumer's plan
    producer,bound,run=prepare(plan,admin)
    pack=json.loads(bound['input']);ContextPackV7.model_validate(pack)
    assert pack['schema_version']=='nexloop.context-pack.v7' and producer.protocol=='nexloop.context-pack.v7'
    got=answers(pack)
    assert [i['ref'] for i in got]==['nexloop:human_answer:'+corrected+'@3'] and withdrawn not in json.dumps(pack)
    item=got[0]
    assert item['evidence_kind']=='user_statement' and item['tags']==[] and item['relevance_permille']==1000
    assert item['content']['answer']=={'kind':'text','text':'他说下周三才在家'} and item['content']['answerer_role']=='owner'
    assert item['content']['question_ref']=='nexloop:human_question:'+corrected and 'principal-k4c' not in json.dumps(item)
    for section in ('constraints','consumer_state','open_work','semantics','experience'):  # never a formal section
        assert not any('human_answer' in i['ref'] for i in pack[section])
    with admin.transaction():
        owner(admin,tenant)
        assert admin.execute('select protocol from runtime.nexloop_context_packs where run_id=%s',(run.run_id,)).fetchone()==('nexloop.context-pack.v7',)
    stored=sources(admin,tenant,role_row(admin,run.run_id)[2])
    assert [r for r in stored if r[2]==item['ref']][0][1:]==('evidence',item['ref'],'3',item['content_hash'],'user_statement',item['access_decision_ref'],'included',None)


def test_the_host_reads_the_v7_plan_pack_and_refuses_a_misplaced_answer(v7,admin,tmp_path):  # V8
    plan=v7
    seed_question(admin,plan['tenant'],plan['consumer'],answers=[('option',1,'operator')])
    source,run,command,ids=new_role_run(plan,admin)
    bound=RoleContextV6ArtifactProducer(source,STRATEGY_ID).prepare(run_token=run.token,command=command,body='后续角色触发',**ids)
    command={**command,'context_manifest_ref':bound['artifact_ref']}
    pack=json.loads(bound['input'])
    assert json.loads(run_parser(tmp_path,command,pack))['body']==pack['current_event']['body']
    def changed(change):
        forged=copy.deepcopy(pack);change(forged);return run_parser(tmp_path,command,forged)
    item=lambda p:[i for i in p['evidence'] if i['subsection']=='human_answers'][0]
    assert changed(lambda p:p.update(schema_version='nexloop.context-pack.v6'))=='runtime_context_invalid'  # never in a v6 pack
    assert changed(lambda p:item(p).update(evidence_kind='conversation'))=='runtime_context_invalid'
    assert changed(lambda p:item(p).update(tags=['unconfirmed']))=='runtime_context_invalid'
    assert changed(lambda p:item(p)['content'].update(answerer_role='reviewer') or _rehash(item(p)))=='runtime_context_invalid'
    assert changed(lambda p:p['consumer_state'].append(dict(item(p),subsection='human_answers',evidence_kind='policy')))=='runtime_context_invalid'


def test_the_switch_off_or_a_v6_strategy_keeps_v6(v7,admin,monkeypatch):
    plan=v7
    seed_question(admin,plan['tenant'],plan['consumer'],answers=[('option',0,'operator')])
    _,bound,_=prepare(plan,admin,context_v7=False)
    pack=json.loads(bound['input']);ContextPackV6.model_validate(pack)
    assert pack['schema_version']=='nexloop.context-pack.v6' and not answers(pack)
    monkeypatch.setenv('NEXLOOP_CONTEXT_V7','off')
    _,bound,_=prepare(plan,admin)
    assert json.loads(bound['input'])['schema_version']=='nexloop.context-pack.v6'


def test_the_quota_keeps_eight_and_names_the_rest_section_quota(v7,admin):
    plan=v7
    ids=[seed_question(admin,plan['tenant'],plan['consumer'],answers=[('option',i%2,'operator')]) for i in range(9)]
    _,bound,run=prepare(plan,admin)
    pack=json.loads(bound['input'])
    kept=answers(pack)
    omitted=[o for o in pack['budget_report']['omitted'] if o['subsection']=='human_answers']
    assert 1<=len(kept)<=8 and len(kept)+len(omitted)==9 and {o['reason'] for o in omitted}=={'section_quota'}
    assert {o['ref'].split(':')[2].split('@')[0] for o in omitted}<=set(ids)
    assert pack['insufficient']==[]
    stored=sources(admin,plan['tenant'],role_row(admin,run.run_id)[2])
    assert all([r[7:] for r in stored if r[2]==o['ref']]==[('omitted','section_quota')] for o in omitted)


def tamper(mode,question=None):
    def apply(body):
        got=[i for i in body['evidence'] if i['subsection']=='human_answers']
        if mode=='content':got[0]['content']['answer']['text']='改过的回答';_rehash(got[0])
        elif mode=='stale_revision':got[0]['ref']=got[0]['ref'].rsplit('@',1)[0]+'@2';got[0]['revision']='2'
        elif mode=='foreign':got[0]['ref']='nexloop:human_answer:'+str(uuid.uuid4())+'@2'
        elif mode=='missing':body['evidence']=[i for i in body['evidence'] if i['subsection']!='human_answers']
        elif mode=='misplaced':
            item=got[0];body['evidence'].remove(item);item=dict(item,evidence_kind='policy');body['consumer_state'].append(item)
        elif mode=='kind':got[0]['evidence_kind']='conversation'
        elif mode=='tagged':got[0]['tags']=['unconfirmed']
        _recount(body)
    return apply


TAMPER={'content':'context v7 human answer mismatch','stale_revision':'context v7 human answer mismatch','foreign':'context v7 human answer mismatch',
        'missing':'context v7 human answer missing','kind':'context v7 human answer mismatch','tagged':'context v7 human answer mismatch'}


@pytest.mark.parametrize('mode',sorted(TAMPER))
def test_sql_refuses_a_forged_stale_foreign_missing_or_relabelled_answer(v7,admin,monkeypatch,mode):
    plan=v7
    seed_question(admin,plan['tenant'],plan['consumer'],answers=[('option',0,'operator'),('text','改到周三','operator')])
    original=RoleContextV6ArtifactProducer.assemble
    def assemble(self,snapshot,command):
        body,outcome,items,proofs=original(self,snapshot,command);tamper(mode)(body);return body,outcome,items,proofs
    monkeypatch.setattr(RoleContextV6ArtifactProducer,'assemble',assemble)
    source,run,command,ids=new_role_run(plan,admin)
    producer=RoleContextV6ArtifactProducer(source,STRATEGY_ID)
    with pytest.raises(ContextArtifactUnavailable):producer.prepare(run_token=run.token,command=command,body='后续角色触发',**ids)
    assert producer.last_diagnostic[1]==TAMPER[mode],(mode,producer.last_diagnostic)
    assert role_row(admin,run.run_id) is None


def test_after_the_erasure_or_a_withdrawal_the_answer_is_gone(v7,admin):
    plan=v7
    question=seed_question(admin,plan['tenant'],plan['consumer'],answers=[('text','他说周五在家','operator')])
    _,bound,_=prepare(plan,admin)
    assert len(answers(json.loads(bound['input'])))==1
    with admin.transaction():  # what the K4b erasure trigger leaves: question and answer texts blanked
        admin.execute('set local session_replication_role=replica')
        admin.execute("update runtime.nexloop_human_questions set text='',options='[]' where question_id=%s",(question,))
        admin.execute("update runtime.nexloop_human_answers set text='' where question_id=%s",(question,))
    _,bound,_=prepare(plan,admin)
    assert answers(json.loads(bound['input']))==[]
    again=seed_question(admin,plan['tenant'],plan['consumer'],answers=[('option',0,'operator'),('withdraw',None,'operator')],state='withdrawn')
    _,bound,_=prepare(plan,admin)
    assert again not in bound['input']


def test_a_v7_pack_needs_a_v7_strategy_and_a_v6_pack_no_answers(v7,admin,monkeypatch):
    plan=v7
    seed_question(admin,plan['tenant'],plan['consumer'],answers=[('option',0,'operator')])
    original=RoleContextV6ArtifactProducer.assemble
    def as_v6(self,snapshot,command):  # a v7 body relabelled v6: the v6 bind refuses the answer subsection
        body,outcome,items,proofs=original(self,snapshot,command);body['schema_version']='nexloop.context-pack.v6';return body,outcome,items,proofs
    monkeypatch.setattr(RoleContextV6ArtifactProducer,'assemble',as_v6)
    source,run,command,ids=new_role_run(plan,admin)
    producer=RoleContextV6ArtifactProducer(source,STRATEGY_ID)
    with pytest.raises(ContextArtifactUnavailable):producer.prepare(run_token=run.token,command=command,body='后续角色触发',**ids)
    assert role_row(admin,run.run_id) is None


def test_the_completeness_check_refuses_a_v7_pack_under_a_v6_strategy(role_v6,admin):  # noqa: F811  (L6 W1: Q1 locked)
    import psycopg
    tenant=role_v6['tenant']  # the fixture's current strategy declares v6
    pack={'strategy_ref':'context-strategy:recent_plus_required@'+str(STRATEGY['version']),'evidence':[],'constraints':[],'consumer_state':[],
          'open_work':[],'semantics':[],'experience':[]}
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused,admin.transaction():
        admin.execute('select authz.nexloop_context_human_answers_complete(%s,%s,%s,%s,%s)',(tenant,'real','e'*64,Jsonb(pack),Jsonb({'omitted_items':[]})))
    assert refused.value.diag.message_primary=='context v7 strategy not v7'
    seed_strategy(admin,tenant,V7)
    pack['strategy_ref']='context-strategy:recent_plus_required@'+str(V7['version'])
    with admin.transaction():  # the same call under the v7 strategy passes (no answers expected for that Consumer)
        admin.execute('select authz.nexloop_context_human_answers_complete(%s,%s,%s,%s,%s)',(tenant,'real','e'*64,Jsonb(pack),Jsonb({'omitted_items':[]})))
