"""Relay multi-consumer, Source read derivation (0161): the rule is published only by trusted configuration.

service-grants v16 carries relay_source_read_rules; service_grants.apply (as nexloop_configurator) compiles them into
relay_source_read_rule facts, re-apply is a no-op, removing a rule deactivates it, the Python validator refuses bad manifests
and control.nexloop_configure_manifest itself refuses malformed facts. Real PostgreSQL, synthetic tenant.
"""
import copy

import psycopg
import pytest

from nexloop_eios import service_grants as G
from test_service_grants_pg import TENANT, _raw_configure, deployment  # noqa: F401

SOURCE = 'nexloop-service-relay-source-principal'


def stored(admin):
    return admin.execute("select entity_key,payload from authz.nexloop_authority_facts where fact_kind='relay_source_read_rule'").fetchall()


def test_v16_rule_is_written_by_apply_reapplied_as_noop_and_deactivated_when_removed(deployment, admin):
    d = deployment
    report = d['apply']()
    relay = next(p for p in d['manifest']['principals'] if p['role'] == 'message_relay')['principal_id']
    assert d['manifest']['manifest_version'] == 16
    ((key, payload),) = stored(admin)
    assert key == [SOURCE] and payload['principal_id'] == SOURCE and payload['relay_principal'] == relay and payload['active'] is True
    assert {'kind': 'relay_source_read_rule', 'key': [SOURCE]} in report['facts_written']
    again = d['apply']()
    assert {'kind': 'relay_source_read_rule', 'key': [SOURCE]} not in again['facts_written']
    assert G.doctor(d['manifest'], TENANT, database_url_file=d['paths']['dsn'])['in_sync'] is True
    removed = copy.deepcopy(d['manifest'])
    removed.pop('relay_source_read_rules')
    assert {'kind': 'relay_source_read_rule', 'key': [SOURCE]} in d['apply'](G.validate(removed))['facts_written']
    assert stored(admin)[0][1]['active'] is False  # deactivated, never deleted


@pytest.mark.parametrize('change,reason', [
    (lambda m: m['relay_source_read_rules'][0].update(source_principal_id='nexloop-service-message-relay-principal'), 'relay_source_rule_principal'),
    (lambda m: m['relay_source_read_rules'][0].update(relay='no_such_principal'), 'relay_source_rule_shape'),
    (lambda m: m['relay_source_read_rules'][0].update(relay='claim_matcher'), 'relay_source_rule_requires_route_grant'),
    (lambda m: m['relay_source_read_rules'].append(dict(m['relay_source_read_rules'][0])), 'relay_source_rule_duplicate'),
    (lambda m: m['relay_source_read_rules'][0].update(extra='x'), 'relay_source_rule_shape'),
    (lambda m: m['relay_source_read_rules'][0].update(valid_until='2027-10-09T08:00:00+08:00'), 'relay_source_rule_shape'),
])
def test_validator_refuses_bad_relay_source_rules(change, reason):
    from test_service_grants_pg import MANIFEST
    manifest = copy.deepcopy(G.load(MANIFEST))
    change(manifest)
    with pytest.raises(G.ServiceGrantsRejected, match=reason):
        G.validate(manifest)


def test_configure_manifest_refuses_malformed_relay_source_rules(deployment, admin):
    d = deployment
    d['apply']()
    relay = next(p for p in d['manifest']['principals'] if p['role'] == 'message_relay')['principal_id']
    rule = {'tenant_id': TENANT, 'principal_id': 'synthetic-relay-source', 'relay_principal': relay, 'active': True, 'valid_until': '2027-01-01T00:00:00+00:00'}
    bad = [rule | {'extra': 1}, {k: v for k, v in rule.items() if k != 'active'}, rule | {'active': 'true'}, rule | {'relay_principal': 'synthetic-relay-source'},
           rule | {'relay_principal': 'not-a-service-principal'}, rule | {'valid_until': None}, rule | {'tenant_id': 'other-tenant'}]
    for payload in bad:
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            _raw_configure(d, admin, [{'kind': 'relay_source_read_rule', 'key': ['synthetic-relay-source'], 'payload': payload}])
    with pytest.raises(psycopg.errors.InvalidParameterValue):  # key and principal must agree
        _raw_configure(d, admin, [{'kind': 'relay_source_read_rule', 'key': ['another-source'], 'payload': rule}])
    assert _raw_configure(d, admin, [{'kind': 'relay_source_read_rule', 'key': ['synthetic-relay-source'], 'payload': rule}])['configured'] is True
