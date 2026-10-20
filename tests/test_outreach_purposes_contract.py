"""Outreach purpose list contract (packages/contracts/outreach-purposes.schema.json; outreach slice 3): marketing purposes keep a
marketing topic, the marketing allowance and the unsubscribe notice; other purposes cannot pose as marketing or ask for consent;
the built-in kinds keep their trigger; unknown fields are refused."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[1] / 'packages/contracts'
VALIDATOR = Draft202012Validator(json.loads((ROOT / 'outreach-purposes.schema.json').read_text()), format_checker=FormatChecker())
EXAMPLE = json.loads((ROOT / 'examples/outreach-purposes.valid.json').read_text())


def with_purpose(index, **change):
    value = copy.deepcopy(EXAMPLE)
    for key, item in change.items():
        if key == 'content_rules':
            value['purposes'][index]['content_rules'].update(item)
        else:
            value['purposes'][index][key] = item
    return value


def test_the_example_is_valid():
    VALIDATOR.validate(EXAMPLE)


@pytest.mark.parametrize('index,change', [
    (1, {'topic': 'promotion'}), (1, {'frequency_class': 'service'}), (1, {'content_rules': {'unsubscribe_notice_required': False}}),
    (0, {'topic': 'marketing.new'}), (0, {'frequency_class': 'marketing'}), (0, {'requires_consent': True}), (0, {'trigger': 'owner_request'}),
    (0, {'kind': 'reminder'}), (0, {'purpose_id': 'Bad Id'}), (0, {'content_rules': {'max_chars': 5}}), (0, {'price': '0.99'})])
def test_marketing_rules_and_unknown_fields_are_refused(index, change):
    with pytest.raises(ValidationError):
        VALIDATOR.validate(with_purpose(index, **change))
