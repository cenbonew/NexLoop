"""I26 (S6 fdd261cf, L6): the relay's semantic read searches the definition terms found in the statement, records one hash per
term, says why an access returned nothing, and the recall-index service reflows published types missing from the definition
index. Actual PostgreSQL, synthetic phrases only."""
from types import SimpleNamespace

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.context_engine.semantic import SemanticAccessRecorder,SemanticAdapter,term_hash
from nexloop_eios.recall import RecallIndexer
from nexloop_eios.recall_index_worker import RecallInstanceIndexWorker
from test_recall_pg import A,consumer_schema,put_schema,recall_for
from test_semantic_access_record_pg import recall_db,semantic  # noqa: F401  (fixtures)

SENTENCE='我只想改会员编号，预算两千元以内就行，月收入不方便说'


def adapter(f,session,signer,recorder):
    """As the relay reads (context_artifacts._semantic_read): full text only, no embedding provider."""
    return SemanticAdapter(f['worker'],session,signer,recall_for(f,session,provider=None),recorder=recorder)


def records(admin):
    return admin.execute('select term_hashes,result_count,unresolved_count,miss_reason from runtime.nexloop_semantic_accesses'
        " where kind='access' order by recorded_at").fetchall()


def test_terms_are_the_readable_definition_bodies_in_the_text(semantic):
    f,session,_=semantic;recall=recall_for(f,session,provider=None)
    # Chinese display names and vocabulary values as substrings; the unreadable monthly_income (月收入) is never a term.
    assert recall.recall(SENTENCE,instances=False,extract_terms=True).terms==('两千元以内','会员编号')
    # ASCII names as whole words, snake_case split; a part of a word is no match.
    assert recall.recall('please change member no and the waterproof concern',instances=False,extract_terms=True).terms==('waterproof_concern','member_no')
    assert recall.recall('remember nothing',instances=False,extract_terms=True).terms==()
    assert recall.recall(SENTENCE,instances=False).terms==()


def test_browse_with_terms_records_one_hash_per_term_and_finds_the_definitions(semantic):
    f,session,signer=semantic;admin=f['admin']
    recorder=SemanticAccessRecorder(f['worker'],session,signer)
    out=adapter(f,session,signer,recorder).browse(SENTENCE,extract_terms=True)
    refs={i['ref'] for i in out['items']}
    assert 'eios:property:Consumer/member_no' in refs and out['coverage']==1.0
    assert not any('monthly_income' in r for r in refs)
    assert records(admin)==[([term_hash('两千元以内'),term_hash('会员编号')],len(out['items']),0,None)]
    # Without extraction the sentence stays the single term (the pre-I26 record), and the same definition scores far lower:
    # its bigrams are a small part of the whole sentence's.
    whole=adapter(f,session,signer,recorder).browse(SENTENCE)
    assert records(admin)[1][0]==[term_hash(SENTENCE)]
    score=lambda o:max(i['score'] for i in o['items'] if i['ref']=='eios:property:Consumer/member_no')
    assert score(out)>=2*score(whole)


def test_a_miss_says_why(semantic):
    f,session,signer=semantic;admin=f['admin']
    recorder=SemanticAccessRecorder(f['worker'],session,signer)
    out=adapter(f,session,signer,recorder).browse('xqzv',extract_terms=True)
    assert out['items']==[] and records(admin)[-1]==([term_hash('xqzv')],0,1,'no_match')
    # A caller that may read no definition type at all.
    blind,_=seed_multi_authority(admin,f['worker'],[('eios:object_type:Elsewhere',ResourceType.OBJECT_TYPE,Operation.READ)],
        identity_suffix='-blind',world='real',tenant=A)
    adapter(f,blind,signer,SemanticAccessRecorder(f['worker'],blind,signer)).browse(SENTENCE,extract_terms=True)
    assert records(admin)[-1][1:]==(0,1,'no_readable_types')


def test_published_types_missing_from_the_index_are_reflowed(recall_db):  # noqa: F811
    f=recall_db;indexer=RecallIndexer(f['worker'],f['indexers'][(A,'real')],provider=f['provider'],expected_dimension=64)
    assert indexer.stale_definitions()==[]
    put_schema(f['admin'],A,consumer_schema(version=2))  # published without touching the index
    assert indexer.stale_definitions()==['Consumer']
    assert RecallInstanceIndexWorker.reflow_definitions(SimpleNamespace(indexer=indexer))==1
    assert indexer.stale_definitions()==[]


def test_every_index_tick_reflows_first():
    calls=[]
    worker=SimpleNamespace(ensure_profile=lambda:None,reflow_definitions=lambda:calls.append('reflow'),
        feed=SimpleNamespace(claim=lambda **k:calls.append('claim') or []),batch=1,lease_seconds=1)
    RecallInstanceIndexWorker.run_once(worker)
    assert calls==['reflow','claim']


# --- option A (0376): glossary glosses take part in recall -------------------------------------------------------------------------

def gloss(admin,kind,identifier,text,source='manual',revision=1):
    machine=dict(model_provider='synthetic',model_id='synthetic-model',input_sha256='e'*64) if source=='machine' else dict(model_provider=None,model_id=None,input_sha256=None)
    admin.execute("""insert into control.nexloop_world_glossary(tenant_id,kind,identifier,gloss,source,revision,model_provider,model_id,input_sha256,translated_at)
        values(%s,%s,%s,%s,%s,%s,%s,%s,%s,case when %s='machine' then now() end)
        on conflict(tenant_id,kind,identifier) do update set gloss=excluded.gloss,source=excluded.source,revision=excluded.revision,
         model_provider=excluded.model_provider,model_id=excluded.model_id,input_sha256=excluded.input_sha256,translated_at=excluded.translated_at""",
        (A,kind,identifier,text,source,revision,machine['model_provider'],machine['model_id'],machine['input_sha256'],source))


def gloss_rows(admin):
    return admin.execute("select source_key,ref,world,gloss_source,source_revision from ontology.nexloop_recall_entries"
        " where source_key like 'glossary:%%' order by ref").fetchall()


def access_gloss(admin):
    return admin.execute("select gloss_hits from runtime.nexloop_semantic_accesses where kind='access' order by recorded_at desc limit 1").fetchone()[0]


def test_manual_and_machine_glosses_are_recalled_and_recorded_by_source(semantic):
    f,session,signer=semantic;admin=f['admin'];recorder=SemanticAccessRecorder(f['worker'],session,signer)
    browse=lambda text:adapter(f,session,signer,recorder).browse(text,extract_terms=True)
    assert browse('我是老客户')['items']==[] and browse('看重雨天耐用性')['items']==[]  # no gloss yet: nothing
    gloss(admin,'object_type','Consumer','客户');gloss(admin,'property','waterproof_concern','雨天耐用性','machine')
    gloss(admin,'relation','consumer_goal','客户目标')  # no recall ref: skipped
    assert gloss_rows(admin)==[('glossary:object_type/Consumer','eios:object_type:Consumer','real','manual',1),
        ('glossary:property/waterproof_concern','eios:property:Consumer/waterproof_concern','real','machine',1)]
    out=browse('我是老客户')
    assert [i['ref'] for i in out['items']][0]=='eios:object_type:Consumer' and access_gloss(admin)=={'manual':1}
    out=browse('看重雨天耐用性')
    assert [i['ref'] for i in out['items']][0]=='eios:property:Consumer/waterproof_concern' and access_gloss(admin)=={'machine':1}
    # The gloss inherits the definition's READ: a gloss of the unreadable monthly_income is never returned.
    gloss(admin,'property','monthly_income','收入水平')
    assert not any('monthly_income' in i['ref'] for i in browse('收入水平怎么样')['items'])


def test_a_correction_clearing_or_restore_takes_the_alias_away_and_simulation_never_sees_it(semantic):
    f,session,_=semantic;admin=f['admin']
    from support.tenants import SIM_WORLD
    gloss(admin,'object_type','Consumer','客户')
    sim_session,_=seed_multi_authority(admin,f['worker'],[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,Operation.READ)],
        identity_suffix='-sim-reader',world=SIM_WORLD,tenant=A)
    sim=recall_for(f,sim_session,provider=None)
    assert 'eios:object_type:Consumer' not in [h.ref for h in sim.recall('客户',instances=False).definitions]
    from nexloop_eios.authorization import authenticate_service
    real=recall_for(f,authenticate_service(f['worker'],f['reader_token'],world='real'),provider=None)
    assert [h.ref for h in real.recall('客户',instances=False).definitions][:1]==['eios:object_type:Consumer']
    gloss(admin,'object_type','Consumer','顾主',revision=2)  # correction
    assert [r[1] for r in gloss_rows(admin)]==['eios:object_type:Consumer'] and real.recall('客户',instances=False).definitions==()
    gloss(admin,'object_type','Consumer','',revision=3)  # cleared by hand
    assert gloss_rows(admin)==[]
    gloss(admin,'object_type','Consumer','客户',revision=4)
    admin.execute("delete from control.nexloop_world_glossary where tenant_id=%s",(A,))  # restored to automatic translation
    assert gloss_rows(admin)==[]


def test_a_new_type_version_moves_the_aliases_and_a_publication_opens_a_watch(recall_db):  # noqa: F811
    f=recall_db;admin=f['admin']
    gloss(admin,'object_type','Consumer','客户')
    put_schema(admin,A,consumer_schema(version=2))
    assert [r[4] for r in gloss_rows(admin)]==[2]
    admin.execute("insert into control.nexloop_world_glossary_publications(tenant_id,manifest_version,manifest_sha256,expected_revision,entries)"
        " values(%s,1,%s,0,1)",(A,'f'*64))
    assert admin.execute("select release_ref from runtime.nexloop_evo_release_watches where tenant_id=%s",(A,)).fetchall()==[('glossary:1',)]


def test_a_type_still_stale_after_its_rebuild_is_warned_and_counted(caplog):
    """S55: an unhealable stale type would be rebuilt on every tick; the tick says so and counts it."""
    from nexloop_eios import recall_index_worker
    before=recall_index_worker._stuck[0]
    indexer=SimpleNamespace(stale_definitions=lambda:['Stuck'],index_object_type=lambda name:1)
    with caplog.at_level('WARNING'):
        assert RecallInstanceIndexWorker.reflow_definitions(SimpleNamespace(indexer=indexer))==1
    assert recall_index_worker._stuck[0]==before+1 and 'still stale after reindex: 1' in caplog.text


def test_the_stale_check_refuses_another_tokenizer(recall_db):  # noqa: F811
    import psycopg
    import pytest
    f=recall_db;indexer=RecallIndexer(f['worker'],f['indexers'][(A,'real')],provider=f['provider'],expected_dimension=64)
    with pytest.raises(psycopg.errors.InvalidParameterValue):indexer._db.call('nexloop_recall_stale_definitions','nexloop-cjk-bigram-v2')
