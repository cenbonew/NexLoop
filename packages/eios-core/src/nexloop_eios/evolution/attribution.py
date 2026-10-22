"""NX-038 E4: deterministic attribution and candidate drafts (docs/implementation/NX-038-design.md §3.1, §3.2, §11.3, §12.2, §12.6;
owner D12, D13, D19, D20).

Inputs are what E2 and E3 already return: trajectory samples (``nexloop_eios.evolution_traces``, the `trajectory-sample`
contract) and per-access semantic records (``authz.nexloop_semantic_access_read``). Both hold ids, codes, counts, hashes and refs
only, so every output here does too: no message body, Claim value, prompt or query text is read or produced.

``attribute`` puts every sample in one category, in a fixed order (§3.1: exclusions first, nothing non-semantic is attributed to
semantics), with its evidence chain and a short rationale:

    filtered              test stack or a non-real driver (§11.3), counted only
    not_semantic          control / budget refusal codes (NXC, NXB), timeouts, unknown or unreconciled effects
    expected_behavior     restricted_after_refusal / human_takeover without a send (not a failure)
    safety_regression     the same scenarios with a send, or an unexpected delivery: safety regression, never a candidate
    tool_misuse           a failing tool (unregistered tool, parameter errors) -> semantic_tool, marked only (D19)
    ambiguity_unresolved  a resolve returned ambiguity, nothing was used from it, and the Run failed -> semantic_content
    retrieval_miss        a browse found nothing or a resolve left every term unresolved, and the Run failed -> semantic_content
    none                  no semantic signal

``recovered_tool_errors`` are a weak signal (§11.3): a (tool, code) pair seen at least 3 times from at least 2 Consumers (D13) is
marked for semantic_tool; nothing else happens with it. `context_dropped` and `definition_gap` need inputs E2/E3 do not carry
(context source omissions, unmatched candidate definitions); they are not produced here.

``draft_candidates`` turns the semantic_content groups (D19: the only dimension open in the first batch) into `proposed`
candidate drafts: one hypothesis per group, case ids are the group's Runs, split into design and validation by Consumer cluster
(AT-048: a naive split by Run is checked, a cluster found on both sides is moved whole to its majority side and counted as a
regroup). Drafts are deterministic for one frozen snapshot: the same samples and records give the same ids and splits.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from nexloop_eios.evolution_traces import snapshot_digest

CATEGORIES = ('filtered', 'not_semantic', 'expected_behavior', 'safety_regression', 'tool_misuse', 'ambiguity_unresolved',
              'retrieval_miss', 'none')
DIMENSION = {'tool_misuse': 'semantic_tool', 'ambiguity_unresolved': 'semantic_content', 'retrieval_miss': 'semantic_content'}
OPEN_DIMENSIONS = frozenset({'semantic_content'})  # D19
NOT_SEMANTIC_ERRORS = frozenset({'interaction_timeout', 'reconciliation_missing', 'effect_evidence_unavailable', 'delivery_missing'})
SAFETY_ERRORS = frozenset({'unexpected_delivery'})
NO_SEND_SCENARIOS = frozenset({'restricted_after_refusal', 'human_takeover'})
SENT_STATES = frozenset({'accepted', 'dispatching', 'provider_accepted', 'delivered', 'observed_fulfilled', 'fulfilled', 'confirmed'})
TOOL_PARAMETER_FAILURES = frozenset({'invalid_effect_request', 'invalid_tool_arguments', 'invalid_arguments', 'schema_mismatch'})
UNREGISTERED_TOOL = 'unregistered_tool'
MAXIMUM_ROUNDS = 3        # D20, per candidate
DIAGNOSIS_RUNS = 2000     # D20, per diagnosis
WEAK_SIGNAL = (3, 2)      # D13: occurrences, distinct Consumers
GROUP_MINIMUM = (3, 2)    # a semantic_content group needs Runs from at least this many occurrences and clusters (the D13 bar)
VALIDATION_SHARE = 0.3


@dataclass(frozen=True)
class Attribution:
    run_id: str | None
    interaction_id: str | None
    category: str
    dimension: str | None
    rationale: str
    evidence: dict[str, Any]


@dataclass
class Diagnosis:
    snapshot_digest: str
    attributions: list[Attribution]
    counts: dict[str, int]
    weak_tool_signals: list[dict[str, Any]]
    marked: dict[str, int] = field(default_factory=dict)  # dimensions found but not open (D19): counted, no candidate


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def freeze_digest(samples: list[dict], accesses: list[dict]) -> str:
    """§2.3: one digest over the frozen trajectory samples and semantic access records (both in canonical order)."""
    records = sorted(accesses, key=lambda a: (a.get('recorded_at') or '', a['access_id']))
    blob = json.dumps({'samples': snapshot_digest(samples), 'accesses': records}, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      default=str)
    return _sha(blob)


# Owner 2026-10-22 (failure basis A): a reply not completed is a failure - outcome reply_fallback (the message got the fallback reply Run).
FAILED_OUTCOMES = frozenset({'error', 'reply_fallback'})


def _failed(sample: dict) -> bool:
    return bool(sample.get('failures')) or sample.get('outcome') in FAILED_OUTCOMES or (sample.get('model') or {}).get('failed', 0) > 0


def _sent(sample: dict) -> bool:
    return any(state in SENT_STATES and n > 0 for state, n in (sample.get('intents') or {}).items())


def _not_semantic(sample: dict) -> str | None:
    codes = sorted(c for c, n in (sample.get('refusals') or {}).items() if n > 0 and c[:3].upper() in ('NXC', 'NXB'))
    if codes:
        return 'control or budget refusal ' + ','.join(codes)
    if sample.get('error_code') in NOT_SEMANTIC_ERRORS:
        return 'error ' + sample['error_code']
    if sample.get('reconciliation') == 'unresolved' or (sample.get('intents') or {}).get('unknown', 0) > 0:
        return 'effect result unknown or unreconciled'
    return None


def _tool_failure(sample: dict) -> dict | None:
    for f in sample.get('failures') or []:
        if f.get('failure_tool') == UNREGISTERED_TOOL or (f.get('failure_tool') and f.get('failure_code') in TOOL_PARAMETER_FAILURES):
            return f
    return None


def _accesses_by_run(accesses: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for a in sorted(accesses, key=lambda a: (a.get('recorded_at') or '', a['access_id'])):
        if a.get('run_id'):
            out[a['run_id']].append(a)
    return out


def _semantic(run_accesses: list[dict]) -> tuple[str, dict] | None:
    used = {a['accessed_id'] for a in run_accesses if a['kind'] == 'use'}
    reads = [a for a in run_accesses if a['kind'] == 'access']
    ambiguous = [a for a in reads if a.get('ambiguity') and a['access_id'] not in used]
    if ambiguous:
        refs = sorted({r['ref'] for a in ambiguous for r in a.get('result_refs') or []})
        return 'ambiguity_unresolved', {'access_ids': [a['access_id'] for a in ambiguous], 'refs': refs,
                                        'term_hashes': sorted({h for a in ambiguous for h in a.get('term_hashes') or []})}
    missed = [a for a in reads if (a['operation'] == 'browse' and a.get('result_count', 0) == 0)
              or (a['operation'] == 'resolve' and a.get('unresolved_count', 0) == len(a.get('term_hashes') or []) and a.get('unresolved_count', 0) > 0)]
    if missed:
        found = sorted({r['ref'] for a in reads if a not in missed for r in a.get('result_refs') or []})
        return 'retrieval_miss', {'access_ids': [a['access_id'] for a in missed], 'refs': found,
                                  'term_hashes': sorted({h for a in missed for h in a.get('term_hashes') or []})}
    return None


def attribute(samples: list[dict], accesses: list[dict]) -> Diagnosis:
    """One category per sample (§3.1, §11.3), the weak tool signals (D13) and the frozen digest of the inputs."""
    if len(samples) > DIAGNOSIS_RUNS:
        raise ValueError(f'a diagnosis reads at most {DIAGNOSIS_RUNS} Runs (D20)')
    by_run = _accesses_by_run(accesses)
    out: list[Attribution] = []
    for s in sorted(samples, key=lambda s: (s.get('issued_at') or '', s.get('run_id') or '', s.get('interaction_id') or '')):
        base = {'run_id': s.get('run_id'), 'interaction_id': s.get('interaction_id'), 'scenario': s.get('scenario'), 'persona': s.get('persona')}

        def put(category: str, rationale: str, **evidence: Any) -> None:
            out.append(Attribution(s.get('run_id'), s.get('interaction_id'), category, DIMENSION.get(category), rationale, {**base, **evidence}))

        if s.get('test_stack'):
            put('filtered', 'test stack')
            continue
        reason = _not_semantic(s)
        if reason:
            put('not_semantic', reason, refusals=s.get('refusals') or {}, error_code=s.get('error_code'))
            continue
        if s.get('error_code') in SAFETY_ERRORS or (s.get('scenario') in NO_SEND_SCENARIOS and _sent(s)):
            put('safety_regression', 'a send where none was expected; handed to the safety regression, never a candidate',
                intents=s.get('intents') or {})
            continue
        if s.get('scenario') in NO_SEND_SCENARIOS:
            put('expected_behavior', 'no send in a restricted or human-takeover scenario')
            continue
        tool = _tool_failure(s)
        if tool:
            put('tool_misuse', 'the Run failed in tool ' + tool['failure_tool'] + (' (' + tool['failure_code'] + ')' if tool.get('failure_code') else ''),
                failure_tool=tool['failure_tool'], failure_code=tool.get('failure_code'))
            continue
        semantic = _semantic(by_run.get(s.get('run_id') or '', []))
        if semantic and _failed(s):
            category, evidence = semantic
            put(category, ('a resolve stayed ambiguous and nothing from it was used' if category == 'ambiguity_unresolved'
                           else 'a semantic read found no definition') + '; the Run failed', **evidence)
            continue
        put('none', 'no semantic signal')
    pairs: dict[tuple[str, str], list[str | None]] = defaultdict(list)
    for s in samples:
        if not s.get('test_stack'):
            for e in s.get('recovered_tool_errors') or []:
                pairs[(e['tool'], e['code'])].append(s.get('consumer_id') or s.get('run_id'))
    weak = [{'tool': t, 'code': c, 'occurrences': len(who), 'consumers': len(set(who)), 'dimension': 'semantic_tool'}
            for (t, c), who in sorted(pairs.items()) if len(who) >= WEAK_SIGNAL[0] and len(set(who)) >= WEAK_SIGNAL[1]]
    counts = {k: 0 for k in CATEGORIES}
    for a in out:
        counts[a.category] += 1
    marked: dict[str, int] = defaultdict(int)
    for a in out:
        if a.dimension and a.dimension not in OPEN_DIMENSIONS:
            marked[a.dimension] += 1
    for w in weak:
        marked[w['dimension']] += 1
    return Diagnosis(freeze_digest(samples, accesses), out, counts, weak, dict(marked))


def _cluster(sample: dict | None, run_id: str) -> str:
    """AT-048 cluster: the Consumer (a Consumer's related conversations are one cluster); a Run without one is its own cluster."""
    consumer = (sample or {}).get('consumer_id')
    return 'consumer:' + consumer if consumer else 'run:' + run_id


def split_cases(case_clusters: dict[str, str], seed: str, *, share: float = VALIDATION_SHARE) -> tuple[list[str], list[str], int]:
    """Design / validation split without leakage (AT-048). A first split by Run hash is checked; each cluster found on both sides is
    moved whole to its majority side (a tie goes to design) and counted as one regroup; if a side ends up empty, the cluster that
    sorts first by hash moves over (another regroup). Needs at least two clusters."""
    clusters = sorted(set(case_clusters.values()))
    if len(clusters) < 2:
        raise ValueError('a split needs cases from at least two clusters')
    rank = lambda key: _sha(seed + ':' + key)  # noqa: E731
    side = {c: (int(rank(c)[:8], 16) / 0xFFFFFFFF) < share for c in case_clusters}  # True = validation
    regroups = 0
    for cluster in clusters:
        members = [c for c, k in case_clusters.items() if k == cluster]
        held = sum(side[c] for c in members)
        if 0 < held < len(members):
            regroups += 1
            to_validation = held > len(members) - held
            for c in members:
                side[c] = to_validation
    for want in (True, False):
        if not any(v == want for v in side.values()):
            regroups += 1
            first = min(clusters, key=rank)
            for c, k in case_clusters.items():
                if k == first:
                    side[c] = want
    design = sorted(c for c, v in side.items() if not v)
    validation = sorted(c for c, v in side.items() if v)
    return design, validation, regroups


@dataclass(frozen=True)
class CandidateDraft:
    candidate: dict[str, Any]        # the evolution-candidate wire value (status proposed, publication_permit_ref null)
    category: str
    patch: dict[str, Any]            # ids, hashes and refs only
    case_groups: dict[str, str]
    regroups: int
    source_consumer_ids: list[str]


def draft_candidates(diagnosis: Diagnosis, samples: list[dict], *, tenant_id: str, parent_revision: str, evaluator_version: str,
                     model_profile: str, created_at: str, minimum: tuple[int, int] = GROUP_MINIMUM) -> tuple[list[CandidateDraft], dict[str, int]]:
    """`proposed` drafts for the open dimension (D19). Returns the drafts and the counts of groups that were not drafted."""
    by_run = {s['run_id']: s for s in samples if s.get('run_id')}
    groups: dict[tuple[str, str], list[Attribution]] = defaultdict(list)
    for a in diagnosis.attributions:
        if a.dimension in OPEN_DIMENSIONS and a.run_id:
            key = a.evidence['term_hashes'][0] if a.category == 'retrieval_miss' and a.evidence['term_hashes'] else ','.join(a.evidence['refs'])
            groups[(a.category, key)].append(a)
    drafts, skipped = [], {'below_minimum': 0, 'no_scope': 0}
    for (category, key), members in sorted(groups.items()):
        runs = sorted({a.run_id for a in members})
        clusters = {r: _cluster(by_run.get(r), r) for r in runs}
        if len(runs) < minimum[0] or len(set(clusters.values())) < minimum[1]:
            skipped['below_minimum'] += 1
            continue
        scope = sorted({ref for a in members for ref in a.evidence['refs']})[:256]
        if not scope:
            skipped['no_scope'] += 1
            continue
        design, validation, regroups = split_cases(clusters, diagnosis.snapshot_digest + ':' + category + ':' + key)
        candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'nexloop:evolution:{diagnosis.snapshot_digest}:{category}:{key}'))
        terms = sorted({h for a in members for h in a.evidence['term_hashes']})[:16]
        if category == 'retrieval_miss':
            hypothesis = (f'retrieval_miss: the term with sha256 {key[:16]} found no definition in {len(runs)} failed Runs from '
                          f'{len(set(clusters.values()))} clusters; an alias on one of the {len(scope)} scoped definitions may let the read resolve it.')
        else:
            hypothesis = (f'ambiguity_unresolved: a resolve over the same {len(scope)} definitions stayed ambiguous in {len(runs)} failed Runs from '
                          f'{len(set(clusters.values()))} clusters; sharper descriptions may separate them.')
        candidate = {'schema_version': '1.0', 'candidate_id': candidate_id, 'tenant_id': tenant_id, 'parent_revision': parent_revision,
                     'candidate_revision': f'{parent_revision}+cand.{candidate_id[:8]}', 'dimension': 'semantic_content',
                     'primary_hypothesis': hypothesis, 'patch_artifact_ref': 'evolution-patch:pending',
                     'input_snapshot_digest': diagnosis.snapshot_digest, 'design_case_ids': design, 'validation_case_ids': validation,
                     'evaluator_version': evaluator_version, 'model_profile': model_profile, 'maximum_rounds': MAXIMUM_ROUNDS,
                     'allowed_change_scope': scope, 'status': 'proposed', 'publication_permit_ref': None, 'created_at': created_at}
        patch = {'change_kind': 'alias' if category == 'retrieval_miss' else 'description', 'target_refs': scope, 'term_hashes': terms,
                 'run_ids': runs}
        sources = sorted({by_run[r]['consumer_id'] for r in runs if by_run.get(r, {}).get('consumer_id')})
        drafts.append(CandidateDraft(candidate, category, patch, clusters, regroups, sources))
    return drafts, skipped
