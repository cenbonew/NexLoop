"""P0 2026-11-10 (ADR addendum A', L6 Q4/Q5): the stage grants of the effect context registrar follow its successor versions.

Only the registrar's two Actions may declare follow_latest_version in stage-principals.v1.json, grant by grant; register --dry-run
lists every derived grant with its scope difference and stops on a refused successor (a widened scope is never derived); without the
base grant nothing is derived, so the derived grant goes with its predecessor. Pure (the lineage is given)."""
import copy
import json
import sys
from pathlib import Path

import pytest

from nexloop_eios import service_grants as G

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import stage_secrets as S  # noqa: E402

BIND = 'nexloop.plan.bind_effect_context'
CONFIGURE = 'nexloop.effect.configure_control'
BIND_TYPES = ('Consumer', 'Goal', 'PlanStep', 'EffectControl')


def action(name, version, types, *, consumer=1, publication='pub-1', extra=()):
    refs = [{'tenant_id': 't', 'schema_type': 'object_type', 'stable_name': t, 'version': consumer if t == 'Consumer' else 1,
             'schema_digest': (str(consumer) if t == 'Consumer' else '1') * 64} for t in types + tuple(extra)]
    definition = {'tenant_id': 't', 'stable_name': name, 'version': version, 'status': 'published', 'contract_digest': 'c' * 64, 'object_types': refs,
                  'required_scopes': ['action.execute'], 'governance': {'risk_level': 'low', 'change_scope': {'object_types': copy.deepcopy(refs),
                  'target_systems': ['postgres'], 'properties': []}}}
    return {'resource_id': f'eios:action:{name}:{version}', 'stable_name': name, 'version': version, 'world': 'real', 'active': True,
            'definition': definition, 'capability': {'capability_name': name}, 'review_decision_id': None,
            'schema_publication_id': publication if version > 1 else None}


def lineage(**successor):
    return [action(BIND, 1, BIND_TYPES), action(BIND, 2, BIND_TYPES, consumer=2, **successor), action(CONFIGURE, 1, ('EffectControl',))]


def test_only_the_registrar_grants_declare_follow():
    manifest, _ = S.merged_manifest()
    follow = sorted((g['principal'], g['resource_id']) for g in manifest['grants'] if g.get(G.FOLLOW))
    # claim_matcher -> Consumer.edit:1 is service-grants.v1.json's own (v2, NX-044); the stage file adds exactly the registrar's.
    assert follow == [('business_owner', f'eios:action:{CONFIGURE}:1'), ('claim_matcher', 'eios:action:Consumer.edit:1'),
                      ('relay_planner', f'eios:action:{BIND}:1'), ('role_planner', f'eios:action:{BIND}:1')]


def test_follow_on_another_stage_grant_is_refused(tmp_path):
    stage = json.loads((ROOT / 'deploy/authorization/stage-principals.v1.json').read_text())
    other = next(g for g in stage['grants'] if g['resource_type'] == 'action' and BIND not in g['resource_id'] and CONFIGURE not in g['resource_id'])
    other[G.FOLLOW] = True
    path = tmp_path / 'stage-principals.json'
    path.write_text(json.dumps(stage))
    with pytest.raises(S.Refused, match='follow_latest_version outside the registrar Actions'):
        S.merged_manifest(principals_path=path)


def test_dry_run_lists_each_derived_grant_with_its_scope_difference(monkeypatch, tmp_path):
    monkeypatch.setattr(G, 'action_lineage', lambda dsn, tenant: lineage())
    report = S.register(tmp_path, tenant='t', database_url_file='configurator-dsn', signing_key_id=None, credential_expires_at='2026-12-01T00:00:00+00:00', dry_run=True)
    derived = sorted((d['principal'], d['resource_id'], json.dumps(d['scope_diff'])) for d in report['follow_latest']['derived'])
    diff = json.dumps([{'type': 'Consumer', 'from': 1, 'to': 2}])
    assert derived == [('relay_planner', f'eios:action:{BIND}:2', diff), ('role_planner', f'eios:action:{BIND}:2', diff)]
    assert report['follow_latest']['refused'] == [] and all('(schema publication pub-1)' in d['purpose'] for d in report['follow_latest']['derived'])


@pytest.mark.parametrize('successor', [dict(extra=('Product',)), dict(publication=None)], ids=['scope_widened', 'not_published'])
def test_dry_run_stops_on_a_refused_successor(monkeypatch, tmp_path, successor):
    monkeypatch.setattr(G, 'action_lineage', lambda dsn, tenant: lineage(**successor))
    with pytest.raises(G.ServiceGrantsRejected, match='follow_latest_refused'):
        S.register(tmp_path, tenant='t', database_url_file='configurator-dsn', signing_key_id=None, credential_expires_at='2026-12-01T00:00:00+00:00', dry_run=True)


def test_without_the_base_grant_nothing_is_derived():
    manifest, _ = S.merged_manifest()
    manifest = {**manifest, 'grants': [g for g in manifest['grants'] if g['resource_id'] != f'eios:action:{BIND}:1']}
    assert G.follow_preview(manifest, lineage()) == {'derived': [], 'refused': []}
