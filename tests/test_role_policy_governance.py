from datetime import UTC,datetime,timedelta
from pathlib import Path
import pytest,psycopg,json
from psycopg.types.json import Jsonb
from test_action_definitions import published_action
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.object_actions import GovernedObjectCreator
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
from eios.ontology.semantics import schema_contract_digest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType

@pytest.fixture
def policies(published_action,admin,pg):
    reader,base,capability=published_action
    targets=[]
    from nexloop_eios.role_policies import role_policy_schemas
    for schema in role_policy_schemas():
        kind=schema.type_name
        ref=base.object_types[0].model_copy(update={'stable_name':kind,'schema_digest':schema_contract_digest(schema)})
        definition=base.model_copy(update={'stable_name':kind+'.create','required_scopes':('ontology.roles.manage',),'object_types':(ref,),'governance':base.governance.model_copy(update={'change_scope':base.governance.change_scope.model_copy(update={'object_types':(ref,)})})})
        admin.execute('insert into ontology.object_type_versions values(%s,%s,%s,%s)',('synthetic-a',kind,1,Jsonb(schema.model_dump(mode='json'))))
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',('synthetic-a','real','eios:action:'+kind+'.create:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_copy(update={'required_scopes':('ontology.roles.manage',)}).model_dump(mode='json'))))
        targets.append(('eios:action:'+kind+'.create:1',ResourceType.ACTION,Operation.EXECUTE))
    targets.append(('eios:action:Consumer.create:1',ResourceType.ACTION,Operation.EXECUTE))
    session,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-role-maintainer',extra_scopes=('ontology.roles.manage',))
    creator=GovernedObjectCreator(reader.pool,session,reader.signer)
    def create(kind,intent,properties):return creator.create(action_name=kind+'.create',action_version=1,intent_id=intent,type_name=kind,properties=properties)
    create.token=token;create.pg=pg
    now=datetime.now(UTC);valid=dict(active=True,valid_from=(now-timedelta(seconds=1)).isoformat(),valid_until=(now+timedelta(hours=1)).isoformat())
    return create,valid,reader

from test_role_policy_types import policy

def test_actual_governed_policy_creation(policies,admin):
    create,valid,reader=policies
    result=create('RoleExecutionCeiling','ceiling-create',policy())
    assert admin.execute("select properties->'effect_units' from ontology.objects where object_id=%s",(result['object_id'],)).fetchone()[0]==1

def test_generic_action_cannot_implicitly_manage_policy(policies,admin):
    create,valid,reader=policies
    session,_=seed_multi_authority(admin,reader.pool,[('eios:action:RoleExecutionCeiling.create:1',ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-ordinary-policy')
    creator=GovernedObjectCreator(reader.pool,session,reader.signer)
    with pytest.raises(Exception):creator.create(action_name='RoleExecutionCeiling.create',action_version=1,intent_id='unauthorized-policy',type_name='RoleExecutionCeiling',properties=policy())
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleExecutionCeiling'").fetchone()==(0,)

@pytest.mark.parametrize('field,value',[('goal_ids',[]),('action_resources',['*']),('effect_units',0)])
def test_sql_rejects_invalid_policy_without_typed_port(policies,admin,field,value):
    create,valid,reader=policies
    p=policy();p[field]=value
    with pytest.raises(Exception):create('RoleExecutionCeiling','invalid-policy',p)
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleExecutionCeiling'").fetchone()==(0,)
