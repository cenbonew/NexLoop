"""NX-021 / NX-020 closing on clean catalog PostgreSQL: change feeds drained by service workers.

* recall-instance: governed create/edit (and erasure) mark the instance in the writer's
  transaction; RecallInstanceIndexWorker re-derives or removes its index rows. Recall still
  filters by tenant, world and the caller's current EIOS READ.
* claim-match: recording Claims marks the Conversation; ClaimMatchWorker matches, applies
  through the governed Actions and glues. End to end from a real governed Message through
  background extraction (deterministic provider) to the automatic formal write.
Synthetic data only. Admin connections only set up fixtures, inject faults and probe.
"""
import uuid

import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.grants import GrantSubjectKind
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import ObjectTypeDefinition
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import WITNESS,authenticate_service
from nexloop_eios.claim_extraction_jobs import ClaimExtractionScheduler,ClaimExtractionWorker
from nexloop_eios.claim_match_worker import ClaimMatchWorker
from nexloop_eios.claim_matching import ClaimMatcher,MatchConfiguration,ScriptedMatchProvider
from nexloop_eios.embedding_provider import DeterministicTestEmbeddingProvider
from nexloop_eios.object_edits import GovernedObjectEditor
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall,RecallIndexer
from nexloop_eios.recall_index_worker import RecallInstanceIndexWorker
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied
from multi_authority_fixture import seed_multi_authority
from test_claim_extraction_jobs_pg import EXTRACT_TARGET,QUEUE_TARGET,make_window,registered_provider
from test_claim_matching_pg import decision,env,matcher,obj,publish_action,resolution  # noqa: F401
from test_claim_store_pg import source_targets
from test_conversation_messages import conversations  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

READ,EDIT,EXECUTE=Operation.READ,Operation.EDIT,Operation.EXECUTE
INDEX_FEED=('eios:action:NexLoop.feed.recall-instance:1',ResourceType.ACTION,EXECUTE)
MATCH_FEED=('eios:action:NexLoop.feed.claim-match:1',ResourceType.ACTION,EXECUTE)


def feed_rows(admin,feed):
    return admin.execute('select tenant_id,world,item_key,payload,change_seq,status from runtime.nexloop_work_feed where feed=%s order by item_key',(feed,)).fetchall()


def index_worker(f,*,tenant=None,world='real',suffix='-recall-indexer',targets=(INDEX_FEED,),types=None):
    """A worker factory: like the deployed process, every tick re-authenticates (authority changes make old sessions stale)."""
    _,token=seed_multi_authority(f['admin'],f['worker'],list(targets),identity_suffix=suffix,world=world,tenant=tenant or f['tenant'])
    return lambda:RecallInstanceIndexWorker(f['worker'],authenticate_service(f['worker'],token,world=world),f['signer'],
        types=types or {'Consumer':None,'Product':None},provider=f['provider'],expected_dimension=64)


def drain(make,rounds=10):
    totals={}
    for _ in range(rounds):
        summary=make().run_once()
        if not any(summary.values()):return totals
        for key,value in summary.items():totals[key]=totals.get(key,0)+value
    raise AssertionError('feed did not drain')


def reader(f,targets,*,suffix,world='real',tenant=None):
    session,_=seed_multi_authority(f['admin'],f['worker'],targets,identity_suffix=suffix,world=world,tenant=tenant or f['tenant'])
    return OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)


def instance_targets(type_name,object_id,fields):
    targets=[(f'eios:object_type:{type_name}',ResourceType.OBJECT_TYPE,READ),(f'eios:object:{type_name}/{object_id}',ResourceType.OBJECT,READ)]
    targets+=[(f'eios:property:{type_name}/{p}',ResourceType.PROPERTY,READ) for p in fields]
    targets+=[(f'eios:property:{type_name}/{object_id}/{p}',ResourceType.PROPERTY,READ) for p in fields]
    return targets


def instance_refs(recall,text,**options):return [h.ref for h in recall.recall(text,definitions=False,**options).instances]


def entries(admin,source):
    return admin.execute('select field,body from ontology.nexloop_recall_entries where source_key=%s order by field,body',(source,)).fetchall()


def test_governed_create_and_edit_mark_the_instance_and_reflow_makes_it_recallable(env):
    f=env;admin=f['admin'];worker=index_worker(f)
    drain(worker)
    assert feed_rows(admin,'recall-instance')==[]
    # Governed create (AT-065 strong identifier path): the feed row is written in the same transaction.
    claim=f['claims'].add('sku-new','咨询商品','SKU-2002',kind='user_statement',subject='entity',subject_text='SKU-2002',quote='我想问下SKU-2002这款')
    m,_=matcher(f,{claim:decision(type_ref='eios:object_type:Product',strong={'key':'sku','value':'SKU-2002'})})
    assert list(m.process_conversation(f['conversation'])['applied'].values())==['applied']
    created=admin.execute("select object_id from ontology.objects where type_name='Product' and properties->>'sku'='SKU-2002'").fetchone()[0]
    (row,)=feed_rows(admin,'recall-instance')
    assert row[2:4]==('Product/'+created,{'type_name':'Product','object_id':created,'op':'upsert'}) and row[5]=='pending'
    recall=reader(f,instance_targets('Product',created,('sku','name')),suffix='-product-reader')
    strong=[{'key':'sku','value':'SKU-2002'}]
    assert instance_refs(recall,'SKU-2002',strong_ids=strong)==[]  # not yet indexed
    assert drain(worker)['indexed']==1 and feed_rows(admin,'recall-instance')==[]
    assert instance_refs(recall,'SKU-2002',strong_ids=strong)==['eios:object:Product/'+created]
    # Governed edit of the indexed name: the old text leaves the index, the new one is recallable.
    editor_session,_=seed_multi_authority(admin,f['worker'],[('eios:action:Consumer.edit:1',ResourceType.ACTION,EXECUTE),
        ('eios:object:Consumer/'+f['consumer'],ResourceType.OBJECT,EDIT),(f"eios:property:Consumer/{f['consumer']}/display_name",ResourceType.PROPERTY,EDIT)],
        identity_suffix='-editor',tenant=f['tenant'])
    revision=obj(admin,f['consumer'])[1]
    GovernedObjectEditor(f['worker'],editor_session,f['signer']).edit(action_name='Consumer.edit',action_version=1,intent_id=str(uuid.uuid4()),
        type_name='Consumer',object_id=f['consumer'],expected_revision=revision,properties={'display_name':'李明远'})
    assert [r[2] for r in feed_rows(admin,'recall-instance')]==['Consumer/'+f['consumer']]
    source=f"object:real/Consumer/{f['consumer']}"
    assert entries(admin,source)==[('title','张伟')]
    consumer=reader(f,instance_targets('Consumer',f['consumer'],('display_name',)),suffix='-consumer-reader')
    assert 'eios:object:Consumer/'+f['consumer'] not in instance_refs(consumer,'李明远')
    assert drain(worker)['indexed']==1
    assert entries(admin,source)==[('title','李明远')]
    assert instance_refs(consumer,'李明远')[0]=='eios:object:Consumer/'+f['consumer']


def test_erased_or_revoked_instance_is_never_recalled(env):
    f=env;admin=f['admin'];worker=index_worker(f);drain(worker)
    targets=instance_targets('Product',f['product'],('sku','name'))
    session,token=seed_multi_authority(admin,f['worker'],targets,identity_suffix='-product-reader',tenant=f['tenant'])
    recall=lambda s:OntologyRecall(f['worker'],s,authorizer=EiosRecallAuthorizer(f['worker'],s),provider=f['provider'],expected_dimension=64)
    strong=[{'key':'sku','value':'SKU-1001'}]
    assert instance_refs(recall(session),'防水登山鞋',strong_ids=strong)[0]=='eios:object:Product/'+f['product']
    # Revocation (explicit empty configured grant on the object): excluded at query time, the index row stays.
    principal=session.authentication.subject_principal_id
    admin.execute("""insert into authz.nexloop_authority_facts values(%s,'grants',%s,%s)
        on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload""",(f['tenant'],[principal,'eios:object:Product/'+f['product']],
        Jsonb(F.GrantFacts(tenant_id=f['tenant'],repository_witness=WITNESS,subject_kind=GrantSubjectKind.SERVICE,principal_id=principal,grants=(),
            valid_until=None,complete=True,next_cursor=None,revision=1).model_dump(mode='json'))))
    assert instance_refs(recall(authenticate_service(f['worker'],token,world='real')),'防水登山鞋',strong_ids=strong)==[]
    still,_=seed_multi_authority(admin,f['worker'],targets,identity_suffix='-product-reader-2',tenant=f['tenant'])
    assert instance_refs(recall(still),'防水登山鞋',strong_ids=strong)  # control: another reader still holds READ
    # Erasure outside the governed path (fault/erasure job): the delete marks the feed and the worker removes the rows.
    admin.execute("delete from ontology.objects where type_name='Product' and object_id=%s",(f['product'],))
    assert [(r[2],r[3]['op']) for r in feed_rows(admin,'recall-instance')]==[('Product/'+f['product'],'delete')]
    assert drain(worker)['removed']==1
    assert entries(admin,f"object:real/Product/{f['product']}")==[]
    assert instance_refs(recall(still),'防水登山鞋',strong_ids=strong)==[]


def test_other_tenant_and_world_never_see_or_drain_the_instance(env):
    f=env;admin=f['admin'];drain(index_worker(f))
    targets=instance_targets('Consumer',f['consumer'],('display_name',))
    assert instance_refs(reader(f,targets,suffix='-a-reader'),'张伟')[0]=='eios:object:Consumer/'+f['consumer']
    # Same grants in the simulation world: real instances are invisible there.
    assert instance_refs(reader(f,targets,suffix='-sim-reader',world='simulation'),'张伟')==[]
    # Another tenant holding grants on the same object id: nothing crosses the tenant boundary.
    other='synthetic-other'
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')",(other,))
    definition=admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer'",(f['tenant'],)).fetchone()[0]
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',1,%s)",(other,Jsonb(definition)))
    assert instance_refs(reader(f,targets,suffix='-b-reader',tenant=other),'张伟')==[]
    # Feed work is tenant- and world-scoped too: other workers never see or drain A/real rows.
    admin.execute("update ontology.objects set properties=properties||'{\"display_name\":\"张伟伟\"}' where object_id=%s",(f['consumer'],))
    pending=feed_rows(admin,'recall-instance')
    assert [(r[0],r[1],r[2]) for r in pending]==[(f['tenant'],'real','Consumer/'+f['consumer'])]
    for kwargs in ({'tenant':other,'suffix':'-b-indexer'},{'world':'simulation','suffix':'-sim-indexer'}):
        assert not any(index_worker(f,**kwargs)().run_once().values())
    assert feed_rows(admin,'recall-instance')==pending


def test_feed_requires_its_own_service_authority_and_never_touches_tables(env):
    f=env;admin=f['admin']
    with pytest.raises(WorkFeedDenied):index_worker(f,targets=[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,READ)],suffix='-nofeed')().run_once()
    # The claim-match grant does not open the recall-instance feed (and vice versa).
    with pytest.raises(WorkFeedDenied):index_worker(f,targets=[MATCH_FEED],suffix='-wrongfeed')().run_once()
    for role in ('nexloop_api','nexloop_domain_worker'):
        with open_core(make_conninfo(f['pg'],user=role)) as pool,pool.connection() as db:
            with pytest.raises(Exception):db.execute('select count(*) from runtime.nexloop_work_feed').fetchone()


def test_lease_fence_keeps_a_change_that_arrives_while_indexing(env):
    f=env;admin=f['admin'];worker=index_worker(f);drain(worker)
    admin.execute("update ontology.objects set properties=properties||'{\"display_name\":\"王五\"}' where object_id=%s",(f['consumer'],))
    feed=worker().feed
    (item,)=feed.claim(limit=5,lease_seconds=60)
    # A newer change lands before the worker finishes the older snapshot.
    admin.execute("update ontology.objects set properties=properties||'{\"display_name\":\"王五六\"}' where object_id=%s",(f['consumer'],))
    assert feed.complete(item_key=item['item_key'],fence=item['fence'])=='changed'
    assert feed.complete(item_key=item['item_key'],fence=item['fence'])=='lease_lost'
    assert drain(worker)['indexed']==1
    assert entries(admin,f"object:real/Consumer/{f['consumer']}")==[('title','王五六')]
    # Failures back off and dead-letter after max_attempts; backlog shows them.
    admin.execute("update ontology.objects set properties=properties||'{\"display_name\":\"赵六\"}' where object_id=%s",(f['consumer'],))
    for _ in range(2):
        (item,)=feed.claim(limit=5,lease_seconds=60)
        status=feed.retry(item_key=item['item_key'],fence=item['fence'],code='index_failed',delay_seconds=0,max_attempts=2)
    assert status=='dead_lettered'
    backlog=feed.backlog()
    assert backlog['pending']==0 and backlog['dead_lettered']==[{'item_key':'Consumer/'+f['consumer'],'attempts':2,'code':'index_failed'}]
    # A newer change revives a dead-lettered item.
    admin.execute("update ontology.objects set properties=properties||'{\"display_name\":\"赵六六\"}' where object_id=%s",(f['consumer'],))
    assert drain(worker)['indexed']==1 and feed.backlog()['dead_lettered']==[]


# ------------------------------------------------------------- extraction → matching → apply

PREFERENCE='周末上门服务'


@pytest.fixture
def pipeline(conversations,admin):
    fixture=conversations;tenant='synthetic-a'
    conversation_id,ids=make_window(fixture,['我比较喜欢周末上门服务。','好的'],'e2e')
    api=fixture['reader'].pool;signer=fixture['reader'].signer
    consumer=fixture['consumer']
    # The stored Consumer v1 (published_action 'with-preference'): one open string property.
    schema=ObjectTypeDefinition.model_validate(admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=1",
        (tenant,)).fetchone()[0])
    assert [p.property_name for p in schema.properties]==['preference']
    publish_action(admin,tenant,'Consumer.edit',schema,'ontology.object.edit')
    with open_core(make_conninfo(fixture['pg'],user='nexloop_domain_worker')) as worker_pool:
        provider=DeterministicTestEmbeddingProvider(64)
        _,scheduler_token=seed_multi_authority(admin,api,[EXTRACT_TARGET,QUEUE_TARGET],identity_suffix='-e2e-scheduler')
        _,extract_token=seed_multi_authority(admin,worker_pool,[QUEUE_TARGET]+source_targets(conversation_id,ids),identity_suffix='-e2e-extractor')
        _,index_token=seed_multi_authority(admin,worker_pool,[INDEX_FEED],identity_suffix='-e2e-indexer')
        matcher_targets=[MATCH_FEED,('eios:action:nexloop.claim.match:1',ResourceType.ACTION,EXECUTE),('eios:action:Consumer.edit:1',ResourceType.ACTION,EXECUTE),
            ('eios:object:Conversation/'+conversation_id,ResourceType.OBJECT,READ)]
        matcher_targets+=instance_targets('Consumer',consumer,('preference',))
        matcher_targets+=[('eios:object:Consumer/'+consumer,ResourceType.OBJECT,EDIT),(f'eios:property:Consumer/{consumer}/preference',ResourceType.PROPERTY,EDIT)]
        _,match_token=seed_multi_authority(admin,worker_pool,matcher_targets,identity_suffix='-e2e-matcher')
        auth=lambda pool,token:authenticate_service(pool,token,world='real')
        yield dict(admin=admin,fixture=fixture,conversation_id=conversation_id,ids=ids,api=api,worker_pool=worker_pool,signer=signer,consumer=consumer,
            provider=provider,scheduler=auth(api,scheduler_token),extractor=auth(worker_pool,extract_token),indexer=auth(worker_pool,index_token),
            matcher=auth(worker_pool,match_token))


def extraction_response():
    return {'topics':[{'topic':'服务方式','conversation_summary':'顾客偏好周末上门','user_valid_reply':True,'message_refs':[1,2]}],
        'claims':[{'topic_index':0,'message_ref':1,'quote':'喜欢周末上门服务','kind':'preference','predicate':'preference','value':{'type':'string','value':PREFERENCE}}]}


def extract(s):
    """Background extraction exactly as deployed: scheduler window → durable queue → worker records Claims."""
    tasks=ClaimExtractionScheduler(s['api'],s['scheduler'],s['signer'],quiet_seconds=0).run_once()
    assert len(tasks)==1
    extraction=ClaimExtractionWorker(s['worker_pool'],s['extractor'],s['signer'],
        registered_provider({'worker_pool':s['worker_pool'],'worker':s['extractor'],'signer':s['signer'],'conversation_id':s['conversation_id']},s['ids'],extraction_response()),
        timezone='Asia/Shanghai')
    assert extraction.run_once()=='succeeded'
    (claim_id,)=[r[0] for r in s['admin'].execute("select claim_id from ontology.nexloop_claims where conversation_id=%s",(s['conversation_id'],)).fetchall()]
    return claim_id


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_extracted_claims_are_matched_and_applied_by_the_background_worker(pipeline):
    """AT-061 / AT-020 end to end: governed Message → background extraction → claim-match feed → match → governed write."""
    s=pipeline;admin=s['admin']
    claim_id=extract(s)
    # The extraction run itself marked the Conversation for matching (same transaction as the Claims).
    (row,)=feed_rows(admin,'claim-match')
    assert row[2:4]==(s['conversation_id'],{'conversation_id':s['conversation_id']})
    # Recall index: definitions published by configuration; the governed Consumer instance via the feed worker.
    indexer=RecallIndexer(s['worker_pool'],s['indexer'],provider=s['provider'],expected_dimension=64)
    indexer.activate_profile();indexer.index_object_type('Consumer')
    drain(lambda:RecallInstanceIndexWorker(s['worker_pool'],s['indexer'],s['signer'],types={'Consumer':None},provider=s['provider'],expected_dimension=64))
    recall=OntologyRecall(s['worker_pool'],s['matcher'],authorizer=EiosRecallAuthorizer(s['worker_pool'],s['matcher']),provider=s['provider'],expected_dimension=64)
    provider=ScriptedMatchProvider({claim_id:decision('eios:property:Consumer/preference',PREFERENCE)})
    m=ClaimMatcher(s['worker_pool'],s['matcher'],s['signer'],recall=recall,provider=provider,
        configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',1)},create_actions={}))
    worker=ClaimMatchWorker(s['worker_pool'],s['matcher'],s['signer'],matcher=m)
    summary=worker.run_once()
    assert summary['conversations']==1 and summary['matched']==1 and summary['applied']==1,summary
    assert obj(admin,s['consumer'])[0]['preference']==PREFERENCE and resolution(admin,claim_id)=='resolved'
    assert feed_rows(admin,'claim-match')==[]
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='Consumer.edit'").fetchone()==(1,)
    # The formal write itself re-marked the Consumer for the instance index.
    assert [r[2] for r in feed_rows(admin,'recall-instance')]==['Consumer/'+s['consumer']]
    # Re-marking the same Conversation (AT-020 rerun) replays: no duplicate proposal or write, no model call.
    admin.execute("select authz.nexloop_work_feed_touch('synthetic-a','real','claim-match',%s,%s)",(s['conversation_id'],Jsonb({'conversation_id':s['conversation_id']})))
    calls=provider.calls
    again=worker.run_once()
    assert again['conversations']==1 and provider.calls==calls
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='Consumer.edit'").fetchone()==(1,)
    assert admin.execute('select count(*) from ontology.nexloop_mutation_proposals').fetchone()==(1,)


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_match_failures_retry_and_missing_authority_never_writes(pipeline):
    s=pipeline;admin=s['admin'];extract(s)
    # A matcher without claim.match EXECUTE: the conversation is retried (backoff), nothing is recorded.
    session,_=seed_multi_authority(admin,s['worker_pool'],[MATCH_FEED,('eios:object:Conversation/'+s['conversation_id'],ResourceType.OBJECT,READ)],
        identity_suffix='-e2e-unauthorized')
    recall=OntologyRecall(s['worker_pool'],session,authorizer=EiosRecallAuthorizer(s['worker_pool'],session))
    m=ClaimMatcher(s['worker_pool'],session,s['signer'],recall=recall,provider=ScriptedMatchProvider({}),configuration=MatchConfiguration(edit_actions={},create_actions={}))
    summary=ClaimMatchWorker(s['worker_pool'],session,s['signer'],matcher=m,max_attempts=1).run_once()
    assert summary['dead_lettered']==1 and summary['applied']==0
    assert admin.execute("select status,last_code from runtime.nexloop_work_feed where feed='claim-match'").fetchone()==('dead_lettered','denied')
    assert admin.execute('select count(*) from ontology.nexloop_mutation_proposals').fetchone()==(0,)


def test_match_worker_matches_marked_conversation_then_glues_staged_candidates(env):
    """NX-020 + NX-045 in the worker: a recorded Claim marks its Conversation; the unmatched one is staged, then glued to review."""
    from test_candidate_merge_pg import candidates,new_property,publish_config
    from test_claim_matching_pg import matcher_targets
    f=env;admin=f['admin'];publish_config(f)
    far=f['claims'].add('far','常用付款方式','花呗',kind='user_statement',quote='我一般用花呗付款')
    assert [r[2] for r in feed_rows(admin,'claim-match')]==[f['conversation']]
    session,_=seed_multi_authority(admin,f['worker'],matcher_targets(f)+[MATCH_FEED],identity_suffix='-match-worker',tenant=f['tenant'])
    recall=OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)
    m=ClaimMatcher(f['worker'],session,f['signer'],recall=recall,
        provider=ScriptedMatchProvider({far:decision(new_prop=new_property('payment_method','常用付款方式','purchase_behavior'),value='花呗')}),
        configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',1)},create_actions={'Product':('Product.create',1)}))
    from nexloop_eios.candidate_merge import CandidateGluer
    gluer=CandidateGluer(f['worker'],session,f['signer'],indexer=f['indexer'],matcher=m,provider=f['provider'])
    summary=ClaimMatchWorker(f['worker'],session,f['signer'],matcher=m,gluer=gluer).run_once()
    assert summary['conversations']==1 and summary['matched']==1 and summary['glued']==1,summary
    (row,)=candidates(admin).values()
    assert row[1]=='property' and row[2]=='pending_review' and resolution(admin,far)=='awaiting_definition'
    assert feed_rows(admin,'claim-match')==[]
