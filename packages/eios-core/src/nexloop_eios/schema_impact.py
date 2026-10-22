"""EIOS gap A1 (NX-061): before a Schema proposal is published, the published Definitions it may affect.

The reviewer's dependents (authz.nexloop_read_schema_dependents, 0322: the latest Object Sets and Object Views, the active Functions
and Actions of the tenant that mention the proposal's type) are parsed into the vendored Definition contracts and assembled into
the vendored static dependency graph (eios.ontology.impact, upstream 1e363db, unchanged). The answer lists every Definition that
depends on the type, and separately those that depend on a property the proposal changes (the property a later `hide` would hide).

NexLoop reads objects at the latest type version, so a Definition counts whichever version of the type it pins; the pinned version
is reported. A row that does not parse against its contract is listed under `unreadable`, never dropped silently. Read-only.
"""
from __future__ import annotations

import json

from eios.ontology.definitions import ActionDefinition, FunctionDefinition, ObjectSetDefinition, ObjectViewDefinition
from eios.ontology.impact import build_dependency_graph

_KINDS = (('object_sets', ObjectSetDefinition), ('object_views', ObjectViewDefinition), ('functions', FunctionDefinition),
          ('actions', ActionDefinition))


def changed_properties(operation: str | None, change: dict | None) -> list[str]:
    """The existing properties a proposal changes: a new optional property (by name), the property gaining a vocabulary value."""
    change = change if isinstance(change, dict) else {}
    if operation in ('add_optional_property', 'add_vocabulary_value'):
        name = change.get('property_name') or (change.get('property') or {}).get('property_name')
        return [name] if isinstance(name, str) and name else []
    return []


def _definitions(dependents: dict) -> tuple[list, list[dict]]:
    parsed, unreadable = [], []
    for key, model in _KINDS:
        for row in dependents.get(key) or ():
            try:
                parsed.append(model.model_validate_json(json.dumps(row)))  # strict contracts: JSON mode, as every stored reader
            except (ValueError, TypeError):
                unreadable.append({'kind': key, 'stable_name': str((row or {}).get('stable_name', ''))[:200] if isinstance(row, dict) else ''})
    return parsed, unreadable


def _definition(node: str) -> dict:
    _, kind, rest = node.split(':', 2)
    name, version = rest.rsplit(':', 1)
    return {'definition_type': kind, 'stable_name': name, 'version': int(version)}


def impact(dependents: dict | None) -> dict | None:
    """{type_name, changed_properties, dependents: [...], truncated, unreadable} or None when the proposal is unknown."""
    if dependents is None:
        return None
    type_name = dependents['type_name']
    changed = changed_properties(dependents.get('operation'), dependents.get('change'))
    definitions, unreadable = _definitions(dependents)
    graph = build_dependency_graph(definitions)
    type_nodes = sorted(n for n in graph.nodes if n.startswith(f'object_type:{type_name}:'))
    property_nodes = sorted(n for n in graph.nodes if n.startswith(f'property:{type_name}:') and n.rsplit(':', 1)[1] in changed)
    found: dict[str, dict] = {}
    for origin in type_nodes:
        pinned = int(origin.rsplit(':', 1)[1])
        for node, hops in graph.impact_of(origin, 'downstream'):
            if node.startswith('definition:'):
                entry = found.setdefault(node, {**_definition(node), 'pinned_type_version': pinned, 'hops': hops, 'changed_properties': []})
                entry['hops'] = min(entry['hops'], hops)
    for origin in property_nodes:
        prop = origin.rsplit(':', 1)[1]
        for node, _ in graph.impact_of(origin, 'downstream'):
            if node in found and prop not in found[node]['changed_properties']:
                found[node]['changed_properties'].append(prop)
    listed = sorted(found.values(), key=lambda e: (not e['changed_properties'], e['definition_type'], e['stable_name'], e['version']))
    for entry in listed:
        entry['changed_properties'].sort()
    return {'type_name': type_name, 'changed_properties': changed, 'dependents': listed,
            'affected_by_changed_properties': sum(1 for e in listed if e['changed_properties']),
            'truncated': bool(dependents.get('truncated')), 'unreadable': unreadable}
