"""Offline acceptance evidence checks; no stage connection or database fixture."""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/acceptance'))
import s6_stage_evolution as S


def result(dry=False):
    return S.Result('at048', {'release': 'local', 'executor': 'test', 'results_dir': '/unused'}, dry)


def stage(*, pairs=True, same=True, replay=False):
    # Four consumers, with the near duplicate pair separate from the repeated consumer.
    clusters = {'a': 'A', 'b': 'B', 'c': 'C', 'd': 'D', 'e': 'D' if same else 'E'}
    inputs = {'pairs': [('a', 'b')] if pairs else [], 'clusters': clusters}
    calls = []
    def plan(label, units, *, groups=None, design=None, validation=None):
        return SimpleNamespace(candidate={'candidate_id': label, 'input_snapshot_digest': 'f' * 64,
                               'design_case_ids': design or ['a', 'b', 'c'], 'validation_case_ids': validation or ['d', 'e']},
                               case_groups=groups or clusters)
    def freeze(draft, label, *, verify_cohort=False):
        calls.append(label)
        bundle = {**draft.candidate, 'case_groups': draft.case_groups}
        opened = label.endswith(('by_consumer', 'regrouped'))
        return {'bundle': bundle, 'bundle_digest': 'a' * 64, 'replayed': replay}, {
            'opened': opened, 'reason': None if opened else 'leakage_detected', 'replayed': replay}
    return SimpleNamespace(inputs=lambda: inputs, holdout_units=lambda _: ['D'], plan=plan, freeze_open=freeze, calls=calls)


@pytest.mark.parametrize('dry', [False, True])
@pytest.mark.parametrize('kwargs', [{'pairs': False}, {'same': False}])
def test_missing_route_is_blocked_before_any_freeze(kwargs, dry):
    st, r = stage(**kwargs), result(dry)
    with pytest.raises(S.Precondition) as caught:
        S.at048(st, r)
    r.block('precondition', str(caught.value))
    assert r.res.document()['passed'] is None and not st.calls


def test_old_positive_opening_cannot_be_a_fresh_pass():
    st, r = stage(replay=True), result()
    S.at048(st, r)
    doc = r.res.document()
    assert doc['passed'] is None and len(doc['blocked']) == 2
    evidence = [s['evidence'] for s in doc['steps'] if s['name'] == 'frozen cohort']
    assert len(evidence) == 4 and all(e['opening_replayed'] for e in evidence)


def test_fresh_four_routes_have_distinct_cohort_evidence():
    st, r = stage(), result()
    S.at048(st, r)
    doc = r.res.document()
    assert doc['passed'] is True and doc['blocked'] == []
    plan = next(step['evidence'] for step in doc['steps'] if step['name'] == 'plan')
    assert plan['routes'] == ['by_consumer', 'by_run', 'near_duplicate', 'regrouped']
    evidence = [s['evidence'] for s in r.res.steps if s['name'] == 'frozen cohort']
    assert len({e['candidate_id'] for e in evidence}) == 4
    assert len({e['cohort_digest'] for e in evidence}) == 4
    assert all(e['input_snapshot_digest'] == 'f' * 64 for e in evidence)


@pytest.mark.parametrize('field', ['candidate_id', 'input_snapshot_digest', 'design_case_ids', 'validation_case_ids', 'case_groups'])
def test_old_frozen_cohort_is_refused_before_open(field):
    draft = stage().plan('new', [])
    bundle = {**copy.deepcopy(draft.candidate), 'case_groups': copy.deepcopy(draft.case_groups)}
    bundle[field] = 'old' if isinstance(bundle[field], str) else ([] if isinstance(bundle[field], list) else {})
    st = object.__new__(S.Stage)
    st.propose = lambda _: None
    st.seed = lambda _: 'seed'
    st.writer = SimpleNamespace(freeze_bundle=lambda *a, **kw: {'bundle': bundle, 'bundle_digest': 'a' * 64})
    st.evaluator = SimpleNamespace(open=lambda *a: pytest.fail('old cohort reached evaluation'))
    with pytest.raises(S.Precondition, match='frozen cohort'):
        st.freeze_open(draft, 'new', verify_cohort=True)
