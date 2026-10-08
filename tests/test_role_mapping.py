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
def roles(published_action,admin,pg):
    reader,base,capability=published_action
    targets=[]
    from nexloop_eios.role_mapping import role_mapping_schemas
    for schema in role_mapping_schemas():
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

def test_actual_governed_roles_n_m_and_no_resident_runs(roles,admin):
    create,valid,reader=roles
    consumers=[create('Consumer','role-consumer-'+str(i),{})['object_id'] for i in range(2)]
    role_ids=[create('RoleDefinition','role-definition-'+str(i),dict(name='role'+str(i),responsibility='synthetic responsibility',ceiling_ref='eios:ceiling:declared-only',**valid))['object_id'] for i in range(2)]
    for i,c in enumerate(consumers):
        for j,r in enumerate(role_ids):
            props=dict(consumer_id=c,role_id=r,scope='synthetic dispatch responsibility',**valid)
            first=create('ConsumerRoleLink',f'role-link-{i}-{j}',props)
            assert create('ConsumerRoleLink',f'role-link-{i}-{j}',props)==first
    assert admin.execute("select count(*) from ontology.objects where type_name='ConsumerRoleLink'").fetchone()==(4,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)
    with reader.pool.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute("update ontology.objects set properties='{}'")

@pytest.mark.parametrize('invalid',['endpoint','interval','naive'])
def test_role_invalid_mapping_rolls_back(roles,admin,invalid):
    create,valid,_=roles
    consumer=create('Consumer','invalid-consumer',{})['object_id']
    role=create('RoleDefinition','invalid-role',dict(name='role',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))['object_id']
    props=dict(consumer_id=consumer,role_id=role,scope='synthetic',**valid)
    if invalid=='endpoint':props['role_id']=consumer
    elif invalid=='interval':props['valid_from']=props['valid_until']
    else:props['valid_from']='2026-01-01T00:00:00'
    with pytest.raises(psycopg.Error):create('ConsumerRoleLink','invalid-mapping',props)
    assert admin.execute("select count(*) from ontology.objects where type_name='ConsumerRoleLink'").fetchone()==(0,)

def test_duplicate_pair_different_intent_is_rejected(roles,admin):
    create,valid,_=roles
    consumer=create('Consumer','duplicate-consumer',{})['object_id']
    role=create('RoleDefinition','duplicate-role',dict(name='role',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))['object_id']
    props=dict(consumer_id=consumer,role_id=role,scope='synthetic',**valid)
    create('ConsumerRoleLink','duplicate-first',props)
    with pytest.raises(psycopg.errors.UniqueViolation):create('ConsumerRoleLink','duplicate-second',props)
    assert admin.execute("select count(*) from ontology.objects where type_name='ConsumerRoleLink'").fetchone()==(1,)


def test_governed_end_and_actual_property_authorized_projection(roles,admin):
    from nexloop_eios.object_edits import GovernedObjectEditor
    from nexloop_eios.object_reads import AuthorizedObjectReader
    from nexloop_eios.authorization import authenticate_service
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    create,valid,reader=roles
    consumer=create('Consumer','end-consumer',{})['object_id']
    role=create('RoleDefinition','end-role',dict(name='role',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))['object_id']
    props=dict(consumer_id=consumer,role_id=role,scope='synthetic',**valid)
    link=create('ConsumerRoleLink','end-link',props)['object_id']
    from eios.ontology.definitions import ActionDefinition
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    raw=admin.execute("select definition,capability from control.nexloop_action_definitions where resource_id='eios:action:ConsumerRoleLink.create:1'").fetchone()
    definition,capability=ActionDefinition.model_validate_json(json.dumps(raw[0])),CapabilityContractSnapshot.model_validate_json(json.dumps(raw[1]))
    binding=definition.capability_binding.model_copy(update={'capability_name':'consumer.edit'})
    edit=definition.model_copy(update={'stable_name':'ConsumerRoleLink.edit','capability_binding':binding,'contract_digest':None})
    capability=capability.model_copy(update={'capability_name':'consumer.edit'})
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',('synthetic-a','real','eios:action:ConsumerRoleLink.edit:1',Jsonb(edit.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
    targets=[('eios:action:ConsumerRoleLink.edit:1',ResourceType.ACTION,Operation.EXECUTE),('eios:object:ConsumerRoleLink/'+link,ResourceType.OBJECT,Operation.EDIT),('eios:property:ConsumerRoleLink/'+link+'/active',ResourceType.PROPERTY,Operation.EDIT)]
    for kind,obj,names in [('ConsumerRoleLink',link,props),('RoleDefinition',role,dict(name=1,responsibility=1,ceiling_ref=1,**valid))]:
        targets.append(('eios:object:'+kind+'/'+obj,ResourceType.OBJECT,Operation.READ))
        targets += [('eios:property:'+kind+'/'+obj+'/'+n,ResourceType.PROPERTY,Operation.READ) for n in names]
    session,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-role-end-reader',extra_scopes=('ontology.roles.manage',))
    editor=GovernedObjectEditor(reader.pool,session,reader.signer)
    projection=AuthorizedObjectReader(reader.pool,session,reader.signer)
    row=projection.get('ConsumerRoleLink',link,fields=tuple(props));assert row['properties']['active'] is True
    args=dict(action_name='ConsumerRoleLink.edit',action_version=1,intent_id='end-role-link',type_name='ConsumerRoleLink',object_id=link,expected_revision=1,properties={'active':False})
    result=editor.edit(**args);assert editor.edit(**args)==result
    assert projection.get('ConsumerRoleLink',link,fields=tuple(props))['properties']['active'] is False
    before=admin.execute("select properties from ontology.objects where object_id=%s",(link,)).fetchone()
    replace_fact(admin,'synthetic-a','grants',[session.authentication.subject_principal_id,'eios:property:ConsumerRoleLink/'+link+'/scope'],F.GrantFacts,grants=[])
    with pytest.raises(Exception):projection.get('ConsumerRoleLink',link,fields=('scope',))
    assert admin.execute("select properties from ontology.objects where object_id=%s",(link,)).fetchone()==before

def test_management_scope_missing_is_denied_before_business_write(roles,admin):
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    _,valid,reader=roles
    session,_=seed_multi_authority(admin,reader.pool,[('eios:action:RoleDefinition.create:1',ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-ordinary-actor')
    creator=GovernedObjectCreator(reader.pool,session,reader.signer)
    before=admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone()
    with pytest.raises(Exception) as error:creator.create(action_name='RoleDefinition.create',action_version=1,intent_id='ordinary-must-not-manage',type_name='RoleDefinition',properties=dict(name='ordinary',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))
    assert 'scope' in str(error.value).lower()
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone()==before


def test_current_management_execute_revocation_denies_dispatch(roles,admin):
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    create,valid,reader=roles
    actor=create.__closure__[0].cell_contents.session.authentication.subject_principal_id
    replace_fact(admin,'synthetic-a','grants',[actor,'eios:action:RoleDefinition.create:1'],F.GrantFacts,grants=[])
    with pytest.raises(Exception):create('RoleDefinition','revoked-manager',dict(name='revoked',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone()==(0,)

@pytest.mark.parametrize('fault',['key','ttl','action'])
def test_actual_create_after_write_fault_rolls_back_business_and_terminal(roles,admin,monkeypatch,fault):
    import nexloop_eios.object_actions as module
    create,valid,reader=roles
    if fault=='ttl':
        class ShortAuthorityClock:
            calls=0
            @classmethod
            def now(cls,tz):
                cls.calls+=1
                return datetime.now(tz)-(timedelta(seconds=24.8) if cls.calls==2 else timedelta())
        monkeypatch.setattr(module,'datetime',ShortAuthorityClock)
        statement='perform pg_sleep(.35);'
    elif fault=='key':statement='update authz.nexloop_authority_signing_keys set active=false;'
    else:statement="update control.nexloop_action_definitions set active=false where resource_id='eios:action:RoleDefinition.create:1';"
    # Owned fault-injection changes authority metadata only, after the genuine
    # governed business INSERT. The outer command must roll back all its writes.
    admin.execute('create function ontology.test_role_after_write() returns trigger language plpgsql security definer set search_path=pg_catalog as $$ begin '+statement+' return new;end $$')
    admin.execute("create trigger test_role_after_write after insert on ontology.objects for each row when(new.type_name='RoleDefinition') execute function ontology.test_role_after_write()")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):create('RoleDefinition','tail-fault-role',dict(name='fault',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='RoleDefinition.create' and claim->>'state'='terminal'").fetchone()==(0,)
    assert admin.execute('select bool_and(active) from authz.nexloop_authority_signing_keys').fetchone()==(True,)

def test_actual_agent_cannot_acquire_management_from_consumer_grant(roles,admin):
    from agent_authority_fixture import seed_agent
    from nexloop_eios.authorization import authenticate_service
    from eios.authz.errors import AuthorizationUnavailable
    from eios.authz.facts import AuthorizationFactDenied
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    _,valid,reader=roles
    token,_=seed_agent(admin)
    session=authenticate_service(reader.pool,token,world='real')
    assert session.authentication.subject_kind.value=='agent'
    creator=GovernedObjectCreator(reader.pool,session,reader.signer)
    with pytest.raises((AuthorizationUnavailable,AuthorizationFactDenied,ActionAuthorizationDenied)):
        creator.create(action_name='RoleDefinition.create',action_version=1,intent_id='agent-not-manager',type_name='RoleDefinition',properties=dict(name='agent',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone()==(0,)


def test_fixed_role_port_uses_actual_backend_and_validity(roles,tmp_path):
    from nexloop_eios.role_mapping import RoleMappingPort
    from nexloop_eios.backend import open_backend
    from psycopg.conninfo import make_conninfo
    create,valid,reader=roles
    key=tmp_path/'owned-role-signing-key';key.write_bytes(reader.signer.material);key.chmod(0o600)
    with open_backend(database_url=make_conninfo(create.pg,user='nexloop_api'),artifact_root=tmp_path/'role-artifacts',signing_key_file=key,signing_key_id=reader.signer.key_id) as backend:
        service=backend.authenticate(create.token,world='real');port=RoleMappingPort(service)
        args=dict(intent_id='fixed-role-port',name='fixed',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',valid_from=valid['valid_from'],valid_until=valid['valid_until'])
        role=port.create_role(**args);assert port.create_role(**args)==role
        consumer=service.create_object(action_name='Consumer.create',action_version=1,intent_id='fixed-port-consumer',type_name='Consumer',properties={})
        link=port.assign(intent_id='fixed-port-link',consumer_id=consumer['object_id'],role_id=role['object_id'],scope='synthetic',valid_from=valid['valid_from'],valid_until=valid['valid_until'])
        assert link['type_name']=='ConsumerRoleLink'
        with pytest.raises(ValueError):port.create_role(**{**args,'valid_from':'2026-01-01T00:00:00'})

def test_generic_create_without_management_contract_cannot_bypass_role_guard(roles,admin):
    from eios.ontology.definitions import ActionDefinition
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    _,valid,reader=roles
    raw=admin.execute("select definition,capability from control.nexloop_action_definitions where resource_id='eios:action:RoleDefinition.create:1'").fetchone()
    definition=ActionDefinition.model_validate_json(json.dumps(raw[0])).model_copy(update={'stable_name':'RoleDefinition.unprivileged','required_scopes':(),'contract_digest':None})
    capability=CapabilityContractSnapshot.model_validate_json(json.dumps(raw[1])).model_copy(update={'required_scopes':()})
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',('synthetic-a','real','eios:action:RoleDefinition.unprivileged:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
    session,_=seed_multi_authority(admin,reader.pool,[('eios:action:RoleDefinition.unprivileged:1',ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-non-management-create')
    creator=GovernedObjectCreator(reader.pool,session,reader.signer)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):creator.create(action_name='RoleDefinition.unprivileged',action_version=1,intent_id='generic-bypass-must-fail',type_name='RoleDefinition',properties=dict(name='bypass',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone()==(0,)

def test_current_scope_authority_removal_denies_even_requested_manage_still_present(roles,admin):
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    from eios.authz.errors import AuthorizationUnavailable
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    create,valid,_=roles
    session=create.__closure__[0].cell_contents.session
    assert 'ontology.roles.manage' in session.authentication.requested_scopes
    replace_fact(admin,'synthetic-a','scope',[session.authentication.subject_principal_id,'eios:action:RoleDefinition.create:1','execute'],F.ScopeAuthorityFacts,authorized_scopes=sorted(session.authentication.requested_scopes-frozenset({'ontology.roles.manage'})))
    from nexloop_eios.authorization import authenticate_service
    reader=roles[2]
    refreshed=authenticate_service(reader.pool,create.token,world='real')
    assert 'ontology.roles.manage' in refreshed.authentication.requested_scopes
    fresh_creator=GovernedObjectCreator(reader.pool,refreshed,reader.signer)
    with pytest.raises((AuthorizationUnavailable,F.AuthorizationFactDenied,ActionAuthorizationDenied)):
        fresh_creator.create(action_name='RoleDefinition.create',action_version=1,intent_id='current-scope-revoked',type_name='RoleDefinition',properties=dict(name='denied',responsibility='synthetic',ceiling_ref='eios:ceiling:declared-only',**valid))
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone()==(0,)

def test_fixed_port_configurable_duties_scope_and_authorized_current_projection(roles,admin,tmp_path):
    from eios.ontology.definitions import ActionDefinition
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    from nexloop_eios.role_mapping import RoleMappingPort
    from nexloop_eios.backend import open_backend
    from psycopg.conninfo import make_conninfo
    create,valid,reader=roles
    consumer=create('Consumer','configure-consumer',{})['object_id']
    role=create('RoleDefinition','configure-role',dict(name='initial',responsibility='initial duties',ceiling_ref='eios:ceiling:declared-only',**valid))['object_id']
    link=create('ConsumerRoleLink','configure-link',dict(consumer_id=consumer,role_id=role,scope='initial scope',**valid))['object_id']
    targets=[]
    for kind,obj,names in [('RoleDefinition',role,('name','responsibility','ceiling_ref','active','valid_from','valid_until')),('ConsumerRoleLink',link,('scope','active','valid_from','valid_until','consumer_id','role_id'))]:
        raw=admin.execute('select definition,capability from control.nexloop_action_definitions where resource_id=%s',('eios:action:'+kind+'.create:1',)).fetchone()
        definition=ActionDefinition.model_validate_json(json.dumps(raw[0]));capability=CapabilityContractSnapshot.model_validate_json(json.dumps(raw[1]))
        binding=definition.capability_binding.model_copy(update={'capability_name':'consumer.edit'})
        definition=definition.model_copy(update={'stable_name':kind+'.edit','capability_binding':binding,'contract_digest':None});capability=capability.model_copy(update={'capability_name':'consumer.edit'})
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',('synthetic-a','real','eios:action:'+kind+'.edit:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
        targets.append(('eios:action:'+kind+'.edit:1',ResourceType.ACTION,Operation.EXECUTE))
        for operation in (Operation.READ,Operation.EDIT):
            targets.append(('eios:object:'+kind+'/'+obj,ResourceType.OBJECT,operation))
            targets.extend(('eios:property:'+kind+'/'+obj+'/'+n,ResourceType.PROPERTY,operation) for n in names)
    _,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-configurable-role-maintainer',extra_scopes=('ontology.roles.manage',))
    key=tmp_path/'configurable-role-key';key.write_bytes(reader.signer.material);key.chmod(0o600)
    with open_backend(database_url=make_conninfo(create.pg,user='nexloop_api'),artifact_root=tmp_path/'configurable-role-artifacts',signing_key_file=key,signing_key_id=reader.signer.key_id) as backend:
        service=backend.authenticate(token,world='real');port=RoleMappingPort(service)
        initial=port.read(link_id=link);assert initial['currently_applicable'] is True and initial['grants_authority'] is False
        port.edit_role(intent_id='role-configured-duties',role_id=role,expected_revision=1,name='configured',responsibility='configured duties',ceiling_ref='eios:ceiling:still-declaration',**valid)
        port.configure_link(intent_id='role-configured-scope',link_id=link,expected_revision=1,scope='configured scope',**valid)
        current=port.read(link_id=link);assert current['currently_applicable'] is True
        assert current['role']['properties']['responsibility']=='configured duties' and current['mapping']['properties']['scope']=='configured scope'
        port.end(intent_id='role-end-configured-link',link_id=link,expected_revision=2)
        assert port.read(link_id=link)['currently_applicable'] is False
