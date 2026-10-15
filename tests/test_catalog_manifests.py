"""NX-032 A2: the capability snapshot manifest (deploy/configuration/capabilities.v1.json) and its validator
(nexloop_eios.catalog_manifests): computed schema hashes, governed capabilities registered by migrations, side-effecting Action
snapshots, and every business Action compiles against the committed snapshots."""
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexloop_eios import business_actions as BA
from nexloop_eios import catalog_manifests as C
from support.tenants import TENANT_A
from test_business_actions import object_types

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'deploy/configuration/capabilities.v1.json'
COMMITTED = json.loads(PATH.read_text())


def test_the_committed_manifest_is_valid_and_every_hash_is_the_computed_one():
    manifest = C.load(PATH)
    for item in manifest['capabilities']:
        assert item['schema_hash'] == C.schema_hash(item['capability_name'], item['capability_version'], item['contract'])
    snapshots = C.snapshots(manifest)
    binds = {(i['capability_name'], i['capability_version']): i['binds'] for i in manifest['capabilities']}
    assert all(s.has_side_effects == (binds[k] == 'action') for k, s in snapshots.items())  # 0050 for Actions; readers for Functions
    assert {k[0] for k in snapshots} >= set(C.governed_capabilities()) & {k[0] for k in snapshots}


def test_every_business_action_compiles_against_the_committed_snapshots_deterministically():
    business = BA.load(ROOT / 'deploy/configuration/business-actions.v1.json')
    by_name = {name: s for (name, _), s in C.snapshots(C.load(PATH)).items()}
    assert {a['capability_name'] for a in business['actions']} <= set(by_name)
    def compile_once():
        return BA.compile_actions(business, tenant=TENANT_A, created_by='nexloop-system-catalog', created_at=datetime(2026, 10, 15, tzinfo=UTC),
                                  object_types=object_types(), capabilities={n: by_name[n] for n in {a['capability_name'] for a in business['actions']}})
    rows = compile_once()
    assert len(rows) == len(business['actions']) and json.dumps(rows, sort_keys=True) == json.dumps(compile_once(), sort_keys=True)
    for row in rows:
        snapshot = by_name[row['capability']['capability_name']]
        assert row['definition']['capability_binding']['schema_hash'] == snapshot.schema_hash


def test_governed_capabilities_come_from_the_migration_catalog():
    governed = C.governed_capabilities()
    assert len(governed) >= 28 and governed['goals.budget.set'] == 'set_budget' and governed['contact_key.revoke']
    in_manifest = {i['capability_name']: i for i in COMMITTED['capabilities']}
    for name, operation in governed.items():
        if name in in_manifest:
            assert in_manifest[name]['governed'] is True and in_manifest[name]['contract']['governed_operation'] == operation


def mutated(change):
    value = copy.deepcopy(COMMITTED)
    change(value)
    return value


def first(name):
    return lambda v: next(i for i in v['capabilities'] if i['capability_name'] == name)


@pytest.mark.parametrize('change,reason', [
    (lambda v: first('ontology.object.create')(v)['contract'].update(input='changed'), 'schema_hash is not the computed hash'),
    (lambda v: first('ontology.object.create')(v).update(has_side_effects=False), 'side-effecting snapshot'),
    (lambda v: first('ontology.object.create')(v).update(governed=True), 'not registered'),
    (lambda v: first('goals.budget.set')(v)['contract'].update(governed_operation='set_control'), 'registered operation'),
    (lambda v: first('commercial.observe')(v)['contract'].update(governed_operation='x'), 'not governed'),
    (lambda v: v['capabilities'].append(copy.deepcopy(v['capabilities'][0])), 'duplicate capability version'),
    (lambda v: first('cost.record')(v).update(required_scopes=['b', 'a']), 'required_scopes'),
    (lambda v: first('cost.record')(v).update(kind='workflow'), 'atomic'),
    (lambda v: first('cost.record')(v).update(capability_version='1'), 'capability identity'),
    (lambda v: first('cost.record')(v).update(extra=1), 'entry shape'),
    (lambda v: v.update(schema_version='nexloop-capabilities/2'), 'manifest shape'),
])
def test_the_validator_refuses(change, reason):
    with pytest.raises(C.CatalogManifestRejected, match=reason):
        C.validate(mutated(change))


def test_write_hashes_fills_only_stale_hashes(tmp_path):
    path = tmp_path / 'capabilities.json'
    value = mutated(lambda v: first('cost.record')(v).update(schema_hash=''))
    path.write_text(json.dumps(value))
    assert C.write_hashes(path) == ['cost.record'] and C.write_hashes(path) == []
    assert json.loads(path.read_text()) == COMMITTED


def test_a_governed_insert_with_other_columns_is_refused(tmp_path):
    (tmp_path / '0001_x.sql').write_text("insert into control.nexloop_governed_capabilities(capability,operation) values ('a.b','c');")
    with pytest.raises(C.CatalogManifestRejected, match='columns'):
        C.governed_capabilities(tmp_path)


# ---- system Actions --------------------------------------------------------------------------------------------------------------
SYSTEM = ROOT / 'deploy/configuration/system-actions.v1.json'
SYSTEM_COMMITTED = json.loads(SYSTEM.read_text())
T1 = json.loads((ROOT / 'deploy/configuration/system-catalog-names.v1.json').read_text())


def ontology_types():
    from nexloop_eios import business_object_types, system_object_types
    return [(t['type_name'], t['version']) for t in system_object_types.trusted_object_types(ROOT / 'deploy/ontology/system-object-types.v1.json')
            + business_object_types.trusted_object_types(ROOT / 'deploy/ontology/business-object-types.v1.json')]


def business_keys():
    return [(a['stable_name'], a['version']) for a in BA.load(ROOT / 'deploy/configuration/business-actions.v1.json')['actions']]


def check(value, **kw):
    return C.validate_system_actions(value, capabilities=C.load(PATH), other_object_types=ontology_types(), other_actions=business_keys(), **kw)


def test_the_committed_system_manifest_is_valid():
    check(SYSTEM_COMMITTED)


def test_the_system_manifest_matches_the_t1_name_list():
    declared_types = {t['type_name'] for t in SYSTEM_COMMITTED['object_types']} | {n for n, _ in ontology_types()}
    assert declared_types == {t['type_name'] for t in T1['object_types']}
    def rows(entries):
        return {(e['stable_name'], e['version']): (sorted(r[0] for r in e['object_types']), e['capability'][0]) for e in entries}
    t1_actions = {(a['stable_name'], a['version']): (sorted(a['object_types']), a['capability']) for a in T1['system_actions']}
    assert rows(SYSTEM_COMMITTED['actions']) == t1_actions
    assert {(a['stable_name'], a['version']) for a in T1['business_actions']} == set(business_keys())
    t1_functions = {(f['stable_name'], f['version']): sorted(f['object_types']) for f in T1['functions']}
    assert {k: v[0] for k, v in rows(SYSTEM_COMMITTED['functions']).items()} == t1_functions
    # The one capability T1 left to confirm: conversation_scope_denials.QUERY_CAPABILITY.
    from nexloop_eios.conversation_effect_receipts import QUERY_CAPABILITY as RECEIPTS
    from nexloop_eios.conversation_scope_denials import QUERY_CAPABILITY as DENIALS
    caps = {f['stable_name']: f['capability'][0] for f in SYSTEM_COMMITTED['functions']}
    assert caps == {'nexloop.conversation.service_receipt': RECEIPTS, 'nexloop.conversation.scope_denial': DENIALS}


def test_every_snapshot_is_bound_by_a_declared_action_or_function():
    business = {a['capability_name'] for a in BA.load(ROOT / 'deploy/configuration/business-actions.v1.json')['actions']}
    system = {e['capability'][0] for e in SYSTEM_COMMITTED['actions'] + SYSTEM_COMMITTED['functions']}
    assert {c['capability_name'] for c in COMMITTED['capabilities']} == business | system


def system_mutated(change):
    value = copy.deepcopy(SYSTEM_COMMITTED)
    change(value)
    return value


def action(name):
    return lambda v: next(a for a in v['actions'] if a['stable_name'] == name)


@pytest.mark.parametrize('change,reason', [
    (lambda v: action('Goal.create')(v).update(object_types=[['Nothing', 1]]), 'object types not declared'),
    (lambda v: action('Goal.create')(v).update(capability=['ontology.object.create', '2.0.0']), 'capability snapshot missing'),
    (lambda v: action('Goal.create')(v).update(capability=['nexloop.conversation.receipts.read', '1.0.0']), 'binds function'),
    (lambda v: v['actions'].append({**action('Goal.create')(v), 'stable_name': 'Consumer.edit'}), 'declared twice'),
    (lambda v: v['actions'].append(copy.deepcopy(action('Goal.create')(v))), 'declared twice'),
    (lambda v: action('Goal.create')(v).update(target_systems=['service']), 'differ from the capability contract'),
    (lambda v: action('Goal.create')(v).update(idempotency_key_fields=['intent_id']), 'idempotency key'),
    (lambda v: action('nexloop.context.bind')(v).update(input={'builder': 'Not A Key'}), 'builder key'),
    (lambda v: action('Goal.create')(v).update(input={'schema': {'type': 'string'}}), 'input schema'),
    (lambda v: v.update(valid_from='2026-10-15T00:00:00'), 'time zone'),
    (lambda v: v['object_types'].append(copy.deepcopy(v['object_types'][0])), 'declared twice'),
    (lambda v: v['object_types'].append({'type_name': 'Commitment', 'version': 1, 'builder': 'x'}), 'declared twice'),
    (lambda v: v['functions'][0].update(capability=['nexloop.conversation.read', '1.0.0']), 'binds action'),
    (lambda v: action('Goal.create')(v).update(extra=1), 'entry shape'),
])
def test_the_system_validator_refuses(change, reason):
    with pytest.raises(C.CatalogManifestRejected, match=reason):
        check(system_mutated(change))


def test_builder_keys_are_checked_against_the_builder_table_when_given():
    keys = {t['builder'] for t in SYSTEM_COMMITTED['object_types']} | {f['builder'] for f in SYSTEM_COMMITTED['functions']} | {
        a['input']['builder'] for a in SYSTEM_COMMITTED['actions'] if a['input'] and 'builder' in a['input']}
    check(SYSTEM_COMMITTED, builders=keys)
    with pytest.raises(C.CatalogManifestRejected, match='not in system_catalog.BUILDERS'):
        check(SYSTEM_COMMITTED, builders=keys - {'context_binding_schema'})
