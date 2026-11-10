"""P0 2026-11-10 (ADR-046, L6 Q4/Q5, owner Q5'): the registrar grants and business_owner Consumer.create follow successor versions.

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
CREATE = 'Consumer.create'


def action(name, version, types, *, consumer=1, publication='pub-1', extra=(), bumped=()):
    def at(t):
        return consumer if t == 'Consumer' else 2 if t in bumped else 1
    refs = [{'tenant_id': 't', 'schema_type': 'object_type', 'stable_name': t, 'version': at(t),
             'schema_digest': str(at(t)) * 64} for t in types + tuple(extra)]
    definition = {'tenant_id': 't', 'stable_name': name, 'version': version, 'status': 'published', 'contract_digest': 'c' * 64, 'object_types': refs,
                  'required_scopes': ['action.execute'], 'governance': {'risk_level': 'low', 'change_scope': {'object_types': copy.deepcopy(refs),
                  'target_systems': ['postgres'], 'properties': []}}}
    return {'resource_id': f'eios:action:{name}:{version}', 'stable_name': name, 'version': version, 'world': 'real', 'active': True,
            'definition': definition, 'capability': {'capability_name': name}, 'review_decision_id': None,
            'schema_publication_id': publication if version > 1 else None}


def lineage(create=None, **successor):
    """The registrar's lineage; with create (a dict of successor options, plus 'base_types' and 'edit'), Consumer.create :1 and :2."""
    rows = [action(BIND, 1, BIND_TYPES), action(BIND, 2, BIND_TYPES, consumer=2, **successor), action(CONFIGURE, 1, ('EffectControl',))]
    if create is not None:
        create = dict(create)
        types = ('Consumer',) + create.pop('base_types', ())
        edit = create.pop('edit', None)
        succ = action(CREATE, 2, types, **{'consumer': 2, **create})
        if edit:
            edit(succ)
        rows += [action(CREATE, 1, types), succ]
    return rows


def test_the_followable_list_is_exactly_three_grants():
    # Q5' (owner 2026-11-10): listed one by one; the create follow is not widened to any other effect or create Action.
    assert S.FOLLOWABLE == {f'eios:action:{BIND}:1', f'eios:action:{CONFIGURE}:1', f'eios:action:{CREATE}:1'}


def test_only_the_registrar_grants_declare_follow():
    manifest, _ = S.merged_manifest()
    follow = sorted((g['principal'], g['resource_id']) for g in manifest['grants'] if g.get(G.FOLLOW))
    # claim_matcher -> Consumer.edit:1 is service-grants.v1.json's own (v2, NX-044); the stage file adds exactly the registrar's.
    assert follow == [('business_owner', f'eios:action:{CREATE}:1'), ('business_owner', f'eios:action:{CONFIGURE}:1'),
                      ('claim_matcher', 'eios:action:Consumer.edit:1'),
                      ('relay_planner', f'eios:action:{BIND}:1'), ('role_planner', f'eios:action:{BIND}:1')]


def test_follow_on_another_stage_grant_is_refused(tmp_path):
    stage = json.loads((ROOT / 'deploy/authorization/stage-principals.v1.json').read_text())
    other = next(g for g in stage['grants'] if g['resource_type'] == 'action' and g['resource_id'] not in S.FOLLOWABLE)
    other[G.FOLLOW] = True
    path = tmp_path / 'stage-principals.json'
    path.write_text(json.dumps(stage))
    with pytest.raises(S.Refused, match='follow_latest_version outside the listed Actions'):
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


# ---- Q5' (owner 2026-11-10 "按推荐"): business_owner -> Consumer.create follows the human publication's equivalent successor ----

def _dry(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(G, 'action_lineage', lambda dsn, tenant: rows)
    return S.register(tmp_path, tenant='t', database_url_file='configurator-dsn', signing_key_id=None,
                      credential_expires_at='2026-12-01T00:00:00+00:00', dry_run=True)


def test_dry_run_derives_business_owner_consumer_create_2(monkeypatch, tmp_path):
    report = _dry(monkeypatch, tmp_path, lineage(create={}))
    derived = sorted((d['principal'], d['resource_id'], json.dumps(d['scope_diff'])) for d in report['follow_latest']['derived'])
    diff = json.dumps([{'type': 'Consumer', 'from': 1, 'to': 2}])
    assert derived == [('business_owner', f'eios:action:{CREATE}:2', diff), ('relay_planner', f'eios:action:{BIND}:2', diff),
                       ('role_planner', f'eios:action:{BIND}:2', diff)]
    assert report['follow_latest']['refused'] == []


def _set(path, value):
    def edit(row):
        node = row
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return edit


def _drop(row):
    row['definition']['governance']['change_scope']['properties'] = ['name']


@pytest.mark.parametrize('create, reason', [
    (dict(base_types=('Tenant',), bumped=('Tenant',)), 'create_scope_other_type'),   # another type's version moves too
    (dict(consumer=1, base_types=('Tenant',), bumped=('Tenant',)), 'create_scope_other_type'),  # only another type moves
    (dict(extra=('Product',)), 'resource_scope_changed'),                              # scope widened
    (dict(publication=None), 'not_published_by_review_decision'),                      # not a human publication (or digest changed)
    (dict(edit=_set(('active',), False)), 'successor_inactive'),
    (dict(edit=_set(('capability',), {'capability_name': 'other'})), 'capability_changed'),
    (dict(edit=_set(('definition', 'required_scopes'), ['action.execute', 'object.write'])), 'definition_changed'),
    (dict(edit=_set(('definition', 'governance', 'risk_level'), 'high')), 'governance_changed'),
    (dict(edit=_drop), 'change_scope_changed'),
    (dict(edit=_set(('world',), 'sim')), None),                                        # another world's row is not this lineage
], ids=['other_type_too', 'other_type_only', 'scope_widened', 'not_published', 'inactive', 'capability', 'definition', 'governance',
        'change_scope', 'other_world'])
def test_create_follow_refusals(monkeypatch, tmp_path, create, reason):
    rows = lineage(create=create)
    if reason is None:  # the :2 row lives in another world: nothing derived for Consumer.create, nothing refused
        report = _dry(monkeypatch, tmp_path, rows)
        assert all(CREATE not in d['resource_id'] for d in report['follow_latest']['derived'])
        return
    manifest, _ = S.merged_manifest()
    refused = G.follow_preview(manifest, rows)['refused']
    assert [(r['principal'], r['successor'], r['reason']) for r in refused] == [('business_owner', f'eios:action:{CREATE}:2', reason)]
    with pytest.raises(G.ServiceGrantsRejected, match='follow_latest_refused'):
        _dry(monkeypatch, tmp_path, rows)


def test_the_create_scope_rule_holds_only_for_create_actions():
    # claim_matcher -> Consumer.edit follows as before (NX-044): the Q5' type rule names <Type>.create only.
    assert G._create_scope_reason('Consumer.edit', action('Consumer.edit', 1, ('Consumer', 'Tenant')),
                                  action('Consumer.edit', 2, ('Consumer', 'Tenant'), consumer=2, bumped=('Tenant',))) is None
    assert G._create_scope_reason(CREATE, action(CREATE, 1, ('Consumer',)), action(CREATE, 2, ('Consumer',), consumer=2)) is None
