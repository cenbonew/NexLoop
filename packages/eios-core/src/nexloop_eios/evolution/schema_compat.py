"""NX-040 F1: the compatibility library for ontology_schema changes (design docs/implementation/NX-040-design.md §4.1-§4.5, §6, §7.2;
owner D21-D27 decided 2026-10-20).

Pure: no database, network or effect. A change is judged against the Parent (the current EIOS definitions and what references them)
on four dimensions - migration, index, tool signature, contract - and classified for NX-039's `classify_change` (39 design §12.3):

- Whitelist (§4.1): add_optional_property, add_vocabulary_value (minor, may be published automatically later, D22), and
  add_object_type (minor, human publication only, D22). add_relation_type is refused (D24). Everything else is major and closed
  (reason breaking_change) with a fine code (§6).
- The vendored `eios.ontology.semantics.assert_object_compatible` is applied unchanged to the Parent and the candidate definition; the
  library adds the stricter rules it does not have (an enum may only grow, the new property must be optional with a default or
  nullable, of a scalar type, at most normal visibility, ...).
- Any failed dimension closes the publication (§4). Codes are fixed strings, listed in CODES.
"""
from __future__ import annotations

from typing import Any

from eios.ontology.models import ObjectTypeDefinition, PropertyDefinition, PropertyValueType, PropertyVisibility
from eios.ontology.semantics import SchemaCompatibilityError, assert_object_compatible

WHITELIST = {'add_optional_property': ('minor', True), 'add_vocabulary_value': ('minor', True), 'add_object_type': ('minor', False)}
REFUSED_OPS = ('add_relation_type',)
SCALARS = {PropertyValueType.STRING, PropertyValueType.INTEGER, PropertyValueType.NUMBER, PropertyValueType.BOOLEAN, PropertyValueType.DATETIME}
MIGRATION_LIMIT = 10_000  # D25: objects migrated by one publication
# Fixed codes: the §6 breaking codes, the closure of anything off the whitelist, and the per-dimension failures.
BREAKING = ('breaking_remove_property', 'breaking_change_value_type', 'breaking_required_property', 'breaking_remove_vocabulary_value',
            'breaking_primary_key', 'breaking_relation_endpoints', 'breaking_metric_definition', 'requires_ddl', 'authority_content')
CODES = BREAKING + (
    'operation_not_whitelisted', 'relation_type_closed',
    'unknown_type', 'unknown_property', 'type_exists', 'property_exists', 'value_type_not_allowed', 'visibility_above_normal',
    'no_default_not_nullable', 'vocabulary_on_new_property', 'not_closed_vocabulary', 'vocabulary_value_exists',
    'vocabulary_alias_conflict', 'edit_mode_open', 'definition_invalid', 'parent_incompatible',
    'migration_over_limit', 'embedding_dimension_changed', 'reflow_over_budget', 'semantic_ref_missing',
    'tool_signature_changed', 'tool_required_parameter', 'successor_not_one_to_one', 'contract_changed',
    'property_security_bypass')
# Codes that make a change major (closed) rather than merely failing one dimension of an otherwise additive change.
_MAJOR = set(BREAKING) | {'operation_not_whitelisted', 'edit_mode_open', 'parent_incompatible', 'property_security_bypass'}


def _vocabulary(definition: PropertyDefinition) -> tuple:
    d = definition.type_descriptor
    return tuple(d.enum) if d is not None and d.enum else ()


def _dimension(failures: list[str], evidence: list[str]) -> dict:
    return {'result': 'fail' if failures else 'pass', 'codes': sorted(set(failures)), 'evidence_ids': sorted(set(evidence))[:64]}


def _candidate_types(change: dict, parent_types: dict[str, ObjectTypeDefinition], codes: list[str]) -> dict[str, ObjectTypeDefinition] | None:
    """The definitions after the change (only the touched type), or None when the change cannot be built."""
    op, name = change.get('op'), change.get('type_name')
    try:
        if op == 'add_object_type':
            definition = ObjectTypeDefinition.model_validate(change['definition'])
            if definition.type_name != name:
                codes.append('definition_invalid')
                return None
            return {name: definition}
        current = parent_types[name]
        if op == 'add_optional_property':
            prop = PropertyDefinition.model_validate(change['property'])
            data = current.model_dump(mode='json')
            data['properties'] = list(data['properties']) + [prop.model_dump(mode='json')]
            data['version'] = current.version + 1
            return {name: ObjectTypeDefinition.model_validate(data)}
        if op == 'add_vocabulary_value':
            data = current.model_dump(mode='json')
            for p in data['properties']:
                if p['property_name'] == change['property_name'] and p.get('type_descriptor'):
                    p['type_descriptor']['enum'] = list(p['type_descriptor'].get('enum') or []) + [change['value']]
            data['version'] = current.version + 1
            return {name: ObjectTypeDefinition.model_validate(data)}
    except (KeyError, TypeError, ValueError):
        codes.append('definition_invalid')
    return None


def _whitelist_rules(change: dict, parent_types: dict[str, ObjectTypeDefinition], parent: dict, codes: list[str]) -> None:
    op, name = change.get('op'), change.get('type_name')
    # AT-024: a property name the owner restricted on any type is never re-exposed through a new property or a new type.
    restricted = set(parent.get('restricted_names') or ())
    added = [((change.get('property') or {}).get('property_name'))] if op == 'add_optional_property' else \
        [p.get('property_name') for p in (change.get('definition') or {}).get('properties') or ()] if op == 'add_object_type' else []
    if restricted & set(added):
        codes.append('property_security_bypass')
    if op == 'add_object_type':
        if name in parent_types:
            codes.append('type_exists')
        if change.get('actions'):
            codes.append('authority_content')  # a new type publishes no Action (AT-024, §5)
        if change.get('relations'):
            codes.append('breaking_relation_endpoints')
        definition = change.get('definition') or {}
        if definition.get('primary_key'):
            codes.append('breaking_primary_key')
        if definition.get('only_edit_via_actions') is not True:
            codes.append('edit_mode_open')
        return
    if name not in parent_types:
        codes.append('unknown_type')
        return
    current = parent_types[name]
    by_name = current.property_map()
    if op == 'add_optional_property':
        prop = change.get('property') or {}
        pname = prop.get('property_name')
        if pname in by_name:
            codes.append('property_exists')
        if prop.get('required'):
            codes.append('breaking_required_property')
        if prop.get('value_type') not in {v.value for v in SCALARS}:
            codes.append('value_type_not_allowed')
        if (prop.get('type_descriptor') or {}).get('enum'):
            codes.append('vocabulary_on_new_property')
        if prop.get('visibility', 'normal') == PropertyVisibility.PROMINENT.value:
            codes.append('visibility_above_normal')
        if not prop.get('nullable') and prop.get('default_value') is None:
            codes.append('no_default_not_nullable')
    elif op == 'add_vocabulary_value':
        pname, value = change.get('property_name'), change.get('value')
        if pname not in by_name:
            codes.append('unknown_property')
            return
        values = _vocabulary(by_name[pname])
        if not values:
            codes.append('not_closed_vocabulary')
        if value in values:
            codes.append('vocabulary_value_exists')
        aliases = {str(a).strip().casefold() for a in (parent.get('vocabulary_aliases') or {}).get(f'{name}/{pname}', ())}
        if isinstance(value, str) and (value.strip().casefold() in aliases or value.strip().casefold() in {str(v).strip().casefold() for v in values} - {value}):
            codes.append('vocabulary_alias_conflict')


def breaking_codes(current: ObjectTypeDefinition | None, candidate: ObjectTypeDefinition) -> list[str]:
    """What the vendored check and the stricter rules say about Parent -> candidate (the vendored function is not changed)."""
    if current is None:
        return []
    out = []
    try:
        assert_object_compatible(current, candidate)
    except SchemaCompatibilityError as error:
        text = str(error)
        out.append('breaking_primary_key' if 'primary_key' in text else 'breaking_change_value_type' if 'type cannot change' in text
                   or 'kind cannot change' in text else 'breaking_remove_vocabulary_value' if 'enum values' in text else
                   'breaking_remove_property' if 'removed' in text else 'parent_incompatible')
    old, new = current.property_map(), candidate.property_map()
    if set(old) - set(new):
        out.append('breaking_remove_property')
    for name in set(old) & set(new):
        if old[name].value_type != new[name].value_type:
            out.append('breaking_change_value_type')
        if not old[name].required and new[name].required:
            out.append('breaking_required_property')
        before, after = _vocabulary(old[name]), _vocabulary(new[name])
        if before and not set(before) <= set(after):
            out.append('breaking_remove_vocabulary_value')  # stricter than the vendored check: dropping the enum is a removal too
    for name in set(new) - set(old):
        if new[name].required:
            out.append('breaking_required_property')
    if tuple(current.primary_key) != tuple(candidate.primary_key):
        out.append('breaking_primary_key')
    return out


def _migration(change, parent, current, candidate, codes) -> dict:
    failures, evidence = [], []
    if change.get('ddl') or change.get('indexes'):
        failures.append('requires_ddl')
    if change.get('metric_definitions'):
        failures.append('breaking_metric_definition')
    failures += breaking_codes(current, candidate) if candidate is not None else []
    objects = int((parent.get('object_counts') or {}).get(change.get('type_name'), 0))
    if change.get('op') != 'add_object_type' and objects > MIGRATION_LIMIT:
        failures.append('migration_over_limit')
        evidence.append(f'objects:{objects}')
    evidence.append('schema_version:+1' if change.get('op') != 'add_object_type' else 'new_type:no_instances')
    return _dimension(failures, evidence)


def _index(change, parent, candidate_types) -> dict:
    failures, evidence = [], []
    if change.get('indexes'):
        failures.append('requires_ddl')
    if change.get('embedding_dimension') is not None and change.get('embedding_dimension') != parent.get('embedding_dimension'):
        failures.append('embedding_dimension_changed')
    reflow = change.get('reflow') or {}
    cap = parent.get('reflow_cap') or {}
    if reflow and (int(reflow.get('vectors', 0)) > int(cap.get('vectors', 0)) or float(reflow.get('cost', 0)) > float(cap.get('cost', 0))):
        failures.append('reflow_over_budget')
        evidence.append('reflow:cap')
    names = {**{n: d for n, d in (parent.get('types_parsed') or {}).items()}, **(candidate_types or {})}
    for ref in parent.get('semantic_refs') or ():
        type_name, _, prop = ref.partition('/')
        d = names.get(type_name)
        if d is None or (prop and prop not in d.property_map()):
            failures.append('semantic_ref_missing')
            evidence.append('ref:' + ref[:120])
    return _dimension(failures, evidence)


def _tool_signature(change, parent, candidate_types) -> dict:
    failures, evidence = [], []
    name = change.get('type_name')
    successors = {(s.get('stable_name'), s.get('replaces')): s for s in change.get('successor_actions') or ()}
    for action in parent.get('actions') or ():
        if name not in {t.get('stable_name') for t in action.get('object_types') or ()}:
            continue
        succ = [s for (stable, replaces), s in successors.items() if stable == action['stable_name'] and replaces == action['version']]
        if len(succ) != 1:
            failures.append('successor_not_one_to_one')
            evidence.append('action:' + action['stable_name'])
            continue
        s = succ[0]
        bumped = all(t.get('version') == (o.get('version') + 1 if o.get('stable_name') == name else o.get('version'))
                     for t, o in zip(s.get('object_types') or (), action.get('object_types') or ()))
        if (s.get('version') != action['version'] + 1 or not bumped or len(s.get('object_types') or ()) != len(action.get('object_types') or ())
                or s.get('input_schema_hash') != action.get('input_schema_hash') or s.get('capability_schema_hash') != action.get('capability_schema_hash')
                or s.get('risk_level') != action.get('risk_level') or set(s.get('effect_categories') or ()) != set(action.get('effect_categories') or ())):
            failures.append('tool_signature_changed')
            evidence.append('action:' + action['stable_name'])
        new_prop = (change.get('property') or {}).get('property_name')
        if new_prop and new_prop in (s.get('required_parameters') or ()):
            failures.append('tool_required_parameter')
    extra = [s for (stable, _), s in successors.items() if stable not in {a['stable_name'] for a in parent.get('actions') or ()}]
    if extra:
        failures.append('authority_content')  # a successor without a predecessor is a new Action
    if change.get('op') == 'add_object_type' and change.get('actions'):
        failures.append('authority_content')
    regression = change.get('tool_regression') or {}
    if regression and regression.get('parent') != regression.get('candidate'):
        failures.append('tool_signature_changed')
        evidence.append('tool_regression:differs')
    return _dimension(failures, evidence)


def _contract(change) -> dict:
    return _dimension(['contract_changed'] if change.get('contracts') else [], ['contracts:unchanged'] if not change.get('contracts') else [])


def judge(change: dict, parent: dict) -> dict:
    """{op, change_class, allowed, reason, codes, dimensions, auto_publish_eligible, candidate_definition}.

    `parent`: {'types': {name: definition dict}, 'object_counts': {name: n}, 'vocabulary_aliases': {'Type/prop': [alias]},
    'actions': [{stable_name, version, object_types:[{stable_name,version}], input_schema_hash, capability_schema_hash, risk_level,
    effect_categories}], 'semantic_refs': ['Type' | 'Type/prop'], 'embedding_dimension': n, 'reflow_cap': {vectors, cost},
    'restricted_names': [property names in owner-restricted groups]}."""
    op = change.get('op') if isinstance(change, dict) else None
    out = {'op': op, 'change_class': 'major', 'allowed': False, 'reason': 'breaking_change', 'codes': [], 'dimensions': {}, 'auto_publish_eligible': False,
           'candidate_definition': None}
    if op in REFUSED_OPS:
        return {**out, 'change_class': 'refused', 'reason': 'refused', 'codes': ['relation_type_closed']}
    if op not in WHITELIST:
        # Closed. When the change carries the next version of an existing type, the fine codes say what it breaks (§6).
        fine = []
        try:
            name = change.get('type_name')
            current = ObjectTypeDefinition.model_validate((parent.get('types') or {})[name])
            fine = breaking_codes(current, ObjectTypeDefinition.model_validate(change['definition']))
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        return {**out, 'codes': sorted({'operation_not_whitelisted', *fine})}
    codes: list[str] = []
    try:
        parent_types = {n: ObjectTypeDefinition.model_validate(d) for n, d in (parent.get('types') or {}).items()}
    except ValueError:
        return {**out, 'codes': ['parent_incompatible']}
    _whitelist_rules(change, parent_types, parent, codes)
    candidate_types = None if codes else _candidate_types(change, parent_types, codes)
    name = change.get('type_name')
    candidate = (candidate_types or {}).get(name)
    current = parent_types.get(name) if op != 'add_object_type' else None
    dims = {'migration': _migration(change, parent, current, candidate, codes),
            'index': _index(change, {**parent, 'types_parsed': parent_types}, candidate_types),
            'tool_signature': _tool_signature(change, parent, candidate_types), 'contract': _contract(change)}
    all_codes = sorted(set(codes) | {c for d in dims.values() for c in d['codes']})
    major = bool(set(all_codes) & _MAJOR)
    change_class = 'major' if major else WHITELIST[op][0]
    allowed = not all_codes
    return {'op': op, 'change_class': change_class, 'allowed': allowed, 'reason': None if allowed else ('breaking_change' if major else 'incompatible'),
            'codes': all_codes, 'dimensions': dims,
            # D22: eligible only; the first batch publishes every change by hand (ontology.schema.review).
            'auto_publish_eligible': allowed and WHITELIST[op][1],
            'candidate_definition': candidate.model_dump(mode='json') if allowed and candidate is not None else None}


def classify(change: dict, parent: dict) -> str:
    """The `classify_change` answer for an ontology_schema change: minor, major (closed) or refused."""
    verdict = judge(change, parent)
    return verdict['change_class'] if verdict['allowed'] or verdict['change_class'] in ('major', 'refused') else 'major'
