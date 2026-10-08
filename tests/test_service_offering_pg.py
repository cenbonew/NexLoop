"""Actual 50 technical publication and real governed CREATE/READ supply catalog.

Only synthetic authority/schema config is installed; no admin business writes.
This first slice does not prove Runtime integration or dispatch enforcement.
"""
import json,secrets,uuid
from pathlib import Path
from datetime import UTC,datetime,timedelta
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.applications import ResourceRestriction,OperationRestriction
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import ObjectTypeDefinition
from nexloop_eios.backend import open_backend
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.service_offerings import offering_schemas,json_export_example,read_catalog,assess_request_scope,OFFERING_FIELDS,BINDING_FIELDS,governed_scope
from nexloop_eios.trusted_configuration import apply_manifest
from authority_fixture import authority_records,replace_fact
from test_trusted_configuration_pg import configured
from test_business_setup_pg import declared_service
from test_postgres_action_claims import governance_inputs


def read_declaration(tenant,targets):
    records={};apps=[]
    for target,kind in targets:
        binding,expiry,rows=authority_records(tenant,target,operation=Operation.READ,resource_type=kind,identity_suffix='-catalog-reader')
        for name,key,fact in rows:
            if name=='application':apps.append(fact)
            records[name,tuple(key)]=(name,key,fact)
    scopes=frozenset(kind.value+'.read' for unused,kind in targets)
    app=apps[0].model_copy(update={'resources':tuple(ResourceRestriction(tenant_id=tenant,resource_type=kind.value,resource_id=target) for target,kind in targets),'operations':(OperationRestriction(operation=Operation.READ),)})
    binding=binding.model_copy(update={'caller_application_digest':app.version_digest,'requested_scopes':scopes})
    for index,(name,key,fact) in list(records.items()):
        if name=='application':fact=app
        if name=='scope':fact=fact.model_copy(update={'catalog_scopes':scopes,'authorized_scopes':scopes,'catalog_digest':F.canonical_authority_digest({'scopes':sorted(scopes)})})
        if name=='authentication':fact=F.CredentialAuthenticationFacts(**binding.model_dump(),status='active',expires_at=expiry,repository_witness=fact.repository_witness)
        body=fact.model_dump(mode='json');body.pop('snapshot_digest',None)
        records[index]=(name,key,type(fact).model_validate_json(json.dumps(body)))
    return binding,expiry,[{'kind':name,'key':key,'payload':fact.model_dump(mode='json')} for name,key,fact in records.values()]

@pytest.fixture
def catalog(configured,admin,tmp_path):
    f=configured;tenant=f['tenant'];schemas=list(offering_schemas())+[ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True)]
    base=governance_inputs();original=base['action_definition'];cap=base['capability_snapshot'];actions=[]
    for schema in schemas:
        name=schema.type_name+'.create'
        definition=json.loads(json.dumps(original.model_dump(mode='json')).replace('synthetic-a',tenant));definition.pop('contract_digest',None)
        ref={**definition['object_types'][0],'stable_name':schema.type_name,'schema_digest':schema_contract_digest(schema)}
        definition['stable_name']=name;definition['object_types']=[ref];definition['governance']['change_scope']['object_types']=[ref]
        actions.append({'definition':type(original).model_validate_json(json.dumps(definition)).model_dump(mode='json'),'capability':cap.model_dump(mode='json')})
    creator,expiry,facts=declared_service(tenant,[s.type_name+'.create' for s in schemas],'-catalog-creator');token=secrets.token_urlsafe(48)
    manifest={**f['manifest'],'manifest_id':str(uuid.uuid4()),'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0],
      'object_types':[s.model_dump(mode='json') for s in schemas],'actions':actions,'identity_allowances':[],
      'authority_facts':list({(r['kind'],tuple(r['key'])):r for r in f['manifest']['authority_facts']+facts}.values()),'service_credentials':f['manifest']['service_credentials']+[{'reference':'catalog-creator','binding':creator.model_dump(mode='json'),'worlds':['real'],'expires_at':expiry.isoformat(),'status':'active'}]}
    f['paths']['secrets'].write_text(json.dumps({'source':f['token'],'catalog-creator':token}))
    options=dict(database_url_file=f['paths']['dsn'],signing_key_file=f['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=f['paths']['secrets'])
    apply_manifest(manifest,**options)
    config=dict(database_url=make_conninfo(f['pg'],user='nexloop_api'),artifact_root=tmp_path/'catalog-artifacts',signing_key_file=f['paths']['backend_signing'],signing_key_id='explicit-configuration')
    with open_backend(**config) as backend:
        source=backend.authenticate(token,world='real')
        consumer=source.create_object(action_name='Consumer.create',action_version=1,intent_id='offering-consumer',type_name='Consumer',properties={})
        offered=source.create_object(action_name='ServiceOffering.create',action_version=1,intent_id='offering-export',type_name='ServiceOffering',properties=json_export_example(valid_until=(datetime.now(UTC)+timedelta(minutes=5)).isoformat()))
        principal=tenant+'-catalog-reader-principal'
        linked=source.create_object(action_name='ConsumerServiceOffering.create',action_version=1,intent_id='offering-binding',type_name='ConsumerServiceOffering',properties={'consumer_id':consumer['object_id'],'offering_id':offered['object_id'],'offering_revision':1,'source_principal':principal,'active':True})
        targets=[]
        for receipt,fields in [(offered,OFFERING_FIELDS),(linked,BINDING_FIELDS)]:
            target=receipt['type_name']+'/'+receipt['object_id'];targets.append(('eios:object:'+target,ResourceType.OBJECT))
            targets.extend(('eios:property:'+target+'/'+field,ResourceType.PROPERTY) for field in fields)
        reader,readexpiry,readfacts=read_declaration(tenant,targets);reader_token=secrets.token_urlsafe(48)
        manifest={**manifest,'manifest_id':str(uuid.uuid4()),'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0],
          'authority_facts':list({(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']+readfacts}.values()),'service_credentials':manifest['service_credentials']+[{'reference':'catalog-reader','binding':reader.model_dump(mode='json'),'worlds':['real'],'expires_at':readexpiry.isoformat(),'status':'active'}]}
        f['paths']['secrets'].write_text(json.dumps({'source':f['token'],'catalog-creator':token,'catalog-reader':reader_token}));apply_manifest(manifest,**options)
        yield {'source':backend.authenticate(reader_token,world='real'),'tenant':tenant,'consumer':consumer['object_id'],'offering':offered['object_id'],'binding':linked['object_id'],'principal':principal}


def test_actual_governed_catalog_read_and_outside_terms_never_admit(catalog,admin):
    f=catalog;value=read_catalog(f['source'],offering_id=f['offering'],binding_id=f['binding'],consumer_id=f['consumer'])
    assert value['provenance']=='eios:object:'+f['offering'] and value['revision']==1
    for guarantees,discounts in [(['guaranteed-profit'],[]),([],['50%-discount'])]:
        scoped=assess_request_scope(value['properties'],{'offering_id':f['offering'],'offering_revision':1,'requested_guarantees':guarantees,'requested_discounts':discounts},offering_id=f['offering'],revision=1)
        assert scoped['allowed'] is False and scoped['scope']['evidence_kind']=='fsynced_json_export'
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.objects where type_name in ('Consumer','ServiceOffering','ConsumerServiceOffering')").fetchone()==(3,)


def test_actual_catalog_cross_consumer_and_read_revocation_refused(catalog,admin):
    f=catalog
    with pytest.raises(ValueError):read_catalog(f['source'],offering_id=f['offering'],binding_id=f['binding'],consumer_id='f'*64)
    target='eios:property:ServiceOffering/'+f['offering']+'/allowed_guarantees'
    replace_fact(admin,f['tenant'],'grants',[f['principal'],target],F.GrantFacts,grants=[])
    with pytest.raises((PermissionError,AuthorizationUnavailable)):read_catalog(f['source'],offering_id=f['offering'],binding_id=f['binding'],consumer_id=f['consumer'])


def test_actual_signed_sql_catalog_refusal_is_read_only_not_dispatch_permit(catalog,admin):
    f=catalog
    for terms,allowed in [([],True),(['unlimited-guarantee'],False)]:
        result=governed_scope(f['source'],offering_id=f['offering'],binding_id=f['binding'],consumer_id=f['consumer'],
         request_scope={'offering_id':f['offering'],'offering_revision':1,'requested_guarantees':terms,'requested_discounts':[]})
        assert result['allowed'] is allowed and result['dispatch_permit'] is False
        assert result['scope']['guarantees']==[] and result['scope']['discounts']==[]
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
