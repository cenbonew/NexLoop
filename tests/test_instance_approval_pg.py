"""NX-050: object_instance candidate approval and governed reflow creation, closing AT-067 to "applied".

New type Pet (NX-044) → its identifying property is reviewed first → the name-only instance is reviewed →
the service reflow creates it through Pet.create (once trusted configuration grants it) → the dependent
Claims are released to the four-layer matcher, locate the approved instance and apply. Rejection and
duplicate identities are refused without side effects. A real browser Human decides; synthetic data only.
"""
import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.candidate_merge import CandidateGluer
from nexloop_eios.claim_match_worker import ClaimMatchWorker
from nexloop_eios.claim_matching import ClaimMatcher,MatchConfiguration,ScriptedMatchProvider
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall
from test_candidate_merge_pg import candidates,setup
from test_claim_matching_pg import decision,env,matcher_targets,obj,resolution  # noqa: F401
from test_review_decisions_pg import TENANT,human,review,worker  # noqa: F401
from test_review_type_actions_pg import PET,create_capability
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

pytestmark=pytest.mark.parametrize('env',[TENANT],indirect=True)
READ,EDIT,EXECUTE=Operation.READ,Operation.EDIT,Operation.EXECUTE
NAME={'name':'name','display_name':'名字','value_type':'string','property_group':'other'}
BREED={'name':'breed','display_name':'品种','value_type':'string','property_group':'other'}


def approve(f,cid,key,rationale='通过'):
    admin=f['admin']
    return human(f).decide(candidate_id=cid,decision='approve',expected_revision=candidates(admin)[cid][8],rationale=rationale,idempotency_key='synthetic-nx050-'+key)


def pending_candidates(admin,kind):
    return sorted((cid,row[7]) for cid,row in candidates(admin).items() if row[1]==kind and row[2]=='pending_review')


def pet_objects(admin):
    return admin.execute("select object_id,properties from ontology.objects where tenant_id=%s and type_name='Pet' order by object_id",(TENANT,)).fetchall()


def matcher(f,claims,*,suffix,version,objects=()):
    """The claim_matcher service with Pet authority as trusted configuration would grant it (fresh principal per stage)."""
    targets=matcher_targets(f)+[('eios:action:NexLoop.feed.claim-match:1',ResourceType.ACTION,EXECUTE),('eios:object_type:Pet',ResourceType.OBJECT_TYPE,READ)]
    targets+=[(f'eios:action:Pet.{s}:{version}',ResourceType.ACTION,EXECUTE) for s in ('create','edit')]
    targets+=[(f'eios:property:Pet/{p}',ResourceType.PROPERTY,READ) for p in ('name','breed')]
    for oid in objects:
        targets+=[(f'eios:object:Pet/{oid}',ResourceType.OBJECT,op) for op in (READ,EDIT)]
        targets+=[(f'eios:property:Pet/{oid}/{p}',ResourceType.PROPERTY,op) for p in ('name','breed') for op in (READ,EDIT)]
    session,_=seed_multi_authority(f['admin'],f['worker'],targets,identity_suffix=suffix,tenant=f['tenant'])
    recall=OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)
    provider=ScriptedMatchProvider({claims['name']:decision(type_ref='eios:object_type:Pet',name='布丁',new_prop=NAME,value='布丁'),
        claims['breed']:decision(type_ref='eios:object_type:Pet',name='布丁',new_prop=BREED,value='英短')})
    m=ClaimMatcher(f['worker'],session,f['signer'],recall=recall,provider=provider,
        configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',1),'Pet':('Pet.edit',version)},create_actions={'Pet':('Pet.create',version)}))
    gluer=CandidateGluer(f['worker'],session,f['signer'],indexer=f['indexer'],matcher=m,provider=f['provider'])
    return ClaimMatchWorker(f['worker'],session,f['signer'],matcher=m,gluer=gluer)


def to_instance_review(f):
    """Pet approved (NX-044) → identifying property 'name' reviewed and published → name-only instance pending review."""
    admin=f['admin'];c=f['claims']
    claims={'name':c.add('pet-name','宠物名字','布丁',kind='user_statement',subject='entity',subject_text='布丁',quote='我家的宠物猫叫布丁'),
            'breed':c.add('pet-breed','宠物品种','英短',kind='user_statement',subject='entity',subject_text='布丁',quote='宠物布丁是英短')}
    m,gluer=setup(f,{cid:decision(new_type=PET) for cid in claims.values()})
    m.process_conversation(f['conversation']);gluer.process_staged()
    (type_cid,_),=pending_candidates(admin,'object_type');create_capability(f)
    admin.execute("delete from runtime.nexloop_work_feed where feed='claim-match'")  # fixture Claims were matched directly
    assert approve(f,type_cid,'type-01')['outcome']=='published'
    assert worker(f,suffix='-nx050-reflow-type').run_pending()  # releases both Claims (generation 1)
    summary=matcher(f,claims,suffix='-nx050-match-a',version=1).run_once()
    assert summary['conversations']==1 and summary['glued']>=1,summary
    (name_cid,deps),=pending_candidates(admin,'property')
    assert sorted(deps)==sorted('claim:'+x for x in claims.values())
    published=approve(f,name_cid,'name-01','名字是宠物的标识属性')
    assert published['outcome']=='published' and 'eios:action:Pet.create:2' in published['publication']['published_refs']
    report=worker(f,suffix='-nx050-reflow-name').run_pending()[published['decision_id']]
    assert report['status']=='done',report  # entity Claims released, not re-pointed
    assert {resolution(admin,x) for x in claims.values()}=={'unresolved'}
    summary=matcher(f,claims,suffix='-nx050-match-b',version=2).run_once()
    assert summary['conversations']==1,summary
    (instance_cid,deps),=pending_candidates(admin,'object_instance')
    assert sorted(deps)==sorted('claim:'+x for x in claims.values())
    assert admin.execute("select candidate->'proposed'->'identifying_properties' from ontology.nexloop_candidate_definitions where candidate_id=%s",(instance_cid,)).fetchone()==({'name':'布丁'},)
    return claims,instance_cid


def test_approved_instance_is_created_by_reflow_and_dependent_claims_apply(review):
    """AT-067 closed loop: approve instance → reflow waits for create authority (no write, no error) → granted →
    governed create → Claims released → identity Claim resolved, breed reviewed → published → applied."""
    f=review;admin=f['admin'];claims,instance_cid=to_instance_review(f)
    fingerprint=lambda:admin.execute('select authz.nexloop_tenant_authority_fingerprint(%s)',(TENANT,)).fetchone()[0]
    before=fingerprint()
    decided=approve(f,instance_cid,'instance-01','确认这只猫')
    assert decided['outcome']=='published' and decided['reflow_status']=='pending' and fingerprint()==before
    assert decided['publication']['published_refs']==['candidate:'+instance_cid,'eios:action:Pet.create:2'] and pet_objects(admin)==[]
    assert set(decided['record']['publication'])=={'schema_revision_before','schema_revision_after','published_refs','applied_claim_count','gate_failures'}
    # The reflow service has no Pet.create grant yet: waits, writes nothing, raises nothing.
    waiting=worker(f,suffix='-nx050-reflow-nogrant').run_pending()[decided['decision_id']]
    assert waiting['status']=='waiting' and waiting['reason']=='awaiting_grants' and pet_objects(admin)==[]
    # Trusted configuration grants it: the reflow creates the instance exactly once, binds it and releases the Claims.
    granted=worker(f,suffix='-nx050-reflow-granted',extra_targets=[('eios:action:Pet.create:2',ResourceType.ACTION,EXECUTE)])
    created=granted.run_pending()[decided['decision_id']]
    (oid,props),=pet_objects(admin)
    assert props=={'name':'布丁'} and created['object_id']==oid and sorted(created['released'])==sorted(claims.values())
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='Pet.create'").fetchone()==(1,)
    again=granted.run_pending()[decided['decision_id']]  # idempotent: same object, nothing new released, waits for the matcher
    assert again['object_id']==oid and again['released']==[] and again['status']=='waiting' and again['reason']=='awaiting_matching'
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='Pet.create'").fetchone()==(1,) and len(pet_objects(admin))==1
    # The matcher locates the approved instance (no new candidate): the identity Claim is already true → resolved;
    # the breed is a new property → reviewed.
    summary=matcher(f,claims,suffix='-nx050-match-c',version=2,objects=[oid]).run_once()
    assert summary['conversations']==1,summary
    assert resolution(admin,claims['name'])=='resolved' and resolution(admin,claims['breed'])=='awaiting_definition'
    assert pending_candidates(admin,'object_instance')==[]
    after=worker(f,suffix='-nx050-reflow-after-match').run_pending();assert after[decided['decision_id']]['status']=='done',after
    (breed_cid,_),=pending_candidates(admin,'property')
    published=approve(f,breed_cid,'breed-01','品种属性')
    assert published['outcome']=='published' and admin.execute("select schema_version from ontology.objects where object_id=%s",(oid,)).fetchone()==(3,)
    worker(f,suffix='-nx050-reflow-breed').run_pending()
    summary=matcher(f,claims,suffix='-nx050-match-d',version=3,objects=[oid]).run_once()
    assert summary['applied']==1,summary
    assert obj(admin,oid,'Pet')[0]=={'name':'布丁','breed':'英短'} and resolution(admin,claims['breed'])=='resolved'
    assert pet_objects(admin)==[(oid,{'name':'布丁','breed':'英短'})]  # still one instance


def test_rejected_instance_creates_nothing_and_keeps_claims_as_evidence(review):
    f=review;admin=f['admin'];claims,instance_cid=to_instance_review(f)
    admin.execute("delete from runtime.nexloop_work_feed where feed='claim-match'")
    result=human(f).decide(candidate_id=instance_cid,decision='reject',expected_revision=candidates(admin)[instance_cid][8],rationale='不是同一只',
        idempotency_key='synthetic-nx050-reject-01')
    assert result['outcome']=='rejected' and result['reflow_status']=='none'
    assert {resolution(admin,x) for x in claims.values()}=={'rejected_definition'} and pet_objects(admin)==[]
    assert admin.execute('select count(*) from ontology.nexloop_instance_approvals').fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='claim-match'").fetchone()==(0,)


def test_duplicate_identity_is_refused_and_the_candidate_stays_pending(review):
    f=review;admin=f['admin'];claims,instance_cid=to_instance_review(f)
    # The same identity already exists (created by another governed path).
    session,_=seed_multi_authority(admin,f['worker'],[('eios:action:Pet.create:2',ResourceType.ACTION,EXECUTE)],identity_suffix='-nx050-other-creator',tenant=f['tenant'])
    existing=GovernedObjectCreator(f['worker'],session,f['signer']).create(action_name='Pet.create',action_version=2,intent_id='nx050-duplicate-existing-01',
        type_name='Pet',properties={'name':'布丁'})['object_id']
    result=approve(f,instance_cid,'dup-01')
    assert result['outcome']=='publication_failed' and result['publication']['gate_failures']==['instance_already_exists']
    assert candidates(admin)[instance_cid][2]=='pending_review' and pet_objects(admin)==[(existing,{'name':'布丁'})]
    assert admin.execute('select count(*) from ontology.nexloop_instance_approvals').fetchone()==(0,)
    # A bound object must carry exactly the approved identity: the reflow cannot bind an arbitrary object.
    assert {resolution(admin,x) for x in claims.values()}=={'awaiting_definition'}


def test_instance_approval_requires_published_identifying_property_and_create_action(review):
    """Gates: an identity on an unpublished property, or a type without an active create Action, is refused."""
    f=review;admin=f['admin'];c=f['claims']
    claim=c.add('pet-raw','名字','布丁',kind='user_statement',subject='entity',subject_text='布丁',quote='我家的宠物猫叫布丁')
    m,gluer=setup(f,{claim:decision(new_type=PET)});m.process_conversation(f['conversation']);gluer.process_staged()
    (type_cid,_),=pending_candidates(admin,'object_type');create_capability(f)
    assert approve(f,type_cid,'gate-type')['outcome']=='published'
    # Fault injection: an instance candidate naming an identifying property Pet does not publish.
    candidate={'schema_version':'1.0','candidate_id':'00000000-0000-4000-8000-0000000c0050','tenant_id':TENANT,'world_id':'real','mode':'real','kind':'object_instance',
        'extraction_ref':'claim:'+claim,'source_content_hash':'0'*64,'extractor_version':'nx019-extractor/1','ontology_schema_revision':'recall:none',
        'proposed':{'display_name':'布丁','type_ref':'eios:object_type:Pet','identifying_properties':{'name':'布丁'},'strong_identifier':False},
        'recall':[],'status':'pending_review','dependent_claim_refs':['claim:'+claim],'evidence_refs':['claim:'+claim],'created_at':'2026-10-10T00:00:00Z'}
    from psycopg.types.json import Jsonb
    admin.execute("""insert into ontology.nexloop_candidate_definitions(tenant_id,world,candidate_id,kind,dedupe_key,candidate,dependent_claims,status,merge_scores,config_version)
        values(%s,'real',%s,'object_instance',%s,%s,%s,'pending_review',%s,'test')""",(TENANT,candidate['candidate_id'],'f'*64,Jsonb(candidate),['claim:'+claim],
        Jsonb({'lexical_similarity':0,'core_term_containment':0,'vector_cluster':0,'rule_whitelist':0,'weighted_total':0,'threshold':1,'config_version':'test'})))
    result=approve(f,candidate['candidate_id'],'gate-instance')
    assert result['outcome']=='publication_failed' and result['publication']['gate_failures']==['identifying_property_not_published:name']
    admin.execute("update control.nexloop_action_definitions set active=false where resource_id='eios:action:Pet.create:1'")
    result=approve(f,candidate['candidate_id'],'gate-instance-2')
    assert 'type_create_action_unavailable' in result['publication']['gate_failures'] and pet_objects(admin)==[]
