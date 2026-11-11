"""Option B (owner 2026-11-11 B1): context-pack v8 = v7 with the effect Action version no longer pinned to 1. The live example (an
effect context bound to nexloop.service.request:2, its offering still naming delivery_action ...:1 - object data is not migrated)
validates; another stable name, version 0, a leading zero or a non-number is refused; v8 differs from v7 only at the five effect
Action fields, the schema_version, $id, title and description."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

C = Path(__file__).resolve().parents[1] / 'packages/contracts'
V7 = json.loads((C / 'context-pack-v7.schema.json').read_text())
V8 = json.loads((C / 'context-pack-v8.schema.json').read_text())
EXAMPLE = json.loads((C / 'examples/context-pack-v8.valid.json').read_text())
VALIDATOR = Draft202012Validator(V8, format_checker=FormatChecker())


def test_the_example_binds_a_successor_version():
    VALIDATOR.validate(EXAMPLE)
    assert EXAMPLE['current_constraints']['action'] == 'nexloop.service.request:2'
    assert EXAMPLE['supply']['properties']['delivery_action'] == 'nexloop.service.request:1'


@pytest.mark.parametrize('action', ['nexloop.service.query:2', 'nexloop.service.request:0', 'nexloop.service.request:02',
                                    'nexloop.service.request:x', 'nexloop.service.request', 'nexloop.service.request:10000'])
def test_other_actions_and_malformed_versions_are_refused(action):
    for path in (('current_constraints', 'action'), ('supply', 'properties', 'delivery_action')):
        pack = copy.deepcopy(EXAMPLE)
        node = pack
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = action
        assert not VALIDATOR.is_valid(pack), (path, action)


def test_the_enterprise_fields_take_only_the_enterprise_action():
    effect = V8['properties']['effect_supplies']['items']['properties']
    for schema in (effect['action'], effect['properties']['properties']['delivery_action']):
        pattern = Draft202012Validator(schema)
        assert pattern.is_valid('nexloop.enterprise.deliver:3') and not pattern.is_valid('nexloop.service.request:3')


def test_v8_is_v7_with_only_the_effect_action_fields_changed():
    changed = []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                walk(a.get(key), b.get(key), path + (key,))
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, path + (i,))
        elif a != b:
            changed.append(path)
    walk(V7, V8, ())
    fields = {p[:p.index(k)] for p in changed for k in ('enum', 'const', 'type', 'pattern') if k in p}
    assert {p for p in changed if p[0] in ('$id', 'title', 'description')} == {('$id',), ('title',), ('description',)}
    assert ('properties', 'schema_version', 'const') in changed
    effect_fields = {f for f in fields if f and f[-1] in ('action', 'delivery_action')}
    assert len(effect_fields) == 5, effect_fields
    assert {p for p in changed if p[0] not in ('$id', 'title', 'description')} - {('properties', 'schema_version', 'const')} \
        <= {f + (k,) for f in effect_fields for k in ('enum', 'const', 'type', 'pattern')}
