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
