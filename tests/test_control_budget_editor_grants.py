"""service-grants v25: object/property EDIT only for control_budget_editor, on EffectControl budget_units/valid_until, the control id
only from the private target file; the new principal changes no other principal's binding (no rotation)."""
import copy
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexloop_eios import service_grants as G

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'deploy/authorization/service-grants.v1.json'
EXPIRES = datetime.now(UTC) + timedelta(days=30)


def manifest():
    return copy.deepcopy(G.load(MANIFEST))


def grant(principal, kind, rid, op='edit'):
    return {'principal': principal, 'resource_type': kind, 'resource_id': rid, 'operations': [op], 'purpose': 'test', 'source_task': 'test'}


def test_public_manifest_names_no_control_id():
    text = MANIFEST.read_text()
    editor = [g for g in G.load(MANIFEST)['grants'] if g['principal'] == 'control_budget_editor']
    assert sorted((g['resource_type'], g['resource_id']) for g in editor) == [
        ('action', 'eios:action:EffectControl.edit:1'), ('object', 'eios:object:EffectControl/' + G.PRIVATE_TARGET),
        ('object_type', 'eios:object_type:EffectControl'), ('property', f'eios:property:EffectControl/{G.PRIVATE_TARGET}/budget_units'),
        ('property', f'eios:property:EffectControl/{G.PRIVATE_TARGET}/valid_until')]
    import re
    assert not re.search(r'EffectControl/[a-f0-9]{64}', text)


@pytest.mark.parametrize('principal,kind,rid,op,reason', [
    ('catalog_maintainer', 'object', 'eios:object:EffectControl/' + G.PRIVATE_TARGET, 'edit', 'excluded_by_policy:resource_type'),
    ('claim_matcher', 'property', f'eios:property:EffectControl/{G.PRIVATE_TARGET}/budget_units', 'edit', 'excluded_by_policy:resource_type'),
    ('control_budget_editor', 'object', 'eios:object:Consumer/' + G.PRIVATE_TARGET, 'edit', 'grant_resource'),
    ('control_budget_editor', 'property', f'eios:property:EffectControl/{G.PRIVATE_TARGET}/executor_principal', 'edit', 'grant_resource'),
    ('control_budget_editor', 'property', f'eios:property:EffectControl/{G.PRIVATE_TARGET}/owner_principal', 'edit', 'grant_resource'),
    ('control_budget_editor', 'object', 'eios:object:EffectControl/not-an-id', 'edit', 'grant_resource'),
    ('control_budget_editor', 'object', 'eios:object:EffectControl/' + G.PRIVATE_TARGET, 'read', 'excluded_by_policy:operation'),
])
def test_object_grants_refused_outside_the_editor_scope(principal, kind, rid, op, reason):
    m = manifest()
    m['grants'].append(grant(principal, kind, rid, op))
    with pytest.raises(G.ServiceGrantsRejected) as refused:
        G.validate(m)
    assert refused.value.reason == reason


def test_resolution_expands_drops_and_refuses_bad_ids():
    m = manifest()
    a, b = secrets.token_hex(32), secrets.token_hex(32)
    resolved, count = G.resolve_private_targets(m, [a, b])
    assert count == 6 and G.validate(resolved)
    assert not any(G.PRIVATE_TARGET in g['resource_id'] for g in resolved['grants'])
    dropped, none = G.resolve_private_targets(m, [])
    assert none == 0 and len(dropped['grants']) == len(m['grants']) - 3
    for bad in ([a, a], ['ABC'], [a.upper()], 'x', [secrets.token_hex(32) for _ in range(17)]):
        with pytest.raises(G.ServiceGrantsRejected):
            G.resolve_private_targets(m, bad)


def test_an_unresolved_grant_never_compiles():
    m = manifest()
    editor = next(p for p in m['principals'] if p['role'] == 'control_budget_editor')
    with pytest.raises(G.ServiceGrantsRejected) as refused:
        G.compile_principal(m, 'tenant-a', editor, credential_expires_at=EXPIRES)
    assert refused.value.reason == 'private_target_unresolved'


def test_no_rotation_other_principals_unchanged_and_editor_binding_independent_of_targets():
    m = manifest()
    without = {**m, 'principals': [p for p in m['principals'] if p['role'] != 'control_budget_editor'],
               'grants': [g for g in m['grants'] if g['principal'] != 'control_budget_editor']}
    resolved, _ = G.resolve_private_targets(m, [secrets.token_hex(32)])
    for p in without['principals']:
        before, _ = G.compile_principal(without, 'tenant-a', p, credential_expires_at=EXPIRES)
        after, _ = G.compile_principal(resolved, 'tenant-a', p, credential_expires_at=EXPIRES)
        assert before == after, p['role']
    editor = next(p for p in m['principals'] if p['role'] == 'control_budget_editor')
    bindings = {G.compile_principal(G.resolve_private_targets(m, ids)[0], 'tenant-a', editor, credential_expires_at=EXPIRES)[0]
                for ids in ([], [secrets.token_hex(32)], [secrets.token_hex(32), secrets.token_hex(32)])}
    assert len(bindings) == 1
    assert next(iter(bindings)).requested_scopes == {'action.execute', 'object_type.read', 'object.edit', 'property.edit'}
