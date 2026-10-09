"""NX-023-A pure Context Engine logic: strategies, budget/trim/insufficient, partition."""
import copy
import json
from pathlib import Path

import pytest

from nexloop_eios.context_engine.budget import ConservativeEstimator,Item,apply_budget
from nexloop_eios.context_engine.sections import (PartitionViolation,assert_partition,claim_items,conversation_items,
    formal_object_item,open_work_items)
from nexloop_eios.context_engine.strategy import StrategyRejected,TRIM_STEPS,load_builtins,strategy_ref,validate

ROOT=Path(__file__).resolve().parents[1]
BUILTINS=load_builtins(ROOT/'deploy/configuration/context-strategies.v1.json')
BASE=next(s for s in BUILTINS if s['strategy_id']=='recent_plus_required')
D='decision:'+'0'*64


def strategy(budget=4000,**sections):
    s=copy.deepcopy(BASE);s['input_token_budget']=budget;s['framing_reserve']=0
    for name,quota in sections.items():s['sections'][name].update(quota)
    return s


def item(section,sub,ref,size,*,relevance=0.5,at='2026-10-09T00:00:00Z',kind=None,dup=None,tags=()):
    kind=kind or {'consumer_state':'formal_object','constraints':'policy','open_work':'execution_state'}.get(section,'conversation')
    return Item(section,sub,ref,'1',{'text':'x'*size},kind,D,relevance=relevance,at=at,duplicate_key=dup,tags=frozenset(tags))


def cost(i):return ConservativeEstimator().estimate(i)


# ------------------------------------------------------------------ strategies
def test_builtin_strategies_are_valid_and_differ_only_in_evidence_selection():
    assert [strategy_ref(s) for s in BUILTINS]==['context-strategy:recent_plus_required@1','context-strategy:ontology_hybrid@1']
    a,b=BUILTINS
    same=lambda s:{k:v for k,v in s.items() if k not in ('strategy_id','sections','semantic')}
    assert same(a)==same(b) and not a['semantic']['enabled'] and b['semantic']['enabled']
    assert a['include_hypotheses'] is False and b['include_hypotheses'] is False


@pytest.mark.parametrize('change',['extra_key','bad_protocol','trim_missing','trim_duplicate','unknown_section','hypotheses_real','rejected_definitions','framing_too_big','quota_negative'])
def test_strategy_validation_rejects(change):
    s=copy.deepcopy(BASE)
    if change=='extra_key':s['pin']=[]
    if change=='bad_protocol':s['protocol']='nexloop.context-pack.v5'
    if change=='trim_missing':s['trim_order']=s['trim_order'][:-1]
    if change=='trim_duplicate':s['trim_order']=s['trim_order'][:-1]+[s['trim_order'][0]]
    if change=='unknown_section':s['sections']['memory']={'max_items':1}
    if change=='hypotheses_real':s['include_hypotheses']=True
    if change=='rejected_definitions':s['include_rejected_definitions']=True
    if change=='framing_too_big':s['framing_reserve']=s['input_token_budget']
    if change=='quota_negative':s['sections']['evidence']['max_items']=-1
    with pytest.raises(StrategyRejected):validate(s,world='real')


def test_hypotheses_allowed_only_outside_real_world():
    s=copy.deepcopy(BASE);s['include_hypotheses']=True
    assert validate(s,world='shadow')['include_hypotheses'] is True


# ------------------------------------------------------------------ budget
def test_under_budget_keeps_everything_and_reports():
    items=[item('current_event','message','m',50),item('consumer_state','Consumer','c',50),item('evidence','conversation','e1',50)]
    out=apply_budget(items,strategy())
    assert out.kept==items and out.omitted==[] and out.insufficient==[]
    assert out.report['used']==sum(cost(i) for i in items) and out.report['sections']['evidence']['included']==1


def test_estimator_overcounts_cjk_and_ascii():
    zh=Item('evidence','conversation','z','1','付款页面一直报错'*10,'conversation',D)
    en=Item('evidence','conversation','e','1','payment page keeps failing '*10,'conversation',D)
    est=ConservativeEstimator()
    assert est.estimate(zh)>=len('付款页面一直报错'*10)            # ≥ 1 token per CJK char
    assert est.estimate(en)>=len('payment page keeps failing '*10)/4  # ≥ 1 token per 4 ASCII bytes


def test_section_quota_drops_lowest_relevance_first():
    items=[item('evidence','recall',f'r{i}',10,relevance=i/10) for i in range(5)]
    out=apply_budget(items,strategy(evidence={'max_items':2}))
    assert [i.ref for i in out.kept]==['r3','r4'] and {o['reason'] for o in out.omitted}=={'section_quota'}


def test_trim_order_is_exactly_the_strategy_order():
    s=strategy(budget=10**6,evidence={'max_items':100,'recent_turns':1},semantics={'max_items':10},experience={'max_items':10})
    items=[item('current_event','message','now',100),
        item('evidence','conversation','dup-old',100,at='2026-01-01',dup='k'),item('evidence','conversation','dup-new',100,at='2026-01-03',dup='k'),
        item('experience','case','exp',100),item('semantics','definition','sem',100),
        item('evidence','conversation','old-turn',100,at='2026-01-02'),item('evidence','claim_evidence','claim-old',100,at='2026-01-01',kind='user_statement'),
        item('evidence','recall','recall-low',100,relevance=0.1)]
    full=sum(cost(i) for i in items)
    # Budget forces dropping exactly 4 of the 7 loose items: duplicate, experience, semantics, older conversation.
    s['input_token_budget']=full-4*cost(items[1])+1
    out=apply_budget(items,s)
    assert [o['ref'] for o in out.omitted]==['dup-old','exp','sem','old-turn']
    assert [o['reason'] for o in out.omitted]==['duplicate_evidence','experience','semantics_low_relevance','older_conversation']
    assert out.insufficient==[]


def test_negation_contact_limit_and_unconfirmed_execution_are_never_trimmed():
    s=strategy(budget=10**6,evidence={'max_items':100,'recent_turns':0})
    pinned=[item('evidence','claim_evidence','no-promo',100,kind='user_statement',tags={'contact_limit'},at='2020-01-01'),
        item('evidence','conversation','negated',100,tags={'negation'},at='2020-01-01'),
        item('open_work','outbound','unconfirmed',100,tags={'unconfirmed'})]
    loose=[item('evidence','conversation',f'turn{i}',100,at=f'2026-01-0{i+1}') for i in range(5)]
    items=[item('current_event','message','now',100)]+pinned+loose
    s['input_token_budget']=sum(cost(i) for i in items[:4])+cost(loose[0])  # room for exactly one loose turn
    out=apply_budget(items,s)
    assert {i.ref for i in out.kept}>={'now','no-promo','negated','unconfirmed'} and out.insufficient==[]
    assert [i.ref for i in out.kept if i.ref.startswith('turn')]==['turn4']  # newest survives, oldest go first


def test_mandatory_over_budget_is_insufficient_not_truncated():
    items=[item('current_event','message','now',5000),item('constraints','policy','no-contact',5000),item('evidence','conversation','e',10)]
    out=apply_budget(items,strategy(budget=2000))
    assert [i.ref for i in out.kept]==['now','no-contact'] and [i.content for i in out.kept]==[items[0].content,items[1].content]
    assert out.insufficient==[{'code':'mandatory_exceeds_budget','section':None,'refs':['now','no-contact']}]
    assert out.omitted==[{'section':'evidence','subsection':'conversation','ref':'e','reason':'mandatory_exceeds_budget'}]


def test_core_trimming_is_reported_as_insufficient():
    items=[item('current_event','message','now',100)]+[item('consumer_state','Consumer',f'c{i}',400,relevance=i/10) for i in range(3)]
    s=strategy();s['input_token_budget']=cost(items[0])+cost(items[1])+10
    out=apply_budget(items,s)
    assert [i.ref for i in out.kept]==['now','c2'] and out.insufficient==[{'code':'core_trimmed','section':None,'refs':['c0','c1']}]


# ------------------------------------------------------------------ partition (AT-064)
def claim(claim_id,kind='need_problem',state='unresolved',*,polarity='affirmed',speaker='consumer',source=True,confidence=0.8):
    return {'claim_id':claim_id*64,'epistemic_kind':kind,'resolution_state':state,'polarity':polarity,'speaker':speaker,'modality':'asserted','condition':'',
        'predicate':'付款页面故障','value':{'type':'string','value':'报错'},'derived_from':[] if source else ['e'*64],'confidence':confidence,
        'extractor_version':'nx019-extractor/2','recorded_at':'2026-10-09T00:00:00Z','correlation_key':'c'*64,
        'source':{'message_id':'m'*64,'sequence':1,'span_start':0,'span_end':3,'content_hash':'h'*64,'quote':'付款页'} if source else None}


def test_hypotheses_excluded_by_default_and_review_claims_are_text_only():
    view={'statements':[claim('a',state='awaiting_definition'),claim('b',state='rejected_definition'),claim('c',state='superseded'),
            claim('d','constraint',polarity='negated')],'hypotheses':[claim('e','hypothesis','hypothesis_only',source=False)]}
    items=claim_items(view,BASE,decision=D)
    assert sorted(i.ref for i in items)==['claim:'+'a'*64,'claim:'+'d'*64]
    for i in items:
        assert i.section=='evidence' and i.subsection=='claim_evidence' and 'value' not in i.content and i.content['quote']=='付款页'
    d=next(i for i in items if i.ref.endswith('d'*64));assert {'negation','contact_limit'}<=d.tags and d.pinned
    research=copy.deepcopy(BASE);research['include_hypotheses']=True
    hyp=[i for i in claim_items(view,research,decision=D) if i.evidence_kind=='hypothesis']
    assert len(hyp)==1 and (hyp[0].section,hyp[0].subsection)==('evidence','hypotheses') and hyp[0].relevance<=0.6 and not hyp[0].pinned


@pytest.mark.parametrize('bad',[('consumer_state','Consumer','claim:'+'a'*64,'formal_object'),('consumer_state','Consumer','eios:object:Consumer/x','user_statement'),
    ('constraints','policy','eios:object:Consumer/x','hypothesis'),('open_work','outbound','nexloop:outbound:x','conversation'),
    ('evidence','claim_evidence','claim:x','hypothesis'),('evidence','free_text','x','conversation')])
def test_formal_zone_never_admits_claims_or_hypotheses(bad):
    section,sub,ref,kind=bad
    with pytest.raises(PartitionViolation):assert_partition([Item(section,sub,ref,'1',{},kind,D)])


def test_open_work_outbound_states_and_conversation_labels():
    work={'intents':[{'intent_id':'i1','action':'nexloop.service.request:1','state':'unknown','created_at':'2026-10-09T00:00:00Z'}],
        'outbound':[{'intent_id':s,'delivery_state':s,'delivery_changed_at':'2026-10-09T00:00:00Z'} for s in ('persisted','dispatching','unknown','failed','provider_accepted','delivered')]}
    items=open_work_items(work,decision=D)
    assert sorted(i.ref for i in items)==sorted(['nexloop:intent:i1']+['nexloop:outbound:'+s for s in ('persisted','dispatching','unknown','failed')])
    assert all(i.pinned for i in items if i.revision in ('persisted','dispatching','unknown')) and not next(i for i in items if i.revision=='failed').pinned
    convo=conversation_items([{'id':'a'*64,'sequence':1,'body':'不要再发促销','accepted_at':'t1'},
        {'id':'b'*64,'sequence':2,'body':'好的','accepted_at':'t2','direction':'outbound'}],decision=D,negation_refs={'eios:object:Message/'+'a'*64})
    assert [i.content['speaker'] for i in convo]==['consumer','agent'] and convo[0].pinned and not convo[1].pinned
    formal=formal_object_item('consumer_state',{'type_name':'Consumer','object_id':'c'*64,'revision':3,'properties':{'budget_level':'两千元以内'}},decision=D)
    assert formal.evidence_kind=='formal_object' and formal.ref=='eios:object:Consumer/'+'c'*64 and formal.revision=='3'


def test_strategy_and_section_vocabularies_match_storage_constraints():
    sql=(ROOT/'packages/eios-core/src/eios/migrations').glob('*_nx023_context_manifests.sql')
    text=next(sql).read_text()
    from nexloop_eios.context_engine.budget import SECTIONS
    for name in SECTIONS:assert f"'{name}'" in text
    assert set(TRIM_STEPS)==set(BASE['trim_order'])
