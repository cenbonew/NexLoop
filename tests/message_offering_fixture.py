"""Owned synthetic configuration; catalog instances exclusively genuine GovCREATE."""
import copy,hashlib,json,secrets,uuid
from datetime import UTC,datetime,timedelta
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.service_offerings import offering_schemas,json_export_example,OFFERING_FIELDS,BINDING_FIELDS
from nexloop_eios.trusted_configuration import apply_manifest
from context_source_declarations import source_declarations
from runtime_effect_fixture import seed_multi_uuid
from test_postgres_action_claims import governance_inputs
from test_business_setup_pg import declared_service


def definitions(tenant):
    original=governance_inputs()['action_definition'];cap=governance_inputs()['capability_snapshot'];result=[]
    for schema in offering_schemas():
        d=json.loads(json.dumps(original.model_dump(mode='json')).replace('synthetic-a',tenant));d.pop('contract_digest',None)
        ref={**d['object_types'][0],'stable_name':schema.type_name,'schema_digest':schema_contract_digest(schema)}
        d.update(stable_name=schema.type_name+'.create',object_types=[ref]);d['governance']['change_scope']['object_types']=[ref]
        result.append({'definition':type(original).model_validate_json(json.dumps(d)).model_dump(mode='json'),'capability':cap.model_dump(mode='json')})
    return result


def catalog_instances(backend,keeper_token,source_token,consumer,expiry):
    keeper=backend.authenticate(keeper_token,world='real');source=backend.authenticate(source_token,world='real')
    principal=source._session.authentication.subject_principal_id
    offered=keeper.create_object(action_name='ServiceOffering.create',action_version=1,intent_id='message-catalog-offering',type_name='ServiceOffering',properties=json_export_example(valid_until=expiry))
    linked=keeper.create_object(action_name='ConsumerServiceOffering.create',action_version=1,intent_id='message-catalog-binding',type_name='ConsumerServiceOffering',properties={'consumer_id':consumer,'offering_id':offered['object_id'],'offering_revision':1,'source_principal':principal,'active':True})
    targets=[]
    for receipt,fields in ((offered,OFFERING_FIELDS),(linked,BINDING_FIELDS)):
        target=receipt['type_name']+'/'+receipt['object_id'];targets.append(('eios:object:'+target,ResourceType.OBJECT))
        targets.extend(('eios:property:'+target+'/'+field,ResourceType.PROPERTY) for field in fields)
    assert len(targets)==19
    return offered,linked,targets,principal


def install_message_catalog(admin,backend,tenant,consumer,source_token,*,suffix,manifest=None,paths=None):
    expiry=(datetime.now(UTC)+timedelta(minutes=5)).isoformat();actions=definitions(tenant)
    if manifest is None:
        for schema in offering_schemas():admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',(tenant,schema.type_name,Jsonb(schema.model_dump(mode='json'))))
        for action in actions:admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',(tenant,'real','eios:action:'+action['definition']['stable_name']+':1',Jsonb(action['definition']),Jsonb(action['capability'])))
        keeper=seed_multi_uuid(admin,tenant,[('eios:action:'+a['definition']['stable_name']+':1',ResourceType.ACTION,Operation.EXECUTE) for a in actions],suffix='-message-catalog-maintainer')
    else:
        manifest=copy.deepcopy(manifest);manifest['object_types'].extend(s.model_dump(mode='json') for s in offering_schemas());manifest['actions'].extend(actions)
        binding,end,rows=declared_service(tenant,[a['definition']['stable_name'] for a in actions],'-message-catalog-maintainer');keeper=secrets.token_urlsafe(48)
        publish(admin,manifest,paths,binding,rows,keeper,'message-catalog-maintainer',end.isoformat())
    offered,linked,targets,principal=catalog_instances(backend,keeper,source_token,consumer,expiry)
    binding,rows=source_declarations(tenant,targets,identity_suffix=suffix);assert binding.subject_principal_id==principal
    new_token=secrets.token_urlsafe(48);end=next(r['payload']['expires_at'] for r in rows if r['kind']=='authentication')
    if manifest is None:
        admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',(hashlib.sha256(new_token.encode()).hexdigest(),tenant,binding.credential_id,Jsonb(binding.model_dump(mode='json')),['real'],'nexloop-core','active',end))
        for row in rows:admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',(tenant,row['kind'],row['key'],Jsonb(row['payload'])))
    else:publish(admin,manifest,paths,binding,rows,new_token,'message-catalog-source',end)
    fresh=backend.authenticate(new_token,world='real');assert fresh._session.authentication.subject_principal_id==principal
    return {'offering_id':offered['object_id'],'offering_binding_id':linked['object_id'],'source_token':new_token,'source':fresh,'manifest':manifest}


def publish(admin,manifest,paths,binding,rows,token,reference,expiry):
    records={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']};records.update({(r['kind'],tuple(r['key'])):r for r in rows});manifest['authority_facts']=list(records.values())
    manifest['service_credentials'].append({'reference':reference,'binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expiry,'status':'active'})
    secret_map=json.loads(paths['secrets'].read_text());secret_map[reference]=token;paths['secrets'].write_text(json.dumps(secret_map))
    manifest.update(manifest_id=str(uuid.uuid4()),expected_revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(binding.tenant_id,)).fetchone()[0])
    apply_manifest(manifest,database_url_file=paths['dsn'],signing_key_file=paths['signing'],signing_key_id='explicit-configuration',service_secrets_file=paths['secrets'])
