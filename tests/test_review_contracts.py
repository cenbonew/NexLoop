"""ADR-019 wire contracts, deliberately not an implemented review/authorization API."""
import copy
import importlib
import json
from pathlib import Path
import pytest
from pydantic import ValidationError as PydanticError
from jsonschema import ValidationError as SchemaError
from test_contracts import contract

ROOT=Path(__file__).resolve().parents[1]/'packages/contracts'


def model(name):
    return getattr(importlib.import_module('nexloop_eios.contracts'),''.join(p.title() for p in name.split('-')))


def assert_rejected(name,value):
    validator,_=contract(name)
    with pytest.raises(SchemaError):validator.validate(value)
    with pytest.raises(PydanticError):model(name).model_validate(value)


@pytest.mark.parametrize('path',sorted(ROOT.glob('*.schema.json')),ids=lambda p:p.stem)
def test_all_canonical_examples_are_typed_and_roundtrip(path):
    name=path.name.removesuffix('.schema.json');validator,example=contract(name)
    assert model(name).model_json_schema()==json.loads(path.read_text())
    typed=model(name).model_validate(example)
    assert typed.model_dump(mode='json',exclude_unset=True)==example
    validator.validate(typed.model_dump(mode='json',exclude_unset=True))
    assert model(name).model_validate_json(json.dumps(example)).model_dump(exclude_unset=True)==example


KINDS={
 'object_type':{'name':'sample_type'},
 'property':{'name':'sample_property','owner_type_ref':'eios:ObjectType/Consumer','value_type':'boolean','closed_vocabulary':False,'property_group':'needs_intent'},
 'vocabulary_value':{'property_ref':'eios:Property/Consumer.preference','value':'synthetic'},
 'alias':{'canonical_ref':'eios:Property/Consumer.preference','alias_text':'合成别名'},
 'object_instance':{'type_ref':'eios:ObjectType/Consumer','identifying_properties':{'name':'synthetic'},'strong_identifier':False},
}


@pytest.mark.parametrize('kind',KINDS)
def test_candidate_each_kind_requires_its_fields(kind):
    validator,example=contract('candidate-definition')
    example.update(kind=kind,proposed={'display_name':'synthetic',**KINDS[kind]})
    validator.validate(example);model('candidate-definition').model_validate(example)
    for field in KINDS[kind]:
        missing=copy.deepcopy(example);del missing['proposed'][field]
        assert_rejected('candidate-definition',missing)


@pytest.mark.parametrize('name',['candidate-definition','review-decision'])
@pytest.mark.parametrize('mode,world',[('real','simulation-1'),('simulation','real'),('shadow','real'),('test','real')])
def test_review_world_boundaries(name,mode,world):
    _,value=contract(name);value.update(mode=mode,world_id=world)
    assert_rejected(name,value)


@pytest.mark.parametrize('status',['merged','pending_review'])
def test_candidate_merge_evidence_required(status):
    _,value=contract('candidate-definition');value['status']=status;del value['merge_scores']
    assert_rejected('candidate-definition',value)


def test_reject_has_no_publication_and_merge_requires_target():
    _,value=contract('review-decision')
    value.update(decision='reject',publication={'schema_revision_before':'synthetic-1'})
    assert_rejected('review-decision',value)
    value.pop('publication');model('review-decision').model_validate(value)
    value['decision']='merge_into';assert_rejected('review-decision',value)
    value['merge_target_ref']='eios:Property/Consumer.preference'
    model('review-decision').model_validate(value)


@pytest.mark.parametrize('name', ['candidate-definition','review-decision'])
def test_extra_permissions_and_invalid_dates_cannot_become_wire_authority(name):
    _,value=contract(name);value['permissions']=['ontology.schema.review']
    assert_rejected(name,value)
    value.pop('permissions');value['created_at' if name=='candidate-definition' else 'decided_at']='2026-02-31T10:00:00Z'
    assert_rejected(name,value)


def test_schema_validation_explicitly_does_not_publish_or_authenticate():
    # Published is an OUTPUT state. A shape validator has no governance history,
    # account lookup or tenant-aware ref resolver, and must not pretend to have it.
    _,candidate=contract('candidate-definition');candidate['status']='published'
    typed=model('candidate-definition').model_validate(candidate)
    assert typed.status=='published'
    assert not hasattr(typed,'apply') and not hasattr(typed,'publish')
    _,decision=contract('review-decision')
    decision.update(decision='merge_into',reviewer_ref='eios:Subject/unverified-synthetic',merge_target_ref='eios:ObjectType/cross-kind-synthetic')
    typed=model('review-decision').model_validate(decision)
    assert typed.reviewer_ref==decision['reviewer_ref']
    assert not hasattr(typed,'authorize')
    # Human identity/current permission, same-tenant/same-kind merge resolution,
    # publish gates and simulation→real queue exclusion remain backend work.
    # Test/shadow data is valid wire data, never an execution permit.
    decision.update(mode='shadow',world_id='shadow-synthetic')
    model('review-decision').model_validate(decision)


@pytest.mark.parametrize('override',[{'decision':'reject','publication':{'schema_revision_before':'synthetic'}},{'mode':'real','world_id':'shadow-synthetic'}])
def test_copied_model_is_revalidated_instead_of_becoming_authority(override):
    _,value=contract('review-decision')
    cls=model('review-decision');original=cls.model_validate(value)
    copied=original.model_copy(update=override)
    with pytest.raises(PydanticError):cls.model_validate(copied)


def test_nested_mutation_is_revalidated():
    _,value=contract('candidate-definition');cls=model('candidate-definition')
    original=cls.model_validate(value)
    original.proposed.owner_type_ref=None
    with pytest.raises(PydanticError):cls.model_validate(original)
    original=cls.model_validate(value)
    original.recall[0].score=2
    with pytest.raises(PydanticError):cls.model_validate(original)


@pytest.mark.parametrize('invalid',[float('nan'),float('inf'),float('-inf'),{'nested':[float('nan')]},{1:'non-string-key'},('tuple',),object()])
def test_unconstrained_value_still_requires_actual_json_data(invalid):
    _,value=contract('candidate-definition')
    value.update(kind='vocabulary_value',proposed={'display_name':'synthetic','property_ref':'eios:Property/Consumer.preference','value':invalid})
    with pytest.raises(PydanticError,match='canonical contract validation failed'):
        model('candidate-definition').model_validate(value)


def test_revalidated_instance_is_rebuilt_and_valid_copy_stays_valid():
    _,value=contract('candidate-definition');cls=model('candidate-definition')
    instance=cls.model_validate(value)
    rebuilt=cls.model_validate(instance)
    assert rebuilt is not instance and rebuilt.proposed is not instance.proposed
    assert rebuilt.model_dump(exclude_unset=True)==value
    changed=instance.model_copy(update={'status':'staged'})
    assert cls.model_validate(changed).status=='staged'
