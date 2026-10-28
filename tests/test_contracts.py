"""Live target contracts, independently of the frozen handoff verifier."""
import copy
import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[1]/'packages/contracts'
NAMES = sorted(p.name.removesuffix('.schema.json') for p in ROOT.glob('*.schema.json'))


def contract(name):
    schema = json.loads((ROOT/f'{name}.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema,format_checker=FormatChecker()), json.loads((ROOT/f'examples/{name}.valid.json').read_text())


@pytest.mark.parametrize('name',NAMES)
def test_live_examples_and_no_extra_authority_fields(name):
    validator,example=contract(name)
    validator.validate(example)
    forged=copy.deepcopy(example);forged['scopes']=['admin']
    with pytest.raises(ValidationError):validator.validate(forged)


@pytest.mark.parametrize('name',['event-envelope','run-command','action-intent','ontology-mutation'])
def test_real_nonreal_world_boundary(name):
    validator,example=contract(name)
    forged=copy.deepcopy(example);forged.update(mode='real',world_id='shadow')
    with pytest.raises(ValidationError):validator.validate(forged)
    forged.update(mode='shadow',world_id='real')
    with pytest.raises(ValidationError):validator.validate(forged)


def test_action_requires_expected_revision_and_checksum():
    validator,example=contract('action-intent')
    for override in [{'expected_versions':[]},{'payload_digest':'unknown'}]:
        forged=copy.deepcopy(example);forged.update(override)
        with pytest.raises(ValidationError):validator.validate(forged)


def test_runtime_cannot_request_unbounded_budget_or_embed_credentials():
    validator,example=contract('run-command')
    forged=copy.deepcopy(example);forged['budget']['maximum_model_turns']=100000
    with pytest.raises(ValidationError):validator.validate(forged)
    forged=copy.deepcopy(example);forged['api_key']='synthetic-invalid-field'
    with pytest.raises(ValidationError):validator.validate(forged)


VARIANTS = sorted(p.name for p in (ROOT/'examples').glob('*.*.valid.json'))


@pytest.mark.parametrize('variant',VARIANTS)
def test_example_variants_are_valid(variant):
    """A variant example `<schema>.<variant>.valid.json` (beside the one `<schema>.valid.json`) validates against its schema."""
    name = variant.split('.',1)[0]
    assert name in NAMES, f'{variant} names no schema'
    validator,_ = contract(name)
    validator.validate(json.loads((ROOT/'examples'/variant).read_text()))


def test_experiment_full_guardrails_example_has_every_guardrail():
    example = json.loads((ROOT/'examples/experiment-registration.full-guardrails.valid.json').read_text())
    schema = json.loads((ROOT/'experiment-registration.schema.json').read_text())
    assert set(example['guardrails']) == set(schema['properties']['guardrails']['properties'])
    assert example['guardrails']['stop_on_budget_exhausted'] is True
