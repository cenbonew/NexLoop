"""NX-023-B prep: v6 Context pack contract (derived from v2/v3/v5), assembly, manifest kinds."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator,FormatChecker

from nexloop_eios.context_engine.budget import Item
from nexloop_eios.context_engine.pack import assemble_v6
from nexloop_eios.context_engine.sections import PartitionViolation
from nexloop_eios.context_pack import PACK_SCHEMA
from nexloop_eios.contracts import ContextManifest,ContextPackV6
from nexloop_eios.role_context_pack import PACK_SCHEMA_V3,POLICY_SCHEMA,PROTOCOL,PROTOCOL_V5,ROLE_CONTEXT_SCHEMA
from context_v6_example import D,inputs

ROOT=Path(__file__).resolve().parents[1]/'packages/contracts'
V6=json.loads((ROOT/'context-pack-v6.schema.json').read_text())
EXAMPLE=json.loads((ROOT/'examples/context-pack-v6.valid.json').read_text())
VALID=Draft202012Validator(V6,format_checker=FormatChecker())


def test_v6_is_derived_from_frozen_v2_v3_v5_sections():
    p=V6['properties']
    for name in ('bindings','formal_facts','current_constraints','supply'):assert p[name]==PACK_SCHEMA['properties'][name],name
    role=p['role']['anyOf'][1]['properties']
    assert role['role_binding']==ROLE_CONTEXT_SCHEMA and role['role_policy']['anyOf'][1]==POLICY_SCHEMA
    user,trigger=p['current_event']['anyOf']
    assert trigger==PACK_SCHEMA_V3['properties']['trigger_statement']
    assert {k:v for k,v in user['properties'].items() if k!='kind'}==PACK_SCHEMA['properties']['user_statement']['properties']
    assert user['properties']['kind']=={'const':'consumer_message'}
    # v2-v5 stay frozen: old Runs keep their own protocol strings and schemas.
    assert PACK_SCHEMA['properties']['schema_version']=={'const':'nexloop.context-pack.v2'}
    assert (PROTOCOL,PROTOCOL_V5)==('nexloop.context-pack.v3','nexloop.context-pack.v5')


def test_example_is_the_assembled_pack_and_validates_in_both_models():
    body,outcome=assemble_v6(**inputs())
    assert body==EXAMPLE and outcome.insufficient==[]
    VALID.validate(EXAMPLE);ContextPackV6.model_validate(EXAMPLE)
    assert [i['evidence_kind'] for i in EXAMPLE['open_work']]==['execution_state'] and EXAMPLE['open_work'][0]['tags']==['unconfirmed']
    assert EXAMPLE['role'] is None and EXAMPLE['current_event']['kind']=='consumer_message'


def forged(path,value):
    body=copy.deepcopy(EXAMPLE);target=body
    for key in path[:-1]:target=target[key]
    target[path[-1]]=value
    return body


@pytest.mark.parametrize('label,path,value',[
    ('claim_in_formal_state',('consumer_state',0,'evidence_kind'),'user_statement'),
    ('hypothesis_in_claim_evidence',('evidence',0,'evidence_kind'),'hypothesis'),
    ('non_hypothesis_in_hypotheses',('evidence',0,'subsection'),'hypotheses'),
    ('conversation_as_open_work',('open_work',0,'evidence_kind'),'conversation'),
    ('unknown_insufficient_code',('insufficient',),[{'code':'invented','section':None,'refs':[]}]),
    ('authority_field',('grants',),['admin']),
    ('v5_protocol',('schema_version',),'nexloop.context-pack.v5'),
    ('unknown_tag',('open_work',0,'tags'),['trusted']),
    ('bad_strategy_ref',('strategy_ref',),'recent_plus_required'),
    ('role_without_policy_key',('role',),{'role_binding':{}}),
])
def test_contract_rejects(label,path,value):
    body=forged(path,value)
    assert not VALID.is_valid(body),label
    with pytest.raises(ValueError):ContextPackV6.model_validate(body)


def test_assembly_enforces_partition_and_reports_insufficiency():
    bad=inputs();bad['items'].append(Item('consumer_state','Consumer','claim:'+'9'*64,'1',{},'formal_object',D))
    with pytest.raises(PartitionViolation):assemble_v6(**bad)
    tiny=inputs();tiny['strategy']['input_token_budget']=1024;tiny['strategy']['framing_reserve']=0
    tiny['items'].append(Item('constraints','policy','eios:action:nexloop.service.request:1','1',{'text':'x'*4000},'policy',D))
    body,outcome=assemble_v6(**tiny)
    assert body['insufficient'][0]['code']=='mandatory_exceeds_budget' and body['evidence']==[] and body['consumer_state']==[]
    assert len(body['constraints'])==2  # mandatory constraints kept whole, never truncated
    VALID.validate(body)


def test_manifest_evidence_kinds_extended_without_invalidating_old_values():
    manifest=json.loads((ROOT/'examples/context-manifest.valid.json').read_text())
    schema=json.loads((ROOT/'context-manifest.schema.json').read_text());validator=Draft202012Validator(schema,format_checker=FormatChecker())
    validator.validate(manifest);ContextManifest.model_validate(manifest)
    for kind in ('verified_fact','user_statement','hypothesis','policy','schema','memory','current_message','formal_object','conversation','execution_state'):
        body=copy.deepcopy(manifest);body['sources'][0]['evidence_kind']=kind
        assert validator.is_valid(body),kind
    body=copy.deepcopy(manifest);body['sources'][0]['evidence_kind']='claim'
    assert not validator.is_valid(body)
    # The Context storage vocabulary (0089) is now expressible in the manifest contract.
    sql=next((Path(__file__).resolve().parents[1]/'packages/eios-core/src/eios/migrations').glob('*_nx023_context_manifests.sql')).read_text()
    enum=set(schema['properties']['sources']['items']['properties']['evidence_kind']['enum'])
    for kind in ('formal_object','policy','schema','current_message','conversation','user_statement','hypothesis','execution_state','memory'):
        assert f"'{kind}'" in sql and kind in enum
