"""NX-040 F2: the schema-proposal contract (packages/contracts/schema-proposal.schema.json): ids, digests, codes and counts only; the
§4.1 whitelist; the three proposal states; no free text and no unknown field."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[1] / 'packages/contracts'
VALIDATOR = Draft202012Validator(json.loads((ROOT / 'schema-proposal.schema.json').read_text()), format_checker=FormatChecker())
EXAMPLE = json.loads((ROOT / 'examples/schema-proposal.valid.json').read_text())


def test_the_example_is_valid():
    VALIDATOR.validate(EXAMPLE)


@pytest.mark.parametrize('change', [{'operation': 'add_relation_type'}, {'operation': 'remove_property'}, {'status': 'evaluating'},
    {'status': 'accepted_for_release'}, {'target_ref': 'eios:action:Grant.issue:1'}, {'parent_schema_digest': 'abc'}, {'definition': {'x': 1}},
    {'source': {**EXAMPLE['source'], 'quote': '客户原话'}}, {'permit_ref': ''}, {'parent_schema_version': -1}])
def test_closed_values_and_unknown_fields_are_refused(change):
    with pytest.raises(ValidationError):
        VALIDATOR.validate({**copy.deepcopy(EXAMPLE), **change})
