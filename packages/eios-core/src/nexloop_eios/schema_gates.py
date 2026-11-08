"""NX-040 F4: the evaluator's answer to nx040-gates-1 for an ontology_schema candidate (design §4.2-§4.6; D23, D25, D26).

Deterministic: the F1 compatibility verdict, the frozen tool regression set (D23: per successor Action at least one valid and one
invalid synthetic parameter case, validated against the Action's input schema under the Parent and under the candidate revision -
the results must agree with the expectation and with each other) and the Context volume baseline (at most 105%), the reflow
reservation (G11, counted as evolution by authz.nexloop_evo_reflow_reserve, D26) and the migration plan (G10, D25: at most 10 000
objects, or a batched publication). The report is an `evolution-evaluation-report` value for authz.nexloop_evo_evaluation_conclude.
Run twice (D39-2): the second pass reads the cases in reverse order; a different report is incomplete/not_reproducible.

D23 as revised by the owner 2026-11-09 (option A, docs/implementation/d23-open-input-schemas.md): an Action whose input schema is the
same under both revisions (canonical JSON, byte for byte), has no non-local $ref and no $dynamicRef, and whose Parent accepts every
value of OPEN_PROBES has no input to refuse - it needs its valid case only (replayed under both revisions) and is listed as open_input,
not incomplete. Computed here from action_schemas alone; nothing in the inputs can claim it. Any difference keeps the original rule.
"""
from __future__ import annotations

import hashlib
import json
import uuid

import jsonschema

from nexloop_eios.evolution.evaluation import anonymize_swaps
from nexloop_eios.evolution.schema_compat import judge

GATES_VERSION = 'nx040-gates-1'
GATES = ('references_valid', 'structure_valid', 'zero_overreach', 'zero_cross_tenant', 'zero_fact_pollution', 'no_forbidden_regression',
         'semantic_regression', 'cost_performance', 'semantic_conflict', 'index_migration_impact', 'migration_bounded', 'reflow_budget',
         'tool_context_compatible', 'authority_unchanged')
CONTEXT_RATIO = 1.05
_BY_GATE = {
    'references_valid': {'unknown_type', 'unknown_property', 'definition_invalid', 'type_exists', 'property_exists', 'parent_incompatible'},
    'structure_valid': {'value_type_not_allowed', 'visibility_above_normal', 'no_default_not_nullable', 'vocabulary_on_new_property',
                        'not_closed_vocabulary', 'vocabulary_value_exists', 'edit_mode_open', 'operation_not_whitelisted', 'relation_type_closed'},
    'zero_overreach': {'authority_content', 'property_security_bypass'},
    'semantic_regression': {'semantic_ref_missing'},
    'semantic_conflict': {'vocabulary_alias_conflict'},
    'index_migration_impact': {'requires_ddl', 'breaking_metric_definition', 'breaking_remove_property', 'breaking_change_value_type',
                               'breaking_required_property', 'breaking_remove_vocabulary_value', 'breaking_primary_key', 'breaking_relation_endpoints'},
    'reflow_budget': {'reflow_over_budget', 'embedding_dimension_changed'},
    'tool_context_compatible': {'tool_signature_changed', 'tool_required_parameter', 'successor_not_one_to_one', 'contract_changed'},
    'authority_unchanged': {'authority_content', 'successor_not_one_to_one'},
}


def _gate(gate, ok, *evidence, keep=()):
    """keep: evidence ids that always come first and are never cut by the 64-id bound (the rest sorted, as before)."""
    kept = list(dict.fromkeys(keep))[:64]
    return {'gate': gate, 'result': 'pass' if ok else 'fail', 'evidence_ids': kept + sorted(set(evidence) - set(kept))[:64 - len(kept)]}


# D23 (revised 2026-11-09): the values an input schema must accept, all of them, to count as accepting every value. The one set: the
# r8 checker (scripts/acceptance/s6_stage_schema.py) picks its invalid cases from it.
OPEN_PROBES = ([], None, 'x', 0, {}, {'s6_unexpected_field': 1}, True, '', -1, 1.5, 'x' * 4096, {'a': {'b': {'c': [1, 'x', None]}}},
               [1, 'x', None, {}, [], True])


def _canonical(schema) -> str:
    return json.dumps(schema, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _non_local_reference(schema) -> bool:
    """A $dynamicRef anywhere, or a $ref that is not a local JSON pointer (#...): such a schema is never exempt."""
    if isinstance(schema, dict):
        if '$dynamicRef' in schema or (isinstance(schema.get('$ref'), str) and not schema['$ref'].startswith('#')):
            return True
        return any(_non_local_reference(v) for v in schema.values())
    if isinstance(schema, list):
        return any(_non_local_reference(v) for v in schema)
    return False


def open_input(parent, candidate) -> bool:
    """D23 (2026-11-09): the same schema in both revisions that refuses no probe value (one validator, one format setting)."""
    if _canonical(parent) != _canonical(candidate) or _non_local_reference(parent):
        return False
    return all(_valid(parent, value) for value in OPEN_PROBES)


def _valid(schema, params) -> bool:
    try:
        jsonschema.Draft202012Validator(schema).validate(params)
        return True
    except jsonschema.ValidationError:
        return False


def _case_id(case) -> str:
    """Order-independent: the case's own id, else a digest of what it is (so a reordered set gives the same report)."""
    if isinstance(case.get('id'), str) and case['id']:
        return case['id'][:60]
    return hashlib.sha256(json.dumps([case.get('action'), case.get('expect'), case.get('params')], sort_keys=True, default=str).encode()).hexdigest()[:16]


def replay_tools(cases: list[dict], schemas: dict[str, dict]) -> dict:
    """D23. schemas: {stable_name: {'parent': input schema, 'candidate': input schema}}. Every Action needs one valid and one invalid case
    (an open_input Action, computed here: its valid case only); each case must meet its expectation under both revisions, with the
    same result."""
    failures, incomplete = set(), []
    open_names = sorted(name for name, s in schemas.items() if open_input(s.get('parent'), s.get('candidate')))
    seen: dict[str, set] = {name: set() for name in schemas}
    for case in cases:
        name, expect = case.get('action'), case.get('expect')
        if name not in schemas or expect not in ('valid', 'invalid'):
            failures.add(f'case:{_case_id(case)}:unknown')
            continue
        seen[name].add(expect)
        parent, candidate = _valid(schemas[name]['parent'], case.get('params')), _valid(schemas[name]['candidate'], case.get('params'))
        if parent != candidate or parent != (expect == 'valid'):
            failures.add(f'case:{_case_id(case)}:{name}')
    for name, kinds in sorted(seen.items()):
        if not ({'valid'} if name in open_names else {'valid', 'invalid'}) <= kinds:
            incomplete.append('action:' + name)
    return {'failures': sorted(failures), 'incomplete': incomplete, 'cases': len(cases), 'open_input': open_names}


def _one(bundle, bundle_digest, change, parent, inputs, created_at, elapsed_ms, order):
    verdict = judge(change, parent)
    codes = set(verdict['codes'])
    batched = bool(inputs.get('batch'))
    if batched:
        codes.discard('migration_over_limit')
    tools = replay_tools(list(inputs.get('tool_cases') or [])[::order], inputs.get('action_schemas') or {})
    ctx = inputs.get('context_volume') or {}
    ctx_ok = bool(ctx) and float(ctx.get('candidate', 0)) <= float(ctx.get('parent', 0)) * CONTEXT_RATIO
    gates = {g: _gate(g, not (codes & _BY_GATE.get(g, set())), f'f1:{g}', *sorted(codes & _BY_GATE.get(g, set()))) for g in GATES}
    gates['zero_cross_tenant'] = _gate('zero_cross_tenant', True, 'schema:tenant_parent_only')
    gates['zero_fact_pollution'] = _gate('zero_fact_pollution', True, 'schema:read_only_evaluation')
    gates['no_forbidden_regression'] = _gate('no_forbidden_regression', not tools['failures'], 'tools:replay', *tools['failures'])
    gates['cost_performance'] = _gate('cost_performance', ctx_ok, 'context:volume' if ctx else 'context:missing')
    gates['migration_bounded'] = _gate('migration_bounded', 'migration_over_limit' not in codes, 'migration:batched' if batched else 'migration:10000')
    gates['reflow_budget'] = _gate('reflow_budget', not (codes & _BY_GATE['reflow_budget']) and inputs.get('reflow_reserved') is True,
                                   'reflow:reserved' if inputs.get('reflow_reserved') is True else 'reflow:not_reserved', *sorted(codes & _BY_GATE['reflow_budget']))
    gates['tool_context_compatible'] = _gate('tool_context_compatible', not (codes & _BY_GATE['tool_context_compatible']) and not tools['failures']
                                             and not tools['incomplete'] and ctx_ok, *tools['incomplete'], *tools['failures'],
                                             *('open_input:' + n for n in tools['open_input']),
                                             keep=('tools:regression_set', f"tools:open_input:{len(tools['open_input'])}"))
    hard = [gates[g] for g in bundle['hard_gates']]
    failed = [g['gate'] for g in hard if g['result'] == 'fail']
    change_class = verdict['change_class'] if verdict['change_class'] in ('major', 'refused') else 'minor'  # F1 decides the class (D39-7)
    if failed:
        conclusion, reason = 'reject', 'hard_gate_failed'
    elif change_class != 'minor':
        conclusion, reason = 'reject', 'breaking_change'
    else:
        conclusion, reason = 'accept', None
    passing = (tools['cases'] - len(tools['failures'])) / tools['cases'] if tools['cases'] else None
    cases = sorted(set(bundle['design_case_ids']) | set(bundle['validation_case_ids']))
    side = lambda ids: {'cases': len(ids), 'parent': 1.0 if tools['cases'] else None, 'candidate': passing}  # noqa: E731
    return {'schema_version': '1.0', 'report_id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'nexloop:evolution:report:{bundle_digest}:L0')),
            'candidate_id': bundle['candidate_id'], 'bundle_digest': bundle_digest, 'level': 'L0', 'evaluator_version': bundle['evaluator_version'],
            'hard_gates_version': bundle['hard_gates_version'], 'hard_gates': hard, 'failed_gates': failed, 'change_class': change_class,
            'scores': {'design': side(bundle['design_case_ids']), 'validation': side(bundle['validation_case_ids']),
                       'metrics': {'ambiguity_rate': None, 'regressions': len(tools['failures']), 'missing_cases': 0}},
            'swaps': anonymize_swaps(bundle['anonymize_seed'], cases), 'cost': {'amount': str(inputs.get('reflow_cost', '0')), 'unit': bundle['budget_remaining']['unit']},
            'elapsed_ms': int(elapsed_ms), 'reproduced': True, 'conclusion': conclusion, 'reason': reason, 'created_at': created_at}


def evaluate_schema(bundle: dict, bundle_digest: str, change: dict, parent: dict, *, inputs: dict, created_at: str, elapsed_ms: int = 0) -> dict:
    if bundle.get('hard_gates_version') != GATES_VERSION or list(bundle.get('hard_gates') or []) != list(GATES):
        raise ValueError('not an nx040-gates-1 bundle')
    first = _one(bundle, bundle_digest, change, parent, inputs, created_at, elapsed_ms, 1)
    second = _one(bundle, bundle_digest, change, parent, inputs, created_at, elapsed_ms, -1)
    digest = lambda r: hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()  # noqa: E731
    if digest(first) != digest(second):
        return {**first, 'reproduced': False, 'conclusion': 'incomplete', 'reason': 'not_reproducible'}
    return first
