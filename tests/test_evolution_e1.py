"""NX-038 E1 (design §4.2 steps 3 and 5, §12.7): the EvoOntology rules ported into nexloop_eios.evolution.

The evaluation gate (decide_gt, decide_judge, reproducible anonymize, decode), the adapter result contract, the
`evolution-candidate` round trip (contract -> code -> contract, the contract unchanged), the chain lifecycle rules (frozen
budget, reject keeps running, legitimate incomplete stops, acceptance only through the frozen gate and never a publication),
and isolation: the package is pure (no database, network, process or effect import; ADR-007, D18)."""
import ast
import json
import random
from pathlib import Path

import jsonschema
import pytest

from nexloop_eios.evolution.adapter import EvaluationAdapter, normalize_result
from nexloop_eios.evolution.candidate import Candidate, TransitionRefused
from nexloop_eios.evolution.gate import EvaluationGate, GateInputError
from nexloop_eios.evolution.lifecycle import BudgetExhausted, Chain, LifecycleError
from support.tenants import TENANT_OTHER

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / 'packages/contracts/evolution-candidate.schema.json').read_text())
EXAMPLE = json.loads((ROOT / 'packages/contracts/examples/evolution-candidate.valid.json').read_text())
PACKAGE = ROOT / 'packages/eios-core/src/nexloop_eios/evolution'


# ---- gate ---------------------------------------------------------------------------------------------------------------
def test_ground_truth_gate_is_strict_by_default_and_never_accepts_an_equal_score():
    assert EvaluationGate.decide_gt([0.5, 0.7], [0.6, 0.8])['accept'] is True
    assert EvaluationGate.decide_gt([0.5, 0.7], [0.5, 0.7])['accept'] is False
    # D7 non-inferiority: within the frozen margin and not equal; an equal score is still not an acceptance.
    assert EvaluationGate.decide_gt([0.80], [0.795], non_inferiority_margin=0.01)['accept'] is True
    assert EvaluationGate.decide_gt([0.80], [0.80], non_inferiority_margin=0.01)['accept'] is False
    assert EvaluationGate.decide_gt([0.80], [0.78], non_inferiority_margin=0.01)['accept'] is False
    for bad in (([], [1.0]), ([1.0], [1.0, 2.0]), ([float('nan')], [1.0]), ([True], [1.0]), (['1'], [1.0])):
        with pytest.raises(GateInputError):
            EvaluationGate.decide_gt(*bad)
    with pytest.raises(GateInputError):
        EvaluationGate.decide_gt([1.0, 2.0], [1.0, 2.0], case_ids=['a', 'a'])
    with pytest.raises(GateInputError):
        EvaluationGate.decide_gt([1.0], [1.0], non_inferiority_margin=-0.1)


def test_judge_gate_needs_more_wins_and_no_critical_error():
    win = {'winner': 'candidate', 'critical_error': False}
    assert EvaluationGate.decide_judge([win, win, {'winner': 'parent', 'critical_error': False}])['accept'] is True
    assert EvaluationGate.decide_judge([win, {'winner': 'tie', 'critical_error': True}])['accept'] is False
    assert EvaluationGate.decide_judge([win, {'winner': 'parent', 'critical_error': False}])['accept'] is False
    for bad in ([], [{'winner': 'A', 'critical_error': False}], [{'winner': 'tie'}]):
        with pytest.raises(GateInputError):
            EvaluationGate.decide_judge(bad)


def test_anonymization_is_reproducible_and_decodes_back():
    with pytest.raises(GateInputError):
        EvaluationGate.anonymize('p', 'c')  # never the module-global random generator
    first = [EvaluationGate.anonymize('p', 'c', rng=random.Random(7))['swap'] for _ in range(1)]
    rng_a, rng_b = random.Random(20261020), random.Random(20261020)
    assert [EvaluationGate.anonymize('p', 'c', rng=rng_a)['swap'] for _ in range(20)] == [EvaluationGate.anonymize('p', 'c', rng=rng_b)['swap'] for _ in range(20)]
    labelled = EvaluationGate.anonymize('parent answer', 'candidate answer', swap=True)
    assert labelled == {'A': 'candidate answer', 'B': 'parent answer', 'A_is': 'candidate', 'B_is': 'parent', 'swap': True} and first
    assert EvaluationGate.decode({'winner': 'A', 'critical_error': 'B', 'reason': 'r'}, labelled) == {'winner': 'candidate', 'critical_error': False, 'reason': 'r'}
    assert EvaluationGate.decode({'winner': 'tie', 'critical_error': 'A'}, labelled)['critical_error'] is True


def test_adapter_results_are_normalized():
    assert normalize_result({'metrics': {'coverage': 0.9}}) == {'metrics': {'coverage': 0.9}, 'cases': [], 'artifact_refs': []}
    for bad in (None, {'metrics': 1}, {'metrics': {}, 'cases': 'x'}):
        with pytest.raises(ValueError):
            normalize_result(bad)

    class Replay:
        def evaluate(self, subject, cases):
            return {'metrics': {'n': len(cases)}}
    assert isinstance(Replay(), EvaluationAdapter)


# ---- contract round trip ------------------------------------------------------------------------------------------------
def candidate(**change):
    return Candidate.from_contract({**EXAMPLE, **change})


def test_evolution_candidate_round_trips_contract_to_code_to_contract():
    record = Candidate.from_contract(EXAMPLE)
    wire = record.to_contract()
    assert wire == EXAMPLE
    jsonschema.validate(wire, SCHEMA)
    assert record.publication_permit_ref is None and isinstance(record.design_case_ids, tuple)
    # Every status of the contract round-trips; a non-null publication permit or an unknown field is refused.
    for status in SCHEMA['properties']['status']['enum']:
        jsonschema.validate(candidate(status=status).to_contract(), SCHEMA)
    for bad in ({'publication_permit_ref': 'permit:1'}, {'status': 'published'}, {'unexpected': 1}, {'maximum_rounds': 33}):
        with pytest.raises(Exception):
            Candidate.from_contract({**EXAMPLE, **bad})


def test_candidate_transitions_follow_the_contract_states():
    proposed = candidate(status='proposed')
    assert proposed.moved_to('evaluating').moved_to('accepted_for_release').status == 'accepted_for_release'
    for start, target in (('proposed', 'accepted_for_release'), ('proposed', 'rejected'), ('rejected', 'evaluating'),
                          ('accepted_for_release', 'evaluating'), ('incomplete', 'proposed')):
        with pytest.raises(TransitionRefused):
            candidate(status=start).moved_to(target)


# ---- lifecycle -----------------------------------------------------------------------------------------------------------
def chain(**kw):
    base = dict(tenant_id=EXAMPLE['tenant_id'], parent_revision=EXAMPLE['parent_revision'], max_rounds=2, protocol='ground_truth')
    return Chain.start(**{**base, **kw})


def round_candidate(n, rounds=2):
    return candidate(status='proposed', candidate_id=f'00000000-0000-4000-8000-00000000000{n}', candidate_revision=f'semantic-candidate-{n}',
                     maximum_rounds=rounds)


GATE_OK = {'protocol': 'ground_truth', 'unacceptable_regressions': False, 'parent_scores': [0.5, 0.6], 'candidate_scores': [0.7, 0.8], 'case_ids': ['a', 'b']}


def test_reject_keeps_the_chain_running_and_accept_ends_in_accepted_for_release_not_published():
    c = chain().begin_round(round_candidate(1)).start_evaluation().reject()
    assert c.status == 'running' and c.rejected_count() == 1 and c.current.status == 'rejected'
    c = c.begin_round(round_candidate(2)).start_evaluation()
    with pytest.raises(LifecycleError, match='did not pass'):
        c.accept({**GATE_OK, 'candidate_scores': [0.5, 0.6]})
    done = c.accept(GATE_OK)
    assert done.status == 'accepted_for_release' and done.current.status == 'accepted_for_release'
    assert done.current.to_contract()['publication_permit_ref'] is None
    with pytest.raises(LifecycleError):
        done.begin_round(round_candidate(3))


def test_the_gate_input_must_rule_out_regressions_and_match_the_frozen_protocol():
    c = chain().begin_round(round_candidate(1)).start_evaluation()
    for bad, message in (({**GATE_OK, 'unacceptable_regressions': None}, 'regressions'), ({**GATE_OK, 'protocol': 'llm_judge'}, 'protocol'),
                         (None, 'gate input')):
        with pytest.raises(LifecycleError, match=message):
            c.accept(bad)
    margin = chain(non_inferiority_margin=0.01).begin_round(round_candidate(1)).start_evaluation()
    assert margin.accept({**GATE_OK, 'candidate_scores': [0.495, 0.6]}).status == 'accepted_for_release'


def test_the_frozen_budget_ends_the_chain_incomplete_and_only_a_named_confirmation_extends_it():
    c = chain().begin_round(round_candidate(1)).start_evaluation().reject().begin_round(round_candidate(2)).start_evaluation().reject()
    with pytest.raises(BudgetExhausted) as spent:
        c.begin_round(round_candidate(3))
    ended = spent.value.args[0]
    assert (ended.status, ended.end_reason) == ('incomplete', 'budget_exhausted')
    with pytest.raises(LifecycleError):
        c.extend_budget(3, confirmed_by='')
    with pytest.raises(ValueError):
        c.extend_budget(2, confirmed_by='owner')
    grown = c.extend_budget(3, confirmed_by='owner')
    assert grown.begin_round(round_candidate(3, rounds=3)).current.status == 'proposed'
    with pytest.raises(LifecycleError, match='round budget'):
        grown.begin_round(round_candidate(3, rounds=2))  # a candidate must carry the frozen budget


def test_incomplete_needs_a_legitimate_external_reason():
    c = chain(max_rounds=3).begin_round(round_candidate(1, 3)).start_evaluation()
    with pytest.raises(ValueError):
        c.mark_incomplete('stagnation')
    with pytest.raises(LifecycleError, match='rejected candidates'):
        c.mark_incomplete('missing_data')
    with pytest.raises(LifecycleError, match='not spent'):
        c.mark_incomplete('budget_exhausted')
    stopped = c.mark_incomplete('user_interrupted')
    assert (stopped.status, stopped.end_reason, stopped.current.status) == ('incomplete', 'user_interrupted', 'incomplete')
    two = chain(max_rounds=3).begin_round(round_candidate(1, 3)).start_evaluation().reject().begin_round(round_candidate(2, 3)).start_evaluation().reject()
    assert two.mark_incomplete('missing_data').end_reason == 'missing_data'  # D17: a deletion invalidates the chain
    with pytest.raises(LifecycleError):
        chain().begin_round(candidate(status='proposed', tenant_id=TENANT_OTHER, maximum_rounds=2))


# ---- isolation (ADR-007, D18) ------------------------------------------------------------------------------------------
def test_the_evolution_package_is_pure_logic_without_storage_network_or_effects():
    allowed = {'__future__', 'dataclasses', 'typing', 'math', 'random', 'nexloop_eios.contracts', 'nexloop_eios.evolution.candidate',
               'nexloop_eios.evolution.gate',
               # E4 attribution (pure rules over E2/E3 values; the store port is nexloop_eios.evolution_store, outside the package)
               'hashlib', 'json', 'uuid', 'collections', 'nexloop_eios.evolution_traces'}
    for path in PACKAGE.glob('*.py'):
        tree = ast.parse(path.read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert imported <= allowed, (path.name, imported - allowed)
        names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        assert not {n for n in names if 'publish' in n.lower() or 'activate' in n.lower()}, path.name
