"""ADR-031 counterexamples: one relay Source derivation per object per read_object call keeps every per-property refusal.

On the relay fixture (test_enterprise_slot_relay_pg; consumer D enrolled with its reply and enterprise offerings), the relay
Source reads the enrolled catalog objects through authz.nexloop_read_object (0420):
- a property whose group the owner restricted is still refused, in the same call where the object and its other properties
  are allowed; without that property the same read succeeds;
- an object that is allowed does not allow another object: a property of another offering is refused with the first
  object's derivation, and an offering outside every enrollment is refused.
Equivalence with the previous body over the wider corpus is checked by support/read_object_compare.py. Synthetic data.
"""
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.property_access import PropertyGroupRestriction
from nexloop_eios.service_offerings import OFFERING_FIELDS, json_export_example
from test_enterprise_slot_relay_pg import slots  # noqa: F401
from test_relay_multi_consumer_pg import multi  # noqa: F401
from message_runtime_fixture import message_plan  # noqa: F401
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401


@pytest.fixture
def source(slots):  # noqa: F811
    s = slots
    s['enroll']('D')
    return dict(s, source=lambda: s['api'].authenticate(s['slot_source_token'], world='real'), d=s['people']['D'])


def group_of(admin, tenant, type_name, field):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        return admin.execute('select ontology.nexloop_property_group_of(definition,%s) from ontology.object_type_versions where tenant_id=%s and type_name=%s '
                             'order by version desc limit 1', (field, tenant, type_name)).fetchone()[0]


def test_a_restricted_group_property_is_still_refused_in_the_call_where_the_object_is_allowed(source):
    s = source
    offering = s['d']['offerings']['reply'][0]
    assert s['source']().read_object(type_name='ServiceOffering', object_id=offering, fields=OFFERING_FIELDS)['object_id'] == offering
    # The published ServiceOffering schema places no catalog field in a property group; the test gives 'title' one in its
    # disposable database (explicit injection) so that the owner-restricted group path is exercised.
    # (A published schema version is immutable by trigger; the superuser skips triggers for this one statement's transaction.)
    with s['admin'].transaction():
        s['admin'].execute('set local session_replication_role=replica')
        s['admin'].execute("update ontology.object_type_versions set definition=definition||%s where tenant_id=%s and type_name='ServiceOffering'",
                           (Jsonb({'property_groups': [{'group_name': 'preference', 'property_names': ['title']}]}), s['tenant']))
    group = group_of(s['admin'], s['tenant'], 'ServiceOffering', 'title')
    assert group == 'preference'
    assert s['source']().read_object(type_name='ServiceOffering', object_id=offering, fields=OFFERING_FIELDS)['object_id'] == offering
    s['admin'].execute('insert into authz.nexloop_authority_facts values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
                       (s['tenant'], 'property_group_restriction', ['ServiceOffering'], Jsonb(PropertyGroupRestriction(
                           tenant_id=s['tenant'], type_name='ServiceOffering', restricted_groups=(group,), decision='synthetic owner decision').model_dump(mode='json'))))
    with pytest.raises(Exception):
        s['source']().read_object(type_name='ServiceOffering', object_id=offering, fields=OFFERING_FIELDS)
    others = tuple(f for f in OFFERING_FIELDS if group_of(s['admin'], s['tenant'], 'ServiceOffering', f) != group)
    assert set(s['source']().read_object(type_name='ServiceOffering', object_id=offering, fields=others)['properties']) == set(others)


def test_an_allowed_object_does_not_allow_another_object(source):
    s = source
    admin, tenant = s['admin'], s['tenant']
    reply, enterprise = s['d']['offerings']['reply'][0], s['d']['offerings']['enterprise'][0]
    assert s['source']().read_object(type_name='ServiceOffering', object_id=enterprise, fields=OFFERING_FIELDS)['object_id'] == enterprise
    # The per-property part with one object's derivation never serves a property of another object (same type, same call).
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        principal = s['source']()._session.authentication.subject_principal_id
        basis = admin.execute("select authz.nexloop_relay_source_derivation(%s,%s,'real',%s,'read')", (tenant, principal, 'eios:object:ServiceOffering/' + reply)).fetchone()[0]
        assert basis is not None
        same = admin.execute("select authz.nexloop_relay_source_property_basis(%s,'real',%s,%s,%s)",
                             (tenant, 'eios:object:ServiceOffering/' + reply, Jsonb(basis), 'eios:property:ServiceOffering/' + reply + '/title')).fetchone()[0]
        other = admin.execute("select authz.nexloop_relay_source_property_basis(%s,'real',%s,%s,%s)",
                              (tenant, 'eios:object:ServiceOffering/' + reply, Jsonb(basis), 'eios:property:ServiceOffering/' + enterprise + '/title')).fetchone()[0]
        assert same is not None and other is None
    # An offering in no enrollment is refused even right after allowed reads of enrolled ones.
    keeper = s['api'].authenticate(s['keeper_token'], world='real')
    stray = keeper.create_object(action_name='ServiceOffering.create', action_version=1, intent_id='stray-offering-0001', type_name='ServiceOffering',
                                 properties=json_export_example(valid_until=s['expiry']))['object_id']
    with pytest.raises(Exception):
        s['source']().read_object(type_name='ServiceOffering', object_id=stray, fields=OFFERING_FIELDS)
    assert s['source']().read_object(type_name='ServiceOffering', object_id=reply, fields=OFFERING_FIELDS)['object_id'] == reply
