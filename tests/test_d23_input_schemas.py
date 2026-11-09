"""D23 option B, batch 0 (docs/implementation/d23-option-b-design.md §1 C1, §2a C2, §3a C4): pure checks.

- The generator (nexloop_eios.input_schemas): no format anywhere; ^(p)$ anchors that are deliberately wider than re.fullmatch on a
  trailing newline; patterns outside the shared dialect and body-chosen specs refused, never guessed; every derived schema refuses
  a probe value (it leaves open_input); unknown checks become {} (wider, never stricter).
- C2 readers that need no database: follow_latest_version never follows an input-schema replacement (a separate grant is needed);
  a replacement keeps the capability binding; the Pi Host source names none of the open Actions; none is an effect Action.
- C4: no Context assembly reads an Action's input_schema, so a filled schema changes nothing the model sees."""
import importlib.util
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from nexloop_eios import input_schemas as I
from nexloop_eios import service_grants as SG
from nexloop_eios import workbench_actions_http as W

ROOT = Path(__file__).resolve().parents[1]
# The probe values of D23 option A (schema_gates.OPEN_PROBES once batch 141 is in): a schema accepting all of them is open.
PROBES = [[], None, 'x', 0, {}, {'nexloop_probe_extra': 1}, True, '', -1, 1.5, 'x' * 4096, {'a': {'b': {'c': 1}}}, [1, 'x', None, {}]]


def _walk(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


@pytest.fixture(scope='module')
def derived():
    return I.derive_all()


def test_no_format_and_every_pattern_anchored(derived):
    schemas, _ = derived
    assert schemas
    for operation, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        for node in _walk(schema):
            assert 'format' not in node, operation
            if 'pattern' in node:
                assert node['pattern'].startswith('^(') and node['pattern'].endswith(')$'), operation


def test_the_anchor_is_wider_than_fullmatch_on_one_trailing_newline():
    check = W._id(re.compile(r'[a-f0-9]{64}'))
    schema = I.field_schema(check)
    value = 'a' * 64 + '\n'
    assert Draft202012Validator(schema).is_valid(value)  # the schema lets it through...
    with pytest.raises(ValueError):
        check(value)  # ...the runtime check refuses it: wider, never stricter
    assert not Draft202012Validator(schema).is_valid('a' * 63) and Draft202012Validator(schema).is_valid('a' * 64)


@pytest.mark.parametrize('pattern', [r'(?=x)y', r'(?i)abc', r'(a)\1', r'\bword', r'\Aabc', r'abc\Z', r'\d{3}', r'\w+'])
def test_a_pattern_outside_the_shared_dialect_is_refused(pattern):
    with pytest.raises(I.NotDerivable):
        I.safe_pattern(pattern)


def test_the_known_checks_and_an_unknown_one():
    assert I.field_schema(W._bool) == {'type': 'boolean'}
    assert I.field_schema(W._reason) == {'type': 'string', 'minLength': 1, 'maxLength': 500}
    assert I.field_schema(W._kind('shadow')) == {'const': 'shadow'}
    assert I.field_schema(lambda v: v) == {}  # unknown: unconstrained, so never stricter than the check it stands for
    scope = I.field_schema(W._scope)
    for value in scope['enum']:
        assert W._scope(value) == value  # the exported enum is exactly what the runtime check accepts
    with pytest.raises(ValueError):
        W._scope('nexloop_not_a_scope')


def test_every_derived_schema_refuses_a_probe_and_body_chosen_specs_are_left_for_a_person(derived):
    schemas, refused = derived
    for operation, schema in schemas.items():
        validator = Draft202012Validator(schema)
        assert not all(validator.is_valid(p) for p in PROBES), operation  # not open: it leaves D23's open_input
    assert refused['read_experiment_report'] == 'spec chosen by the body'
    assert set(refused) | set(schemas) == set(W.OPERATIONS)


def test_follow_latest_never_follows_an_input_schema_replacement():
    """§2a: a successor that changes the definition (here its input_schema) is not a pure re-binding: follow_latest_version stops
    there, so a service grant for the new version must be listed in service-grants."""
    base = {'stable_name': 'Consumer.edit', 'version': 1, 'input_schema': {}, 'object_types': [{'stable_name': 'Consumer', 'version': 1}],
            'capability_binding': {'capability_name': 'ontology.object.edit'}}
    successor = {**base, 'version': 2, 'previous_version': 1, 'input_schema': {'type': 'object'}}
    lineage = [{'world': 'real', 'stable_name': 'Consumer.edit', 'version': v, 'resource_id': f'eios:action:Consumer.edit:{v}', 'definition': d,
                'capability': {'capability_name': 'ontology.object.edit'}, 'review_decision_id': 'synthetic', 'active': True} for v, d in ((1, base), (2, successor))]
    manifest = {'world': 'real', 'grants': [{'principal': 'claim_matcher', 'resource_type': 'action', 'resource_id': 'eios:action:Consumer.edit:1',
                                             'operations': ['execute'], 'follow_latest_version': True, 'source_task': 'NX-020'}]}
    derived, refused = SG.resolve_follow(manifest, lineage)
    assert derived == [] and [r['successor'] for r in refused] == ['eios:action:Consumer.edit:2']


def _catalog():
    from nexloop_eios import catalog_publish as P
    spec = importlib.util.spec_from_file_location('stage_secrets_for_d23', ROOT / 'scripts/deploy/stage_secrets.py')
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return P.compile_catalog('tenant-synthetic', **module.CATALOG)


@pytest.fixture(scope='module')
def open_actions():
    out = {}
    for action in _catalog()['actions']:
        d = action['definition']
        if any(r['stable_name'] == 'Consumer' for r in d.get('object_types', [])) and (d.get('input_schema') or {}) == {}:
            out[d['stable_name']] = action
    return out


# The Pi Host offers two of the open Actions as simulation-world stand-in and Run tools (runtime-effect-tools.ts); their Run credential
# and Role ceiling name the exact version :1, so replacing them needs those references moved in the same batch (design §2a).
HOST_TOOLS = {'nexloop.incentive.issue', 'nexloop.outreach.send'}


def test_the_open_actions_are_none_of_the_effect_actions_and_the_host_names_only_its_two_tools(open_actions):
    assert len(open_actions) >= 80
    effect = json.loads((ROOT / 'deploy/configuration/system-actions.v1.json').read_text())
    effect_names = {a['stable_name'] for a in effect['actions'] if a.get('input') is not None}
    assert not (set(open_actions) & effect_names)
    host = '\n'.join(p.read_text() for p in (ROOT / 'apps/agent-host/src').rglob('*.ts'))
    assert {name for name in open_actions if f"'{name}'" in host or f'"{name}"' in host} == HOST_TOOLS


def test_a_replacement_keeps_the_capability_binding(open_actions):
    from eios.ontology.definitions import ActionDefinition
    action = open_actions['Consumer.edit']['definition']
    previous = {'tenant_id': action['tenant_id'], 'definition_type': 'action', 'stable_name': action['stable_name'], 'version': action['version'],
                'contract_digest': action['contract_digest']}
    body = {**action, 'version': action['version'] + 1, 'previous_version': previous,
            'input_schema': {'type': 'object', 'required': ['request_id'], 'properties': {'request_id': {'type': 'string'}}}}
    body.pop('contract_digest', None)
    replaced = ActionDefinition.model_validate_json(json.dumps(body)).model_dump(mode='json')
    assert replaced['capability_binding'] == action['capability_binding'] and replaced['previous_version'] == previous


def test_no_context_assembly_reads_an_action_input_schema():
    """C4: the model never sees an Action's input_schema, so r8's fixed context_volume stays true. A Context function or module that
    starts reading it makes this fail - then the volume has to be measured (design §3a)."""
    python = [p for p in (ROOT / 'packages/eios-core/src/nexloop_eios/context_engine').rglob('*.py')] + \
        list((ROOT / 'packages/eios-core/src/nexloop_eios').glob('context_*.py'))
    # context_artifacts.py checks the published nexloop.context.bind Action's own schema before binding (an authority check on that
    # one typed Action), not a Context content: it is the one allowed reader.
    assert [str(p.relative_to(ROOT)) for p in python if 'input_schema' in p.read_text()] == ['packages/eios-core/src/nexloop_eios/context_artifacts.py']
    assert 'context_binding_schema()' in (ROOT / 'packages/eios-core/src/nexloop_eios/context_artifacts.py').read_text()
    # In SQL, the context binding commands check the published binding Action's own schema (pub.definition->'input_schema' compared
    # with a fixed one); no context function ever puts an input_schema into what it assembles.
    projected, checked = [], 0
    for path in sorted((ROOT / 'packages/eios-core/src/eios/migrations').glob('*.sql')):
        text = path.read_text()
        for match in re.finditer(r'create (?:or replace )?function ([a-z_]+\.[a-z0-9_]*(?:context|assemble)[a-z0-9_]*)\s*\(.*?\$\$(.*?)\$\$', text, re.S | re.I):
            body = match.group(2)
            uses = body.count('input_schema')
            checked += uses
            if uses != body.count("pub.definition->'input_schema'") or "'input_schema'," in body:
                projected.append(f'{path.name}:{match.group(1)}')
    assert projected == [] and checked > 0



@pytest.mark.parametrize('flags', [re.IGNORECASE, re.VERBOSE, re.DOTALL, re.ASCII])
def test_a_runtime_pattern_with_flags_is_refused(flags):
    with pytest.raises(I.NotDerivable, match='flags'):
        I.field_schema(W._id(re.compile(r'[a-z]{1,8}', flags)))
    assert I.field_schema(W._id(re.compile(r'[a-z]{1,8}')))['pattern'] == '^([a-z]{1,8})$'


def test_the_catalog_lists_one_version_per_action_the_newest_with_its_exact_predecessor(open_actions):
    from nexloop_eios import catalog_publish as P
    catalog = _catalog()
    names = [a['definition']['stable_name'] for a in catalog['actions']]
    assert len(names) == len(set(names))  # one version per Action
    v1 = open_actions['Consumer.edit']
    body = {k: v for k, v in v1['definition'].items() if k != 'contract_digest'}
    v2 = {**v1, 'definition': {**body, 'version': 2, 'input_schema': {'type': 'object'}}}
    out = P.latest_only([v1, v2])
    assert len(out) == 1 and out[0]['definition']['version'] == 2
    assert out[0]['definition']['previous_version'] == {'tenant_id': v1['definition']['tenant_id'], 'definition_type': 'action',
        'stable_name': 'Consumer.edit', 'version': 1, 'contract_digest': v1['definition']['contract_digest']}
    for bad in ([v1, v1], [v2], [v1, {**v2, 'definition': {**v2['definition'], 'version': 3}}],
                [v1, {**v2, 'definition': {**v2['definition'], 'capability_binding': {**v2['definition']['capability_binding'], 'capability_version': '2.0.0'}}}]):
        with pytest.raises(P.CatalogPublishRefused):
            P.latest_only(bad)
