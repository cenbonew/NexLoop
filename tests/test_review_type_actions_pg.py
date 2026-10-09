"""NX-044 closing (0093): approving a new object type publishes its governed create/edit Actions.

Same transaction as Schema v1; the tenant's existing ontology.object.create / ontology.object.edit
Capability snapshots; canonical shape checked exactly in SQL; no authority fact written.
A real browser Human decides; synthetic data only.
"""
import copy
import json

import psycopg
import pytest
from eios.ontology.definitions import ActionDefinition
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.review_actions import PROTOCOL,build_publication
from test_candidate_merge_pg import candidates,setup
from test_claim_matching_pg import decision,env,product_schema,publish_action,resolution  # noqa: F401
from test_review_decisions_pg import TENANT,decisions,human,review  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

pytestmark=pytest.mark.parametrize('env',[TENANT],indirect=True)
PET={'name':'Pet','display_name':'宠物','description':'顾客饲养的宠物'}


def type_pending(f,label='pet'):
    claim=f['claims'].add(label,'养的宠物','布丁',kind='user_statement',subject='entity',subject_text='布丁',quote='我家养了一只叫布丁的猫')
    m,gluer=setup(f,{claim:decision(new_type=PET)})
    cid=m.process_conversation(f['conversation'])['matches'][claim]['candidate_id'];gluer.process_staged()
    row=candidates(f['admin'])[cid]
    assert row[1]=='object_type' and row[2]=='pending_review'
    return claim,cid


def create_capability(f):
    """The tenant publishes an ontology.object.create Action (the env only has consumer.create and ontology.object.edit)."""
    publish_action(f['admin'],TENANT,'Product.register',product_schema(),'ontology.object.create')


def fingerprint(admin):return admin.execute('select authz.nexloop_tenant_authority_fingerprint(%s)',(TENANT,)).fetchone()[0]


def actions(admin,type_name):
    return {r[0]:r[1:] for r in admin.execute("select resource_id,definition,capability,active from control.nexloop_action_definitions where tenant_id=%s and resource_id like %s",
        (TENANT,f'eios:action:{type_name}.%')).fetchall()}


def test_new_type_publishes_schema_and_canonical_create_edit_actions_without_authority(review):
    f=review;admin=f['admin'];claim,cid=type_pending(f);create_capability(f)
    before=fingerprint(admin)
    result=human(f).decide(candidate_id=cid,decision='approve',expected_revision=candidates(admin)[cid][8],rationale='新增宠物类型',idempotency_key='synthetic-type-approve-01')
    assert result['outcome']=='published',result['publication']
    assert set(result['publication']['published_refs'])=={'eios:object_type:Pet','eios:object_type:Pet:1','eios:action:Pet.create:1','eios:action:Pet.edit:1'}
    assert fingerprint(admin)==before  # still no authority fact (ADR-020 §3)
    schema=ObjectTypeDefinition.model_validate(admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Pet' and version=1",(TENANT,)).fetchone()[0])
    published=actions(admin,'Pet')
    assert set(published)=={'eios:action:Pet.create:1','eios:action:Pet.edit:1'}
    capabilities={r[0]:r[1] for r in admin.execute("select capability->>'capability_name',capability from control.nexloop_action_definitions where tenant_id=%s and resource_id in ('eios:action:Product.register:1','eios:action:Consumer.edit:1')",(TENANT,)).fetchall()}
    for resource,(definition,capability,active) in published.items():
        action=ActionDefinition.model_validate_json(json.dumps(definition))
        suffix=action.stable_name.split('.')[1]
        assert active and capability==capabilities['ontology.object.'+suffix] and action.capability_binding.capability_name=='ontology.object.'+suffix
        assert [(r.stable_name,r.version,r.schema_digest) for r in action.object_types]==[('Pet',1,schema_contract_digest(schema))]
        assert action.governance.risk_level.value=='low' and action.governance.approval_mode.value=='none' and action.governance.policy_refs==()
    # No grant exists for the new Actions: they wait for trusted configuration.
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where tenant_id=%s and array_to_string(entity_key,'|') like '%%Pet.%%'",(TENANT,)).fetchone()==(0,)
    assert resolution(admin,claim)=='awaiting_definition'


def test_new_type_without_tenant_create_capability_stays_pending_with_reason(review):
    f=review;admin=f['admin'];claim,cid=type_pending(f,'pet-nocap')
    revision=candidates(admin)[cid][8]
    result=human(f).decide(candidate_id=cid,decision='approve',expected_revision=revision,rationale='新增宠物类型',idempotency_key='synthetic-type-approve-02')
    assert result['outcome']=='publication_failed'
    assert result['publication']['gate_failures']==['eios_type_action_capability_unavailable:ontology.object.create']
    assert candidates(admin)[cid][2]=='pending_review' and actions(admin,'Pet')=={}
    assert admin.execute("select count(*) from ontology.object_type_versions where tenant_id=%s and type_name='Pet'",(TENANT,)).fetchone()==(0,)


@pytest.mark.parametrize('tamper,reason',[
    ('risk','new_type_action_not_canonical:create'),('scope','new_type_action_not_canonical:edit'),('capability','new_type_action_not_canonical:create'),
    ('extra','new_type_actions_must_be_create_and_edit'),('missing','new_type_actions_must_be_create_and_edit'),('rename','new_type_actions_must_be_create_and_edit')])
def test_non_canonical_new_type_actions_are_refused_by_sql(review,tamper,reason):
    f=review;admin=f['admin'];claim,cid=type_pending(f,'pet-'+tamper);create_capability(f)
    port=human(f);basis=port.basis(cid);revision=basis['revision']
    publication=build_publication(basis);assert 'builder_failures' not in publication
    acts=copy.deepcopy(publication['actions'])
    if tamper=='risk':acts[0]['definition']['governance']['risk_level']='high'
    if tamper=='scope':acts[1]['definition']['required_scopes']=sorted(acts[1]['definition']['required_scopes']+['ontology.schema.review'])
    if tamper=='capability':acts[0]['capability']=dict(acts[0]['capability'],has_side_effects=False)
    if tamper=='extra':acts.append(dict(copy.deepcopy(acts[0]),definition=dict(acts[0]['definition'],stable_name='Pet.delete')))
    if tamper=='missing':acts=acts[:1]
    if tamper=='rename':acts[0]['definition']['stable_name']='Consumer.create'
    payload={'decision_id':'00000000-0000-4000-8000-00000000a093','candidate_id':cid,'decision':'approve','expected_revision':revision,
        'rationale':'篡改的 Action','publication':dict(publication,actions=acts)}
    result=port._call('nexloop_review_decide',payload,PROTOCOL)
    assert result['outcome']=='publication_failed' and reason in result['publication']['gate_failures'],result['publication']
    assert actions(admin,'Pet')=={} and candidates(admin)[cid][2]=='pending_review'
    assert admin.execute("select count(*) from ontology.object_type_versions where tenant_id=%s and type_name='Pet'",(TENANT,)).fetchone()==(0,)


def test_rejected_type_keeps_dependent_claims_as_evidence_only(review):
    """A rejected new type publishes nothing; its Claims stay rejected_definition evidence and are not re-matched."""
    f=review;admin=f['admin'];claim,cid=type_pending(f,'pet-reject');create_capability(f)
    admin.execute("delete from runtime.nexloop_work_feed where feed='claim-match'")  # fixture Claims were matched directly
    result=human(f).decide(candidate_id=cid,decision='reject',expected_revision=candidates(admin)[cid][8],rationale='不是业务概念',idempotency_key='synthetic-type-reject-01')
    assert result['outcome']=='rejected' and result['reflow_status']=='none'
    assert resolution(admin,claim)=='rejected_definition' and actions(admin,'Pet')=={}
    assert admin.execute("select count(*) from ontology.object_type_versions where tenant_id=%s and type_name='Pet'",(TENANT,)).fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.nexloop_mutation_proposals").fetchone()==(0,)
    # Not handed to the matcher: no claim-match feed entry, no rematch generation.
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='claim-match'").fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.nexloop_claim_rematch").fetchone()==(0,)


# ------------------------------------------------- reflow: hand the Claims back to the full matching chain

MATCH_FEED=('eios:action:NexLoop.feed.claim-match:1',None,None)


def feed(admin):
    return admin.execute("select item_key,status from runtime.nexloop_work_feed where feed='claim-match' order by item_key").fetchall()


def rematch(admin,claim):
    return admin.execute('select generation,cause from ontology.nexloop_claim_rematch where claim_id=%s',(claim,)).fetchone()


def match_worker(f,claim,*,suffix,pet_grants):
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios.candidate_merge import CandidateGluer
    from nexloop_eios.claim_match_worker import ClaimMatchWorker
    from nexloop_eios.claim_matching import ClaimMatcher,MatchConfiguration,ScriptedMatchProvider
    from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall
    from test_claim_matching_pg import matcher_targets
    targets=matcher_targets(f)+[('eios:action:NexLoop.feed.claim-match:1',ResourceType.ACTION,Operation.EXECUTE)]
    if pet_grants:
        targets+=[('eios:object_type:Pet',ResourceType.OBJECT_TYPE,Operation.READ)]
        targets+=[(f'eios:action:Pet.{s}:1',ResourceType.ACTION,Operation.EXECUTE) for s in ('create','edit')]
    session,_=seed_multi_authority(f['admin'],f['worker'],targets,identity_suffix=suffix,tenant=f['tenant'])
    recall=OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)
    # The model now places the Claim on the published type and names the instance (no strong identifier exists: Pet has no key).
    provider=ScriptedMatchProvider({claim:decision(type_ref='eios:object_type:Pet',name='布丁')})
    m=ClaimMatcher(f['worker'],session,f['signer'],recall=recall,provider=provider,
        configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',1),'Pet':('Pet.edit',1)},create_actions={'Pet':('Pet.create',1)}))
    gluer=CandidateGluer(f['worker'],session,f['signer'],indexer=f['indexer'],matcher=m,provider=f['provider'])
    return ClaimMatchWorker(f['worker'],session,f['signer'],matcher=m,gluer=gluer)


def reflow_worker(f,*,suffix):
    from test_review_decisions_pg import worker
    return worker(f,suffix=suffix)


def test_approved_type_hands_claims_back_to_the_full_matching_chain(review):
    """AT-067 (new type): approve → Claims back to unresolved + claim-match feed → the worker re-runs all four layers.
    Without type authority nothing is written and nothing fails; once trusted configuration grants it, the Claims are
    released again and matched; the name-only instance goes to review (no layer is skipped)."""
    f=review;admin=f['admin'];claim,cid=type_pending(f,'pet-chain');create_capability(f)
    admin.execute("delete from runtime.nexloop_work_feed where feed='claim-match'")  # fixture Claims were matched directly
    result=human(f).decide(candidate_id=cid,decision='approve',expected_revision=candidates(admin)[cid][8],rationale='新增宠物类型',idempotency_key='synthetic-type-chain-01')
    assert result['outcome']=='published' and result['reflow_status']=='pending' and resolution(admin,claim)=='awaiting_definition'
    assert feed(admin)==[]  # publication itself releases nothing
    released=reflow_worker(f,suffix='-type-reflow').run_pending()[result['decision_id']]
    assert released['status']=='waiting' and released['released']==[claim] and released['reason']=='awaiting_matching'
    assert resolution(admin,claim)=='unresolved' and feed(admin)==[(f['conversation'],'pending')] and rematch(admin,claim)==(1,'type:'+cid)
    # Authority for the new type not configured yet: the matcher cannot even recall it — no write, no error.
    summary=match_worker(f,claim,suffix='-type-match-nogrant',pet_grants=False).run_once()
    assert summary['conversations']==1 and summary['applied']==0 and summary['retry']==0 and summary['dead_lettered']==0
    assert resolution(admin,claim)=='needs_resolution' and feed(admin)==[]
    assert admin.execute("select outcome,reason from ontology.nexloop_claim_matches where claim_id=%s and matcher_version like '%%rematch:1'",(claim,)).fetchone()==('needs_resolution','type_not_recalled')
    again=reflow_worker(f,suffix='-type-reflow-2').run_pending()[result['decision_id']]
    assert again['released']==[claim] and rematch(admin,claim)[0]==2  # the reflow worker's own seeding changed authority
    # (A release with no authority change in between releases nothing.)
    admin.execute("update ontology.nexloop_claims set resolution_state='needs_resolution' where claim_id=%s",(claim,))
    from test_review_decisions_pg import worker as review_worker
    idle_reflow=review_worker(f,suffix='-type-reflow-3')
    with admin.transaction():  # fault injection: pretend the last release already saw the current authority
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("update ontology.nexloop_claim_rematch set authority_fingerprint=authz.nexloop_tenant_authority_fingerprint(tenant_id) where claim_id=%s",(claim,))
    still=idle_reflow.run_pending()[result['decision_id']]
    assert still['released']==[] and still['status']=='waiting' and still['reason']=='awaiting_grants' and rematch(admin,claim)[0]==2
    # Trusted configuration grants the type to the matcher: the next reflow releases the Claim again (generation 3).
    match_worker(f,claim,suffix='-type-match-granted',pet_grants=True)  # seeding = the trusted-configuration grant
    third=review_worker(f,suffix='-type-reflow-4').run_pending()[result['decision_id']]
    assert third['released']==[claim] and rematch(admin,claim)[0]==3 and feed(admin)==[(f['conversation'],'pending')]
    summary=match_worker(f,claim,suffix='-type-match-granted-2',pet_grants=True).run_once()
    assert summary['conversations']==1 and summary['glued']>=1,summary
    # Four layers ran: type recalled, the instance is only named and Pet publishes no identifying property yet → that
    # property ('name') is reviewed first (NX-050: an instance is approved only on a published identifying property). Nothing written.
    match=admin.execute("select outcome,reason,candidate_id from ontology.nexloop_claim_matches where claim_id=%s and matcher_version like '%%rematch:3'",(claim,)).fetchone()
    row=candidates(admin)[match[2]]
    assert match[0]=='no_match' and row[1]=='property' and row[2] in ('staged','pending_review')
    assert admin.execute('select candidate->\'proposed\'->>\'name\',candidate->\'proposed\'->>\'owner_type_ref\' from ontology.nexloop_candidate_definitions where candidate_id=%s',(match[2],)).fetchone()==('name','eios:object_type:Pet')
    assert resolution(admin,claim)=='awaiting_definition'
    assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Pet'",(TENANT,)).fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.nexloop_mutation_proposals").fetchone()==(0,)
    done=review_worker(f,suffix='-type-reflow-5').run_pending()[result['decision_id']]
    assert done['status']=='done' and 'reason' not in done
