"""NX-040 F4 (design §4.2-§4.6; D23, D25, D26), pure: the evaluator's nx040-gates-1 answer. Each gate G9-G13 (and the G1-G8 it
inherits for a Schema change) fails with its own evidence; the tool regression set needs a valid and an invalid case per Action and
must agree under both revisions; a batched publication passes the migration bound; the report is reproducible."""
import json
import uuid
from pathlib import Path

import jsonschema
import pytest

from nexloop_eios.evolution.evaluation import GATE_VERSIONS
from nexloop_eios.schema_gates import GATES, GATES_VERSION, evaluate_schema, replay_tools

ROOT = Path(__file__).resolve().parents[1]
REPORT = json.loads((ROOT / 'packages/contracts/evolution-evaluation-report.schema.json').read_text())
INPUT = {'type': 'object', 'additionalProperties': False, 'required': ['consumer_id'], 'properties': {'consumer_id': {'type': 'string'}, 'note': {'type': 'string'}}}
SCHEMAS = {'Consumer.edit': {'parent': INPUT, 'candidate': INPUT}}
CASES = [{'id': 'edit-valid', 'action': 'Consumer.edit', 'params': {'consumer_id': 'c1'}, 'expect': 'valid'},
         {'id': 'edit-invalid', 'action': 'Consumer.edit', 'params': {'note': 'x'}, 'expect': 'invalid'}]
GOOD = {'tool_cases': CASES, 'action_schemas': SCHEMAS, 'context_volume': {'parent': 1000, 'candidate': 1020}, 'reflow_reserved': True, 'reflow_cost': '0.2'}


def cid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, label))


def bundle():
    return {'candidate_id': cid('c'), 'design_case_ids': [cid('d')], 'validation_case_ids': [cid('v')], 'evaluator_version': 'f4-1',
            'hard_gates_version': GATES_VERSION, 'hard_gates': list(GATES), 'anonymize_seed': '0' * 32, 'budget_remaining': {'unit': 'CNY'}}


def parent(**over):
    p = {'types': {'Consumer': {'type_name': 'Consumer', 'version': 1, 'only_edit_via_actions': True,
                                'properties': [{'property_name': 'display_name', 'value_type': 'string', 'nullable': True}]}},
         'object_counts': {'Consumer': 10}, 'actions': [], 'embedding_dimension': 64, 'reflow_cap': {'vectors': 100, 'cost': 1}}
    p.update(over)
    return p


def change(**over):
    c = {'op': 'add_optional_property', 'type_name': 'Consumer', 'property': {'property_name': 'preferred_store', 'value_type': 'string', 'nullable': True},
         'reflow': {'vectors': 10, 'cost': 0.2}}
    c.update(over)
    return c


def run(c=None, p=None, inputs=GOOD):
    r = evaluate_schema(bundle(), 'd' * 64, c or change(), p or parent(), inputs=inputs, created_at='2026-10-20T14:00:00Z')
    jsonschema.validate(r, REPORT)
    return r


def test_the_registry_holds_nx040_gates_1_in_the_same_mechanism():
    assert GATE_VERSIONS['nx040-gates-1'] == GATES and GATES[:10] == GATE_VERSIONS['nx039-gates-1']


def test_an_additive_change_passes_every_gate_and_is_minor():
    r = run()
    assert (r['conclusion'], r['change_class'], r['failed_gates'], r['reproduced']) == ('accept', 'minor', [], True)
    assert r == run()  # deterministic


@pytest.mark.parametrize('label,kwargs,gate', [
    ('G9 DDL', {'c': change(ddl=['alter table'])}, 'index_migration_impact'),
    ('G10 over the migration limit', {'p': parent(object_counts={'Consumer': 10_001})}, 'migration_bounded'),
    ('G11 reflow over its cap', {'c': change(reflow={'vectors': 101, 'cost': 0.2})}, 'reflow_budget'),
    ('G11 reflow not reserved (evolution budget refused)', {'inputs': {**GOOD, 'reflow_reserved': False}}, 'reflow_budget'),
    ('G11 embedding dimension', {'c': change(embedding_dimension=128)}, 'reflow_budget'),
    ('G12 tool signature', {'p': parent(actions=[{'stable_name': 'Consumer.edit', 'version': 1, 'object_types': [{'stable_name': 'Consumer', 'version': 1}],
                                                   'input_schema_hash': 'h', 'capability_schema_hash': 'c', 'risk_level': 'low', 'effect_categories': []}])},
     'tool_context_compatible'),
    ('G12 incomplete regression set', {'inputs': {**GOOD, 'tool_cases': CASES[:1]}}, 'tool_context_compatible'),
    ('G12 context volume over 105%', {'inputs': {**GOOD, 'context_volume': {'parent': 1000, 'candidate': 1100}}}, 'tool_context_compatible'),
    ('G13 authority content', {'c': change(op='add_object_type', type_name='Wallet', actions=[{'stable_name': 'Wallet.grant'}],
                                           definition={'type_name': 'Wallet', 'version': 1, 'only_edit_via_actions': True, 'properties': []})}, 'authority_unchanged'),
    ('G2 restricted name', {'p': parent(restricted_names=['preferred_store'])}, 'zero_overreach'),
    ('G1 unknown type', {'c': change(type_name='Nothing')}, 'references_valid'),
    ('G5 regression changed', {'inputs': {**GOOD, 'action_schemas': {'Consumer.edit': {'parent': INPUT, 'candidate': {**INPUT, 'required': ['consumer_id', 'note']}}}}},
     'no_forbidden_regression'),
])
def test_each_gate_fails_with_its_own_code(label, kwargs, gate):
    r = run(**kwargs)
    assert gate in r['failed_gates'] and (r['conclusion'], r['reason']) == ('reject', 'hard_gate_failed'), (label, r['failed_gates'])


def test_a_batched_publication_passes_the_migration_bound():
    r = run(p=parent(object_counts={'Consumer': 10_001}), inputs={**GOOD, 'batch': True})
    assert 'migration_bounded' not in r['failed_gates'] and r['conclusion'] == 'accept'
    assert next(g for g in r['hard_gates'] if g['gate'] == 'migration_bounded')['evidence_ids'] == ['migration:batched']


def test_the_tool_regression_set_valid_and_invalid_cases():
    assert replay_tools(CASES, SCHEMAS) == {'failures': [], 'incomplete': [], 'cases': 2}
    wrong = [{**CASES[0], 'expect': 'invalid'}, CASES[1]]
    assert replay_tools(wrong, SCHEMAS)['failures'] == ['case:edit-valid:Consumer.edit']  # a valid case expected invalid is a failure
    assert replay_tools([{'id': 'n', 'action': 'Nope.do', 'params': {}, 'expect': 'valid'}], SCHEMAS)['failures'] == ['case:n:unknown']
    assert replay_tools(CASES[1:], SCHEMAS)['incomplete'] == ['action:Consumer.edit']


def test_a_major_change_is_closed():
    r = run(c={'op': 'rename_property', 'type_name': 'Consumer'})
    assert r['change_class'] == 'major' and r['conclusion'] == 'reject'


def test_only_an_nx040_bundle_is_answered():
    b = bundle()
    b['hard_gates_version'] = 'nx039-gates-1'
    with pytest.raises(ValueError):
        evaluate_schema(b, 'd' * 64, change(), parent(), inputs=GOOD, created_at='2026-10-20T14:00:00Z')
