"""NX-046 ontology side: reject cooldown (AT-068 data path), NX-044 reflow entry points, workbench detail.

The human review decisions themselves are NX-044. Here a "human" transition is
driven through ontology.nexloop_candidate_transition by the bootstrap identity,
exactly the entry NX-044's governed definer will use, to prove the state,
cooldown and reflow consequences. Synthetic data only.
"""
import json
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.ontology.models import PropertyDefinition,PropertyValueType
from nexloop_eios.candidate_merge import CandidateGluer
from test_candidate_merge_pg import CONFIG,candidates,new_property,reviewer,setup
from test_claim_matching_pg import consumer_schema,decision,env,matcher,obj,resolution,table  # noqa: F401  (env is a fixture)


def rebuild(f,m,suffix):
    """New authority facts or a Schema publication make earlier sessions stale (fail closed): re-authenticate."""
    fresh,_=matcher(f,m.provider.decisions,suffix=suffix)
    return fresh,CandidateGluer(f['worker'],fresh.session,f['signer'],indexer=f['indexer'],matcher=fresh,provider=f['provider'])


def human(f,sql,*args):
    """Stand-in for NX-044's governed human Action definer calling the documented entry points."""
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)",(f['tenant'],))
    try:return f['admin'].execute(sql,args).fetchone()[0]
    finally:f['admin'].execute("select set_config('eios.tenant_id','',false)")


def to_review(f,label,predicate,value,quote,prop):
    claim=f['claims'].add(label,predicate,value,kind='user_statement',quote=quote)
    return claim,decision(new_prop=new_property(prop,predicate,'purchase_behavior'),value=value)


def test_reject_cooldown_keeps_evidence_and_blocks_requeue_until_expiry(env):
    """AT-068 (data): rejected → Claims rejected_definition; same text within cooldown not queued; re-opens after."""
    f=env;first,d1=to_review(f,'rej-1','常用付款方式','花呗','我一般用花呗付款','payment_method')
    m,gluer=setup(f,{first:d1})
    candidate=m.process_conversation(f['conversation'])['matches'][first]['candidate_id'];gluer.process_staged()
    revision=candidates(f['admin'])[candidate][8]
    assert human(f,"select ontology.nexloop_candidate_transition(%s,'real',%s,%s,'pending_review','rejected','human','human:synthetic-reviewer','not a business concept',null)",
        f['tenant'],candidate,revision)==revision+1
    assert resolution(f['admin'],first)=='rejected_definition'
    cooldown=f['admin'].execute('select cooldown_until-rejected_at,config_version from ontology.nexloop_candidate_rejections').fetchone()
    assert cooldown==(timedelta(days=30),CONFIG['config_version'])
    # Same text again inside the cooldown: evidence kept, nothing queued, no new candidate.
    again,d2=to_review(f,'rej-2','常用付款方式','微信支付','我常用付款方式是微信支付','payment_method')
    m.provider.decisions[again]=d2
    result=m.process_conversation(f['conversation'])['matches'][again]
    assert result['outcome']=='no_match' and result['candidate_id']==candidate
    assert resolution(f['admin'],again)=='rejected_definition' and len(candidates(f['admin']))==1
    assert gluer.process_staged()==[] and reviewer(f).pending()==[]
    assert candidates(f['admin'])[candidate][7]==sorted(['claim:'+first,'claim:'+again])
    assert 'payment_method' not in obj(f['admin'],f['consumer'])[0]
    # After the cooldown the same text re-opens as a new candidate and is queued again.
    # Time passes: the whole rejection record moves 31 days into the past (keeps the single-instant invariant).
    f['admin'].execute("update ontology.nexloop_candidate_rejections set rejected_at=rejected_at-interval '31 days',cooldown_until=cooldown_until-interval '31 days'")
    m,gluer=rebuild(f,m,'-matcher-after-review')
    later,d3=to_review(f,'rej-3','常用付款方式','银行卡','常用付款方式换成银行卡了','payment_method')
    m.provider.decisions[later]=d3
    reopened=m.process_conversation(f['conversation'])['matches'][later]['candidate_id']
    assert reopened!=candidate and resolution(f['admin'],later)=='awaiting_definition'
    row=f['admin'].execute('select candidate,status from ontology.nexloop_candidate_definitions where candidate_id=%s',(reopened,)).fetchone()
    assert row[0]['candidate_id']==reopened and row[1]=='staged'
    gluer.process_staged()
    assert [i['candidate_id'] for i in reviewer(f,suffix='-reviewer-2').pending()]==[reopened]


def test_merge_into_reflow_entry_reindexes_and_applies(env):
    """NX-044 merge_into path: merge effects (alias + re-point) → reflow_merged → recall hit and governed apply."""
    f=env;claim,d=to_review(f,'mi','平时的消遣','钓鱼','平时的消遣是钓鱼','pastime')
    m,gluer=setup(f,{claim:d})
    candidate=m.process_conversation(f['conversation'])['matches'][claim]['candidate_id'];gluer.process_staged()
    assert candidates(f['admin'])[candidate][2]=='pending_review'
    revision=candidates(f['admin'])[candidate][8];target='eios:property:Consumer/favorite_sport'
    effects=human(f,"select ontology.nexloop_candidate_merge_effects(%s,'real',%s,%s,'平时的消遣','review')",f['tenant'],candidate,target)
    assert effects['claims']==[claim] and resolution(f['admin'],claim)=='unresolved'
    human(f,"select ontology.nexloop_candidate_transition(%s,'real',%s,%s,'pending_review','merged','human','human:synthetic-reviewer','merge_into',%s)",
        f['tenant'],candidate,revision,Jsonb({'merge_target_ref':target}))
    outcome=gluer.reflow_merged(candidate)
    assert outcome.rematched=={claim:{'outcome':'partial_match','applied':'applied'}}
    assert [h.ref for h in m.recall.recall('平时的消遣',instances=False).definitions][0]==target
    assert obj(f['admin'],f['consumer'])[0]['favorite_sport']=='钓鱼' and resolution(f['admin'],claim)=='resolved'
    # Instances are never merged through an alias.
    named=f['claims'].add('mi-inst','咨询商品','蓝色冲锋衣',kind='user_statement',subject='entity',subject_text='蓝色冲锋衣',quote='那款蓝色冲锋衣怎么样')
    m.provider.decisions[named]=decision(type_ref='eios:object_type:Product',name='蓝色冲锋衣')
    instance=m.process_conversation(f['conversation'])['matches'][named]['candidate_id']
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='identity resolution'):
        human(f,"select ontology.nexloop_candidate_merge_effects(%s,'real',%s,'eios:object:Product/x','蓝色冲锋衣','review')",f['tenant'],instance)


def test_publish_reflow_entry_reindexes_and_rematches(env):
    """NX-044 approve path: publish effects → reflow_published refreshes recall; apply waits for the published Action bundle."""
    f=env;claim,d=to_review(f,'pub','常用付款方式','花呗','我一般用花呗付款','payment_method')
    m,gluer=setup(f,{claim:d})
    candidate=m.process_conversation(f['conversation'])['matches'][claim]['candidate_id'];gluer.process_staged()
    revision=candidates(f['admin'])[candidate][8]
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='published candidate required'):
        human(f,"select ontology.nexloop_candidate_publish_effects(%s,'real',%s)",f['tenant'],candidate)
    # Stand-in for the EIOS Schema registration NX-044 performs before marking published.
    schema=consumer_schema().model_copy(update={'version':2,'properties':consumer_schema().properties+(
        PropertyDefinition(property_name='payment_method',value_type=PropertyValueType.STRING,display_name='常用付款方式'),)})
    f['admin'].execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,2,%s)',(f['tenant'],'Consumer',Jsonb(schema.model_dump(mode='json'))))
    human(f,"select ontology.nexloop_candidate_transition(%s,'real',%s,%s,'pending_review','published','human','human:synthetic-reviewer','approve',null)",f['tenant'],candidate,revision)
    assert human(f,"select ontology.nexloop_candidate_publish_effects(%s,'real',%s)",f['tenant'],candidate)=={'claims':[claim]}
    m,gluer=rebuild(f,m,'-matcher-after-publish')
    outcome=gluer.reflow_published(candidate)
    # Recall now indexes the published property (visible to principals granted its READ).
    assert f['admin'].execute("select count(*) from ontology.nexloop_recall_entries where ref='eios:property:Consumer/payment_method' and source_revision=2").fetchone()[0]>0
    # The edit Action bundle still binds Consumer v1: the NX-020 guards refuse to write.
    assert outcome.rematched=={claim:{'outcome':'needs_resolution','applied':None}}
    assert 'payment_method' not in obj(f['admin'],f['consumer'])[0]


def test_workbench_detail_shows_span_recall_scores_dependents_and_similar(env):
    f=env;c=f['claims']
    one=c.add('wb-1','会员等级','金卡',kind='user_statement',quote='我是金卡会员等级')
    two=c.add('wb-2','会员等级信息','金卡',kind='user_statement',quote='会员等级信息是金卡')
    m,gluer=setup(f,{one:decision(new_prop=new_property('member_tier','会员等级','purchase_behavior'),value='金卡'),
        two:decision(new_prop=new_property('member_tier_info','会员等级信息','purchase_behavior'),value='金卡')})
    matched=m.process_conversation(f['conversation'])['matches'];gluer.process_staged()
    detail=reviewer(f).candidate(matched[one]['candidate_id'])
    assert detail['status']=='pending_review' and detail['dependent_claim_count']==2 and detail['config_version']==CONFIG['config_version']
    assert {e['quote'] for e in detail['evidence']}=={'我是金卡会员等级','会员等级信息是金卡'}
    span=next(e for e in detail['evidence'] if e['claim_id']==one)
    assert (span['span_start'],span['span_end'])==(0,len('我是金卡会员等级'))
    assert detail['candidate']['recall'] and set(detail['merge_scores'])>={'lexical_similarity','core_term_containment','vector_cluster','rule_whitelist','weighted_total'}
    assert [s['candidate_id'] for s in detail['similar']]==[matched[two]['candidate_id']] and detail['similar'][0]['proposed']['display_name']=='会员等级信息'
    assert detail['cooldown'] is None
    # Superseded or unknown candidates are not workbench items.
    assert reviewer(f,suffix='-reviewer-b').candidate(matched[two]['candidate_id']) is None
    with pytest.raises(ValueError):reviewer(f,suffix='-reviewer-c').candidate('not-a-uuid')


def test_cooldown_fields_come_from_one_instant(env):
    """Deterministic regression for the 0074 two-clock_timestamp() skew; independent of host speed."""
    f=env;admin=f['admin']
    # The trigger takes the time exactly once.
    body=admin.execute("select pg_get_functiondef('ontology.nexloop_candidate_rejected()'::regprocedure)").fetchone()[0]
    assert body.count('clock_timestamp()')==1 and 'v_now+make_interval' in body
    claim,d=to_review(f,'inst-1','常用付款方式','花呗','我一般用花呗付款','payment_method')
    m,gluer=setup(f,{claim:d})
    candidate=m.process_conversation(f['conversation'])['matches'][claim]['candidate_id'];gluer.process_staged()
    admin.execute('update ontology.nexloop_merge_configurations set reject_cooldown_seconds=7 where tenant_id=%s and active',(f['tenant'],))
    revision=candidates(admin)[candidate][8]
    human(f,"select ontology.nexloop_candidate_transition(%s,'real',%s,%s,'pending_review','rejected','human','human:synthetic-reviewer','x',null)",f['tenant'],candidate,revision)
    row=admin.execute('select rejected_at,cooldown_until,cooldown_seconds,cooldown_until=rejected_at+make_interval(secs=>cooldown_seconds) from ontology.nexloop_candidate_rejections').fetchone()
    assert row[2]==7 and row[3] is True and row[1]-row[0]==timedelta(seconds=7)
    # The relation is a table invariant: a skew of one microsecond cannot be stored.
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("update ontology.nexloop_candidate_rejections set cooldown_until=cooldown_until+interval '1 microsecond'")
