"""NX-041 D10 (trusted configuration): a manifest issues a simulation world's three credentials with their use.

The Python validator (trusted_configuration.validate_manifest) and control.nexloop_configure_manifest (0273, the 0211 body with the use)
both require sim_use (api | worker | agent) on a credential of one sim:/shadow: world and refuse it on a real or test credential; the use
is written to authz.nexloop_service_credentials.sim_use and compared on replay. The real manifest of the service grants is applied
before and after, unchanged (regression). Real PostgreSQL through the configurator role; synthetic tenant and principals.
"""
import copy
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import authority_records
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.trusted_configuration import ConfigurationRejected, apply_manifest_with_secrets, validate_manifest
from test_service_grants_pg import TENANT, deployment  # noqa: F401

USES = ('api', 'worker', 'agent')


def sim_world(admin):
    return admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner-principal','owner-subject','i-d10',%s)",
                         (TENANT, Jsonb({'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': 'simulation',
                                         'purpose': 'D10 manifest', 'expires_in_days': 7}))).fetchone()[0]['world']


def credential(world, use, label):
    binding, _, _ = authority_records(TENANT, 'eios:artifact:' + 'd' * 32, world, identity_suffix='-d10m-' + label)
    item = {'reference': 'd10m-' + label, 'binding': binding.model_dump(mode='json'), 'worlds': [world],
            'expires_at': (datetime.now(UTC) + timedelta(hours=1)).isoformat(), 'status': 'active'}
    if use is not None:
        item['sim_use'] = use
    return item


def manifest(admin, items):
    revision = admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT,)).fetchone()[0]
    return {'schema_version': '1.0', 'manifest_id': str(uuid.uuid4()), 'tenant_id': TENANT, 'expected_revision': revision, 'tenant_status': 'active',
            'object_types': [], 'actions': [], 'functions': [], 'authority_facts': [], 'service_credentials': items, 'browser_applications': [],
            'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}


def apply(d, body):
    secrets_map = {i['reference']: secrets.token_urlsafe(48) for i in body['service_credentials']}
    return apply_manifest_with_secrets(body, database_url_file=d['paths']['dsn'], signing_key_file=d['paths']['signing'], signing_key_id='nx048-deploy',
                                       service_secrets=secrets_map), secrets_map


def raw_configure(d, admin, body, digests):
    """Bypass the Python validator: the SQL function itself must refuse."""
    text = canonical_payload(body)
    with psycopg.connect(make_conninfo(d['pg'], user='nexloop_configurator')) as db, db.transaction():
        return db.execute('select control.nexloop_configure_manifest(%s,%s,%s,%s,%s,%s::jsonb)',
                          (TENANT, text, hashlib.sha256(text.encode()).hexdigest(), 'nx048-deploy', d['signer'].material, Jsonb(digests))).fetchone()[0]


def real_rows(admin):
    return admin.execute("select credential_id,binding,worlds,status,sim_use from authz.nexloop_service_credentials where 'real'=any(worlds) order by credential_id").fetchall()


def test_a_manifest_issues_the_three_uses_of_a_simulation_world(deployment, admin):  # noqa: F811
    d = deployment
    report = d['apply']()  # the real service-grants manifest first (regression below)
    assert report
    real_before = real_rows(admin)
    world = sim_world(admin)
    body = manifest(admin, [credential(world, use, use) for use in USES])
    result, tokens = apply(d, body)
    assert result['configured'] is True
    rows = admin.execute('select sim_use,worlds,status from authz.nexloop_service_credentials where worlds=%s order by sim_use', ([world],)).fetchall()
    assert rows == [(u, [world], 'active') for u in sorted(USES)]
    for use in USES:  # each authenticates in its world
        digest = hashlib.sha256(tokens['d10m-' + use].encode()).hexdigest()
        assert admin.execute('select authz.nexloop_service_identity(%s,%s)', (digest, world)).fetchone()[0]['binding']['tenant_id'] == TENANT
    # Replaying the same manifest changes nothing (0050 replay), the use included.
    assert apply_manifest_with_secrets(body, database_url_file=d['paths']['dsn'], signing_key_file=d['paths']['signing'], signing_key_id='nx048-deploy',
                                       service_secrets=tokens)['configured'] is True
    # A fourth credential of a use already held is refused (one per world and use).
    with pytest.raises(ConfigurationRejected):
        apply(d, manifest(admin, [credential(world, 'worker', 'worker-2')]))
    # Real regression: the real manifest applies again unchanged and real credentials carry no use.
    d['apply']()
    assert real_rows(admin) == real_before and all(r[4] is None for r in real_before)


def test_a_missing_or_wrong_use_and_a_use_on_a_real_credential_are_refused(deployment, admin):  # noqa: F811
    d = deployment
    d['apply']()
    world = sim_world(admin)
    cases = {'missing': credential(world, None, 'missing'), 'unknown': credential(world, 'operator', 'unknown'),
             'on real': credential('real', 'api', 'on-real'), 'on test': credential('test', 'worker', 'on-test')}
    for label, item in cases.items():
        body = manifest(admin, [item])
        # The Python validator refuses it ...
        with pytest.raises(ConfigurationRejected):
            validate_manifest(copy.deepcopy(body))
        # ... and so does the SQL function on its own (the Python check bypassed).
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='configuration rejected'):
            raw_configure(d, admin, body, {item['reference']: secrets.token_hex(32)})
        assert label
    # Positive control of the same paths: a sim credential with its use passes both.
    good = manifest(admin, [credential(world, 'agent', 'good')])
    validate_manifest(copy.deepcopy(good))
    assert raw_configure(d, admin, good, {'d10m-good': secrets.token_hex(32)})['configured'] is True
    assert admin.execute('select sim_use from authz.nexloop_service_credentials where worlds=%s', ([world],)).fetchall() == [('agent',)]
