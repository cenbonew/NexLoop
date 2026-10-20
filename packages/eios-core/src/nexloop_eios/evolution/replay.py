"""NX-038 E5: L0 deterministic replay and the evaluation report (design §12.3, §12.4, §7; D7, D16, D19).

L0 replays the frozen semantic accesses (E3 records: term hashes, refs, counts) of the bundle's design and validation cases twice:
against the Parent as recorded, and against the Parent plus the candidate patch as an overlay. No model, no Run, no effect, no
database: the inputs are the frozen bundle, the patch (refs and hashes) and the access records, and the output is an
`evolution-evaluation-report` value. Hard gates are answered separately from the score, and a failed gate is never an acceptance;
an equal score is never an acceptance either (D7, through the ported EvaluationGate). Missing inputs give `incomplete`, never a
pass (08 §5).

Per access: `covered` when it returned at least one ref; the candidate adds the patch's target refs to an access whose term hashes
the alias patch names. A `description` patch changes no lookup, so L0 scores it equal to the Parent (and so does not accept it).
A case scores the share of its accesses covered.
"""
from __future__ import annotations

import uuid

from nexloop_eios.evolution.gate import EvaluationGate

LEVEL = 'L0'
_PREFIXES = ('eios:object_type:', 'eios:property:', 'nexloop:vocabulary:')
_PATCH_KEYS = ('change_kind', 'run_ids', 'target_refs', 'term_hashes')


class BundleLeak(ValueError):
    """AT-048: a Consumer or session cluster on both sides of the frozen split; NX-039 must not start."""


def check_bundle(bundle: dict) -> None:
    """Re-check before an evaluation starts (§12.6 AT-048): every case has a group and no group is on both sides."""
    design, validation, groups = bundle['design_case_ids'], bundle['validation_case_ids'], bundle['case_groups']
    if set(groups) != set(design) | set(validation) or set(design) & set(validation):
        raise BundleLeak('case groups do not match the cases')
    if {groups[c] for c in design} & {groups[c] for c in validation}:
        raise BundleLeak('a cluster is on both sides')


def _score(accesses: list[dict], patch: dict | None) -> tuple[float, int, int]:
    """(coverage share, ambiguous count, regressions) of one case's accesses."""
    covered = ambiguous = regressions = 0
    hashes = set(patch['term_hashes']) if patch and patch['change_kind'] == 'alias' else set()
    for a in accesses:
        refs = {r['ref'] for r in a['result_refs']}
        before = bool(refs)
        if hashes & set(a['term_hashes']):
            refs |= set(patch['target_refs'])
        covered += bool(refs)
        ambiguous += bool(a['ambiguity'])
        regressions += before and not refs
    return covered / len(accesses), ambiguous, regressions


def _gate(gate: str, ok: bool, *evidence: str) -> dict:
    return {'gate': gate, 'result': 'pass' if ok else 'fail', 'evidence_ids': sorted(evidence)}


def replay_l0(bundle: dict, bundle_digest: str, patch: dict, accesses: list[dict], *, created_at: str, elapsed_ms: int = 0) -> dict:
    """The L0 report of one frozen bundle: the same inputs give the same report (M22); the caller measures elapsed_ms (this package
    reads no clock)."""
    check_bundle(bundle)
    if LEVEL not in bundle['levels']:
        raise ValueError('the bundle does not allow L0')
    cases = list(bundle['design_case_ids']) + list(bundle['validation_case_ids'])
    by_run: dict[str, list[dict]] = {}
    for a in accesses:
        if a.get('kind') == 'access':
            by_run.setdefault(a.get('run_id'), []).append(a)
    margin = bundle['non_inferiority']['margin']
    targets = list(patch.get('target_refs') or [])
    structure = sorted(patch) == list(_PATCH_KEYS) and patch.get('change_kind') in ('alias', 'description')
    gates = [_gate('references_valid', bool(targets) and all(t.startswith(_PREFIXES) for t in targets), 'patch:target_refs'),
             _gate('structure_valid', structure, 'patch:keys'),
             _gate('zero_overreach', all(t.startswith(_PREFIXES) for t in targets), 'l0:definition_refs_only', 'l0:no_effect_entry'),
             _gate('zero_cross_tenant', all(a.get('run_id') in cases for a in accesses if a.get('kind') == 'access'), 'l0:case_runs_only'),
             _gate('zero_fact_pollution', True, 'l0:read_only')]
    scores, regressions, ambiguous, total = {}, 0, 0, 0
    missing = [c for c in cases if c not in by_run]
    for side, ids in (('design', bundle['design_case_ids']), ('validation', bundle['validation_case_ids'])):
        parent, candidate = [], []
        for c in ids:
            if c in by_run:
                p, amb, _ = _score(by_run[c], None)
                q, _, reg = _score(by_run[c], patch if structure else None)
                parent.append(p)
                candidate.append(q)
                regressions += reg
                ambiguous += amb
                total += len(by_run[c])
        scores[side] = {'cases': len(parent), 'parent': round(sum(parent) / len(parent), 6) if parent else None,
                        'candidate': round(sum(candidate) / len(candidate), 6) if candidate else None,
                        '_pair': (parent, candidate, [c for c in ids if c in by_run])}
    gates += [_gate('no_forbidden_regression', regressions == 0, 'l0:regressions'),
              _gate('index_migration_impact', True, 'semantic_content:no_schema_change')]
    reason = None
    if missing:
        conclusion, reason = 'incomplete', 'missing_data'
    else:
        decisions = [EvaluationGate.decide_gt(*scores[s]['_pair'][:2], case_ids=scores[s]['_pair'][2], non_inferiority_margin=margin)
                     for s in ('design', 'validation')]
        if any(g['result'] == 'fail' for g in gates):
            conclusion, reason = 'reject', 'hard_gate_failed'
        elif all(d['accept'] for d in decisions):
            conclusion = 'accept'
        else:
            conclusion, reason = 'reject', 'not_better' if all(d['delta'] == 0 for d in decisions) else 'inferior'
    for s in scores.values():
        s.pop('_pair')
    scores['metrics'] = {'ambiguity_rate': round(ambiguous / total, 6) if total else None, 'regressions': regressions, 'missing_cases': len(missing)}
    return {'schema_version': '1.0', 'report_id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'nexloop:evolution:report:{bundle_digest}:{LEVEL}')),
            'candidate_id': bundle['candidate_id'], 'bundle_digest': bundle_digest, 'level': LEVEL, 'evaluator_version': bundle['evaluator_version'],
            'hard_gates': gates, 'scores': scores, 'swaps': [], 'cost': {'amount': '0', 'unit': bundle['budget_remaining']['unit']},
            'elapsed_ms': int(elapsed_ms), 'conclusion': conclusion, 'reason': reason, 'created_at': created_at}
