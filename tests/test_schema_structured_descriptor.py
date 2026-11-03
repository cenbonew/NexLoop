"""Claim → Consumer phase two (design claim-consumer-delivery-windows v2 §1, §2.2; L6 conditions C1, dispatcher 2026-11-03), pure:
the F1 side of the structured-property gate (same table as the SQL side, tests/support/structured_descriptor_cases.py), the vendored
max_items adaptation (PROVENANCE) and the digest regression of definitions without it, and the delivery_windows normalizer rule table."""
import json

import pytest

from eios.ontology.models import OntologyValidationError, PropertyDefinition, PropertyTypeDescriptor
from eios.ontology.semantics import definition_digest, schema_contract_digest
from nexloop_eios import delivery_windows as W
from nexloop_eios.consumer_profile import consumer_profile_schema
from nexloop_eios.evolution import schema_compat as S
from nexloop_eios.schema_publication import build
from support.structured_descriptor_cases import ACCEPTED, REFUSED, VALID
from test_schema_compat import SUCCESSOR, parent


@pytest.mark.parametrize('label,descriptor', ACCEPTED)
def test_f1_accepts_the_closed_vocabulary_struct_array(label, descriptor):
    assert S.structured_descriptor_valid(descriptor) is True
    change = {'op': 'add_optional_property', 'type_name': 'Consumer', 'successor_actions': [dict(SUCCESSOR)],
              'property': {'property_name': 'delivery_windows', 'value_type': 'json', 'nullable': True, 'type_descriptor': descriptor}}
    verdict = S.judge(change, parent())
    assert verdict['allowed'] is True and verdict['codes'] == [], verdict


@pytest.mark.parametrize('label,descriptor', REFUSED)
def test_f1_refuses_every_other_shape_as_value_type_not_allowed(label, descriptor):
    assert S.structured_descriptor_valid(descriptor) is False, label
    prop = {'property_name': 'delivery_windows', 'value_type': 'json', 'nullable': True}
    if descriptor is not None:
        prop['type_descriptor'] = descriptor
    verdict = S.judge({'op': 'add_optional_property', 'type_name': 'Consumer', 'property': prop, 'successor_actions': [dict(SUCCESSOR)]}, parent())
    assert verdict['allowed'] is False and 'value_type_not_allowed' in verdict['codes'], (label, verdict)


# Published Consumer v1 (consumer_profile_schema) before the max_items adaptation: its digests may not move.
CONSUMER_V1_DEFINITION_DIGEST = '86ab2881c991d7d7a9b4be4b152e39e9b9721bbeed4c2c1f7a12872bcadde375'
CONSUMER_V1_CONTRACT_DIGEST = '94beae29e708a0fbd090155ced772f17139a4ea9b4f7fffe05cf58f878c0020c'


def test_definitions_without_max_items_keep_their_digests_byte_for_byte():
    schema = consumer_profile_schema()
    assert definition_digest(schema, allow_breaking=False) == CONSUMER_V1_DEFINITION_DIGEST
    assert schema_contract_digest(schema) == CONSUMER_V1_CONTRACT_DIGEST
    assert 'max_items' not in json.dumps(schema.model_dump(mode='json'))


def test_max_items_is_array_only_kept_when_set_and_checked_on_write():
    descriptor = PropertyTypeDescriptor.model_validate(VALID)
    assert descriptor.model_dump(mode='json')['max_items'] == 40
    assert 'max_items' not in descriptor.element.model_dump(mode='json')
    with pytest.raises(ValueError):
        PropertyTypeDescriptor.model_validate({'kind': 'string', 'max_items': 3})
    with pytest.raises(ValueError):
        PropertyTypeDescriptor.model_validate({'kind': 'array', 'max_items': 0, 'element': {'kind': 'string'}})
    prop = PropertyDefinition(property_name='delivery_windows', value_type='json', nullable=True, type_descriptor=descriptor)
    assert prop.normalize_value([{'weekday': 'fri', 'part': 'daytime'}], field_name='v') == [{'weekday': 'fri', 'part': 'daytime'}]
    with pytest.raises(OntologyValidationError, match='more than 40'):
        prop.normalize_value([{'weekday': 'fri', 'part': 'daytime'}] * 41, field_name='v')
    with pytest.raises(OntologyValidationError):
        prop.normalize_value([{'weekday': 'fri', 'part': 'noon'}], field_name='v')


def test_the_publication_build_carries_text_and_the_dumped_descriptor():
    old = {'type_name': 'Consumer', 'version': 1, 'only_edit_via_actions': True, 'primary_key': [],
           'properties': [{'property_name': 'display_name', 'value_type': 'string', 'nullable': True}]}
    change = {'property_name': 'delivery_windows', 'value_type': 'json', 'type_descriptor': VALID,
              'display_name': W.DISPLAY_NAME, 'description': W.DESCRIPTION}
    publication, report = build({'operation': 'add_optional_property', 'change': change, 'type_name': 'Consumer', 'schema': old, 'actions': []}, 'publish')
    prop = publication['schema']['properties'][-1]
    assert report['allowed'] is True and prop['display_name'] == W.DISPLAY_NAME and prop['description'] == W.DESCRIPTION
    assert prop['type_descriptor'] == PropertyTypeDescriptor.model_validate(VALID).model_dump(mode='json')


# Normalization rule table: (statement, expected). None: not normalized, never written.
CASES = [
    ('周五白天我在家', [('fri', 'daytime')]),
    ('星期五白天', [('fri', 'daytime')]),
    ('礼拜天上午', [('sun', 'morning')]),
    ('周末上午', [('sat', 'morning'), ('sun', 'morning')]),
    ('工作日晚上', [(d, 'evening') for d in ('mon', 'tue', 'wed', 'thu', 'fri')]),
    ('周一到周五下午', [(d, 'afternoon') for d in ('mon', 'tue', 'wed', 'thu', 'fri')]),
    ('周五和周六的白天，周日晚上', [('fri', 'daytime'), ('sat', 'daytime'), ('sun', 'evening')]),
    ('每天晚上', [('any', 'evening')]),
    ('随时都行', [('any', 'any')]),
    ('什么时候都可以', [('any', 'any')]),
    ('周三全天', [('wed', 'any')]),
    ('傍晚', [('any', 'evening')]),
    # Limiting "只 / 仅" is accepted; "不仅 / 不只" is not a negation.
    ('只有周六在家', [('sat', 'any')]),
    ('仅周日下午', [('sun', 'afternoon')]),
    ('不只周六，周日也行', [('sat', 'any'), ('sun', 'any')]),
    ('不仅仅是周六上午，周日上午也可以', [('sat', 'morning'), ('sun', 'morning')]),
    # Habitual wording is accepted (a window is a habit).
    ('一般周五白天在家', [('fri', 'daytime')]),
    ('通常周末晚上', [('sat', 'evening'), ('sun', 'evening')]),
    ('平时工作日早上', [(d, 'morning') for d in ('mon', 'tue', 'wed', 'thu', 'fri')]),
    ('Fridays during the day', [('fri', 'daytime')]),
    ('weekends in the morning', [('sat', 'morning'), ('sun', 'morning')]),
    ('usually weekday evenings', [(d, 'evening') for d in ('mon', 'tue', 'wed', 'thu', 'fri')]),
    ('Sat afternoon', [('sat', 'afternoon')]),
    ('not only Saturday, Sunday too', [('sat', 'any'), ('sun', 'any')]),
    ('anytime', [('any', 'any')]),
    ('every day in the evening', [('any', 'evening')]),
    # Refused: clock times, negation, questions, hypotheticals, tentative wording, unmapped times, nothing recognisable.
    ('9点以后', None), ('周五晚上七点', None), ('after 6pm', None), ('Friday at 10:30', None),
    ('周五不在家', None), ('周末别送', None), ('not on Fridays', None), ("I can't do weekends", None),
    ('周五白天可以吗', None), ('周五白天？', None), ('when are you coming', None),
    ('如果周五有空', None), ('if Friday works', None),
    ('可能周五白天', None), ('maybe Saturday', None),
    ('周五中午', None), ('凌晨', None), ('Friday at noon', None), ('midnight', None),
    ('我住在三楼', None), ('', None), (None, None), (' ', None),
]


@pytest.mark.parametrize('text,expected', CASES)
def test_normalization_rule_table(text, expected):
    out = W.normalize(text)
    assert out == (None if expected is None else [{'weekday': d, 'part': p} for d, p in expected]), (text, out)


def test_normalized_values_are_canonical_and_valid_for_the_descriptor():
    prop = PropertyDefinition(property_name='delivery_windows', value_type='json', nullable=True, type_descriptor=PropertyTypeDescriptor.model_validate(VALID))
    for text, expected in CASES:
        out = W.normalize(text)
        if out is not None:
            assert out == W.canonical(list(reversed(out))) and prop.normalize_value(out, field_name='v') == out
    assert W.canonical([{'weekday': 'any', 'part': 'any'}, {'weekday': 'mon', 'part': 'any'}, {'weekday': 'mon', 'part': 'any'}]) == [
        {'weekday': 'mon', 'part': 'any'}, {'weekday': 'any', 'part': 'any'}]
