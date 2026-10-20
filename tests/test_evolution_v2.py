"""NX-039 V2 (design §3.2, §3.3, §4.1, §6.2, §7.2; owner D7, D39-2, D39-4, D39-5, D39-7 decided 2026-10-20), pure: the gate registry
(each failed gate is its own rejection code), non-inferiority boundaries, reproducible anonymization and reports, and the change class
table with its bias to major. Synthetic ids, hashes and refs only."""
import json
import uuid
from pathlib import Path

import jsonschema
import pytest

from nexloop_eios.evolution import evaluation as E

ROOT = Path(__file__).resolve().parents[1]
REPORT = json.loads((ROOT / 'packages/contracts/evolution-evaluation-report.schema.json').read_text())
REF, OTHER = 'eios:property:Consumer/waterproof_concern', 'eios:property:Consumer/budget'
TERM, SEED, CREATED = 'a' * 64, '0' * 32, '2026-10-20T10:00:00Z'
FAST = {'latency_ms': {'parent': [10.0] * 50, 'candidate': [10.5] * 50}}


def cid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, label))


def bundle(design=('d1', 'd2'), validation=('v1',), version='nx039-gates-1'):
    ids = {k: cid(k) for k in design + validation}
    return {'candidate_id': cid('candidate'), 'design_case_ids': [ids[k] for k in design], 'validation_case_ids': [ids[k] for k in validation],
            'case_groups': {ids[k]: 'consumer:' + k for k in design + validation}, 'levels': ['L0'], 'evaluator_version': 'l0-2',
            'hard_gates_version': version, 'hard_gates': list(E.GATE_VERSIONS[version]), 'anonymize_seed': SEED,
            'non_inferiority': {'primary_metric': 'coverage', 'margin': 0.01, 'ties_accept': False}, 'budget_remaining': {'unit': 'CNY'}}


def accesses(b, *, term=TERM, other_refs=(REF, OTHER)):
    out = []
    for run in b['design_case_ids'] + b['validation_case_ids']:
        out.append({'access_id': cid('miss' + run), 'kind': 'access', 'run_id': run, 'term_hashes': [term], 'result_refs': [], 'ambiguity': False})
        out.append({'access_id': cid('hit' + run), 'kind': 'access', 'run_id': run, 'term_hashes': ['b' * 64],
                    'result_refs': [{'ref': r, 'revision': '1'} for r in other_refs], 'ambiguity': False})
    return out


ALIAS = {'change_kind': 'alias', 'target_refs': [REF], 'term_hashes': [TERM], 'run_ids': []}


def run(patch=ALIAS, *, b=None, acc=None, uses=(), inputs=FAST, dimension='semantic_content'):
    b = b or bundle()
    report = E.evaluate_l0(b, 'd' * 64, patch, acc if acc is not None else accesses(b), uses=list(uses), inputs=inputs, dimension=dimension,
                           created_at=CREATED)
    jsonschema.validate(report, REPORT)
    return report


def test_a_clean_alias_is_accepted_and_the_report_is_reproducible():
    r = run()
    assert (r['conclusion'], r['reason'], r['change_class'], r['failed_gates'], r['reproduced']) == ('accept', None, 'patch', [], True)
    assert [g['gate'] for g in r['hard_gates']] == list(E.GATE_VERSIONS['nx039-gates-1'])
    assert E.report_digest(r) == E.report_digest(run())  # M22, D39-2


@pytest.mark.parametrize('label,patch,kwargs,gate', [
    ('G1 unknown definition', {**ALIAS, 'target_refs': ['eios:property:Consumer/never_returned']}, {}, 'references_valid'),
    ('G1 structure', {**ALIAS, 'extra': 1}, {}, 'structure_valid'),
    ('G2 overreach', {**ALIAS, 'target_refs': ['eios:action:Order.cancel:1']}, {}, 'zero_overreach'),
    ('G3 cross tenant', ALIAS, {'stray': True}, 'zero_cross_tenant'),
    ('G5 protected ref lost', ALIAS, {'push_out': True, 'protected': True}, 'no_forbidden_regression'),
    ('G6 used ref pushed past the browse limit', ALIAS, {'push_out': True, 'used': True}, 'semantic_regression'),
    ('G7 latency not measured', ALIAS, {'inputs': {}}, 'cost_performance'),
    ('G7 latency p95 over 110%', ALIAS, {'inputs': {'latency_ms': {'parent': [10.0] * 50, 'candidate': [12.0] * 50}}}, 'cost_performance'),
    ('G7 alias ceiling per ref', ALIAS, {'inputs': {**FAST, 'alias_counts': {REF: 20}}}, 'cost_performance'),
    ('G8 conflict with a published alias', ALIAS, {'inputs': {**FAST, 'alias_index': {TERM: OTHER}}}, 'semantic_conflict'),
])
def test_each_failed_gate_is_its_own_rejection_code(label, patch, kwargs, gate):
    b = bundle()
    acc = accesses(b)
    uses = []
    if kwargs.get('stray'):
        acc.append({'access_id': cid('stray'), 'kind': 'access', 'run_id': cid('elsewhere'), 'term_hashes': ['c' * 64], 'result_refs': [], 'ambiguity': False})
    if kwargs.get('push_out'):
        # The miss access of every case now returns ten refs; the overlay ranks REF first and the tenth falls past the limit.
        full = [{'ref': f'eios:property:Consumer/p{i}', 'revision': '1'} for i in range(10)]
        for a in acc:
            if a['access_id'].startswith(cid('miss')[:0]) and a['term_hashes'] == [TERM]:
                a['result_refs'] = list(full)
                a['term_hashes'] = [TERM, 'e' * 64]
        last = full[-1]['ref']
        if kwargs.get('used'):
            first = next(a for a in acc if a['term_hashes'] == [TERM, 'e' * 64])
            uses = [{'access_id': cid('use'), 'kind': 'use', 'run_id': first['run_id'], 'accessed_id': first['access_id'],
                     'result_refs': [{'ref': last, 'revision': '1'}]}]
    inputs = kwargs.get('inputs', FAST)
    if kwargs.get('protected'):
        inputs = {**inputs, 'protected_refs': {full[-1]['ref']}}
    r = run(patch, b=b, acc=acc, uses=uses, inputs=inputs)
    assert gate in r['failed_gates'], (label, r['failed_gates'])
    assert (r['conclusion'], r['reason']) == ('reject', 'hard_gate_failed')
    failed = next(g for g in r['hard_gates'] if g['gate'] == gate)
    assert failed['result'] == 'fail' and failed['evidence_ids']


def test_gates_are_separate_from_the_score():
    """A candidate that scores far better is still rejected when one gate fails (M22)."""
    r = run(inputs={**FAST, 'alias_index': {TERM: OTHER}})
    assert r['scores']['design']['candidate'] > r['scores']['design']['parent'] and r['conclusion'] == 'reject'


def test_an_unknown_gate_version_is_never_answered():
    b = bundle()
    b['hard_gates_version'] = 'nx099-gates-1'
    with pytest.raises(ValueError):
        run(b=b)
    b = bundle()
    b['hard_gates'] = b['hard_gates'][:-1]
    with pytest.raises(ValueError):
        run(b=b)


@pytest.mark.parametrize('design,validation,expected', [
    ((0.50, 0.51), (0.50, 0.49), (True, None)),          # validation exactly at -1 point, design better
    ((0.50, 0.51), (0.50, 0.4899), (False, 'inferior')),  # just past the margin
    ((0.50, 0.50), (0.50, 0.50), (False, 'not_better')),  # equal is never an acceptance (D7)
    ((0.50, 0.495), (0.50, 0.50), (False, 'not_better')),  # within the margin but nothing strictly better
    ((0.50, 0.60), (0.50, 0.50), (True, None)),          # one side strictly better, the other equal
    ((0.40, 0.60), (0.70, 0.50), (False, 'inferior')),   # a large gain does not buy a loss on the holdout
])
def test_non_inferiority_boundaries(design, validation, expected):
    ok, why, _ = E.non_inferior(([design[0]], [design[1]]), ([validation[0]], [validation[1]]), 0.01)
    assert (ok, why) == expected


def test_anonymization_is_reproducible_from_the_frozen_seed():
    ids = [cid(f'case{i}') for i in range(40)]
    first, again = E.anonymize_swaps(SEED, ids), E.anonymize_swaps(SEED, ids)
    assert first == again and {s['swapped'] for s in first} == {True, False}
    assert E.anonymize_swaps('1' * 32, ids) != first
    r = run()
    assert r['swaps'] == E.anonymize_swaps(SEED, sorted(r['swaps'][i]['case_id'] for i in range(len(r['swaps']))))


def test_a_report_that_does_not_reproduce_is_incomplete(monkeypatch):
    calls = []
    real = E._one_pass

    def drifting(*a, **k):
        calls.append(1)
        out = real(*a, **k)
        return {**out, 'elapsed_ms': len(calls)}  # the second pass differs
    monkeypatch.setattr(E, '_one_pass', drifting)
    r = E.evaluate_l0(bundle(), 'd' * 64, ALIAS, accesses(bundle()), inputs=FAST, created_at=CREATED)
    assert (r['conclusion'], r['reason'], r['reproduced']) == ('incomplete', 'not_reproducible', False)


@pytest.mark.parametrize('dimension,patch,kwargs,expected', [
    ('semantic_content', ALIAS, {}, 'patch'),
    ('semantic_content', {**ALIAS, 'target_refs': [REF, OTHER]}, {}, 'minor'),
    ('semantic_content', ALIAS, {'alias_index': {TERM: OTHER}}, 'major'),          # a term moved to another definition
    ('semantic_content', {**ALIAS, 'term_hashes': []}, {}, 'major'),                # nothing to show it is additive
    ('semantic_content', {**ALIAS, 'change_kind': 'description'}, {}, 'major'),     # unknown whether a note exists: uncertain
    ('semantic_content', {**ALIAS, 'change_kind': 'description'}, {'noted_refs': {REF}}, 'patch'),
    ('semantic_content', {**ALIAS, 'change_kind': 'description'}, {'noted_refs': set()}, 'minor'),
    ('semantic_content', {**ALIAS, 'change_kind': 'rename'}, {}, 'major'),
    ('semantic_content', {**ALIAS, 'target_refs': ['eios:action:Order.cancel:1']}, {}, 'refused'),
    ('semantic_content', {'change_kind': 'alias', 'target_refs': [REF]}, {}, 'refused'),
    ('semantic_tool', ALIAS, {}, 'major'),
    ('ontology_schema', ALIAS, {}, 'major'),
])
def test_the_change_class_table_leans_to_major(dimension, patch, kwargs, expected):
    b = bundle()
    assert E.classify_change(dimension, patch, accesses(b), **kwargs) == expected


def test_a_major_change_is_rejected_as_breaking_whatever_it_scores():
    r = run({**ALIAS, 'change_kind': 'description'})
    assert (r['change_class'], r['conclusion'], r['reason'], r['failed_gates']) == ('major', 'reject', 'breaking_change', [])
