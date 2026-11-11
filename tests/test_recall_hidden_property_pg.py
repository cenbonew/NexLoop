"""Recall of hidden properties (L3 P5 Q6, 933f8f82; design docs/planning-drafts/recall-hidden-properties-20261111.md, L6 conditional
PASS 2026-11-11; migration 0449, temporary number) on PostgreSQL.

A property hidden in a type's latest version (the NX-040 F3 hide, 0254: visibility "hidden") is retired in the semantic layer: no
definition row (name, display name, description, vocabulary values), so no index row, no recall term, no search hit, no live alias and
no new alias. The migration's backfill deletes the type rows already written at the latest version for such a property - their
vectors cascade, every other row and vector stays, and a second run deletes nothing. A later version that shows the property again
brings everything back, the alias included. The S1-S4 impact queries of the run sheet parse and count here. Synthetic phrases only."""
from pathlib import Path
import re

import psycopg
import pytest
from eios.ontology.models import PropertyDefinition,PropertyValueType,PropertyVisibility
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.recall import RecallIndexer
from test_recall_pg import A,consumer_schema,put_schema,recall_db,recall_for,refs  # noqa: F401  (fixture)

HIDDEN=('budget_level','waterproof_concern')
WATER='eios:property:Consumer/waterproof_concern'
SENTENCE='我很关注防水，预算两千元以内'
MIGRATION=Path(__file__).resolve().parents[1]/'packages/eios-core/src/eios/migrations/0449_recall_hidden_properties.sql'


def schema(version,hidden=HIDDEN):
    base=consumer_schema(version)
    return base.model_copy(update={'properties':tuple(p.model_copy(update={'visibility':PropertyVisibility.HIDDEN}) if p.property_name in hidden else p
        for p in base.properties)})


def indexer(f):
    return RecallIndexer(f['worker'],f['indexers'][(A,'real')],provider=f['provider'],expected_dimension=64)


def fresh(f):
    return recall_for(f,authenticate_service(f['worker'],f['reader_token'],world='real'),provider=None)


def definition_rows(admin,version):
    admin.execute("select set_config('eios.tenant_id',%s,false)",(A,))
    try:
        return admin.execute("select distinct ref,field,body from ontology.nexloop_recall_definition_rows(%s,'Consumer',%s) where gate_property=any(%s)",
            (A,version,list(HIDDEN))).fetchall()
    finally:
        admin.execute("select set_config('eios.tenant_id','',false)")


def hidden_entries(admin,version=2):
    return admin.execute("""select count(*) from ontology.nexloop_recall_entries where tenant_id=%s and source_key='type:Consumer'
        and source_revision=%s and gate_property=any(%s)""",(A,version,list(HIDDEN))).fetchone()[0]


def test_a_hidden_property_leaves_every_recall_surface_and_comes_back_when_shown(recall_db):
    f=recall_db;admin=f['admin'];ix=indexer(f)
    assert ix.index_alias('alias-waterproof-h','eios:property:Consumer/waterproof_concern','防泼水')==1
    before=fresh(f).recall(SENTENCE,instances=False,extract_terms=True)
    assert {'关注防水','两千元以内'}<=set(before.terms)
    put_schema(admin,A,schema(2))
    assert definition_rows(admin,2)==[] and definition_rows(admin,1)!=[]  # judged on the version read
    assert ix.index_object_type('Consumer')>0 and hidden_entries(admin)==0
    recall=fresh(f)
    result=recall.recall(SENTENCE,instances=False,extract_terms=True)
    assert '关注防水' not in result.terms and '两千元以内' not in result.terms
    assert WATER not in refs(result.definitions)
    assert WATER not in refs(recall.recall('防泼水',instances=False).definitions)  # the alias row stays, the scope drops it
    with pytest.raises(psycopg.Error,match='target unavailable'):ix.index_alias('alias-waterproof-h2',WATER,'防水在意')
    # Other properties are untouched.
    assert '会员编号' in recall.recall('会员编号是多少',instances=False,extract_terms=True).terms
    # A version that shows the property again revives the terms and the kept alias.
    put_schema(admin,A,schema(3,hidden=()))
    assert ix.index_object_type('Consumer')>0 and hidden_entries(admin,3)>=7
    recall=fresh(f)
    assert {'关注防水','两千元以内'}<=set(recall.recall(SENTENCE,instances=False,extract_terms=True).terms)
    alias=recall.recall('防泼水',instances=False)
    assert refs(alias.definitions)[:1]==[WATER] and 'alias' in alias.definitions[0].fields


def backfill_sql():
    text=MIGRATION.read_text()
    return re.search(r'do \$backfill\$.*?end \$backfill\$;',text,re.S).group(0)


def counts(admin):
    hidden="gate_property=any(%s)"
    q=lambda where:admin.execute(f"""select count(distinct e.entry_id),count(m.entry_id) from ontology.nexloop_recall_entries e
        left join ontology.nexloop_recall_embeddings m on m.entry_id=e.entry_id where e.tenant_id=%s and {where}""",(A,list(HIDDEN))).fetchone()
    return {'hidden_v2':q(f"e.source_key='type:Consumer' and e.source_revision=2 and e.{hidden}"),
            'hidden_v1':q(f"e.source_key='type:Consumer' and e.source_revision=1 and e.{hidden}"),
            'other':q(f"not (e.source_key='type:Consumer' and e.{hidden})"),
            'vectors':admin.execute("select count(*) from ontology.nexloop_recall_embeddings where tenant_id=%s",(A,)).fetchone()}


def test_the_backfill_deletes_hidden_rows_at_the_latest_version_with_their_vectors_only_and_is_idempotent(recall_db):
    f=recall_db;admin=f['admin'];ix=indexer(f)
    put_schema(admin,A,schema(2))
    assert ix.index_object_type('Consumer')>0
    # Rows the pre-0449 function wrote at version 2 for the hidden properties, with vectors (simulated: the function is replaced),
    # plus one stale version-1 row the scope never reads and the backfill leaves alone.
    profile='nexloop-test-ngram-v1@64';vector='['+','.join(['1']+['0']*63)+']'
    old=[(2,WATER,'property','waterproof_concern','name','waterproof_concern'),(2,WATER,'property','waterproof_concern','display_name','关注防水'),
         (2,'nexloop:vocabulary:Consumer/budget_level/两千元以内','vocabulary_value','budget_level','vocabulary_value','两千元以内'),
         (1,WATER,'property','waterproof_concern','display_name','关注防水')]
    admin.execute("select set_config('eios.tenant_id',%s,false)",(A,))
    for rev,ref,kind,prop,field,body in old:
        entry=admin.execute("select ontology.nexloop_recall_insert(%s,null,'definition','type:Consumer',%s,%s,%s,'Consumer',%s,null,%s,null,null,%s)",
            (A,rev,ref,kind,prop,field,body)).fetchone()[0]
        if field!='name':
            admin.execute("insert into ontology.nexloop_recall_embeddings(entry_id,tenant_id,profile_id,embedding) values(%s,%s,%s,%s::extensions.vector)",
                (entry,A,profile,vector))
    admin.execute("select set_config('eios.tenant_id','',false)")
    start=counts(admin)
    assert start['hidden_v2']==(3,2) and start['hidden_v1']==(1,1)
    admin.execute(backfill_sql())
    after=counts(admin)
    assert after['hidden_v2']==(0,0)  # rows and their vectors (cascade)
    assert after['hidden_v1']==start['hidden_v1'] and after['other']==start['other']
    assert after['vectors'][0]==start['vectors'][0]-2
    admin.execute(backfill_sql())
    assert counts(admin)==after  # idempotent


IMPACT=Path(__file__).resolve().parents[1]/'docs/planning-drafts/recall-hidden-properties-20261111.md'


def test_the_s1_s4_impact_queries_count_the_hidden_surface(recall_db):
    """The read-only S1-S4 queries of the design (§5) run on this schema and count what the backfill and the scope change."""
    f=recall_db;admin=f['admin'];ix=indexer(f)
    assert ix.index_alias('alias-waterproof-s','eios:property:Consumer/waterproof_concern','防泼水')==1
    put_schema(admin,A,schema(2))
    queries=re.findall(r'ro "(.*?)"\n',IMPACT.read_text(),re.S)
    assert len(queries)==4
    out=[admin.execute(q.replace('%','%%').replace("'$TENANT_ID'","%s"),(A,)*q.count("'$TENANT_ID'")).fetchall() for q in queries]
    assert sorted(r[2] for r in out[0])==sorted(HIDDEN) and dict((r[2],r[3]) for r in out[0])['budget_level']==3
    assert ('alias','alias',True,1) in out[1]  # the alias row the scope now drops
    assert out[2]==[] and out[3]==[]
