"""Claim → Consumer property phase one (migration claim_auto_write; plan §9.1, §10 item 1): the per-property auto-write whitelist,
enforced in SQL. Default deny: with edit_actions configured and an empty whitelist, a match yields a proposal recorded for review
(reason auto_write_not_allowed, status proposed, Claim needs_resolution) and nothing is written - the matcher's configuration cannot
loosen it. The owner's manifest, published by the trusted configuration identity only, whitelists a property; a sensitive property
(identity, contact, address, payment) is refused in the manifest and can never be applied, even by a row put in the table.
Actual PostgreSQL authority; the matcher of tests/test_claim_matching_pg.py with its whitelist helper switched off."""
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.auto_write import AutoWriteRejected, apply_manifest, load_manifest, validate_manifest
from support.auto_write import allow
from test_claim_matching_pg import decision, env, matcher, obj, resolution  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
BUDGET = 'eios:property:Consumer/budget_level'


def deny_all(admin):
    """Undo the matching tests' whitelist stand-in: the database as deployed, before any manifest."""
    admin.execute('drop trigger if exists nexloop_test_auto_write_published on ontology.object_type_versions')
    admin.execute('delete from control.nexloop_auto_write_properties')


def proposal_rows(admin):
    return admin.execute('select status,reason,property_name from ontology.nexloop_mutation_proposals order by created_at').fetchall()


def edits(admin):
    return admin.execute("select count(*) from runtime.nexloop_action_claims where action_name like 'Consumer.edit%'").fetchone()[0]


def configurator(f, tmp_path):
    f['admin'].execute('alter role nexloop_configurator login')  # the configurator window opens it for one call on stage
    dsn = tmp_path / 'configurator-dsn'
    dsn.write_text(make_conninfo(f['pg'], user='nexloop_configurator'))
    dsn.chmod(0o600)
    return dsn


def manifest(version, *pairs, world='real'):
    return {'schema_version': 'nexloop-auto-write/1', 'version': version, 'world': world,
            'properties': [{'type_name': t, 'property_name': p} for t, p in pairs]}


def test_with_an_empty_whitelist_a_match_is_only_a_proposal_for_review(env):
    f = env;admin = f['admin'];deny_all(admin)
    claim = f['claims'].add('auto-write-budget', '预算', '两千元以内', kind='constraint')
    m, _ = matcher(f, {claim: decision(BUDGET, '两千元以内')})
    out = m.process_conversation(f['conversation'])
    assert out['matches'][claim]['outcome'] == 'full_match'
    assert list(out['applied'].values()) == ['auto_write_not_allowed']
    assert proposal_rows(admin) == [('proposed', 'auto_write_not_allowed', 'budget_level')]
    assert resolution(admin, claim) == 'needs_resolution' and 'budget_level' not in obj(admin, f['consumer'])[0] and edits(admin) == 0
    # Again: nothing changes, nothing is retried as an error.
    assert list(m.process_conversation(f['conversation'])['applied'].values()) == ['auto_write_not_allowed']
    assert proposal_rows(admin) == [('proposed', 'auto_write_not_allowed', 'budget_level')] and edits(admin) == 0
    # The transition itself is refused in SQL, whatever the caller (the matcher's configuration cannot loosen it).
    row = m.port.read({'verb': 'proposal', 'proposal_id': out['matches'][claim]['proposal_id']})
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='auto write not allowed'):
        m.port.record({'verb': 'transition', 'proposal_id': row['proposal_id'], 'from': 'proposed', 'to': 'applying', 'apply_args': {'kind': 'edit'}})


def test_the_owner_manifest_whitelists_a_property_and_the_waiting_proposal_applies(env, tmp_path):
    f = env;admin = f['admin'];deny_all(admin)
    claim = f['claims'].add('auto-write-budget', '预算', '两千元以内', kind='constraint')
    m, _ = matcher(f, {claim: decision(BUDGET, '两千元以内')})
    m.process_conversation(f['conversation'])
    dsn = configurator(f, tmp_path)
    assert apply_manifest(manifest(1, ('Consumer', 'budget_level')), f['tenant'], database_url_file=dsn)['changed'] is True
    assert list(m.process_conversation(f['conversation'])['applied'].values()) == ['applied']
    assert obj(admin, f['consumer'])[0]['budget_level'] == '两千元以内' and resolution(admin, claim) == 'resolved' and edits(admin) == 1
    # Versions are immutable and move forward; the same content again changes nothing; a newer empty one takes it back.
    assert apply_manifest(manifest(1, ('Consumer', 'budget_level')), f['tenant'], database_url_file=dsn)['changed'] is False
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='immutable'):
        apply_manifest(manifest(1), f['tenant'], database_url_file=dsn)
    assert apply_manifest(manifest(3), f['tenant'], database_url_file=dsn)['properties'] == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='newer'):  # an unpublished but older version
        apply_manifest(manifest(2, ('Consumer', 'favorite_sport')), f['tenant'], database_url_file=dsn)
    assert admin.execute('select count(*) from control.nexloop_auto_write_properties').fetchone() == (0,)
    # Only the trusted configuration identity publishes.
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_domain_worker')) as worker:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute('select control.nexloop_configure_auto_write(%s,%s::jsonb)', (f['tenant'], json.dumps(manifest(4))))


@pytest.mark.parametrize('pair', [('Consumer', 'display_name'), ('Consumer', 'external_id_refs'), ('Consumer', 'contact_preferences'),
                                  ('Consumer', 'phone'), ('Consumer', 'home_address'), ('Consumer', 'checkout_payment_card'),
                                  ('Consumer', 'nickname'), ('Consumer', 'id_number'), ('Order', 'delivery_address'), ('Order', 'billing_email')])
def test_a_sensitive_property_is_refused_in_the_manifest_and_never_applies(env, tmp_path, pair):
    f = env;admin = f['admin'];deny_all(admin)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='auto write property sensitive'):
        apply_manifest(manifest(1, ('Consumer', 'budget_level'), pair), f['tenant'], database_url_file=configurator(f, tmp_path))
    assert admin.execute('select count(*) from control.nexloop_auto_write_properties').fetchone() == (0,)
    # Even a row written past the manifest never makes it writable: the check is repeated at every use.
    allow(admin, f['tenant'], [pair])
    assert admin.execute('select ontology.nexloop_auto_write_allowed(%s,%s,%s,%s)', (f['tenant'], 'real', pair[0], [pair[1]])).fetchone() == (False,)


def test_a_non_personal_name_on_another_type_is_not_sensitive(env):
    f = env;admin = f['admin'];deny_all(admin)
    allow(admin, f['tenant'], [('Product', 'name'), ('Product', 'sku')])
    allowed = lambda t, props: admin.execute('select ontology.nexloop_auto_write_allowed(%s,%s,%s,%s)', (f['tenant'], 'real', t, props)).fetchone()[0]
    assert allowed('Product', ['name', 'sku']) is True and allowed('Product', ['name', 'price']) is False and allowed('Product', []) is False
    assert allowed('Consumer', ['name']) is False
    # Another tenant or world sees nothing of it.
    assert admin.execute('select ontology.nexloop_auto_write_allowed(%s,%s,%s,%s)', (f['tenant'], 'sim:other', 'Product', ['name'])).fetchone() == (False,)


def test_the_shipped_manifest_is_empty_and_the_shape_is_checked():
    shipped = load_manifest(ROOT / 'deploy/configuration/auto-write.v1.json')
    assert shipped == manifest(1)
    for bad in ({**manifest(1), 'tenant_id': 'x'}, {**manifest(0)}, {**manifest(1), 'properties': [{'type_name': 'consumer', 'property_name': 'x'}]},
                {**manifest(1), 'properties': [{'type_name': 'Consumer', 'property_name': 'x'}] * 2}, {**manifest(True)}):
        with pytest.raises(AutoWriteRejected):
            validate_manifest(bad)


def test_the_apply_command_checks_without_connecting_and_never_prints_a_dsn(tmp_path, capsys):
    import sys
    sys.path.insert(0, str(ROOT / 'scripts/deploy'))
    import auto_write_apply as A
    assert A.main(['--manifest', str(ROOT / 'deploy/configuration/auto-write.v1.json'), '--check']) == 0
    assert json.loads(capsys.readouterr().out) == {'valid': True, 'version': 1, 'world': 'real', 'properties': 0}
    assert A.main(['--manifest', str(ROOT / 'deploy/configuration/auto-write.v1.json')]) == 2
    assert json.loads(capsys.readouterr().out)['applied'] is False
