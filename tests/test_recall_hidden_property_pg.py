"""Recall of hidden properties (L3 P5 Q6, 933f8f82; design docs/planning-drafts/recall-hidden-properties-20261111.md) on PostgreSQL.

Current behaviour, reproduced: a property hidden by the next Schema version (the NX-040 F3 hide, 0254: visibility "hidden") keeps
every recall surface - ontology.nexloop_recall_definition_rows (0067) has no visibility filter, so the reflow at the new version
indexes its name, display name, description and vocabulary values again, control.nexloop_recall_terms (0379) extracts them,
control.nexloop_recall_search returns the property, an alias to it stays live in ontology.nexloop_recall_scope and a new alias to it
is accepted. The design's migration turns each assertion marked CURRENT into its opposite. Synthetic phrases only."""
from eios.ontology.models import PropertyDefinition,PropertyValueType,PropertyVisibility
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.recall import RecallIndexer
from test_recall_pg import A,consumer_schema,put_schema,recall_db,recall_for,refs  # noqa: F401  (fixture)

HIDDEN=('budget_level','waterproof_concern')
WATER='eios:property:Consumer/waterproof_concern'


def hidden_v2():
    base=consumer_schema(2)
    return base.model_copy(update={'properties':tuple(p.model_copy(update={'visibility':PropertyVisibility.HIDDEN}) if p.property_name in HIDDEN else p
        for p in base.properties)})


def indexer(f):
    return RecallIndexer(f['worker'],f['indexers'][(A,'real')],provider=f['provider'],expected_dimension=64)


def test_a_hidden_property_stays_in_every_recall_surface(recall_db):
    f=recall_db;admin=f['admin'];ix=indexer(f)
    assert ix.index_alias('alias-waterproof-h','eios:property:Consumer/waterproof_concern','防泼水')==1
    put_schema(admin,A,hidden_v2())
    stored=admin.execute("select definition->'properties' from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=2",
        (A,)).fetchone()[0]
    assert {p['property_name'] for p in stored if p.get('visibility')=='hidden'}==set(HIDDEN)
    # The definition rows the reflow writes at version 2 (the same function feeds definition_texts, the alias check and reindexing).
    admin.execute("select set_config('eios.tenant_id',%s,false)",(A,))
    rows=admin.execute("select distinct ref,field,body from ontology.nexloop_recall_definition_rows(%s,'Consumer',2) where gate_property=any(%s)",
        (A,list(HIDDEN))).fetchall()
    admin.execute("select set_config('eios.tenant_id','',false)")
    assert ('eios:property:Consumer/waterproof_concern','display_name','关注防水') in rows  # CURRENT
    assert any(r[1]=='vocabulary_value' and r[2]=='两千元以内' for r in rows)  # CURRENT
    assert ix.index_object_type('Consumer')>0
    indexed=admin.execute("""select count(*) from ontology.nexloop_recall_entries where tenant_id=%s and source_key='type:Consumer'
        and source_revision=2 and gate_property=any(%s)""",(A,list(HIDDEN))).fetchone()[0]
    assert indexed>=7  # CURRENT: 2 names, 2 display names, 2 descriptions, 3 vocabulary values
    recall=recall_for(f,authenticate_service(f['worker'],f['reader_token'],world='real'),provider=None)
    result=recall.recall('我很关注防水，预算两千元以内',instances=False,extract_terms=True)
    assert '关注防水' in result.terms and '两千元以内' in result.terms  # CURRENT
    assert WATER in refs(result.definitions)  # CURRENT
    # The alias written before the hide is still live (its canonical ref is in the latest definition rows) ...
    alias=recall.recall('防泼水',instances=False)
    assert refs(alias.definitions)[:1]==[WATER] and 'alias' in alias.definitions[0].fields  # CURRENT
    # ... and a new alias to the hidden property is accepted.
    assert ix.index_alias('alias-waterproof-h2',WATER,'防水在意')==1  # CURRENT


def test_hide_is_a_new_version_the_property_value_contract_is_unchanged(recall_db):
    """Hidden is a presentation flag on the definition (eios.ontology.models.PropertyVisibility): the property keeps its name,
    type and READ resource; only the recall index is in question here."""
    p=PropertyDefinition(property_name='x',value_type=PropertyValueType.STRING,visibility=PropertyVisibility.HIDDEN)
    assert p.visibility.value=='hidden' and p.model_dump(mode='json')['visibility']=='hidden'
