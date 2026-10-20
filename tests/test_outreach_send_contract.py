"""Outreach send contract (packages/contracts/outreach-send.schema.json; outreach slice 4): the caller gives consumer, purpose,
origin, body and the native WebChat channel only; topic, purpose version, origin kind and the business intent id are frozen by
the server; unknown fields are refused."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[1] / 'packages/contracts'
VALIDATOR = Draft202012Validator(json.loads((ROOT / 'outreach-send.schema.json').read_text()), format_checker=FormatChecker())
EXAMPLE = json.loads((ROOT / 'examples/outreach-send.valid.json').read_text())


def changed(part, **change):
    value = copy.deepcopy(EXAMPLE)
    value[part].update(change)
    return value


def test_the_example_is_valid():
    VALIDATOR.validate(EXAMPLE)


@pytest.mark.parametrize('part,change', [
    ('parameters', {'channel': 'sms'}), ('parameters', {'topic': 'marketing'}), ('parameters', {'origin_ref': 'free text'}),
    ('parameters', {'body': ''}), ('parameters', {'body': 'x' * 2001}), ('parameters', {'consumer_id': 'Consumer-1'}),
    ('parameters', {'purpose': 'Bad Purpose'}), ('parameters', {'intent_id': 'chosen-by-caller'}),
    ('frozen', {'origin_kind': 'reply'}), ('frozen', {'intent_id': 'outreach:abc'}), ('frozen', {'frequency_class': 'other'})])
def test_caller_choices_and_unknown_fields_are_refused(part, change):
    with pytest.raises(ValidationError):
        VALIDATOR.validate(changed(part, **change))
