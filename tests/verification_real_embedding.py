"""NX-021 real embedding endpoint + real PostgreSQL recall.
Manual opt-in verification: NEXLOOP_REAL_EMBEDDING_ENV_FILE=<private env file> pytest tests/verification_real_embedding.py.
Default CI does not collect this file. No implicit .env read and no skips; the
key stays in process memory. Synthetic phrases only, never customer data.
"""
import os
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import open_core
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.embedding_profile import load_embedding_profile
from nexloop_eios.embedding_provider import ArkMultimodalEmbeddingProvider
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall,RecallIndexer
from multi_authority_fixture import seed_multi_authority
from test_recall_pg import A,DEF_PROPS,consumer_schema,put_schema


def real_provider():
    supplied=os.environ.get('NEXLOOP_REAL_EMBEDDING_ENV_FILE')
    assert supplied,'explicit private embedding configuration file required; no implicit .env read'
    profile=load_embedding_profile(env_file=supplied)
    assert profile.dimension is not None,'EMBEDDING_DIMENSION must be measured and set (NX-021 probe recommends 1024)'
    return ArkMultimodalEmbeddingProvider(profile)


def test_real_endpoint_text_dimension_and_determinism():
    provider=real_provider()
    import math
    cos=lambda x,y:sum(p*q for p,q in zip(x,y))/math.sqrt(sum(p*p for p in x)*sum(q*q for q in y))
    a=provider.embed('关注防水功能');again=provider.embed('关注防水功能')
    # Measured: dimensions=1024 repeats are near-identical (quantization noise), not bitwise equal.
    assert len(a)==provider.dimension and cos(a,again)>0.999
    print({'repeat_cosine':round(cos(a,again),6),'bitwise_equal':a==again,'max_abs_diff':max(abs(x-y) for x,y in zip(a,again))})
    assert cos(a,provider.embed('在意是否防水'))>cos(a,provider.embed('预算两千元以内'))


def test_real_vector_route_recall_on_postgres(admin,pg):
    provider=real_provider()
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing",(A,))
    put_schema(admin,A,consumer_schema())
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as worker:
        indexer_session,_=seed_multi_authority(admin,worker,[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,Operation.READ)],identity_suffix='-real-indexer',tenant=A)
        indexer=RecallIndexer(worker,indexer_session,provider=provider,expected_dimension=provider.dimension)
        assert indexer.activate_profile()==provider.profile_id and indexer.index_object_type('Consumer')>0
        targets=[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,Operation.READ)]+[(f'eios:property:Consumer/{p}',ResourceType.PROPERTY,Operation.READ) for p in DEF_PROPS]
        reader,_=seed_multi_authority(admin,worker,targets,identity_suffix='-real-reader',tenant=A)
        recall=OntologyRecall(worker,reader,authorizer=EiosRecallAuthorizer(worker,reader),provider=provider,expected_dimension=provider.dimension)
        result=recall.recall('在意是否防水',instances=False)
        assert result.methods==('vector','fts','trgm')
        top=result.definitions[0]
        assert top.ref=='eios:property:Consumer/waterproof_concern' and top.method_scores.get('vector',0)>0.5
        assert not any('monthly_income' in h.ref for h in recall.recall('月收入两万',instances=False).definitions)
        print({'dimension':provider.dimension,'top':top.contract(),'vector':round(top.method_scores['vector'],4),'transport_failures':provider.transport_failures})
