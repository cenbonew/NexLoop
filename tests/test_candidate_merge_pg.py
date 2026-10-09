"""NX-045 candidate glue, similar dedupe, re-pointing and the review queue on clean catalog PostgreSQL.

Reuses the NX-020 fixture (uuid tenant, governed Consumer.edit/Product.create,
recall index). The merge configuration is published by the trusted
configuration identity. Synthetic data only.
"""
import json
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator,FormatChecker
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.candidate_merge import (FEATURES,CandidateGluer,FeatureScorer,ReviewQueueReader,calibrate,core_term_containment,
    lexical_similarity,weighted)
from nexloop_eios.embedding_provider import DeterministicTestEmbeddingProvider
from multi_authority_fixture import seed_multi_authority
from test_claim_matching_pg import CANDIDATE,decision,env,matcher,obj,resolution,table  # noqa: F401  (env is a fixture)

ROOT=Path(__file__).resolve().parents[1]
DATASET=json.loads((ROOT/'tests/data/nx045_merge_calibration.json').read_text())
CONFIG=json.loads((ROOT/'tests/data/nx045_merge_config.json').read_text())
SCORES=Draft202012Validator(json.loads((ROOT/'packages/contracts/candidate-definition.schema.json').read_text())['properties']['merge_scores'],format_checker=FormatChecker())


def publish_config(f,config=CONFIG,version=None):
    f['admin'].execute('alter role nexloop_configurator login')
    with psycopg.connect(make_conninfo(f['pg'],user='nexloop_configurator'),autocommit=True) as c:
        return c.execute('select control.nexloop_publish_merge_configuration(%s,%s,%s,%s::numeric,%s::numeric,%s,%s)',(f['tenant'],version or config['config_version'],
            Jsonb(config['weights']),config['merge_threshold'],config['dedupe_threshold'],Jsonb(config['whitelist']),Jsonb(config['calibration']))).fetchone()[0]


def new_property(name,display,group='preference'):
    return {'name':name,'display_name':display,'description':display,'value_type':'string','closed_vocabulary':False,'property_group':group}


def candidates(admin):
    return {r[0]:r for r in admin.execute('select candidate_id,kind,status,merge_target_ref,merge_scores,config_version,superseded_by,dependent_claims,revision from ontology.nexloop_candidate_definitions').fetchall()}


def setup(f,decisions):
    publish_config(f)
    m,provider=matcher(f,decisions)
    gluer=CandidateGluer(f['worker'],m.session,f['signer'],indexer=f['indexer'],matcher=m,provider=f['provider'])
    return m,gluer


def test_initial_configuration_is_reproduced_by_calibration():
    """The committed initial weights/threshold are exactly the calibration output on the synthetic set."""
    result=calibrate(DATASET['pairs'],DeterministicTestEmbeddingProvider(64))
    assert result['weights']==CONFIG['weights'] and result['merge_threshold']==CONFIG['merge_threshold']==CONFIG['dedupe_threshold']
    assert result['precision']==1.0 and CONFIG['calibration']['false_positive']==0 and CONFIG['calibration']['pairs']==len(DATASET['pairs'])
    assert CONFIG['weights']['rule_whitelist']>=CONFIG['merge_threshold']
    scorer=FeatureScorer(DeterministicTestEmbeddingProvider(64),CONFIG['whitelist'])
    hard_negatives={('联系时间偏好','联系方式偏好'),('肤质类型','发质类型'),('一万元以上','五千元以上'),('线上','线下'),('订单','退货单')}
    for pair in DATASET['pairs']:
        total=weighted(scorer.features([pair['candidate']],[pair['target']]),CONFIG['weights'])
        if not pair['same']:assert total<CONFIG['merge_threshold'],pair
    assert {(p['candidate'],p['target']) for p in DATASET['pairs'] if not p['same']}>=hard_negatives
    assert lexical_similarity('喜欢的运动项目','喜欢的运动')==0.8 and core_term_containment('订单','退货单')==0.0


def test_glue_merges_above_threshold_and_reviews_below(env):
    """AT-066: merged → alias + Claim re-pointed + automatic apply; below → pending_review; scores carry the config version."""
    f=env;c=f['claims']
    near=c.add('near','喜欢的运动项目','登山',quote='喜欢的运动项目是登山')
    far=c.add('far','常用付款方式','花呗',kind='user_statement',quote='我一般用花呗付款')
    m,gluer=setup(f,{near:decision(new_prop=new_property('sport_item','喜欢的运动项目'),value='登山'),
        far:decision(new_prop=new_property('payment_method','常用付款方式','purchase_behavior'),value='花呗')})
    matched=m.process_conversation(f['conversation'])['matches']
    assert matched[near]['outcome']==matched[far]['outcome']=='no_match' and resolution(f['admin'],near)=='awaiting_definition'
    outcomes={o.candidate_id:o for o in gluer.process_staged()}
    rows=candidates(f['admin'])
    merged=rows[matched[near]['candidate_id']];review=rows[matched[far]['candidate_id']]
    assert merged[2]=='merged' and merged[3]=='eios:property:Consumer/favorite_sport' and merged[5]==CONFIG['config_version']
    assert review[2]=='pending_review' and review[3] is None
    for scores in (merged[4],review[4]):
        assert not list(SCORES.iter_errors(scores)) and set(FEATURES)<=set(scores) and scores['config_version']==CONFIG['config_version']
    assert merged[4]['weighted_total']>=CONFIG['merge_threshold']>review[4]['weighted_total']
    alias=f['admin'].execute('select alias_text,canonical_ref,config_version from ontology.nexloop_definition_aliases').fetchall()
    assert alias==[('喜欢的运动项目','eios:property:Consumer/favorite_sport',CONFIG['config_version'])]
    # Re-pointed Claim went through the NX-020 guards and the governed edit Action.
    assert outcomes[merged[0]].rematched=={near:{'outcome':'partial_match','applied':'applied'}}
    assert obj(f['admin'],f['consumer'])[0]['favorite_sport']=='登山' and resolution(f['admin'],near)=='resolved'
    assert resolution(f['admin'],far)=='awaiting_definition' and 'payment_method' not in obj(f['admin'],f['consumer'])[0]
    events=f['admin'].execute('select candidate_id,from_status,to_status,actor_kind from ontology.nexloop_candidate_events order by event_id').fetchall()
    assert {(e[1],e[2],e[3]) for e in events}=={('staged','merged','service'),('staged','pending_review','service')}
    # Idempotent: a second glue pass changes nothing.
    assert gluer.process_staged()==[] and candidates(f['admin'])==rows


def test_reflowed_alias_prevents_duplicate_candidates(env):
    """AT-069 (no duplicate candidate): after the merge, the same concept is recalled and applied, never queued again."""
    f=env;c=f['claims']
    first=c.add('alias-1','喜欢的运动项目','登山',quote='喜欢的运动项目是登山')
    m,gluer=setup(f,{first:decision(new_prop=new_property('sport_item','喜欢的运动项目'),value='登山')})
    m.process_conversation(f['conversation']);gluer.process_staged()
    before=len(candidates(f['admin']))
    # Next conversation: recall now returns the canonical property through the alias.
    later=c.add('alias-2','喜欢的运动项目','滑雪',quote='喜欢的运动项目现在是滑雪')
    hits=[h.ref for h in m.recall.recall('喜欢的运动项目 滑雪',instances=False).definitions]
    assert hits[0]=='eios:property:Consumer/favorite_sport'
    m.provider.decisions[later]=decision('eios:property:Consumer/favorite_sport','滑雪')
    result=m.process_conversation(f['conversation'])
    assert result['matches'][later]['outcome']=='partial_match' and len(candidates(f['admin']))==before
    assert obj(f['admin'],f['consumer'])[0]['favorite_sport']=='滑雪'
    # Even if a model proposes the merged text as "new" again, it lands on the merged
    # candidate (exact dedupe) and is re-pointed, not queued.
    again=c.add('alias-3','喜欢的运动项目','攀岩',quote='喜欢的运动项目也有攀岩')
    m.provider.decisions[again]=decision(new_prop=new_property('sport_item','喜欢的运动项目'),value='攀岩')
    assert m.process_conversation(f['conversation'])['matches'][again]['outcome']=='no_match'
    assert len(candidates(f['admin']))==before and resolution(f['admin'],again)=='awaiting_definition'
    swept=gluer.process_staged()
    assert [o.rematched for o in swept]==[{again:{'outcome':'partial_match','applied':'applied'}}]
    assert obj(f['admin'],f['consumer'])[0]['favorite_sport']=='攀岩' and resolution(f['admin'],again)=='resolved'
    assert table(f['admin'],'nexloop_candidate_definitions',status='pending_review')==[]


def test_similar_open_candidates_are_deduplicated_and_claims_accumulate(env):
    f=env;c=f['claims']
    one=c.add('dup-1','会员等级','金卡',kind='user_statement',quote='我是金卡会员等级')
    two=c.add('dup-2','会员等级信息','金卡',kind='user_statement',quote='会员等级信息是金卡')
    m,gluer=setup(f,{one:decision(new_prop=new_property('member_tier','会员等级','purchase_behavior'),value='金卡'),
        two:decision(new_prop=new_property('member_tier_info','会员等级信息','purchase_behavior'),value='金卡')})
    matched=m.process_conversation(f['conversation'])['matches']
    assert matched[one]['candidate_id']!=matched[two]['candidate_id']
    outcomes={o.candidate_id:o for o in gluer.process_staged()}
    rows=candidates(f['admin'])
    first,second=rows[matched[one]['candidate_id']],rows[matched[two]['candidate_id']]
    assert first[2]=='pending_review' and second[2]=='superseded' and second[6]==first[0]
    assert outcomes[second[0]].merge_scores['weighted_total']>=CONFIG['dedupe_threshold']
    # The queue item is the surviving candidate; its accumulated dependent Claims are authoritative.
    assert first[7]==sorted(['claim:'+one,'claim:'+two])
    assert resolution(f['admin'],one)==resolution(f['admin'],two)=='awaiting_definition'


def test_object_instances_never_merge_by_similarity(env):
    f=env;c=f['claims']
    named=c.add('inst','咨询商品','防水登山鞋',kind='user_statement',subject='entity',subject_text='防水登山鞋',quote='那双防水登山鞋怎么样')
    m,gluer=setup(f,{named:decision(type_ref='eios:object_type:Product',name='防水登山鞋')})
    candidate_id=m.process_conversation(f['conversation'])['matches'][named]['candidate_id']
    outcome=gluer.process(candidate_id)
    assert outcome.status=='pending_review'
    assert f['admin'].execute('select status_reason from ontology.nexloop_candidate_definitions where candidate_id=%s',(candidate_id,)).fetchone()[0]=='instance_identity_requires_review'


def reviewer(f,*,world='real',suffix='-reviewer',grant=True):
    targets=[('eios:action:ontology.schema.review:1',ResourceType.ACTION,Operation.EXECUTE)] if grant else [('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,Operation.READ)]
    session,_=seed_multi_authority(f['admin'],f['worker'],targets,identity_suffix=suffix,world=world,tenant=f['tenant'])
    return ReviewQueueReader(f['worker'],session,f['signer'])


def test_review_queue_scoped_to_tenant_world_and_review_permission(env):
    """NX-046 read API: pending_review only, own tenant/world, current review EXECUTE; simulation candidates never in real."""
    f=env;c=f['claims']
    far=c.add('queue','常用付款方式','花呗',kind='user_statement',quote='我一般用花呗付款')
    m,gluer=setup(f,{far:decision(new_prop=new_property('payment_method','常用付款方式','purchase_behavior'),value='花呗')})
    m.process_conversation(f['conversation']);gluer.process_staged()
    # A simulation-world candidate (fixture row) must not reach the real queue.
    real=f['admin'].execute("select candidate,dependent_claims from ontology.nexloop_candidate_definitions").fetchone()
    sim=dict(real[0],candidate_id='00000000-0000-5000-8000-000000000045',world_id='simulation',mode='simulation')
    f['admin'].execute("""insert into ontology.nexloop_candidate_definitions(tenant_id,world,candidate_id,kind,dedupe_key,status,candidate,dependent_claims,merge_scores,config_version)
        values(%s,'simulation',%s,'property',%s,'pending_review',%s,%s,'{}','x')""",(f['tenant'],sim['candidate_id'],'5'*64,Jsonb(sim),real[1]))
    queue=reviewer(f).pending()
    assert [item['candidate']['candidate_id'] for item in queue]==[real[0]['candidate_id']]
    item=queue[0]
    assert item['status']=='pending_review' and item['dependent_claim_count']==1 and item['merge_scores']['config_version']==CONFIG['config_version']
    assert item['evidence'][0]['quote']=='我一般用花呗付款' and item['evidence'][0]['resolution_state']=='awaiting_definition'
    with pytest.raises(Exception):reviewer(f,suffix='-no-review',grant=False).pending()
    sim_queue=reviewer(f,world='simulation',suffix='-sim-reviewer').pending()
    assert [i['candidate_id'] for i in sim_queue]==[sim['candidate_id']]


def test_human_only_transitions_and_no_application_entry(env):
    """pending_review decisions are NX-044's human Actions; services cannot reach them."""
    f=env;c=f['claims']
    far=c.add('human','常用付款方式','花呗',kind='user_statement',quote='我一般用花呗付款')
    m,gluer=setup(f,{far:decision(new_prop=new_property('payment_method','常用付款方式','purchase_behavior'),value='花呗')})
    candidate_id=m.process_conversation(f['conversation'])['matches'][far]['candidate_id'];gluer.process_staged()
    revision=candidates(f['admin'])[candidate_id][8]
    with f['worker'].connection() as conn,pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("select ontology.nexloop_candidate_transition(%s,'real',%s,%s,'pending_review','published','human','x','',null)",(f['tenant'],candidate_id,revision))
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)",(f['tenant'],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='not allowed'):
        f['admin'].execute("select ontology.nexloop_candidate_transition(%s,'real',%s,%s,'pending_review','published','service','svc','',null)",(f['tenant'],candidate_id,revision))
    with pytest.raises(psycopg.errors.SerializationFailure):
        f['admin'].execute("select ontology.nexloop_candidate_transition(%s,'real',%s,%s,'pending_review','rejected','human','reviewer','',null)",(f['tenant'],candidate_id,revision+5))
    # The NX-044 entry with a human actor and the current revision is accepted.
    assert f['admin'].execute("select ontology.nexloop_candidate_transition(%s,'real',%s,%s,'pending_review','rejected','human','human:synthetic-reviewer','synthetic',null)",
        (f['tenant'],candidate_id,revision)).fetchone()[0]==revision+1
    f['admin'].execute("select set_config('eios.tenant_id','',false)")


def test_glue_requires_active_configuration_and_match_grant(env):
    f=env;c=f['claims']
    far=c.add('noconf','常用付款方式','花呗',kind='user_statement',quote='我一般用花呗付款')
    m,_=matcher(f,{far:decision(new_prop=new_property('payment_method','常用付款方式','purchase_behavior'),value='花呗')})
    m.process_conversation(f['conversation'])
    gluer=CandidateGluer(f['worker'],m.session,f['signer'],indexer=f['indexer'],matcher=m,provider=f['provider'])
    with pytest.raises(LookupError):gluer.process_staged()
    with f['worker'].connection() as conn,pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("select control.nexloop_publish_merge_configuration(%s,'x','{}',0.5,0.5,'[]','{}')",(f['tenant'],))
    assert table(f['admin'],'nexloop_candidate_definitions')[0][5]=='staged'
