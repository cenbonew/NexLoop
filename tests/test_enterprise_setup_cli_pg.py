"""ADR-030 operator path (L5 assembly manual §6): open a consumer's enterprise slot with commands only, on clean catalog PostgreSQL.

nexloop-enterprise-setup creates the enterprise offering, the consumer's binding and the enterprise EffectControl through the
governed CREATE Actions (credentials from private files, none printed); nexloop-relay-enroll then enrolls the slot from its
enrollment file (enterprise_control_id, enterprise_offering_id, enterprise_binding_id: all three or none). The relay profile
shows the enterprise block; enrolling again without the keys clears it and the profile is byte-identical to before except
its enrollment_revision (the enrollment is append-only).
Synthetic data; the relay fixture of test_enterprise_slot_relay_pg (consumer E has no enterprise objects of its own).
"""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios import enterprise_setup, relay_enrollment
from nexloop_eios.postgres_artifacts import canonical_payload
from test_enterprise_slot_relay_pg import profile, slots  # noqa: F401
from test_relay_multi_consumer_pg import multi  # noqa: F401
from message_runtime_fixture import message_plan  # noqa: F401
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401


def private(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    path.chmod(0o600)
    return path


@pytest.fixture
def operator(slots, tmp_path):  # noqa: F811
    s = slots
    f = s['f']
    files = {name: private(tmp_path, name, token) for name, token in (('owner', s['owner_token']), ('executor', s['executor_token']),
                                                                      ('maintainer', s['keeper_token']), ('source', s['slot_source_token']))}
    common = ['--database-url-file', str(private(tmp_path, 'dsn', make_conninfo(f['pg'], user='nexloop_api'))), '--signing-key-file', str(f['signing_key']),
              '--signing-key-id', 'runtime-effect', '--artifact-root', str(tmp_path / 'artifacts')]
    e = s['people']['E']
    base = {'consumer_id': e['consumer'], 'control_id': e['controls']['reply'], 'offering_id': e['offerings']['reply'][0],
            'binding_id': e['offerings']['reply'][1], 'relay_principal': s['route_principal']}
    return dict(s, files=files, common=common, base=base, e=e)


def setup(o, tmp_path, capsys, **change):
    until = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    recipe = {'schema_version': '1.0', 'request_id': str(uuid.uuid4()), 'consumer_id': o['e']['consumer'],
              'offering': {'service_code': 'enterprise.synthetic-delivery', 'title': '合成企业交付', 'price_amount': '12.50', 'valid_until': until},
              'control': {'budget_units': 3, 'valid_until': until}, **change}
    code = enterprise_setup.main([*o['common'], '--owner-credential-file', str(o['files']['owner']), '--executor-credential-file', str(o['files']['executor']),
                                  '--maintainer-credential-file', str(o['files']['maintainer']), '--source-credential-file', str(o['files']['source']),
                                  '--recipe-file', str(private(tmp_path, 'recipe-' + recipe['request_id'], recipe))])
    return code, capsys.readouterr()


def enroll(o, tmp_path, capsys, spec):
    path = private(tmp_path, 'enrollment-' + spec['intent_id'], spec)
    code = relay_enrollment.main([*o['common'], '--owner-credential-file', str(o['files']['owner']), '--enrollment-file', str(path)])
    return code, capsys.readouterr()


def test_both_commands_open_the_enterprise_slot_from_scratch_and_clearing_restores_the_profile_bytes(operator, tmp_path, capsys):
    o = operator
    code, out = enroll(o, tmp_path, capsys, {**o['base'], 'intent_id': 'cli-enroll-0001'})
    assert code == 0
    before = profile(o, 'E')
    code, out = setup(o, tmp_path, capsys)
    assert code == 0 and out.err == ''
    created = json.loads(out.out)
    for secret in (o['owner_token'], o['executor_token'], o['keeper_token'], o['slot_source_token']):
        assert secret not in out.out
    assert created['delivery_action'] == 'nexloop.enterprise.deliver:1' and created['enrolled'] is False
    slot = {k: created[k] for k in ('enterprise_control_id', 'enterprise_offering_id', 'enterprise_binding_id')}
    admin = o['admin']
    offering = admin.execute('select properties from ontology.objects where object_id=%s', (slot['enterprise_offering_id'],)).fetchone()[0]
    assert offering['delivery_action'] == 'nexloop.enterprise.deliver:1' and offering['currency'] == 'CNY' and offering['price_amount'] == '12.50'
    code, out = enroll(o, tmp_path, capsys, {**o['base'], 'intent_id': 'cli-enroll-0002', **slot})
    assert code == 0, out
    assert profile(o, 'E')['enterprise'] == {'control_id': slot['enterprise_control_id'], 'control_revision': 1,
                                             'offering_id': slot['enterprise_offering_id'], 'binding_id': slot['enterprise_binding_id']}
    code, out = enroll(o, tmp_path, capsys, {**o['base'], 'intent_id': 'cli-enroll-0003'})  # the clearing file: no enterprise keys, new intent
    after = profile(o, 'E')
    # Byte-identical except the enrollment revision: the enrollment is append-only, each enroll is a new revision (1 -> 3).
    assert after['enrollment_revision'] == before['enrollment_revision'] + 2
    assert canonical_payload({**after, 'enrollment_revision': 0}) == canonical_payload({**before, 'enrollment_revision': 0})


@pytest.mark.parametrize('missing', ['enterprise_control_id', 'enterprise_offering_id', 'enterprise_binding_id'])
def test_an_enrollment_file_with_part_of_the_enterprise_slot_is_refused(operator, tmp_path, capsys, missing):
    o = operator
    assert enroll(o, tmp_path, capsys, {**o['base'], 'intent_id': 'cli-enroll-0001'})[0] == 0
    before = canonical_payload(profile(o, 'E'))
    code, out = setup(o, tmp_path, capsys)
    slot = {k: v for k, v in json.loads(out.out).items() if k.startswith('enterprise_') and k != missing}
    code, out = enroll(o, tmp_path, capsys, {**o['base'], 'intent_id': 'cli-enroll-partial', **slot})
    assert code == 1 and out.out == 'Relay enrollment unavailable\n'
    assert canonical_payload(profile(o, 'E')) == before


@pytest.mark.parametrize('change', [{'offering': {'service_code': 'local.json-export', 'title': 't', 'price_amount': '1', 'valid_until': '2999-01-01T00:00:00Z'}},
                                    {'control': {'budget_units': 0, 'valid_until': '2999-01-01T00:00:00Z'}}, {'extra': True}])
def test_enterprise_setup_refuses_a_malformed_recipe_and_creates_nothing(operator, tmp_path, capsys, change):
    o = operator
    count = o['admin'].execute("select count(*) from ontology.objects where type_name in ('ServiceOffering','ConsumerServiceOffering','EffectControl')").fetchone()
    code, out = setup(o, tmp_path, capsys, **change)
    assert code == 1 and out.out == '' and out.err == 'Enterprise setup unavailable\n'
    assert o['admin'].execute("select count(*) from ontology.objects where type_name in ('ServiceOffering','ConsumerServiceOffering','EffectControl')").fetchone() == count
