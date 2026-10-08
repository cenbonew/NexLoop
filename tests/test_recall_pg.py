"""NX-021 hybrid recall on a clean catalog PostgreSQL with real EIOS READ decisions.

Synthetic phrases only. Fixture rows (definitions/objects) are written by the
bootstrap identity as test setup; recall and reflow use the restricted
application roles, server-derived tenant and authority facts.
"""
import hashlib
import json
from pathlib import Path
import psycopg
import pytest
from jsonschema import Draft202012Validator
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyGroup,PropertyTypeDescriptor,PropertyTypeKind,PropertyValueType
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.embedding_provider import DeterministicTestEmbeddingProvider,EmbeddingDimensionMismatch
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall,RecallConfig,RecallIndexer,RecallUnavailable,vocabulary_ref
from multi_authority_fixture import seed_multi_authority

A,B='synthetic-a','synthetic-b'
READ=Operation.READ
CONTRACT=json.loads((Path(__file__).resolve().parents[1]/'packages/contracts/candidate-definition.schema.json').read_text())
RECALL_ITEM=Draft202012Validator(CONTRACT['properties']['recall'])


def oid(label):return hashlib.sha256(label.encode()).hexdigest()


def consumer_schema(version=1,extra=()):
    def p(name,display,description='',kind=PropertyValueType.STRING,enum=(),required=False):
        descriptor=PropertyTypeDescriptor(kind=PropertyTypeKind(kind.value),enum=enum) if enum else None
        return PropertyDefinition(property_name=name,value_type=kind,display_name=display,description=description,type_descriptor=descriptor,required=required)
    props=(p('display_name','姓名'),p('member_no','会员编号',required=True),
        p('budget_level','预算区间','顾客可接受的价格范围',enum=('两千元以内','两千到五千元','五千元以上')),
        p('waterproof_concern','关注防水','顾客是否在意产品的防水功能',PropertyValueType.BOOLEAN),
        p('monthly_income','月收入','敏感：顾客自述的月收入',PropertyValueType.NUMBER),
        p('nickname','昵称','敏感：顾客私下称呼'))+tuple(extra)
    groups=(PropertyGroup(group_name='spending_power',display_name='消费能力',property_names=('budget_level','monthly_income')),
        PropertyGroup(group_name='needs_intent',display_name='需求意图',property_names=('waterproof_concern',)))
    return ObjectTypeDefinition(type_name='Consumer',display_name='消费者',description='与企业对话的顾客',version=version,
        properties=props,property_groups=groups,title_property='display_name',primary_key=('member_no',))


def other_tenant_schema():
    return ObjectTypeDefinition(type_name='Consumer',display_name='客户',version=1,title_property='display_name',primary_key=('member_no',),
        properties=(PropertyDefinition(property_name='display_name',value_type=PropertyValueType.STRING,display_name='姓名'),
            PropertyDefinition(property_name='member_no',value_type=PropertyValueType.STRING,display_name='会员编号',required=True),
            PropertyDefinition(property_name='waterproof_rating',value_type=PropertyValueType.STRING,display_name='防水等级',description='关注防水功能的程度')))


def put_schema(admin,tenant,schema):
    admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s)',
        (tenant,schema.type_name,schema.version,Jsonb(schema.model_dump(mode='json'))))


def put_object(admin,tenant,world,label,properties,revision=1):
    admin.execute("""insert into ontology.objects(tenant_id,type_name,object_id,schema_version,properties,created_at,updated_at,world,nexloop_revision)
        values(%s,'Consumer',%s,1,%s,now(),now(),%s,%s)""",(tenant,oid(label),Jsonb(properties),world,revision))
    return oid(label)


DEF_PROPS=('display_name','member_no','budget_level','waterproof_concern','sport_preference')
INSTANCE_SPEC={'name_fields':['display_name'],'alias_fields':['nickname']}


def reader_targets(objects,*,props=DEF_PROPS,object_props=('display_name','member_no','nickname')):
    targets=[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,READ)]
    targets+=[(f'eios:property:Consumer/{p}',ResourceType.PROPERTY,READ) for p in props]
    for o in objects:
        targets.append((f'eios:object:Consumer/{o}',ResourceType.OBJECT,READ))
        targets+=[(f'eios:property:Consumer/{o}/{p}',ResourceType.PROPERTY,READ) for p in object_props]
    return targets


@pytest.fixture
def recall_db(admin,pg):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active'),(%s,'active') on conflict do nothing",(A,B))
    put_schema(admin,A,consumer_schema());put_schema(admin,B,other_tenant_schema())
    ids={
      'zhang':put_object(admin,A,'real','a-zhang',{'display_name':'张伟','member_no':'M-1001','monthly_income':20000,'nickname':'防水达人'}),
      'zhangwei2':put_object(admin,A,'real','a-zhang-2',{'display_name':'张薇','member_no':'M-1002'}),
      'sim':put_object(admin,A,'simulation','a-sim-zhang',{'display_name':'张伟','member_no':'M-1001'}),
      'other':put_object(admin,B,'real','b-zhang',{'display_name':'张伟','member_no':'M-1001'}),
    }
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as worker,open_core(make_conninfo(pg,user='nexloop_api')) as api:
        provider=DeterministicTestEmbeddingProvider(64)
        sessions={}
        # Index writers: one per tenant/world; their grants are irrelevant to indexing.
        for tenant,world in ((A,'real'),(A,'simulation'),(B,'real')):
            session,_=seed_multi_authority(admin,worker,[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,READ)],
                identity_suffix=f'-indexer-{world}',world=world,tenant=tenant)
            sessions[(tenant,world)]=session
            indexer=RecallIndexer(worker,session,provider=provider,expected_dimension=64)
            if world=='real':
                assert indexer.activate_profile()=='nexloop-test-ngram-v1@64'
                assert indexer.index_object_type('Consumer')>0
        for key,(tenant,world) in {'zhang':(A,'real'),'zhangwei2':(A,'real'),'sim':(A,'simulation'),'other':(B,'real')}.items():
            assert RecallIndexer(worker,sessions[(tenant,world)],provider=provider,expected_dimension=64).index_instance('Consumer',ids[key],spec=INSTANCE_SPEC if tenant==A else {'name_fields':['display_name']})>0
        # Reader A/real: Consumer type, non-sensitive properties, object 'zhang' only.
        reader,token=seed_multi_authority(admin,worker,reader_targets([ids['zhang']]),identity_suffix='-reader',world='real',tenant=A)
        yield dict(admin=admin,pg=pg,worker=worker,api=api,provider=provider,ids=ids,reader=reader,reader_token=token,indexers=sessions)


def recall_for(f,session,*,provider='default',pool=None,**options):
    provider=f['provider'] if provider=='default' else provider
    pool=pool or f['worker']
    return OntologyRecall(pool,session,authorizer=EiosRecallAuthorizer(pool,session),provider=provider,
        expected_dimension=None if provider is None else provider.dimension,**options)


def refs(hits):return [h.ref for h in hits]


def test_definition_recall_three_methods_contract_shape(recall_db):
    f=recall_db;recall=recall_for(f,f['reader'])
    result=recall.recall('关注防水功能',instances=False)
    assert result.methods==('vector','fts','trgm') and result.profile_id=='nexloop-test-ngram-v1@64'
    assert result.definitions[0].ref=='eios:property:Consumer/waterproof_concern'
    assert set(result.definitions[0].method_scores)>={'vector','fts'}
    assert len(result.definitions)<=5
    for item in result.contract():assert not list(RECALL_ITEM.iter_errors([item]))
    budget=recall.recall('预算两千元以内',instances=False)
    top=refs(budget.definitions)
    assert vocabulary_ref('Consumer','budget_level','两千元以内') in top[:2] and 'eios:property:Consumer/budget_level' in top
    assert refs(recall.recall('消费者',instances=False).definitions)[0]=='eios:object_type:Consumer'
    # top-k is configuration, not a constant.
    assert len(recall_for(f,f['reader'],config=RecallConfig(top_k=2)).recall('顾客 防水 预算',instances=False).definitions)==2


def test_fts_and_trgm_only_mode_without_provider(recall_db):
    f=recall_db;result=recall_for(f,f['reader'],provider=None).recall('关注防水',instances=False)
    assert result.methods==('fts','trgm') and result.profile_id is None
    assert result.definitions[0].ref=='eios:property:Consumer/waterproof_concern'
    assert all('vector' not in h.method_scores for h in result.definitions)


def test_sensitive_property_invisible_including_vector_route(recall_db):
    """AT-003 recall part: no property READ → not recalled by any method; no text returned."""
    f=recall_db;recall=recall_for(f,f['reader'])
    for text in ('月收入','敏感：顾客自述的月收入','昵称'):
        result=recall.recall(text)
        assert not any('monthly_income' in r or 'nickname' in r for r in refs(result.definitions))
    # Instance alias field 'nickname' has no definition-level READ: the exact
    # alias text (identical vector) must not surface the instance at all.
    import math
    hidden=recall.recall('防水达人',definitions=False).instances
    assert all('alias' not in h.fields and set(h.method_scores)=={'vector'} for h in hidden)
    # Only the readable title row is a (low) vector neighbour; its score is
    # exactly the title similarity, so the hidden alias contributed nothing.
    q,title=f['provider'].embed('防水达人'),f['provider'].embed('张伟')
    for h in hidden:assert math.isclose(h.method_scores['vector'],max(0.0,sum(a*b for a,b in zip(q,title))),abs_tol=1e-5)
    for hit in recall.recall('张伟').instances+recall.recall('关注防水').definitions:
        assert set(vars(hit))=={'ref','method','score','kind','method_scores','fields'}
    # Granting the definition-level property makes the same instance reachable.
    granted,_=seed_multi_authority(f['admin'],f['worker'],reader_targets([f['ids']['zhang']],props=DEF_PROPS+('nickname',)),identity_suffix='-nick',tenant=A)
    assert refs(recall_for(f,granted).recall('防水达人',definitions=False).instances)[0]=='eios:object:Consumer/'+f['ids']['zhang']


def test_instance_post_check_requires_current_object_and_property_read(recall_db):
    f=recall_db;recall=recall_for(f,f['reader'])
    hits=recall.recall('张伟',definitions=False).instances
    assert refs(hits)==['eios:object:Consumer/'+f['ids']['zhang']]
    # 张薇 matches 张/name but its object READ is not granted.
    assert 'eios:object:Consumer/'+f['ids']['zhangwei2'] not in refs(recall.recall('张薇',definitions=False).instances)
    # Missing per-object property READ for the matched field also hides the hit.
    partial,_=seed_multi_authority(f['admin'],f['worker'],reader_targets([f['ids']['zhang']],object_props=('member_no',)),identity_suffix='-partial',tenant=A)
    assert recall_for(f,partial).recall('张伟',definitions=False).instances==()


def test_cross_tenant_and_world_isolation(recall_db):
    """AT-025: other tenant/world rows are excluded before scoring and by the return check."""
    f=recall_db;recall=recall_for(f,f['reader'])
    result=recall.recall('防水等级 关注防水功能的程度')
    assert not any('waterproof_rating' in r for r in refs(result.definitions))
    strong=recall.recall('会员M-1001',strong_ids=[{'key':'member_no','value':'m-1001 '}],definitions=False)
    assert strong.instances[0].ref=='eios:object:Consumer/'+f['ids']['zhang'] and strong.instances[0].method=='strong_id'
    assert strong.instances[0].score==1.0
    assert {f['ids']['other'],f['ids']['sim']}.isdisjoint(r.rsplit('/',1)[1] for r in refs(strong.instances))
    # A verified session consumer is a strong identifier, but only inside this tenant/world.
    verified=recall.recall('你好',verified_refs=['eios:object:Consumer/'+f['ids']['zhang'],'eios:object:Consumer/'+f['ids']['other'],
        'eios:object:Consumer/'+f['ids']['sim']],definitions=False).instances
    assert verified[0].ref=='eios:object:Consumer/'+f['ids']['zhang'] and verified[0].method=='strong_id'
    assert {f['ids']['other'],f['ids']['sim']}.isdisjoint(r.rsplit('/',1)[1] for r in refs(verified))
    # Simulation world: own session sees only the simulation instance.
    sim,_=seed_multi_authority(f['admin'],f['worker'],reader_targets([f['ids']['sim']]),identity_suffix='-sim-reader',world='simulation',tenant=A)
    sim_hits=recall_for(f,sim).recall('张伟',strong_ids=[{'key':'member_no','value':'M-1001'}],definitions=False).instances
    assert refs(sim_hits)==['eios:object:Consumer/'+f['ids']['sim']]
    # Tenant B reader with the same names sees only tenant B.
    other,_=seed_multi_authority(f['admin'],f['worker'],reader_targets([f['ids']['other']],props=('display_name','member_no','waterproof_rating')),identity_suffix='-b-reader',tenant=B)
    b=recall_for(f,other,provider=None).recall('关注防水功能',strong_ids=[{'key':'member_no','value':'M-1001'}])
    assert refs(b.definitions)[0]=='eios:property:Consumer/waterproof_rating'
    assert refs(b.instances)==['eios:object:Consumer/'+f['ids']['other']]
    # The credential is not valid for another world: SQL derives nothing.
    with f['worker'].connection() as c,pytest.raises(psycopg.errors.InsufficientPrivilege,match='authentication denied'):
        c.execute("select control.nexloop_recall_gates(%s,'simulation','instance')",(f['reader'].token_digest,))
    # Application roles cannot read the index directly (RLS + no grants).
    with f['api'].connection() as c,pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute('select * from ontology.nexloop_recall_entries')


def test_return_check_rejects_foreign_rows(recall_db,monkeypatch):
    f=recall_db;recall=recall_for(f,f['reader'],provider=None);original=recall._db.call
    def tamper(function,*args):
        rows=original(function,*args)
        if function=='nexloop_recall_search' and rows:rows[0]=dict(rows[0],tenant_id=B)
        return rows
    monkeypatch.setattr(recall._db,'call',tamper)
    with pytest.raises(PermissionError,match='return check'):recall.recall('关注防水',instances=False)


def test_dimension_fixed_and_mismatch_fails_closed(recall_db):
    f=recall_db;admin=f['admin'];reader=f['reader']
    with pytest.raises(EmbeddingDimensionMismatch):
        OntologyRecall(f['worker'],reader,authorizer=EiosRecallAuthorizer(f['worker'],reader),provider=f['provider'],expected_dimension=32)
    with pytest.raises(EmbeddingDimensionMismatch):RecallIndexer(f['worker'],reader,provider=f['provider'],expected_dimension=None)
    other=DeterministicTestEmbeddingProvider(32)
    # Provider agrees with its own config but not with the frozen index profile: no silent FTS fallback.
    with pytest.raises(psycopg.Error,match='dimension mismatch'):recall_for(f,reader,provider=other).recall('关注防水')
    indexer=RecallIndexer(f['worker'],f['indexers'][(A,'real')],provider=other,expected_dimension=32)
    with pytest.raises(psycopg.Error,match='explicit replacement'):indexer.activate_profile()
    with pytest.raises(psycopg.Error,match='dimension mismatch'):indexer.index_object_type('Consumer')
    # Vectors of another length never enter the index.
    entry=admin.execute("select entry_id from ontology.nexloop_recall_entries where tenant_id=%s and field='name' limit 1",(A,)).fetchone()[0]
    admin.execute("select set_config('eios.tenant_id',%s,false)",(A,))
    with pytest.raises(psycopg.Error,match='dimension mismatch'):
        admin.execute("insert into ontology.nexloop_recall_embeddings values(%s,%s,'nexloop-test-ngram-v1@64','[1,2,3]')",(entry,A))
    admin.execute("select set_config('eios.tenant_id','',false)")
    class Liar(DeterministicTestEmbeddingProvider):
        def embed(self,text):return (1.0,)*16
    with pytest.raises(EmbeddingDimensionMismatch):RecallIndexer(f['worker'],f['indexers'][(A,'real')],provider=Liar(64),expected_dimension=64).index_object_type('Consumer')
    # Explicit replacement switches profile; old vectors are not mixed in.
    assert indexer.activate_profile(replacing='nexloop-test-ngram-v1@64')=='nexloop-test-ngram-v1@32'
    assert all('vector' not in h.method_scores for h in recall_for(f,reader,provider=other).recall('关注防水',instances=False).definitions)
    indexer.index_object_type('Consumer')
    assert recall_for(f,reader,provider=other).recall('关注防水',instances=False).definitions[0].method_scores.get('vector',0)>0


def test_reflow_alias_new_definition_and_new_instance(recall_db):
    """AT-069 recall part: after publish/merge reflow, recall hits the new definition or alias."""
    f=recall_db;admin=f['admin'];indexer=RecallIndexer(f['worker'],f['indexers'][(A,'real')],provider=f['provider'],expected_dimension=64)
    recall=recall_for(f,f['reader'])
    before=recall.recall('防泼水',instances=False).definitions
    assert not before or before[0].score<0.5
    assert indexer.index_alias('alias-waterproof-1','eios:property:Consumer/waterproof_concern','防泼水')==1
    after=recall.recall('防泼水',instances=False).definitions[0]
    assert after.ref=='eios:property:Consumer/waterproof_concern' and after.score>0.9 and 'alias' in after.fields
    with pytest.raises(psycopg.Error,match='target unavailable'):indexer.index_alias('alias-x','eios:property:Consumer/no_such','别名')
    # Publish v2 with a new property: stale v1 rows drop out until reflow, alias survives.
    put_schema(admin,A,consumer_schema(2,(PropertyDefinition(property_name='sport_preference',value_type=PropertyValueType.STRING,display_name='运动偏好',description='喜欢的户外运动'),)))
    # The publish changes the authority directory: the old session fails closed
    # instead of looking like "no match"; the same credential re-authenticates.
    with pytest.raises(RecallUnavailable):recall.recall('防泼水',instances=False)
    recall=recall_for(f,authenticate_service(f['worker'],f['reader_token'],world='real'))
    assert refs(recall.recall('防泼水',instances=False).definitions)==['eios:property:Consumer/waterproof_concern']
    assert indexer.index_object_type('Consumer')>0
    assert recall.recall('喜欢户外跑步 运动偏好',instances=False).definitions[0].ref=='eios:property:Consumer/sport_preference'
    # New instance reflow and stale revision handling.
    new=put_object(admin,A,'real','a-new',{'display_name':'李娜','member_no':'M-2001'})
    assert indexer.index_instance('Consumer',new,spec=INSTANCE_SPEC)>0
    reader,_=seed_multi_authority(admin,f['worker'],reader_targets([new]),identity_suffix='-new-reader',tenant=A)
    assert refs(recall_for(f,reader).recall('李娜',definitions=False).instances)==['eios:object:Consumer/'+new]
    admin.execute("update ontology.objects set properties=properties||'{\"display_name\":\"李娜娜\"}',nexloop_revision=2 where tenant_id=%s and object_id=%s",(A,new))
    assert recall_for(f,reader).recall('李娜',definitions=False).instances==()
    assert indexer.index_instance('Consumer',new,spec=INSTANCE_SPEC)>0
    assert refs(recall_for(f,reader).recall('李娜娜',definitions=False).instances)==['eios:object:Consumer/'+new]
    assert indexer.remove_source('alias:alias-waterproof-1')==1
    recall=recall_for(f,authenticate_service(f['worker'],f['reader_token'],world='real'))
    assert all('alias' not in h.fields for h in recall.recall('防泼水',instances=False).definitions)


def test_index_reflow_is_domain_worker_only_and_derived_from_storage(recall_db):
    f=recall_db
    api_session,_=seed_multi_authority(f['admin'],f['api'],[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,READ)],identity_suffix='-api',tenant=A)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):RecallIndexer(f['api'],api_session).index_object_type('Consumer')
    # API role may search.
    assert recall_for(f,api_session,pool=f['api'],provider=None).recall('消费者',instances=False).definitions[0].ref=='eios:object_type:Consumer'
    indexer=RecallIndexer(f['worker'],f['indexers'][(A,'real')],provider=f['provider'],expected_dimension=64)
    # Vectors must cover exactly the SQL-derived rows; foreign text cannot be injected.
    with f['worker'].connection() as c,pytest.raises(psycopg.Error,match='do not cover'):
        c.execute('select control.nexloop_recall_index_definition(%s,%s,%s,1,%s,64,%s)',(f['indexers'][(A,'real')].token_digest,'real','Consumer',
            'nexloop-test-ngram-v1@64',Jsonb([{'ref':'eios:property:Consumer/injected','field':'name','body':'注入文本','vector':'['+','.join(['0.1']*64)+']'}])))
    with pytest.raises(psycopg.Error,match='undeclared'):indexer.index_instance('Consumer',f['ids']['zhang'],spec={'name_fields':['not_declared']})
    with pytest.raises(psycopg.Error,match='unavailable'):indexer.index_instance('Consumer',f['ids']['other'])
