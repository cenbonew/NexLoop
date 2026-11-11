"""NX-038 P5 route A (docs/planning-drafts/p5-coverage-design-20261111.md §4, owner 2026-11-11 "都按推荐", L6 A1-A5) on PostgreSQL:
can a synthetic fixture type make the relay's semantic read extract a term and still report coverage below 1, reproducibly?

The relay reads as context_artifacts._semantic_read does: full text only (no embedding provider), extract_terms, limit = the strategy
top_k. A fixture type holds its marker property (fixture_marker, no display name: s6_stage_schema FIXTURE_CHANGE), five properties whose display name is the crowding term F and one property, sorted after them by ref,
whose display name is the shared term T. Synthetic phrases only; the fixture type has no object and no Action (A2)."""
import hashlib

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.context_engine.semantic import SemanticAccessRecorder,SemanticAdapter,SemanticConfig,term_hash
from nexloop_eios.evolution.attribution import read_missed
from nexloop_eios.recall import RecallConfig,RecallIndexer
from test_recall_pg import A,put_schema,recall_for,reader_targets
from test_semantic_access_record_pg import recall_db,semantic  # noqa: F401  (fixtures)

FIXTURE='S6SyntheticFixture'
F='榴莲夹心砚台'          # the crowding term: five fixture properties carry it as their display name
T='黛青纹书签套组'         # the shared term: one fixture property, ref after the five; sha256 0e28817c.. is the smallest here
CROWD=('fx_a','fx_b','fx_c','fx_d','fx_e')
TARGET='zz_t'
READ=Operation.READ


def fixture_schema(version=1,hidden=()):
    p=lambda name,display:PropertyDefinition(property_name=name,value_type=PropertyValueType.STRING,display_name=display,
        visibility='hidden' if name in hidden else 'normal')
    return ObjectTypeDefinition(type_name=FIXTURE,version=version,title_property=TARGET,primary_key=(),
        description='stage synthetic acceptance fixture (NX-038 P5); no business fact',
        properties=(PropertyDefinition(property_name='fixture_marker',value_type=PropertyValueType.STRING),)  # s6_stage_schema FIXTURE_CHANGE
        +tuple(p(n,F) for n in CROWD)+(p(TARGET,T),))


def fixture_targets():
    return [(f'eios:object_type:{FIXTURE}',ResourceType.OBJECT_TYPE,READ)]+[
        (f'eios:property:{FIXTURE}/{n}',ResourceType.PROPERTY,READ) for n in CROWD+(TARGET,)]


def publish_fixture(f,schema=None):
    put_schema(f['admin'],A,schema or fixture_schema())
    session,_=seed_multi_authority(f['admin'],f['worker'],[(f'eios:object_type:{FIXTURE}',ResourceType.OBJECT_TYPE,READ)],
        identity_suffix='-fixture-indexer-'+str((schema or fixture_schema()).version),world='real',tenant=A)
    assert RecallIndexer(f['worker'],session,provider=None).index_object_type(FIXTURE)>0


def relay(f,targets,suffix):
    """A reader that may read the Consumer definitions (as recall_db's reader) and the given extra definitions."""
    _,token=seed_multi_authority(f['admin'],f['worker'],reader_targets([])+targets,identity_suffix=suffix,world='real',tenant=A)
    return authenticate_service(f['worker'],token,world='real')


def browse(f,session,signer,text,top_k=5):
    recorder=SemanticAccessRecorder(f['worker'],session,signer)
    out=SemanticAdapter(f['worker'],session,signer,recall_for(f,session,provider=None,config=RecallConfig(top_k=top_k)),
        config=SemanticConfig(top_k=top_k),recorder=recorder).browse(text,limit=top_k,extract_terms=True)
    row=f['admin'].execute("select extraction,coverage,term_hashes from runtime.nexloop_semantic_accesses where access_id=%s",(out['access_id'],)).fetchone()
    return out,row


def fixture_ref(name):return f'eios:property:{FIXTURE}/{name}'


def test_q1_same_score_ref_order_and_top_k_push_t_out(semantic):
    """Q1 (A3): top_k is 5, hits rank by score descending then ref ascending, and an exact term match scores the same for every
    definition carrying it - so T's only hit is the sixth and the read covers F only: coverage 0.5, a miss for attribution."""
    f,_,signer=semantic;publish_fixture(f)
    assert SemanticConfig().top_k==5 and RecallConfig().top_k==5
    session=relay(f,fixture_targets(),'-q1')
    recall=recall_for(f,session,provider=None)
    result=recall.recall(F+'和'+T,instances=False,extract_terms=True)
    assert result.terms==(T,F)  # longest first: T has 7 characters, F 6
    f_hits,t_hits=result.term_hits[1],result.term_hits[0]
    assert [h.ref for h in f_hits]==[fixture_ref(n) for n in CROWD]
    assert [h.ref for h in t_hits][0]==fixture_ref(TARGET)
    scores={h.score for h in f_hits}|{t_hits[0].score}
    assert len(scores)==1, scores  # the same score for all six exact matches
    # Ranking: score descending, ref ascending - the five F refs sort before zz_t, the merged list is cut at top_k.
    assert [h.ref for h in result.definitions]==[fixture_ref(n) for n in CROWD]
    # The marker (ref .../fixture_marker sorts before fx_*) has no display name and matches neither term: it takes no slot.
    assert fixture_ref('fixture_marker') not in {h.ref for g in result.term_hits for h in g}
    out,(extraction,coverage,hashes)=browse(f,session,signer,F+'和'+T)
    assert [i['ref'] for i in out['items']]==[fixture_ref(n) for n in CROWD]
    assert extraction=='terms' and float(coverage)==0.5 and sorted(hashes)==sorted([term_hash(T),term_hash(F)])
    assert read_missed({'kind':'access','extraction':extraction,'coverage':coverage})
    # A top_k of 6 lets T back in: the result depends on top_k, which the stage run sheet records (A3).
    _,(_,wide,_)=browse(f,session,signer,F+'和'+T,top_k=6)
    assert float(wide)==1.0


def test_q2_q3_q4_a_customer_term_keeps_the_key_and_makes_the_sets_differ(semantic):
    """Q2: the merge across terms ranks as _fuse (a customer's own term takes a slot, T still falls out). Q3: F and T contain
    neither each other nor another catalog term. Q4: the group key is the smallest hash - chosen to be T's - and each customer's
    own term makes its set different."""
    f,_,signer=semantic;publish_fixture(f)
    session=relay(f,fixture_targets(),'-q2')
    assert F not in T and T not in F
    own=('会员编号','关注防水')  # two readable Consumer display names of recall_db's schema, one per customer
    sets=[]
    for word in own:
        text=f'{F}和{T}，另外{word}怎么查'
        out,(extraction,coverage,hashes)=browse(f,session,signer,text)
        assert extraction=='terms' and float(coverage)<1
        assert fixture_ref(TARGET) not in {i['ref'] for i in out['items']}
        assert set(hashes)=={term_hash(F),term_hash(T),term_hash(word)}
        sets.append(frozenset(hashes))
    assert sets[0]!=sets[1]
    # The group key is the smallest hash of the read: T's (0e28817c..) is below F's and both customer words', so both reads share it.
    assert min(sets[0])==min(sets[1])==term_hash(T)


def test_q5_without_definition_read_the_fixture_is_invisible_not_a_c3_miss(semantic):
    """Q5 (A5): a reader without READ on the fixture definitions never extracts F or T (the recall gate filters the terms), so the
    route needs the relay's definition READ on the fixture - on stage the explicit definition_read_rules entry of
    stage-principals.v1.json (I29: "a new published type is added here explicitly"). No C3 either way: with READ the details are
    readable, without it nothing is extracted."""
    f,_,signer=semantic;publish_fixture(f)
    blind=relay(f,[],'-q5-blind')
    out,(extraction,coverage,hashes)=browse(f,blind,signer,F+'和'+T)
    assert extraction=='fallback' and hashes==[term_hash(F+'和'+T)]
    seeing=relay(f,fixture_targets(),'-q5-seeing')
    out,(extraction,coverage,_)=browse(f,seeing,signer,F+'和'+T)
    assert extraction=='terms' and all(i['ref'].startswith(f'eios:property:{FIXTURE}/') for i in out['items'])


def terms_of(f,session,text):
    return recall_for(f,session,provider=None).recall(text,instances=False,extract_terms=True).terms


def test_q6_retirement_hide_takes_the_terms_away_and_so_does_removing_the_read(semantic):
    """Q6 (A4, after 0450): hiding the fixture properties (NX-040 F3 hide, the next published version) takes them out of recall -
    0450 keeps hidden properties out of the recall terms - so recall_terms no longer extracts F or T for a reader that still holds
    the definition READ. Removing the definition READ (the definition_read_rules entry) still ends them on its own as well."""
    f,_,_=semantic;publish_fixture(f)
    session=relay(f,fixture_targets(),'-q6')
    assert terms_of(f,session,F+'和'+T)==(T,F)
    retired=relay(f,[],'-q6-retired')
    assert terms_of(f,retired,F+'和'+T)==()  # no definition READ: nothing extracted, before any hide
    publish_fixture(f,fixture_schema(version=2,hidden=CROWD+(TARGET,)))
    session=relay(f,fixture_targets(),'-q6-after')  # the next publication moves the authority revision: a fresh session
    assert terms_of(f,session,F+'和'+T)==()  # hidden: no longer a term, although the READ is still held


def test_the_chosen_words_hash_order_is_computed_offline():
    """The run sheet picks T so that its hash is the smallest of {F, T, U_i}; this is plain sha256 of the stripped body."""
    assert term_hash(F)==hashlib.sha256(F.encode()).hexdigest() and term_hash(' '+T+' ')==term_hash(T)
