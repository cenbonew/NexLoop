"""NX-020 explicit opt-in: real model makes the four-layer decisions on synthetic Claims.

Not collected by default CI. Requires NEXLOOP_NX020_REAL_ENV_FILE naming a
private (0600) env file with MODEL_*; the key stays in process memory. Guard
invariants are asserted (nothing outside the deterministic rules is written);
model agreement with the expected outcome is only measured and reported to
NEXLOOP_NX020_REAL_REPORT or the pytest tmp_path. Synthetic inputs only.
"""
import json
import os
from pathlib import Path

from nexloop_eios.claim_matching import MATCHER_VERSION,PROMPT_VERSION,ClaimMatcher,MatchConfiguration
from nexloop_eios.conversation_extraction import OpenAICompatibleExtractionProvider
from nexloop_eios.model_profile import load_model_profile
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall
from multi_authority_fixture import seed_multi_authority
from test_claim_matching_pg import env,matcher_targets,obj,proposals,table  # noqa: F401  (env is a fixture)

CASES=[
    ('budget-level','预算区间','两千元以内',{'quote':'预算在两千元以内'},'full_match'),
    ('budget-vocab','预算区间','一万元以上',{'quote':'预算一万元以上'},'no_match'),
    ('budget-amount','预算金额',2000,{'quote':'预算2000'},'partial_match'),
    ('sku-new','咨询商品','SKU-2002',{'quote':'我想问下SKU-2002这款','kind':'user_statement','subject':'entity','subject_text':'SKU-2002'},'partial_match'),
    ('name-only','咨询商品','蓝色冲锋衣',{'quote':'那款蓝色冲锋衣怎么样','kind':'user_statement','subject':'entity','subject_text':'蓝色冲锋衣'},'no_match'),
    ('new-prop','常用付款方式','花呗',{'quote':'我一般用花呗付款','kind':'user_statement'},'no_match'),
]


def test_real_model_four_layer_decisions_under_guards(env,tmp_path):
    supplied=os.environ.get('NEXLOOP_NX020_REAL_ENV_FILE')
    assert supplied,'explicit private env file required; no implicit .env read'
    profile=load_model_profile(environment={},env_file=supplied)
    assert profile.provider=='deepseek','real model profile required'
    f=env;ids={label:f['claims'].add(label,predicate,value,**options) for label,predicate,value,options,_ in CASES}
    session,_=seed_multi_authority(f['admin'],f['worker'],matcher_targets(f),identity_suffix='-real-matcher',tenant=f['tenant'])
    recall=OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)
    provider=OpenAICompatibleExtractionProvider(profile,timeout=90)
    matcher=ClaimMatcher(f['worker'],session,f['signer'],recall=recall,provider=provider,
        configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',1)},create_actions={'Product':('Product.create',1)}))
    result=matcher.process_conversation(f['conversation'])
    key=profile.credential_for_provider()
    report={'evidence_class':'real','provider':profile.provider,'model_id':profile.model_id,'matcher_version':MATCHER_VERSION,
        'prompt_version':PROMPT_VERSION,'inputs':'synthetic only','embedding':'deterministic test provider (recall)','cases':[]}
    rows=proposals(f['admin'])
    for label,predicate,value,options,expected in CASES:
        claim=ids[label];match=result['matches'][claim]
        stored=table(f['admin'],'nexloop_claim_matches',claim_id=claim)[0]
        report['cases'].append({'id':label,'expected':expected,'outcome':match['outcome'],'agrees':match['outcome']==expected,
            'reason':stored[7],'decision':stored[5],'proposal_status':rows[claim][1] if claim in rows else None})
    # Guard invariants regardless of what the model chose.
    consumer=obj(f['admin'],f['consumer'])[0]
    assert set(consumer)<={'display_name','budget_level','budget_amount','waterproof_concern','favorite_sport'}
    assert consumer.get('budget_level') in (None,'两千元以内','两千到五千元','五千元以上')
    assert consumer.get('budget_amount') in (None,2000)
    products={r[0]['sku'] for r in f['admin'].execute("select properties from ontology.objects where type_name='Product'").fetchall()}
    assert products<={'SKU-1001','SKU-2002'}
    for claim in rows:assert rows[claim][1] in ('applied','superseded','rejected')
    report['agreement']=sum(c['agrees'] for c in report['cases']);report['total']=len(CASES)
    text=json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True)
    assert key not in text
    Path(os.environ.get('NEXLOOP_NX020_REAL_REPORT') or tmp_path/'nx020-real-report.json').write_text(text+'\n')
    print(json.dumps({'agreement':report['agreement'],'total':report['total']}))
