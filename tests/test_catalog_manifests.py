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
    assert all(s.has_side_effects for s in snapshots.values())  # version 1 only binds Actions
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
