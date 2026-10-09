"""AT-069 end to end after a HUMAN review decision (NX-046 closing): the next conversation recalls the merged alias or the
published definition, applies automatically through the governed write, and never stages a duplicate candidate —
both when the model names the canonical definition and when it proposes the same concept as "new" again.
The glue (service) merge path is tests/test_candidate_merge_pg.py::test_reflowed_alias_prevents_duplicate_candidates.
A real browser Human decides; synthetic data only.
"""
import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.candidate_merge import CandidateGluer
from nexloop_eios.claim_matching import ClaimMatcher,MatchConfiguration,ScriptedMatchProvider
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall
from test_candidate_merge_pg import candidates,new_property
from test_claim_matching_pg import Claims,decision,env,matcher_targets,obj,oid,resolution  # noqa: F401
from test_review_decisions_pg import TENANT,human,pending,review,worker  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

pytestmark=pytest.mark.parametrize('env',[TENANT],indirect=True)
READ,EDIT,EXECUTE=Operation.READ,Operation.EDIT,Operation.EXECUTE


def next_conversation(f,label):
    conversation=oid('conversation-'+label)
    f['admin'].execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,'synthetic-human',%s)",
        (f['tenant'],conversation,f['consumer'],'nx046-'+label))
    return conversation,Claims(f['admin'],f['tenant'],conversation,f['consumer'])


def matcher_for(f,conversation,decisions,*,suffix,edit_version=1,extra=()):
    targets=matcher_targets(f)+[('eios:object:Conversation/'+conversation,ResourceType.OBJECT,READ),(f'eios:action:Consumer.edit:{edit_version}',ResourceType.ACTION,EXECUTE)]+list(extra)
    session,_=seed_multi_authority(f['admin'],f['worker'],targets,identity_suffix=suffix,tenant=f['tenant'])
    recall=OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)
    m=ClaimMatcher(f['worker'],session,f['signer'],recall=recall,provider=ScriptedMatchProvider(decisions),
        configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',edit_version)},create_actions={'Product':('Product.create',1)}))
    return m,CandidateGluer(f['worker'],session,f['signer'],indexer=f['indexer'],matcher=m,provider=f['provider'])


def test_after_human_merge_the_next_conversation_hits_the_alias_and_applies(review):
    """Merge path: the reviewer merges “平时的消遣” into favorite_sport; a later conversation recalls favorite_sport through the
    alias and applies; a model proposing the merged text as new again is re-pointed, never queued."""
    f=review;admin=f['admin'];claim,cid,_,_=pending(f,'m69','平时的消遣','钓鱼','平时的消遣是钓鱼','pastime')
    result=human(f).decide(candidate_id=cid,decision='merge_into',expected_revision=candidates(admin)[cid][8],rationale='就是运动偏好',
        idempotency_key='synthetic-merge-069-1',merge_target_ref='eios:property:Consumer/favorite_sport')
    assert worker(f,suffix='-reflow-069m').run_pending()[result['decision_id']]['status']=='done'
    before=len(candidates(admin))
    conversation,claims=next_conversation(f,'m69-next')
    later=claims.add('m69-later','平时的消遣','露营',quote='最近平时的消遣是露营')
    again=claims.add('m69-again','平时的消遣','冲浪',quote='平时的消遣还有冲浪')
    m,gluer=matcher_for(f,conversation,{later:decision('eios:property:Consumer/favorite_sport','露营'),
        again:decision(new_prop=new_property('pastime','平时的消遣'),value='冲浪')},suffix='-match-069m')
    hits=m.recall.recall('平时的消遣 露营',instances=False).definitions
    assert hits[0].ref=='eios:property:Consumer/favorite_sport' and 'alias' in hits[0].fields
    out=m.process_conversation(conversation)
    assert out['matches'][later]['outcome']=='partial_match' and out['applied']=={out['matches'][later]['proposal_id']:'applied'}
    assert out['matches'][again]['outcome']=='no_match' and out['matches'][again]['candidate_id']==cid  # the merged candidate, not a new one
    swept=gluer.process_staged()
    assert [o.rematched for o in swept]==[{again:{'outcome':'partial_match','applied':'applied'}}]
    assert obj(admin,f['consumer'])[0]['favorite_sport']=='冲浪' and resolution(admin,later)=='resolved' and resolution(admin,again)=='resolved'
    assert len(candidates(admin))==before and [r for r in candidates(admin).values() if r[2] in ('staged','pending_review')]==[]


def test_after_human_publish_the_next_conversation_hits_the_new_property_and_applies(review):
    """Publish path: the reviewer approves payment_method (Consumer v2 + Consumer.edit:2); with the grants configured, a later
    conversation recalls the new property and applies; proposing it as new again lands on the published property."""
    f=review;admin=f['admin'];claim,cid,_,_=pending(f,'p69','常用付款方式','花呗','我一般用花呗付款','payment_method')
    result=human(f).decide(candidate_id=cid,decision='approve',expected_revision=candidates(admin)[cid][8],rationale='新增付款方式属性',idempotency_key='synthetic-approve-069-1')
    assert result['outcome']=='published'
    grants=[('eios:action:Consumer.edit:2',ResourceType.ACTION,EXECUTE),('eios:property:Consumer/payment_method',ResourceType.PROPERTY,READ)]
    grants+=[(f"eios:property:Consumer/{f['consumer']}/payment_method",ResourceType.PROPERTY,op) for op in (READ,EDIT)]
    assert worker(f,edit_version=2,extra_targets=grants,suffix='-reflow-069p').run_pending()[result['decision_id']]['status']=='done'
    assert obj(admin,f['consumer'])[0]['payment_method']=='花呗' and resolution(admin,claim)=='resolved'
    before=len(candidates(admin))
    conversation,claims=next_conversation(f,'p69-next')
    later=claims.add('p69-later','常用付款方式','微信支付',quote='现在常用付款方式是微信支付')
    again=claims.add('p69-again','常用付款方式','银行卡',quote='常用付款方式也会用银行卡')
    m,_=matcher_for(f,conversation,{later:decision('eios:property:Consumer/payment_method','微信支付'),
        again:decision(new_prop=new_property('payment_method','常用付款方式','purchase_behavior'),value='银行卡')},suffix='-match-069p',edit_version=2,extra=grants)
    assert m.recall.recall('常用付款方式 微信支付',instances=False).definitions[0].ref=='eios:property:Consumer/payment_method'
    out=m.process_conversation(conversation)
    assert out['matches'][later]['outcome']=='partial_match' and out['matches'][again]['outcome']=='partial_match'
    assert sorted(out['applied'].values())==['applied','applied']
    assert resolution(admin,later)=='resolved' and resolution(admin,again)=='resolved'
    assert obj(admin,f['consumer'])[0]['payment_method'] in ('微信支付','银行卡')
    assert len(candidates(admin))==before  # no duplicate candidate for the published concept
