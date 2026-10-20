"""NX-019 claim contract: canonical schema, generated model, negatives, dataset conformance."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from nexloop_eios.contracts import Claim
from support.tenants import SIM_WORLD, TENANT_A

ROOT=Path(__file__).resolve().parents[1]
SCHEMA=json.loads((ROOT/'packages/contracts/claim.schema.json').read_text())
EXAMPLE=json.loads((ROOT/'packages/contracts/examples/claim.valid.json').read_text())
VALIDATOR=Draft202012Validator(SCHEMA,format_checker=FormatChecker())


def valid(value):return VALIDATOR.is_valid(value)


def test_example_valid_and_handoff_copy_identical():
    Draft202012Validator.check_schema(SCHEMA);VALIDATOR.validate(EXAMPLE)
    assert Claim.model_validate(EXAMPLE).claim_id==EXAMPLE['claim_id']
    # docs/handoff is frozen; packages/contracts is the live copy. A divergence passes only when registered (file, reason, task, date).
    registered={d['file']:d for d in json.loads((ROOT/'packages/contracts/handoff-divergence.json').read_text())['divergences']}
    assert all(d['reason'] and d['task'].startswith('NX-') and len(d['date'])==10 for d in registered.values())
    for name in ('claim.schema.json','examples/claim.valid.json'):
        same=(ROOT/'docs/handoff/contracts'/name).read_bytes()==(ROOT/'packages/contracts'/name).read_bytes()
        assert same or name in registered,name


def mutate(**changes):
    value=copy.deepcopy(EXAMPLE)
    for key,item in changes.items():
        if item is None and key=='mode':value.pop(key)
        else:value[key]=item
    return value


HYPOTHESIS=dict(epistemic_kind='hypothesis',resolution_state='hypothesis_only',source=None,derived_from=['a'*64],modality='asserted',condition='')

NEGATIVES={
 'verified_fact':dict(epistemic_kind='verified_fact'),
 'hypothesis_without_basis':dict(HYPOTHESIS,derived_from=[]),
 'hypothesis_not_in_hypothesis_layer':dict(HYPOTHESIS,resolution_state='unresolved'),
 'explicit_without_source':dict(source=None),
 'explicit_with_derivation':dict(derived_from=['a'*64]),
 'explicit_in_hypothesis_layer':dict(resolution_state='hypothesis_only'),
 'conditional_without_condition':dict(modality='conditional',condition=''),
 'corrects_on_non_correction':dict(corrects_claim_id='b'*64),
 'agent_preference':dict(speaker='agent',epistemic_kind='preference'),
 'consumer_commitment':dict(speaker='consumer',epistemic_kind='commitment'),
 'consumer_subject_without_server_ref':dict(subject={'kind':'consumer','ref':'','text':''}),
 'entity_subject_with_ref':dict(subject={'kind':'entity','ref':'d'*64,'text':'x'}),
 'extra_authority_field':dict(scopes=['admin']),
 'untyped_value':dict(value={'type':'number','value':'三'}),
 'bad_money':dict(value={'type':'money','value':{'amount':1,'currency':'yuan'}}),
 'confidence_out_of_range':dict(confidence=1.5),
 'bad_digest':dict(claim_id='not-a-digest'),
 'naive_timestamp':dict(recorded_at='2026-10-08 02:00:05'),
 'unknown_resolution':dict(resolution_state='applied'),
 # NX-052: tenant ids are uuids; mode is derived from the world and never shadow.
 'tenant_not_uuid':dict(tenant_id='synthetic-a'),
 'shadow_mode':dict(mode='shadow',world_id='shadow'),
 'real_world_test_mode':dict(mode='test'),
 'simulation_mode_in_real_world':dict(mode='simulation'),
 'real_mode_outside_real_world':dict(world_id='sim-1'),
 'missing_mode':dict(mode=None),
}


@pytest.mark.parametrize('label',sorted(NEGATIVES))
def test_negative_cases_rejected_by_schema_and_generated_model(label):
    forged=mutate(**NEGATIVES[label])
    assert not valid(forged),label
    with pytest.raises(ValueError):Claim.model_validate(forged)


def test_positive_variants():
    assert valid(mutate(**HYPOTHESIS))
    assert valid(mutate(**dict(HYPOTHESIS,source=EXAMPLE['source'],derived_from=[])))
    assert valid(mutate(epistemic_kind='correction',corrects_claim_id='b'*64,modality='asserted',condition=''))
    assert valid(mutate(speaker='agent',epistemic_kind='commitment',subject={'kind':'enterprise','ref':'','text':''},modality='asserted',condition=''))
    unresolved=dict(EXAMPLE['valid_time'],expression='过两天',kind='unparsed',status='unresolved',start=None,end=None)
    assert valid(mutate(valid_time=unresolved))
    assert not valid(mutate(valid_time=dict(unresolved,start='2026-10-09T00:00:00+08:00')))
    assert valid(mutate(mode='simulation',world_id=SIM_WORLD)) and valid(mutate(mode='test',world_id='sim-1'))


def test_valid_time_normalization_is_optional_and_strict():
    """Owner 2026-10-17: valid_time.normalization records how the time was read; a Claim without it (every Claim before) stays valid."""
    assert 'normalization' not in EXAMPLE['valid_time'] and valid(EXAMPLE)
    reading={'kind':'deadline','start':'2026-10-14T10:00:00+08:00','end':'2026-10-17T00:00:00+08:00','granularity':'day'}
    normalization={'source':'llm','verdict':'accepted','llm':reading,'rule':None,'assumptions':['周五指本周五']}
    assert valid(mutate(valid_time=dict(EXAMPLE['valid_time'],normalization=normalization)))
    assert Claim.model_validate(mutate(valid_time=dict(EXAMPLE['valid_time'],normalization=normalization))).valid_time.normalization.source=='llm'
    assert valid(mutate(valid_time=dict(EXAMPLE['valid_time'],normalization={'source':'rule','verdict':'time_llm_weekday'})))
    for bad in ({'source':'model','verdict':'accepted'},{'source':'llm'},{'source':'llm','verdict':'Free text'},
                {'source':'llm','verdict':'accepted','note':'x'},{'source':'llm','verdict':'accepted','llm':dict(reading,start='2026-10-14')},
                {'source':'llm','verdict':'accepted','llm':dict(reading,kind='none')},{'source':'llm','verdict':'accepted','assumptions':['x']*9}):
        forged=mutate(valid_time=dict(EXAMPLE['valid_time'],normalization=bad))
        assert not valid(forged),bad
        with pytest.raises(ValueError):Claim.model_validate(forged)


def test_every_frozen_dataset_extraction_conforms():
    from test_conversation_extraction import CASES,materialize
    from nexloop_eios.conversation_extraction import extract
    count=0
    for case in CASES.values():
        if case['expected'].get('error'):continue
        messages,context,provider=materialize(case,tenant=TENANT_A)
        for c in extract(messages,context,provider).claims:
            wire={'claim_id':c['claim_id'],'tenant_id':context.tenant_id,'world_id':context.world,
                'mode':'real' if context.world=='real' else 'simulation' if context.world in ('simulation','shadow') else 'test','conversation_id':context.conversation_id,
                'consumer_id':context.consumer_id,'topic_key':c['topic_key'],'subject':{'kind':c['subject_kind'],'ref':c['subject_ref'],'text':c['subject_text']},
                'predicate':c['predicate'],'value':c['value'],'speaker':c['speaker'],'polarity':c['polarity'],'modality':c['modality'],'condition':c['condition'],
                'time_expression':c['time_expression'],'valid_time':c['valid_time'],
                'source':None if c['source_message_id'] is None else {'message_id':c['source_message_id'],'sequence':c['source_sequence'],
                    'span_start':c['span_start'],'span_end':c['span_end'],'content_hash':c['source_content_hash'],'quote':c['quote']},
                'derived_from':c['derived_from'],'corrects_claim_id':c['corrects_claim_id'],'extractor_version':c['extractor_version'],
                'confidence':c['confidence'],'epistemic_kind':c['epistemic_kind'],'resolution_state':c['resolution_state'],
                'correlation_key':c['correlation_key'],'guard_flags':c['guard_flags'],'recorded_at':'2026-10-08T02:00:05+00:00'}
            VALIDATOR.validate(wire);count+=1
    assert count>=100
