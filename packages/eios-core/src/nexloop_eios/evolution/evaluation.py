"""NX-039 V2: hard gates, L0 metrics and the change class (design §3.2, §3.3, §4.1, §6.2, §7.2; owner D7, D39-2, D39-4, D39-5,
D39-7 decided 2026-10-20).

Pure: no database, clock, model, Run or effect. Inputs are the frozen bundle, the patch (refs and hashes), the E3 access and use
records of the bundle's cases and the caller's measurements; the output is an `evolution-evaluation-report` value that the
evaluator records through authz.nexloop_evo_evaluation_conclude.

- The gate registry answers exactly the gates frozen in the bundle (`hard_gates_version`); an unknown gate or version is an error,
  never a pass. Gates are computed apart from the scores, and any failed gate is a rejection whatever the scores (M22).
- Non-inferiority (D7): on both the design and the validation set `candidate - parent >= -margin`, and at least one of them strictly
  better; equal scores are never an acceptance.
- Reproducibility (D39-2): L0 runs twice on the same inputs (the second time with the records in reverse order) and the two reports
  must have the same digest; otherwise the evaluation is `incomplete/not_reproducible`.
- Anonymized A/B: one swap per case from random.Random seeded with the bundle's `anonymize_seed`; the same seed gives the same swaps.
- `classify_change` decides patch / minor / major / refused from the patch and what the Parent's records show; a person or an Agent
  cannot set it. When the inputs cannot show a change is safe, the class is major (D39-7), and major is always closed (rejected).
"""
from __future__ import annotations

import hashlib
import json
import random
import uuid
from typing import Any, Callable

from nexloop_eios.evolution.gate import EvaluationGate
from nexloop_eios.evolution.replay import BundleLeak, check_bundle

LEVEL = 'L0'
DEFINITION_PREFIXES = ('eios:object_type:', 'eios:property:', 'nexloop:vocabulary:')
PATCH_KEYS = ('change_kind', 'run_ids', 'target_refs', 'term_hashes')
BROWSE_LIMIT = 10  # the Context Engine's default browse limit: an overlay ref ranks first and may push a ref past it
# D39-5 ceilings (frozen with the gate version; a change is a new version).
LATENCY_RATIO, LATENCY_EXTRA_MS, TOKEN_RATIO, CALLS_RATIO = 1.10, 20.0, 1.05, 1.05
MAX_ALIASES_PER_RELEASE, MAX_ALIASES_PER_REF = 50, 20
CLASSES = ('patch', 'minor', 'major', 'refused')

__all__ = ['GATE_VERSIONS', 'EvaluationInputs', 'classify_change', 'anonymize_swaps', 'evaluate_l0', 'non_inferior', 'report_digest',
           'BundleLeak']


class EvaluationInputs(dict):
    """Keyword bag of the optional measured inputs: latency_ms {'parent': [...], 'candidate': [...]}, protected_refs (refs of contact
    limit / refusal definitions), alias_index {term_hash: canonical_ref} of the published aliases, alias_counts {ref: count},
    noted_refs (refs that already carry a published note)."""


def _gate(gate: str, ok: bool, *evidence: str) -> dict:
    return {'gate': gate, 'result': 'pass' if ok else 'fail', 'evidence_ids': sorted(set(evidence))[:64]}


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


def _results(access: dict, patch: dict | None) -> list[str]:
    """The refs an access returns, as the Context Engine would order them: overlay refs first (E5), then the Parent's, truncated."""
    parent = [r['ref'] for r in access.get('result_refs') or []]
    if patch and patch.get('change_kind') == 'alias' and set(patch['term_hashes']) & set(access.get('term_hashes') or []):
        merged = list(dict.fromkeys(list(patch['target_refs']) + parent))
        return merged[:max(BROWSE_LIMIT, len(parent))]
    return parent


class _Context:
    def __init__(self, bundle, patch, accesses, uses, inputs):
        self.bundle, self.patch, self.inputs = bundle, patch, inputs
        self.cases = list(bundle['design_case_ids']) + list(bundle['validation_case_ids'])
        self.accesses = [a for a in accesses if a.get('kind') == 'access']
        self.uses = [u for u in uses if u.get('kind') == 'use']
        self.targets = list(patch.get('target_refs') or [])
        self.structure = sorted(patch) == list(PATCH_KEYS) and patch.get('change_kind') in ('alias', 'description') \
            and isinstance(patch.get('term_hashes'), list) and isinstance(self.targets, list)
        self.by_id = {a['access_id']: a for a in self.accesses if a.get('access_id')}


# --- the gates (§4.1). Each returns one gate entry; evidence ids name what failed. -------------------------------------------------

def _references_valid(c: _Context) -> dict:  # G1
    bad = [t for t in c.targets if not isinstance(t, str) or not t.startswith(DEFINITION_PREFIXES)]
    known = {r['ref'] for a in c.accesses for r in a.get('result_refs') or []}
    unseen = [t for t in c.targets if t not in known]  # a target the Parent never returned to these cases is not a known definition
    return _gate('references_valid', bool(c.targets) and not bad and not unseen, 'patch:target_refs', *(f'ref-unknown:{i}' for i, _ in enumerate(unseen)))


def _structure_valid(c: _Context) -> dict:  # G1
    return _gate('structure_valid', c.structure, 'patch:keys')


def _zero_overreach(c: _Context) -> dict:  # G2 (AT-024)
    bad = [t for t in c.targets if not isinstance(t, str) or not t.startswith(DEFINITION_PREFIXES)]
    return _gate('zero_overreach', not bad, 'l0:definition_refs_only', 'l0:no_effect_entry', *(f'ref-outside:{i}' for i, _ in enumerate(bad)))


def _zero_cross_tenant(c: _Context) -> dict:  # G3
    stray = sorted({a.get('run_id') for a in c.accesses + c.uses if a.get('run_id') not in c.cases})
    return _gate('zero_cross_tenant', not stray, 'l0:case_runs_only', *(f'run-outside:{i}' for i, _ in enumerate(stray)))


def _zero_fact_pollution(c: _Context) -> dict:  # G4: L0 is read only; the database probe is the PG test (AT-023)
    return _gate('zero_fact_pollution', True, 'l0:read_only')


def _no_forbidden_regression(c: _Context) -> dict:  # G5
    protected = set(c.inputs.get('protected_refs') or ())
    lost, protected_lost = [], []
    for a in c.accesses:
        before, after = _results(a, None), _results(a, c.patch if c.structure else None)
        if before and not after:
            lost.append(a['access_id'])
        if protected & set(before) - set(after):
            protected_lost.append(a['access_id'])
    return _gate('no_forbidden_regression', not lost and not protected_lost, 'l0:regressions', *(f'access:{x}' for x in lost + protected_lost))


def _semantic_regression(c: _Context) -> dict:  # G6: a ref a Run actually used must still be returned to the same access
    dropped = []
    for u in c.uses:
        a = c.by_id.get(u.get('accessed_id'))
        if a is None:
            dropped.append(u.get('access_id', 'unknown'))
            continue
        after = set(_results(a, c.patch if c.structure else None))
        if {r['ref'] for r in u.get('result_refs') or []} - after:
            dropped.append(a['access_id'])
    return _gate('semantic_regression', not dropped, 'l0:used_refs_kept', *(f'access:{x}' for x in dropped))


def _cost_performance(c: _Context) -> dict:  # G7 (D39-5); missing measurements are a failure, never a pass
    evidence, ok = [], True
    lat = c.inputs.get('latency_ms')
    if not lat or not lat.get('parent') or not lat.get('candidate'):
        ok = False
        evidence.append('latency:missing')
    else:
        p, q = _p95(lat['parent']), _p95(lat['candidate'])
        if q > p * LATENCY_RATIO or q - p > LATENCY_EXTRA_MS:
            ok = False
            evidence.append('latency:p95')
    hits = [a for a in c.accesses if a.get('result_refs')]
    if hits:
        before = sum(len(_results(a, None)) for a in hits)
        after = sum(len(_results(a, c.patch if c.structure else None)) for a in hits)
        if after > before * TOKEN_RATIO:
            ok = False
            evidence.append('tokens:semantics')
    if c.patch.get('change_kind') == 'alias':
        counts = c.inputs.get('alias_counts') or {}
        added = len(c.patch.get('term_hashes') or [])
        if added > MAX_ALIASES_PER_RELEASE or any(counts.get(t, 0) + added > MAX_ALIASES_PER_REF for t in c.targets):
            ok = False
            evidence.append('aliases:ceiling')
    return _gate('cost_performance', ok, 'l0:d39-5', *evidence)


def _conflicts(patch: dict, accesses: list[dict], alias_index: dict) -> list[str]:
    """Term hashes the patch would point at a target while the Parent already gives them another definition."""
    out = []
    targets = set(patch.get('target_refs') or [])
    for h in patch.get('term_hashes') or []:
        if h in alias_index and alias_index[h] not in targets:
            out.append(h)
            continue
        for a in accesses:
            if h in (a.get('term_hashes') or []) and len(a.get('term_hashes') or []) == 1:
                refs = {r['ref'] for r in a.get('result_refs') or []}
                if refs and not refs & targets:
                    out.append(h)
                    break
    return sorted(set(out))


def _semantic_conflict(c: _Context) -> dict:  # G8
    conflicts = _conflicts(c.patch, c.accesses, c.inputs.get('alias_index') or {}) if c.patch.get('change_kind') == 'alias' else []
    return _gate('semantic_conflict', not conflicts, 'l0:alias_conflict', *(f'term:{h[:16]}' for h in conflicts))


def _index_migration_impact(c: _Context) -> dict:  # G9: not applicable to semantic content (NX-040 enables it)
    return _gate('index_migration_impact', True, 'semantic_content:no_schema_change')


GATES: dict[str, Callable[[_Context], dict]] = {
    'references_valid': _references_valid, 'structure_valid': _structure_valid, 'zero_overreach': _zero_overreach,
    'zero_cross_tenant': _zero_cross_tenant, 'zero_fact_pollution': _zero_fact_pollution, 'no_forbidden_regression': _no_forbidden_regression,
    'semantic_regression': _semantic_regression, 'cost_performance': _cost_performance, 'semantic_conflict': _semantic_conflict,
    'index_migration_impact': _index_migration_impact}
GATE_VERSIONS = {
    'nx038-hard-gates-1': ('references_valid', 'structure_valid', 'zero_overreach', 'zero_cross_tenant', 'zero_fact_pollution',
                           'no_forbidden_regression', 'index_migration_impact'),
    'nx039-gates-1': ('references_valid', 'structure_valid', 'zero_overreach', 'zero_cross_tenant', 'zero_fact_pollution',
                      'no_forbidden_regression', 'semantic_regression', 'cost_performance', 'semantic_conflict', 'index_migration_impact'),
    # NX-040 F4: ontology_schema bundles; answered by nexloop_eios.schema_gates.evaluate_schema (G9 enabled, G10-G13 added).
    'nx040-gates-1': ('references_valid', 'structure_valid', 'zero_overreach', 'zero_cross_tenant', 'zero_fact_pollution',
                      'no_forbidden_regression', 'semantic_regression', 'cost_performance', 'semantic_conflict', 'index_migration_impact',
                      'migration_bounded', 'reflow_budget', 'tool_context_compatible', 'authority_unchanged')}


def answer_gates(bundle: dict, c: _Context) -> list[dict]:
    version, frozen = bundle.get('hard_gates_version'), list(bundle.get('hard_gates') or [])
    if version not in GATE_VERSIONS or sorted(frozen) != sorted(GATE_VERSIONS[version]):
        raise ValueError('unknown hard gate version or list')  # never answered by default
    return [GATES[g](c) for g in GATE_VERSIONS[version]]


# --- change class (§7.2, D39-7) ------------------------------------------------------------------------------------------------------

def classify_change(dimension: str, patch: dict, accesses: list[dict], *, alias_index: dict | None = None,
                    noted_refs: set | frozenset | None = None, schema_parent: dict | None = None) -> str:
    """patch | minor | major | refused, decided from the patch and the Parent's records only. Unknown or unprovable -> major.

    NX-040 F1: an ontology_schema change is classified by nexloop_eios.evolution.schema_compat when the Schema Parent is given
    (minor for the three whitelisted additions, refused for relation types, major otherwise); without it, major as before."""
    if dimension == 'ontology_schema' and schema_parent is not None:
        from nexloop_eios.evolution.schema_compat import classify
        return classify(patch, schema_parent)
    if dimension != 'semantic_content':
        return 'major'  # ontology_schema without its Parent, and semantic_tool (a code change proposal): never a semantic release
    if not isinstance(patch, dict) or sorted(patch) != list(PATCH_KEYS):
        return 'refused'
    targets = patch.get('target_refs')
    if not isinstance(targets, list) or not targets or any(not isinstance(t, str) or not t.startswith(DEFINITION_PREFIXES) for t in targets):
        return 'refused'  # AT-024: anything but a definition ref is never classified, only refused
    kind = patch.get('change_kind')
    if kind == 'alias':
        if not patch.get('term_hashes'):
            return 'major'
        if _conflicts(patch, [a for a in accesses if a.get('kind') == 'access'], alias_index or {}):
            return 'major'  # an alias moving a term to another definition changes meaning
        return 'patch' if len(set(targets)) == 1 else 'minor'
    if kind == 'description':
        if noted_refs is None:
            return 'major'  # cannot tell a new note from a changed one
        return 'patch' if set(targets) <= set(noted_refs) else 'minor'
    return 'major'


# --- metrics, non-inferiority, anonymization -------------------------------------------------------------------------------------------

def non_inferior(design: tuple[list[float], list[float]], validation: tuple[list[float], list[float]], margin: float,
                 case_ids: tuple[list[str], list[str]] | None = None) -> tuple[bool, str | None, dict]:
    """(accept, reason, deltas): both sides >= -margin and at least one strictly better (D7)."""
    out = {}
    for side, pair, ids in (('design', design, case_ids[0] if case_ids else None), ('validation', validation, case_ids[1] if case_ids else None)):
        d = EvaluationGate.decide_gt(pair[0], pair[1], case_ids=ids, non_inferiority_margin=margin)
        out[side] = round(d['delta'], 9)
    if any(v < -margin - 1e-12 for v in out.values()):
        return False, 'inferior', out
    if not any(v > 1e-12 for v in out.values()):
        return False, 'not_better', out
    return True, None, out


def anonymize_swaps(seed: str, case_ids: list[str]) -> list[dict]:
    rng = random.Random(int(seed, 16))
    return [{'case_id': c, 'swapped': EvaluationGate.anonymize('parent', 'candidate', rng=rng)['swap']} for c in case_ids]


def report_digest(report: dict) -> str:
    return hashlib.sha256(json.dumps(report, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _case_score(accesses: list[dict], patch: dict | None) -> tuple[float, int]:
    covered = sum(1 for a in accesses if _results(a, patch))
    return covered / len(accesses), sum(1 for a in accesses if a.get('ambiguity'))


def _one_pass(bundle, bundle_digest, patch, accesses, uses, inputs, *, dimension, created_at, elapsed_ms) -> dict:
    check_bundle(bundle)
    if LEVEL not in bundle['levels']:
        raise ValueError('the bundle does not allow L0')
    c = _Context(bundle, patch, accesses, uses, inputs)
    gates = answer_gates(bundle, c)
    failed = [g['gate'] for g in gates if g['result'] == 'fail']
    change_class = classify_change(dimension, patch, c.accesses, alias_index=inputs.get('alias_index'), noted_refs=inputs.get('noted_refs'))
    by_run: dict[str, list[dict]] = {}
    for a in c.accesses:
        by_run.setdefault(a['run_id'], []).append(a)
    for rows in by_run.values():
        rows.sort(key=lambda a: a.get('access_id') or '')
    missing = [x for x in c.cases if x not in by_run]
    scores, pairs, ambiguous, total = {}, {}, 0, 0
    for side, ids in (('design', bundle['design_case_ids']), ('validation', bundle['validation_case_ids'])):
        parent, candidate, present = [], [], []
        for x in ids:
            if x in by_run:
                p, amb = _case_score(by_run[x], None)
                q, _ = _case_score(by_run[x], patch if c.structure else None)
                parent.append(p);candidate.append(q);present.append(x)
                ambiguous += amb;total += len(by_run[x])
        pairs[side] = (parent, candidate, present)
        scores[side] = {'cases': len(parent), 'parent': round(sum(parent) / len(parent), 6) if parent else None,
                        'candidate': round(sum(candidate) / len(candidate), 6) if candidate else None}
    regressions = sum(1 for a in c.accesses if _results(a, None) and not _results(a, patch if c.structure else None))
    scores['metrics'] = {'ambiguity_rate': round(ambiguous / total, 6) if total else None, 'regressions': regressions, 'missing_cases': len(missing)}
    reason = None
    if missing:
        conclusion, reason = 'incomplete', 'missing_data'
    elif failed:
        conclusion, reason = 'reject', 'hard_gate_failed'
    elif change_class in ('major', 'refused'):
        conclusion, reason = 'reject', 'breaking_change'
    else:
        ok, why, _ = non_inferior(pairs['design'][:2], pairs['validation'][:2], bundle['non_inferiority']['margin'],
                                  (pairs['design'][2], pairs['validation'][2]))
        conclusion, reason = ('accept', None) if ok else ('reject', why)
    return {'schema_version': '1.0', 'report_id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'nexloop:evolution:report:{bundle_digest}:{LEVEL}')),
            'candidate_id': bundle['candidate_id'], 'bundle_digest': bundle_digest, 'level': LEVEL, 'evaluator_version': bundle['evaluator_version'],
            'hard_gates_version': bundle['hard_gates_version'], 'hard_gates': gates, 'failed_gates': failed, 'change_class': change_class,
            'scores': scores, 'swaps': anonymize_swaps(bundle['anonymize_seed'], sorted(c.cases)),
            'cost': {'amount': '0', 'unit': bundle['budget_remaining']['unit']}, 'elapsed_ms': int(elapsed_ms),
            'reproduced': True, 'conclusion': conclusion, 'reason': reason, 'created_at': created_at}


def evaluate_l0(bundle: dict, bundle_digest: str, patch: dict, accesses: list[dict], *, uses: list[dict] = (), inputs: dict | None = None,
                dimension: str = 'semantic_content', created_at: str, elapsed_ms: int = 0) -> dict:
    """The L0 report of one frozen bundle, run twice (D39-2): the second pass reads the records in reverse order; a different report
    is `incomplete/not_reproducible`. The caller measures latency (inputs['latency_ms']) and elapsed_ms (no clock here)."""
    inputs = dict(inputs or {})
    first = _one_pass(bundle, bundle_digest, patch, list(accesses), list(uses), inputs, dimension=dimension, created_at=created_at, elapsed_ms=elapsed_ms)
    second = _one_pass(bundle, bundle_digest, patch, list(reversed(list(accesses))), list(reversed(list(uses))), inputs, dimension=dimension,
                       created_at=created_at, elapsed_ms=elapsed_ms)
    if report_digest(first) != report_digest(second):
        return {**first, 'reproduced': False, 'conclusion': 'incomplete', 'reason': 'not_reproducible'}
    return first
