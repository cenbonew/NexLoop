"""K4c (context-pack v7) without a database: the contract is v6 plus the human_answers subsection only (V9) and keeps v6's omission
reasons (V2/V11); v6 and its published digest are untouched; the partition admits staff answers only as v7 evidence.human_answers user
statements; the staff answers' quota (8 items, 1500 estimated tokens, section_quota) and their trimming before core state (never
insufficient); the deployment switch; and V1/V12: each 0398 core, with its parameterised points reverted, is its source body exactly."""
import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from nexloop_eios.context_engine.budget import HUMAN_ANSWER_ITEMS,Item,apply_budget
from nexloop_eios.context_engine.sections import PartitionViolation,assert_partition
from nexloop_eios.role_context_artifacts import context_v7_enabled

ROOT=Path(__file__).resolve().parents[1]
C=ROOT/'packages/contracts'
M=ROOT/'packages/eios-core/src/eios/migrations'


def load(name):return json.loads((C/name).read_text())


def test_v7_is_v6_plus_the_human_answers_subsection_only():  # V9, V11
    v6,v7=load('context-pack-v6.schema.json'),load('context-pack-v7.schema.json')
    published={e['path']:e['sha256'] for e in load('published.json')['contracts']}
    assert published['context-pack-v6.schema.json']=='9f50b585d8d5eb7d6e3a7fee9d7b273cd990aac5dc6e86080680f0cf2b354e0a'
    for name in ('context-pack-v6.schema.json','context-pack-v7.schema.json'):
        assert hashlib.sha256((C/name).read_bytes()).hexdigest()==published[name]
    expected=copy.deepcopy(v6)
    expected.update({'$id':'urn:nexloop:contracts:v1:context-pack-v7','title':'context-pack-v7'})
    expected['properties']['schema_version']={'const':'nexloop.context-pack.v7'}
    ev=expected['properties']['evidence']['items']
    ev['properties']['subsection']['enum'].append('human_answers')
    ev['allOf']+=v7['properties']['evidence']['items']['allOf'][len(ev['allOf']):]
    assert v7==expected and len(v7['properties']['evidence']['items']['allOf'])==len(v6['properties']['evidence']['items']['allOf'])+2
    # V2: the omission reasons are v6's exactly; nothing new.
    assert v7['properties']['budget_report']==v6['properties']['budget_report']
    assert set(v7['properties'])==set(v6['properties']) and v7['required']==v6['required']


def item(subsection='human_answers',ref=None,section='evidence',kind='user_statement',at='2026-11-01T00:00:00.000000Z',size=10,tags=()):
    ref=ref or 'nexloop:human_answer:'+'0'*8+'-0000-0000-0000-'+'0'*12+'@2'
    return Item(section,subsection,ref,'2',{'text':'x'*size},kind,'decision:'+'a'*64,relevance=1.0,at=at,tags=tags)


def test_the_partition_admits_staff_answers_only_as_v7_evidence_statements():
    assert assert_partition([item()],human_answers=True)
    for bad,v7 in ((item(),False),(item(kind='conversation'),True),(item(section='consumer_state',kind='policy'),True),
                   (item(subsection='conversation'),True),(item(ref='claim:x'),True),(item(tags={'unconfirmed'}),True)):
        with pytest.raises(PartitionViolation):assert_partition([bad],human_answers=v7)


STRATEGY={'input_token_budget':100000,'output_reserve':1000,'framing_reserve':1000,'trim_order':['duplicate_evidence','experience','hypotheses',
          'semantics_low_relevance','older_conversation','older_claim_evidence','low_score_recall'],
          'sections':{s:{'max_items':64} for s in ('consumer_state','open_work','semantics','experience')}|{'evidence':{'max_items':64,'recent_turns':4}}}


def answers(n,size=10):
    return [item(ref='nexloop:human_answer:%08d-0000-0000-0000-000000000000@2'%i,at='2026-11-01T00:00:%02d.000000Z'%i,size=size) for i in range(n)]


def test_the_quota_keeps_the_newest_eight_and_1500_tokens_as_section_quota():
    from nexloop_eios.context_engine.budget import HUMAN_ANSWER_TOKENS,ConservativeEstimator
    estimate=ConservativeEstimator().estimate
    out=apply_budget(answers(9),dict(STRATEGY))
    # Whichever bound comes first (an item's envelope alone is about 250 estimated tokens, so 1500 tokens keep about six).
    assert 1<=len(out.kept)<=HUMAN_ANSWER_ITEMS and sum(estimate(i) for i in out.kept)<=HUMAN_ANSWER_TOKENS and out.insufficient==[]
    assert len(out.omitted)==9-len(out.kept) and {o['reason'] for o in out.omitted}=={'section_quota'}
    assert min(i.at for i in out.kept)>max(a.at for a in answers(9) if a.ref in {o['ref'] for o in out.omitted})  # the oldest go
    import nexloop_eios.context_engine.budget as B
    old=B.HUMAN_ANSWER_TOKENS;B.HUMAN_ANSWER_TOKENS=10**9
    try:
        count=apply_budget(answers(9),dict(STRATEGY))
        assert len(count.kept)==HUMAN_ANSWER_ITEMS and [o['reason'] for o in count.omitted]==['section_quota']
    finally:B.HUMAN_ANSWER_TOKENS=old
    big=apply_budget(answers(4,size=1500),STRATEGY)  # about 800 tokens each: one fits the 1500
    assert len(big.kept)==1 and {o['reason'] for o in big.omitted}=={'section_quota'} and big.insufficient==[]


def test_over_the_budget_staff_answers_go_before_core_state_and_say_nothing_insufficient():
    tight=dict(STRATEGY,input_token_budget=1500,framing_reserve=0)
    work=Item('open_work','intent','nexloop:intent:'+'1'*8+'-0000-0000-0000-'+'0'*12,'1',{'x':'y'*300},'execution_state','decision:'+'a'*64,relevance=0.5)
    out=apply_budget(answers(3,size=600)+[work],tight)
    assert work in out.kept and out.insufficient==[] and all(o['reason']=='section_quota' for o in out.omitted)


def test_the_switch():
    assert context_v7_enabled({}) is False and context_v7_enabled({'NEXLOOP_CONTEXT_V7':'off'}) is False
    assert context_v7_enabled({'NEXLOOP_CONTEXT_V7':'plan'}) is True
    for bad in ('on','true','message','v7'):
        with pytest.raises(ValueError):context_v7_enabled({'NEXLOOP_CONTEXT_V7':bad})


def _function(path,name):
    text=(M/path).read_text()
    return re.search(r'create (?:or replace )?function '+re.escape(name)+r'\(.*?\nend \$\$;',text,re.S).group(0)


# The parameterised points of each 0398 core and how they revert to the source body (V1, V12).
SECTIONS_REVERTS=(
    ('create function authz.nexloop_context_sections_core(p_protocol text,p_digest text','create or replace function authz.nexloop_context_v6_sections(p_digest text'),
    ("values(p_tenant,p_world,p_context,p_run,p_protocol,","values(p_tenant,p_world,p_context,p_run,'nexloop.context-pack.v6',"),
    ("   tags:=case when p_protocol='nexloop.context-pack.v7' and section='evidence' and item->>'subsection'='human_answers'\n    then authz.nexloop_context_human_answer_in(p_tenant,p_world,p_consumer,item) else authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,section,item,proofs) end;",
     "   tags:=authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,section,item,proofs);"),
    ("  tags:=case when p_protocol='nexloop.context-pack.v7' and omitted->>'section'='evidence' and omitted->'item'->>'subsection'='human_answers'\n   then authz.nexloop_context_human_answer_in(p_tenant,p_world,p_consumer,omitted->'item') else authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,omitted->>'section',omitted->'item',proofs) end;",
     "  tags:=authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,omitted->>'section',omitted->'item',proofs);"),
    (" if p_protocol='nexloop.context-pack.v7' then perform authz.nexloop_context_human_answers_complete(p_tenant,p_world,p_consumer,pack,p);end if;\n",""),
)
ROLE_REVERTS=(
    ('create function authz.nexloop_role_context_command_core(p_protocol text,p_digest text','create or replace function authz.nexloop_role_context_v6_command(p_digest text'),
    ("  or p_protocol not in ('nexloop.context-pack.v6','nexloop.context-pack.v7') or pack->>'schema_version' is distinct from p_protocol or",
     "  or pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' or"),
    ("  perform authz.nexloop_context_sections_core(p_protocol,p_digest,p_world,tenant,principal,a,p,pack,","  perform authz.nexloop_context_v6_sections(p_digest,p_world,tenant,principal,a,p,pack,"),
)


@pytest.mark.parametrize('core,source,reverts',[
    ('authz.nexloop_context_sections_core',('0396_context_degraded_any_run.sql','authz.nexloop_context_v6_sections'),SECTIONS_REVERTS),
    ('authz.nexloop_role_context_command_core',('0239_outreach_run_entry.sql','authz.nexloop_role_context_v6_command'),ROLE_REVERTS)])
def test_each_core_is_its_source_body_but_for_the_parameterised_points(core,source,reverts):
    body=_function('0398_context_pack_v7.sql',core)
    for new,old in reverts:
        assert body.count(new)==1,new[:60]
        body=body.replace(new,old)
    assert body==_function(*source)
    # The v6 entry points are one-line calls of the cores.
    text=(M/'0398_context_pack_v7.sql').read_text()
    assert "perform authz.nexloop_context_sections_core('nexloop.context-pack.v6'," in text
    assert "return authz.nexloop_role_context_command_core('nexloop.context-pack.v6'," in text
    assert "return authz.nexloop_role_context_command_core('nexloop.context-pack.v7'," in text
