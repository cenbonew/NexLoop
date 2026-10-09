"""System metadata object types: generated-schema consistency, Action references, and
publication through the actual trusted-configuration path (no direct table writes)."""
import copy
from datetime import UTC,datetime
import json
from pathlib import Path
import uuid

import pytest
from eios.ontology.version_resolution import CapabilityContractSnapshot
from nexloop_eios import business_actions as BA
from nexloop_eios import system_object_types as SOT
from nexloop_eios.context_engine.strategy import PUBLISH_ACTION,PUBLISH_CAPABILITY,context_strategy_object_type
from nexloop_eios.conversation_messages import conversation_schemas
from test_trusted_configuration_pg import apply,configured  # noqa: F401

ROOT=Path(__file__).resolve().parents[1]
PATH=ROOT/'deploy/ontology/system-object-types.v1.json'
ACTIONS=BA.load(ROOT/'deploy/configuration/business-actions.v1.json')
GRANTS=json.loads((ROOT/'deploy/authorization/service-grants.v1.json').read_text())


def snapshot(name):
    return CapabilityContractSnapshot.model_validate_json(json.dumps({'capability_name':name,'capability_version':'1.0.0','schema_hash':'c'*64,'kind':'atomic',
        'has_side_effects':True,'idempotent':True,'required_scopes':['action.execute'],'risk_level':'low'}))


def test_manifest_matches_generated_schema():
    types=SOT.load(PATH)
    assert [(t.type_name,t.version) for t in types]==[('ContextStrategy',1)]
    assert types[0]==context_strategy_object_type()
    assert SOT.trusted_object_types(PATH)==[context_strategy_object_type().model_dump(mode='json')]


@pytest.mark.parametrize('change',['identity_mismatch','editable','duplicate','missing_purpose'])
def test_manifest_validation(change):
    body=json.loads(PATH.read_text())
    entry=body['object_types'][0]
    if change=='identity_mismatch':entry['version']=2
    if change=='editable':entry['definition']['only_edit_via_actions']=False
    if change=='duplicate':body['object_types'].append(copy.deepcopy(entry))
    if change=='missing_purpose':entry['purpose']=''
    with pytest.raises(SOT.SystemObjectTypesRejected):SOT.validate(body)


def test_every_business_action_object_type_is_published_or_a_system_type():
    published={(s.type_name,s.version) for s in conversation_schemas()}  # governed conversation types (0046)
    system={(t.type_name,t.version) for t in SOT.load(PATH)}
    for action in ACTIONS['actions']:
        ref=(action['object_type']['stable_name'],action['object_type']['version'])
        assert ref in published|system,action['stable_name']
    # System types are configuration metadata: never a service grant target.
    assert not any(t in g['resource_id'] for t,_ in system for g in GRANTS['grants'])


def test_system_types_and_strategy_action_publish_through_trusted_configuration(configured,admin):
    f=configured;tenant=f['tenant']
    rows=BA.compile_actions(ACTIONS,tenant=tenant,created_by='explicit-technical-owner',created_at=datetime.now(UTC),
        object_types=SOT.trusted_object_types(PATH),select=(PUBLISH_ACTION,),capabilities={PUBLISH_CAPABILITY:snapshot(PUBLISH_CAPABILITY)})
    body={**f['manifest'],'manifest_id':str(uuid.uuid4()),
        'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0],
        'object_types':f['manifest']['object_types']+SOT.trusted_object_types(PATH),'actions':f['manifest']['actions']+rows}
    apply(f,body)
    stored=admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='ContextStrategy' and version=1",(tenant,)).fetchone()[0]
    assert stored==context_strategy_object_type().model_dump(mode='json')
    action=admin.execute('select definition from control.nexloop_action_definitions where tenant_id=%s and resource_id=%s and active',(tenant,'eios:action:'+PUBLISH_ACTION+':1')).fetchone()[0]
    assert action==rows[0]['definition']
    # The trusted configurator itself refuses an Action whose referenced type is absent.
    missing={**body,'manifest_id':str(uuid.uuid4()),'object_types':f['manifest']['object_types'],
        'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0]}
    from nexloop_eios.trusted_configuration import ConfigurationRejected,validate_manifest
    validate_manifest({**missing,'object_types':body['object_types']})
    with pytest.raises((ConfigurationRejected,ValueError)):validate_manifest(missing)
    with pytest.raises(Exception):apply(f,missing)
