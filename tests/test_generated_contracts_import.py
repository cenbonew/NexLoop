"""The generated Python contract module imports in a fresh interpreter, every canonical example parses into its model and serialises
back to the same JSON, and the generator refuses a property that is not a Python field name (a keyword such as `from` made the
module unimportable, K5 bcb33333)."""
import importlib.util
import json
import keyword
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT/'packages/contracts'


def generator():
    spec = importlib.util.spec_from_file_location('generate_contracts_under_test', ROOT/'scripts/generate_contracts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generated_module_imports_in_a_fresh_interpreter():
    result = subprocess.run([sys.executable, '-c', 'import nexloop_eios.contracts'], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize('schema', sorted(p.name for p in CONTRACTS.glob('*.schema.json')))
def test_every_example_round_trips_through_its_model(schema):
    import nexloop_eios.contracts as generated
    name = schema.removesuffix('.schema.json')
    model = getattr(generated, generator().type_name(name))
    example = json.loads((CONTRACTS/'examples'/(name+'.valid.json')).read_text())
    parsed = model.model_validate(example)
    assert json.loads(parsed.model_dump_json(exclude_unset=True)) == example


@pytest.mark.parametrize('key', sorted(keyword.kwlist)+['_private', '1st', 'with-dash'])
def test_the_generator_refuses_a_property_that_is_not_a_field_name(key):
    schema = {'type': 'object', 'additionalProperties': False, 'required': [key], 'properties': {key: {'type': 'string'}}}
    with pytest.raises(ValueError, match='not a Python field name'):
        generator().python_models({'Probe': schema})
