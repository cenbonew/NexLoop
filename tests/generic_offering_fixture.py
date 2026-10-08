"""Synthetic publication/authority config; all catalog objects use governed CREATE.

This never converts generic Run to Message Run and never grants Source CREATE.
New source credentials are authenticated before their Run credentials are issued.
"""
import json
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.service_offerings import offering_schemas,json_export_example,OFFERING_FIELDS,BINDING_FIELDS
from multi_authority_fixture import seed_multi_authority
from test_postgres_action_claims import governance_inputs


def install_generic_catalog(admin,backend,*,consumer_id,valid_until,submitter,second_submitter):
    tenant='synthetic-a';base=governance_inputs();original=base['action_definition'];cap=base['capability_snapshot']
    schemas=offering_schemas()
    for schema in schemas:
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',(tenant,schema.type_name,Jsonb(schema.model_dump(mode='json'))))
        body=original.model_dump(mode='json');body.pop('contract_digest',None)
        reference=original.object_types[0].model_copy(update={'stable_name':schema.type_name,'schema_digest':schema_contract_digest(schema)})
        body['stable_name']=schema.type_name+'.create';body['object_types']=[reference.model_dump(mode='json')];body['governance']['change_scope']['object_types']=body['object_types'];body['capability_binding']['capability_name']='ontology.object.create'
        definition=type(original).model_validate_json(json.dumps(body))
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',(tenant,'real','eios:action:'+schema.type_name+'.create:1',Jsonb(definition.model_dump(mode='json')),Jsonb(cap.model_copy(update={'capability_name':'ontology.object.create'}).model_dump(mode='json'))))
    create_targets=[('eios:action:'+s.type_name+'.create:1',ResourceType.ACTION,Operation.EXECUTE) for s in schemas]
    _,keeper_token=seed_multi_authority(admin,backend._pool,create_targets,identity_suffix='-catalog-maintainer')
    keeper=backend.authenticate(keeper_token,world='real')
    offered=keeper.create_object(action_name='ServiceOffering.create',action_version=1,intent_id='generic-catalog-offering',type_name='ServiceOffering',properties=json_export_example(valid_until=valid_until))
    new_credentials=[];links=[]
    for suffix,source in (('-submitter',submitter),('-submitter-B',second_submitter)):
        # The fixture principals are declared by real authority configuration,
        # not accepted from a RunCommand or a forged server session.
        keeper=backend.authenticate(keeper_token,world='real')
        linked=keeper.create_object(action_name='ConsumerServiceOffering.create',action_version=1,intent_id='generic-catalog-binding'+suffix,type_name='ConsumerServiceOffering',properties={'consumer_id':consumer_id,'offering_id':offered['object_id'],'offering_revision':1,'source_principal':source._session.authentication.subject_principal_id,'active':True})
        targets=[('eios:action:nexloop.service.request:1',ResourceType.ACTION,Operation.EXECUTE)]
        for receipt,fields in ((offered,OFFERING_FIELDS),(linked,BINDING_FIELDS)):
            resource=receipt['type_name']+'/'+receipt['object_id'];targets.append(('eios:object:'+resource,ResourceType.OBJECT,Operation.READ))
            targets.extend(('eios:property:'+resource+'/'+field,ResourceType.PROPERTY,Operation.READ) for field in fields)
        # Replace only this fixture-owned pre-Run technical credential record;
        # retain canonical principal and install actual fresh authority facts.
        admin.execute('delete from authz.nexloop_service_credentials where tenant_id=%s and credential_id=%s',(tenant,source._session.authentication.credential_id))
        _,token=seed_multi_authority(admin,backend._pool,targets,identity_suffix=suffix)
        new_credentials.append(token);links.append(linked['object_id'])
    # Last publication/fact writes invalidate snapshots; authenticate both now.
    return {'submitter':backend.authenticate(new_credentials[0],world='real'),'second_submitter':backend.authenticate(new_credentials[1],world='real'),'submitter_token':new_credentials[0],'second_token':new_credentials[1],'offering_id':offered['object_id'],'binding_ids':links}
