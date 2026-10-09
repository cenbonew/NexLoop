"""Versioned business Action declarations: profiles, human-owner-only Actions, compilation."""
import copy
from datetime import UTC,datetime
import json
from pathlib import Path

import pytest
from eios.ontology.version_resolution import CapabilityContractSnapshot
from nexloop_eios import business_actions as BA
from nexloop_eios.context_engine.audit import AUDIT_ACTION,AUDIT_CAPABILITY,context_manifest_object_type
from nexloop_eios.context_engine.strategy import PUBLISH_ACTION,PUBLISH_CAPABILITY,context_strategy_object_type
from nexloop_eios.conversation_messages import conversation_schemas

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=BA.load(ROOT/'deploy/configuration/business-actions.v1.json')
GRANTS=json.loads((ROOT/'deploy/authorization/service-grants.v1.json').read_text())


def capability(name):
    return CapabilityContractSnapshot.model_validate_json(json.dumps({'capability_name':name,'capability_version':'1.0.0','schema_hash':'c'*64,'kind':'atomic',
        'has_side_effects':True,'idempotent':True,'required_scopes':['action.execute'],'risk_level':'low'}))


def object_types():
    return [s.model_dump(mode='json') for s in conversation_schemas()]+[context_strategy_object_type().model_dump(mode='json'),context_manifest_object_type().model_dump(mode='json')]


def test_manifest_declares_service_create_and_human_owner_strategy_publication():
    by={a['stable_name']:a for a in MANIFEST['actions']}
    assert by['Message.agent_create']['authority']=='service' and by['Message.agent_create']['executor_role']=='outbound_message_recorder'
    p=by[PUBLISH_ACTION]
    assert p['authority']=='human_owner' and p['executor_role']=='human_owner' and p['capability_name']==PUBLISH_CAPABILITY
    # Owner decision: Manifest reads are human-only (owner / audit role).
    audit=by[AUDIT_ACTION]
    assert audit['authority']=='human_owner' and audit['executor_role']=='human_owner' and audit['capability_name']==AUDIT_CAPABILITY
    assert audit['object_type']=={'stable_name':'ContextManifest','version':1}


def test_compiles_both_profiles_with_explicit_snapshots():
    rows=BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),
        capabilities={'ontology.object.create':capability('ontology.object.create'),PUBLISH_CAPABILITY:capability(PUBLISH_CAPABILITY),AUDIT_CAPABILITY:capability(AUDIT_CAPABILITY)})
    names={r['definition']['stable_name']:r for r in rows}
    assert set(names)=={'Message.agent_create',PUBLISH_ACTION,AUDIT_ACTION}
    assert [t['stable_name'] for t in names[AUDIT_ACTION]['definition']['object_types']]==['ContextManifest']
    publish=names[PUBLISH_ACTION]['definition']
    assert publish['capability_binding']['capability_name']==PUBLISH_CAPABILITY and publish['governance']['approval_mode']=='none'
    assert [t['stable_name'] for t in publish['object_types']]==['ContextStrategy']
    with pytest.raises(BA.BusinessActionsRejected):  # every declared Action needs its own snapshot
        BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),capability=capability('ontology.object.create'))
    only=BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),
        capability=capability('ontology.object.create'),select=('Message.agent_create',))
    assert [r['definition']['stable_name'] for r in only]==['Message.agent_create']
    with pytest.raises(BA.BusinessActionsRejected):
        BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),
            capability=capability('ontology.object.create'),select=('Undeclared.action',))


@pytest.mark.parametrize('change',['human_publish_by_service','service_create_by_human','unknown_capability','approval','policy','risk','audit_by_service'])
def test_profiles_are_closed(change):
    m=copy.deepcopy(MANIFEST);by={a['stable_name']:a for a in m['actions']}
    if change=='human_publish_by_service':by[PUBLISH_ACTION]['executor_role']='context_assembler'
    if change=='service_create_by_human':by['Message.agent_create'].update(authority='human_owner')
    if change=='unknown_capability':by[PUBLISH_ACTION]['capability_name']='context.strategy.delete'
    if change=='approval':by[PUBLISH_ACTION]['approval_mode']='required'
    if change=='policy':by[PUBLISH_ACTION]['policy_refs']=['policy:x']
    if change=='risk':by['Message.agent_create']['risk_level']='high'
    if change=='audit_by_service':by[AUDIT_ACTION].update(authority='service',executor_role='context_assembler')
    with pytest.raises(BA.BusinessActionsRejected):BA.validate(m)


def test_human_owner_actions_are_never_granted_to_service_principals():
    human={'eios:action:%s:%s'%(a['stable_name'],a['version']) for a in MANIFEST['actions'] if a['authority']=='human_owner'}
    assert human and not human&{g['resource_id'] for g in GRANTS['grants']}
    services={a['executor_role'] for a in MANIFEST['actions'] if a['authority']=='service'}
    assert services<={p['role'] for p in GRANTS['principals']}
    # nexloop.context.assemble:1 is issued with each Run (0104): no standing service principal holds it.
    assert not any(g['resource_id']=='eios:action:nexloop.context.assemble:1' for g in GRANTS['grants'])
    assert [r['role'] for r in GRANTS['retired_principals']]==['context_assembler']
