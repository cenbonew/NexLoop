from concurrent.futures import ThreadPoolExecutor
import pytest
import psycopg
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.errors import AuthorizationUnavailable
from eios.ontology.models import RelationTypeDefinition,RelationCardinality,PropertyDefinition,PropertyValueType
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.relation_actions import GovernedRelationLinker
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from authority_fixture import replace_fact
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action


@pytest.fixture
def linker(published_action,admin,request):
    reader,definition,capability=published_action
    creator=GovernedObjectCreator(reader.pool,reader.session,reader.signer)
    ids=[creator.create(action_name='Consumer.create',action_version=1,intent_id='synthetic-relation-object-'+str(i),type_name='Consumer',properties={})['object_id'] for i in range(3)]
    cardinality=getattr(request,'param',RelationCardinality.MANY_TO_MANY)
    schema=RelationTypeDefinition(relation_name='Peer',source_type='Consumer',target_type='Consumer',cardinality=cardinality,
        properties=(PropertyDefinition(property_name='reason',value_type=PropertyValueType.STRING),))
    binding=definition.capability_binding.model_copy(update={'capability_name':'ontology.relation.link'})
    definition=definition.model_copy(update={'stable_name':'Peer.link','capability_binding':binding})
    capability=capability.model_copy(update={'capability_name':'ontology.relation.link'})
    admin.execute('insert into ontology.relation_type_versions(tenant_id,relation_name,version,definition) values(%s,%s,%s,%s)',
        ('synthetic-a','Peer',1,Jsonb(schema.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
        ('synthetic-a','real','eios:action:Peer.link:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_relation_action_bindings values(%s,%s,%s,%s,%s,%s,%s)',
        ('synthetic-a','real','eios:action:Peer.link:1',definition.reference().contract_digest,'Peer',1,schema_contract_digest(schema)))
    targets=[('eios:action:Peer.link:1',ResourceType.ACTION,Operation.EXECUTE),('eios:relation:Peer',ResourceType.RELATION,Operation.CREATE)]
    targets += [('eios:object:Consumer/'+obj,ResourceType.OBJECT,Operation.EDIT) for obj in ids]
    session,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-linker')
    return GovernedRelationLinker(reader.pool,session,reader.signer),ids,token


def args(ids,intent='synthetic-link-001',**changes):
    return dict(action_name='Peer.link',action_version=1,intent_id=intent,relation_name='Peer',source_id=ids[0],target_id=ids[1],metadata={'reason':'synthetic'},**changes)


def test_actual_relation_link_atomic_and_replay(linker,admin):
    service,ids,_=linker
    receipt=service.link(**args(ids))
    assert service.link(**args(ids))==receipt
    assert service.link(**args(ids,'synthetic-link-002'))==receipt
    row=admin.execute('select relation_id,world,source_object_id,target_object_id,metadata from ontology.relations').fetchone()
    assert row==(receipt['relation_id'],'real',ids[0],ids[1],{'reason':'synthetic'})
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='Peer.link' and claim->>'state'='terminal'").fetchone()[0]==2
    with service.pool.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select * from ontology.relations')


@pytest.mark.parametrize('linker',[RelationCardinality.ONE_TO_ONE,RelationCardinality.ONE_TO_MANY,RelationCardinality.MANY_TO_ONE],indirect=True)
def test_cardinality_conflict_does_not_add_relation(linker,admin):
    service,ids,_=linker
    service.link(**args(ids))
    other=args(ids,'synthetic-cardinality')
    cardinality=admin.execute('select definition from ontology.relation_type_versions').fetchone()[0]['cardinality']
    if cardinality=='one_to_many':other['source_id']=ids[2]
    else:other['target_id']=ids[2]
    with pytest.raises(psycopg.errors.DataException):service.link(**other)
    assert admin.execute('select count(*) from ontology.relations').fetchone()[0]==1


def test_endpoint_revocation_fresh_login_denies_link(linker,admin):
    service,ids,token=linker
    replace_fact(admin,'synthetic-a','grants',['synthetic-a-linker-principal','eios:object:Consumer/'+ids[1]],F.GrantFacts,grants=[])
    service.session=authenticate_service(service.pool,token,world='real')
    with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied)):service.link(**args(ids))
    assert admin.execute('select count(*) from ontology.relations').fetchone()[0]==0


def test_wrong_endpoint_authority_denied(linker,admin):
    service,ids,_=linker;command=args(ids);command['target_id']='f'*64
    with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied)):service.link(**command)
    assert admin.execute('select count(*) from ontology.relations').fetchone()[0]==0


def test_published_relation_schema_immutable(linker,admin):
    with pytest.raises(psycopg.errors.DataException):admin.execute("update ontology.relation_type_versions set definition=definition||jsonb_build_object('description','changed')")
    with pytest.raises(psycopg.errors.DataException):admin.execute("update control.nexloop_relation_action_bindings set schema_digest=repeat('f',64)")


def test_invalid_metadata_rejected_before_claim(linker,admin):
    service,ids,_=linker;command=args(ids);command['metadata']={'reason':42}
    count=admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]
    with pytest.raises(ValueError):service.link(**command)
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==count


def test_two_concurrent_pg_requests_same_pair_have_one_relation(linker,admin):
    service,ids,_=linker
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts=list(executor.map(lambda n:service.link(**args(ids,'synthetic-concurrent-'+str(n))),range(2)))
    assert receipts[0]==receipts[1]
    assert admin.execute('select count(*) from ontology.relations').fetchone()[0]==1


def test_pair_metadata_conflict_never_overwrites(linker,admin):
    service,ids,_=linker
    service.link(**args(ids))
    command=args(ids,'synthetic-pair-conflict');command['metadata']={'reason':'changed'}
    with pytest.raises(psycopg.errors.DataException):service.link(**command)
    assert admin.execute('select metadata from ontology.relations').fetchone()[0]=={'reason':'synthetic'}


def test_endpoint_revoked_after_proof_before_dispatch(linker,admin,monkeypatch):
    import nexloop_eios.relation_actions as relations
    service,ids,_=linker;original=relations.canonical_payload
    def race(value):
        text=original(value)
        if value.get('protocol')=='nexloop-relation-link-v1':
            replace_fact(admin,'synthetic-a','grants',['synthetic-a-linker-principal','eios:object:Consumer/'+ids[1]],F.GrantFacts,grants=[])
        return text
    monkeypatch.setattr(relations,'canonical_payload',race)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):service.link(**args(ids))
    assert admin.execute('select count(*) from ontology.relations').fetchone()[0]==0


def test_missing_relation_binding_denies_before_claim(linker,admin):
    service,ids,token=linker
    admin.execute('delete from control.nexloop_relation_action_bindings')
    service.session=authenticate_service(service.pool,token,world='real')
    count=admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]
    with pytest.raises(ActionAuthorizationDenied):service.link(**args(ids))
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==count


def test_same_endpoint_ids_in_wrong_world_denied(linker,admin):
    service,ids,_=linker
    row=admin.execute('select resource_id,definition,capability from control.nexloop_action_definitions where resource_id=%s',('eios:action:Peer.link:1',)).fetchone()
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',('synthetic-a','test',row[0],Jsonb(row[1]),Jsonb(row[2])))
    binding=admin.execute('select action_contract_digest,relation_name,relation_version,schema_digest from control.nexloop_relation_action_bindings').fetchone()
    admin.execute('insert into control.nexloop_relation_action_bindings values(%s,%s,%s,%s,%s,%s,%s)',('synthetic-a','test',row[0],*binding))
    targets=[('eios:action:Peer.link:1',ResourceType.ACTION,Operation.EXECUTE),('eios:relation:Peer',ResourceType.RELATION,Operation.CREATE)]
    targets += [('eios:object:Consumer/'+obj,ResourceType.OBJECT,Operation.EDIT) for obj in ids]
    session,_=seed_multi_authority(admin,service.pool,targets,identity_suffix='-test-world',world='test')
    wrong=GovernedRelationLinker(service.pool,session,service.signer)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):wrong.link(**args(ids))
    assert admin.execute('select count(*) from ontology.relations').fetchone()[0]==0
