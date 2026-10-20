"""NX-040 F1 (design §4.1-§4.5, §6, §7.2; owner D21-D27 decided 2026-10-20): the compatibility library, pure. Each whitelisted operation
has a positive case; every fixed code has a case that returns it; the boundaries (a required property, a changed type, a removed
value) are always refused; the change class meets NX-039's classify_change."""
import copy

import pytest

from nexloop_eios.evolution import schema_compat as S
from nexloop_eios.evolution.evaluation import classify_change

SEEN: set[str] = set()


def parent(**over):
    base = {
        'types': {'Consumer': {'type_name': 'Consumer', 'version': 1, 'only_edit_via_actions': True, 'properties': [
            {'property_name': 'display_name', 'value_type': 'string', 'nullable': True},
            {'property_name': 'tier', 'value_type': 'string', 'nullable': True, 'type_descriptor': {'kind': 'string', 'enum': ['gold', 'silver']}}]}},
        'object_counts': {'Consumer': 120},
        'vocabulary_aliases': {'Consumer/tier': ['Platinum']},
        'actions': [{'stable_name': 'Consumer.update', 'version': 1, 'object_types': [{'stable_name': 'Consumer', 'version': 1}],
                     'input_schema_hash': 'h1', 'capability_schema_hash': 'c1', 'risk_level': 'low', 'effect_categories': []}],
        'semantic_refs': ['Consumer', 'Consumer/tier'], 'embedding_dimension': 64, 'reflow_cap': {'vectors': 100, 'cost': 1.0}}
    base.update(over)
    return base


SUCCESSOR = {'stable_name': 'Consumer.update', 'replaces': 1, 'version': 2, 'object_types': [{'stable_name': 'Consumer', 'version': 2}],
             'input_schema_hash': 'h1', 'capability_schema_hash': 'c1', 'risk_level': 'low', 'effect_categories': [], 'required_parameters': []}
NEW_PROP = {'property_name': 'preferred_store', 'value_type': 'string', 'nullable': True}


def add_property(**over):
    change = {'op': 'add_optional_property', 'type_name': 'Consumer', 'property': dict(NEW_PROP), 'successor_actions': [dict(SUCCESSOR)],
              'reflow': {'vectors': 10, 'cost': 0.1}, 'tool_regression': {'parent': 'digest-a', 'candidate': 'digest-a'}}
    for k, v in over.items():
        change[k] = v
    return change


def add_value(value='bronze', **over):
    return {'op': 'add_vocabulary_value', 'type_name': 'Consumer', 'property_name': 'tier', 'value': value,
            'successor_actions': [dict(SUCCESSOR)], **over}


def add_type(**over):
    definition = {'type_name': 'Warranty', 'version': 1, 'only_edit_via_actions': True,
                  'properties': [{'property_name': 'months', 'value_type': 'integer', 'nullable': True}]}
    definition.update(over.pop('definition', {}))
    return {'op': 'add_object_type', 'type_name': 'Warranty', 'definition': definition, **over}


def verdict(change, p=None):
    v = S.judge(change, p or parent())
    SEEN.update(v['codes'])
    return v


# --- positives --------------------------------------------------------------------------------------------------------------------

def test_add_optional_property_is_minor_and_passes_every_dimension():
    v = verdict(add_property())
    assert (v['allowed'], v['change_class'], v['reason'], v['codes'], v['auto_publish_eligible']) == (True, 'minor', None, [], True)
    assert {k: d['result'] for k, d in v['dimensions'].items()} == {'migration': 'pass', 'index': 'pass', 'tool_signature': 'pass', 'contract': 'pass'}
    props = {p['property_name'] for p in v['candidate_definition']['properties']}
    assert props == {'display_name', 'tier', 'preferred_store'} and v['candidate_definition']['version'] == 2
    with_default = add_property(property={'property_name': 'visits', 'value_type': 'integer', 'default_value': 0})
    assert verdict(with_default)['allowed'] is True  # a default instead of nullable


def test_add_vocabulary_value_is_minor():
    v = verdict(add_value())
    assert (v['allowed'], v['change_class'], v['auto_publish_eligible']) == (True, 'minor', True)
    tier = next(p for p in v['candidate_definition']['properties'] if p['property_name'] == 'tier')
    assert tier['type_descriptor']['enum'] == ['gold', 'silver', 'bronze']


def test_add_object_type_is_minor_but_only_published_by_hand():
    v = verdict(add_type())
    assert (v['allowed'], v['change_class'], v['auto_publish_eligible']) == (True, 'minor', False)  # D22
    assert v['dimensions']['migration']['evidence_ids'] == ['new_type:no_instances']


# --- every fixed code -------------------------------------------------------------------------------------------------------------

CLOSED = [
    ('operation_not_whitelisted', lambda: {'op': 'rename_property', 'type_name': 'Consumer'}, None),
    ('relation_type_closed', lambda: {'op': 'add_relation_type', 'type_name': 'Consumer'}, None),
    ('unknown_type', lambda: add_property(type_name='Nothing'), None),
    ('unknown_property', lambda: add_value(property_name='nothing'), None),
    ('type_exists', lambda: add_type(type_name='Consumer', definition={'type_name': 'Consumer'}), None),
    ('property_exists', lambda: add_property(property={**NEW_PROP, 'property_name': 'display_name'}), None),
    ('value_type_not_allowed', lambda: add_property(property={**NEW_PROP, 'value_type': 'json'}), None),
    ('visibility_above_normal', lambda: add_property(property={**NEW_PROP, 'visibility': 'prominent'}), None),
    ('no_default_not_nullable', lambda: add_property(property={'property_name': 'x', 'value_type': 'string'}), None),
    ('vocabulary_on_new_property', lambda: add_property(property={**NEW_PROP, 'type_descriptor': {'kind': 'string', 'enum': ['a']}}), None),
    ('not_closed_vocabulary', lambda: add_value(property_name='display_name'), None),
    ('vocabulary_value_exists', lambda: add_value('gold'), None),
    ('vocabulary_alias_conflict', lambda: add_value('platinum'), None),
    ('edit_mode_open', lambda: add_type(definition={'only_edit_via_actions': False}), None),
    ('definition_invalid', lambda: add_type(definition={'type_name': 'Other'}), None),
    ('parent_incompatible', lambda: add_property(), parent(types={'Consumer': {'type_name': 'bad name!'}})),
    ('breaking_required_property', lambda: add_property(property={**NEW_PROP, 'required': True}), None),
    ('authority_content', lambda: add_type(actions=[{'stable_name': 'Warranty.grant'}]), None),
    ('breaking_primary_key', lambda: add_type(definition={'primary_key': ['months']}), None),
    ('breaking_relation_endpoints', lambda: add_type(relations=[{'relation_name': 'covers'}]), None),
    ('requires_ddl', lambda: add_property(indexes=['create index on ontology.objects((properties->>\'preferred_store\'))']), None),
    ('breaking_metric_definition', lambda: add_property(metric_definitions=[{'metric': 'retention'}]), None),
    ('migration_over_limit', lambda: add_property(), parent(object_counts={'Consumer': S.MIGRATION_LIMIT + 1})),
    ('embedding_dimension_changed', lambda: add_property(embedding_dimension=128), None),
    ('reflow_over_budget', lambda: add_property(reflow={'vectors': 101, 'cost': 0.1}), None),
    ('semantic_ref_missing', lambda: add_property(), parent(semantic_refs=['Consumer/gone'])),
    ('tool_signature_changed', lambda: add_property(successor_actions=[{**SUCCESSOR, 'input_schema_hash': 'h2'}]), None),
    ('tool_required_parameter', lambda: add_property(successor_actions=[{**SUCCESSOR, 'required_parameters': ['preferred_store']}]), None),
    ('successor_not_one_to_one', lambda: add_property(successor_actions=[]), None),
    ('contract_changed', lambda: add_property(contracts=['ontology-mutation.schema.json']), None),
]


@pytest.mark.parametrize('code,change,p', CLOSED, ids=[c[0] for c in CLOSED])
def test_each_code_is_returned_and_closes_the_change(code, change, p):
    v = verdict(change(), p)
    assert code in v['codes'] and v['allowed'] is False and v['candidate_definition'] is None and v['auto_publish_eligible'] is False
    if code == 'relation_type_closed':
        assert (v['change_class'], v['reason']) == ('refused', 'refused')
    elif code in S._MAJOR:
        assert (v['change_class'], v['reason']) == ('major', 'breaking_change')


@pytest.mark.parametrize('label,mutate,code', [
    ('required property', lambda d: d['properties'][0].update(required=True, nullable=False, default_value='x'), 'breaking_required_property'),
    ('changed type', lambda d: d['properties'][0].update(value_type='integer'), 'breaking_change_value_type'),
    ('removed value', lambda d: d['properties'][1]['type_descriptor'].update(enum=['gold']), 'breaking_remove_vocabulary_value'),
    ('dropped vocabulary', lambda d: d['properties'][1].pop('type_descriptor'), 'breaking_remove_vocabulary_value'),
    ('removed property', lambda d: d['properties'].pop(0), 'breaking_remove_property'),
    ('primary key', lambda d: (d['properties'][0].update(required=True, nullable=False), d.update(primary_key=['display_name'])), 'breaking_primary_key'),
])
def test_boundaries_are_always_refused_with_their_fine_code(label, mutate, code):
    p = parent()
    definition = copy.deepcopy(p['types']['Consumer'])
    definition['version'] = 2
    mutate(definition)
    v = verdict({'op': 'replace_type_version', 'type_name': 'Consumer', 'definition': definition})
    assert v['change_class'] == 'major' and v['reason'] == 'breaking_change' and {'operation_not_whitelisted', code} <= set(v['codes']), label


def test_the_vendored_check_is_reused_unchanged():
    """assert_object_compatible still lets an enum be dropped (its behaviour); the library's stricter rule refuses it."""
    from eios.ontology.models import ObjectTypeDefinition
    from eios.ontology.semantics import assert_object_compatible
    p = parent()['types']['Consumer']
    dropped = copy.deepcopy(p)
    dropped['properties'][1].pop('type_descriptor')
    assert_object_compatible(ObjectTypeDefinition.model_validate(p), ObjectTypeDefinition.model_validate(dropped))  # no exception
    assert 'breaking_remove_vocabulary_value' in S.breaking_codes(ObjectTypeDefinition.model_validate(p), ObjectTypeDefinition.model_validate(dropped))


def test_classification_meets_nx039_classify_change():
    assert classify_change('ontology_schema', add_property(), [], schema_parent=parent()) == 'minor'
    assert classify_change('ontology_schema', add_type(), [], schema_parent=parent()) == 'minor'
    assert classify_change('ontology_schema', {'op': 'add_relation_type'}, [], schema_parent=parent()) == 'refused'
    assert classify_change('ontology_schema', {'op': 'rename_property'}, [], schema_parent=parent()) == 'major'
    assert classify_change('ontology_schema', add_property(contracts=['x']), [], schema_parent=parent()) == 'major'  # a failed dimension: closed
    assert classify_change('ontology_schema', add_property(), []) == 'major'  # without the Schema Parent: as before


def test_every_code_has_a_case():
    for code, change, p in CLOSED:
        verdict(change(), p)
    for code in ('breaking_remove_property', 'breaking_change_value_type', 'breaking_remove_vocabulary_value'):
        SEEN.add(code)  # covered by test_boundaries_are_always_refused_with_their_fine_code
    assert set(S.CODES) <= SEEN, sorted(set(S.CODES) - SEEN)
