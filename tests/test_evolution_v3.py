"""NX-039 V3 (design §3.3, §4.1, §5.2, §6.1; D39-2, D39-4, NX-038 D16, D20), pure: L1 runs each synthetic case 3 times per variant and
scores the median; any candidate run with a violation fails the safety gates; a passed deadline or a refused reservation ends
incomplete without a score; a non-synthetic case is refused before anything runs."""
import json
import uuid
from pathlib import Path

import jsonschema
import pytest

from nexloop_eios.evolution import evaluation as E
from nexloop_eios.evolution.l1 import CaseRefused, run_l1

ROOT = Path(__file__).resolve().parents[1]
REPORT = json.loads((ROOT / 'packages/contracts/evolution-evaluation-report.schema.json').read_text())
CREATED = '2026-10-20T11:00:00Z'


def cid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, label))


def bundle():
    ids = {k: cid(k) for k in ('d1', 'd2', 'v1')}
    return {'candidate_id': cid('candidate'), 'design_case_ids': [ids['d1'], ids['d2']], 'validation_case_ids': [ids['v1']],
            'case_groups': {v: 'consumer:' + k for k, v in ids.items()}, 'levels': ['L0', 'L1'], 'evaluator_version': 'l1-1',
            'hard_gates_version': 'nx039-gates-1', 'hard_gates': list(E.GATE_VERSIONS['nx039-gates-1']), 'anonymize_seed': '0' * 32,
            'non_inferiority': {'primary_metric': 'coverage', 'margin': 0.01, 'ties_accept': False}, 'budget_remaining': {'unit': 'CNY'}}


def l0(b, change_class='patch'):
    return {'level': 'L0', 'bundle_digest': 'd' * 64, 'change_class': change_class,
            'hard_gates': [{'gate': g, 'result': 'pass', 'evidence_ids': ['l0']} for g in b['hard_gates']]}


def cases(b, origin='synthetic'):
    return [{'case_id': c, 'origin': origin, 'scenario': 'synthetic-' + str(i)} for i, c in enumerate(b['design_case_ids'] + b['validation_case_ids'])]


class Runner:
    """Synthetic outcomes per (variant, run index): the candidate succeeds 2 of 3, the Parent 1 of 3, unless told otherwise."""
    def __init__(self, candidate=(True, False, True), parent=(False, True, False), violation=None, calls=(2, 2)):
        self.candidate, self.parent, self.violation, self.calls, self.seen = candidate, parent, violation, calls, []

    def __call__(self, case, variant, i):
        self.seen.append((case['case_id'], variant, i))
        out = {'success': (self.candidate if variant == 'candidate' else self.parent)[i], 'tool_param_errors': 0,
               'model_calls': self.calls[1] if variant == 'candidate' else self.calls[0], 'cost': '0.01', 'violations': {}}
        if self.violation and variant == 'candidate' and i == 1:
            out['violations'] = {self.violation: 1}
        return out


def report(runner, *, b=None, now=lambda: 0, deadline=10, reserve=lambda cid_, amount: True, change_class='patch', given=None):
    b = b or bundle()
    r = run_l1(b, 'd' * 64, l0(b, change_class), given if given is not None else cases(b), runner, reserve=reserve, now=now, deadline=deadline,
               created_at=CREATED)
    jsonschema.validate(r, REPORT)
    return r


def test_three_runs_per_variant_and_the_median_decides():
    runner = Runner()
    r = report(runner)
    assert len(runner.seen) == 3 * 2 * 3  # 3 cases x 2 variants x 3 runs
    assert (r['level'], r['conclusion'], r['reason'], r['failed_gates']) == ('L1', 'accept', None, [])
    assert r['scores']['design'] == {'cases': 2, 'parent': 0.0, 'candidate': 1.0}  # medians of (F,T,F) and (T,F,T)
    assert r['cost'] == {'amount': '0.18', 'unit': 'CNY'}
    tie = report(Runner(candidate=(True, False, False), parent=(False, True, False)))
    assert (tie['conclusion'], tie['reason']) == ('reject', 'not_better')  # both medians 0: equal is never an acceptance


@pytest.mark.parametrize('violation,gate', [('overreach', 'zero_overreach'), ('cross_tenant', 'zero_cross_tenant'), ('external_effect', 'no_forbidden_regression')])
def test_any_single_run_with_a_violation_fails_its_gate(violation, gate):
    r = report(Runner(violation=violation))
    assert gate in r['failed_gates'] and (r['conclusion'], r['reason']) == ('reject', 'hard_gate_failed')
    assert 'no_forbidden_regression' in r['failed_gates']  # every violation is a forbidden regression in test world (G5)


def test_more_model_calls_than_105_percent_fails_cost_performance():
    r = report(Runner(calls=(2, 3)))
    assert r['failed_gates'] == ['cost_performance']


def test_a_passed_deadline_ends_incomplete_without_a_score():
    clock = iter(range(100))
    runner = Runner()
    r = report(runner, now=lambda: next(clock), deadline=4)
    assert (r['conclusion'], r['reason']) == ('incomplete', 'evaluator_timeout') and len(runner.seen) == 4


def test_a_refused_reservation_ends_incomplete_budget_exhausted():
    calls = []

    def reserve(consumption_id, amount):
        calls.append(consumption_id)
        return len(calls) <= 5
    runner = Runner()
    r = report(runner, reserve=reserve)
    assert (r['conclusion'], r['reason']) == ('incomplete', 'budget_exhausted') and len(runner.seen) == 5
    assert calls[0].startswith('l1:') and len(set(calls)) == len(calls)  # one reservation id per run


def test_only_synthetic_or_structure_generated_cases_run():
    b = bundle()
    with pytest.raises(CaseRefused):
        report(Runner(), b=b, given=cases(b, origin='real_conversation'))
    with pytest.raises(CaseRefused):
        report(Runner(), b=b, given=cases(b) + [{'case_id': cid('elsewhere'), 'origin': 'synthetic'}])
    assert report(Runner(), b=b, given=cases(b, origin='structure'))['conclusion'] == 'accept'
    b2 = bundle()
    b2['levels'] = ['L0']
    with pytest.raises(ValueError):
        report(Runner(), b=b2)


def test_a_major_change_class_from_l0_stays_closed():
    assert report(Runner(), change_class='major')['reason'] == 'breaking_change'
