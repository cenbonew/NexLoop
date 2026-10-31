"""ADR-035 (supplements ADR-025): workbench owners and operators read the operations objects Goal, PlanStep, Commitment and
ServiceOffering through the member derivation, audited; each type's fixed field list only, never an owner-restricted group, never
Commitment's customer content; nothing for reviewers, other tenants, or objects of a Consumer being erased.

Real PostgreSQL and workbench sessions on the slice 1 fixture (test_workbench_read_pg). Admin only seeds (synthetic objects; the
Commitment in the keeper's order the 0111 guard requires; the owner restriction fact and an erasure as configuration stand-ins)
and probes. Reads go through the production path: WorkbenchReader.read_object -> authz.nexloop_read_object -> the derivation.
"""
import hashlib
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from erasure_support import mark_erasing
from support.tenants import TENANT_B
from test_workbench_member_read_pg import reader_for, staff_session
from test_workbench_read_pg import (ROLES, TENANT, browser_business, configure, conversations, identity, published_action, uow,  # noqa: F401
    workbench, write_facts)
from nexloop_eios.workbench_roles import compile_members
from test_workbench_read_pg import members_file

OFFERING_FIELDS = ['service_code', 'title', 'delivery_action', 'content_kind', 'price_amount', 'currency', 'eligibility', 'allowed_guarantees',
                   'allowed_discounts', 'evidence_kind', 'active', 'valid_until']


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def obj(admin, type_name, object_id, props, tenant=TENANT, source_ref=None):
    admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
        values(%s,'real',%s,%s,1,%s,'synthetic',%s,clock_timestamp(),clock_timestamp())""", (tenant, type_name, object_id, Jsonb(props), source_ref or 'ops-' + object_id[:8]))


def seed(w):
    from commitment_fixture import publish_commitment_type
    admin, consumer = w['admin'], w['f']['consumer']
    publish_commitment_type(admin, TENANT, actions=False)
    ids = {t: hexid('ops-' + t) for t in ('Goal', 'PlanStep', 'Commitment', 'ServiceOffering')}
    obj(admin, 'Goal', ids['Goal'], {'consumer_id': consumer, 'state': 'active', 'valid_until': '2099-01-01T00:00:00Z'})
    obj(admin, 'PlanStep', ids['PlanStep'], {'consumer_id': consumer, 'goal_id': ids['Goal'], 'control_id': hexid('control'), 'action_name': 'nexloop.service.request',
                                             'state': 'ready', 'submitter_principals': ['nexloop-service-role-source-principal']})
    obj(admin, 'ServiceOffering', ids['ServiceOffering'], {f: 'x' for f in OFFERING_FIELDS} | {'active': True, 'valid_until': '2099-01-01T00:00:00Z'})
    now = datetime.now(UTC).replace(microsecond=0)
    props = {'made_to': consumer, 'made_by': {'sender_kind': 'agent', 'sender_principal': 'synthetic-agent', 'role_ref': None, 'run_id': None, 'intent_id': None},
             'content_ref': {'claim_id': None, 'message_id': None, 'span': [0, 1], 'content_hash': 'e' * 64}, 'promised_at': now.isoformat(),
             'due_at': (now + timedelta(days=1)).isoformat(), 'due_precision': 'exact', 'condition': '如果下周还没发货就退款', 'related_goal_ref': None, 'supersedes': None,
             'status': 'open', 'late': False, 'fulfillment_basis': 'undetermined', 'fulfillment_evidence': []}
    # The keeper's order (0111 guard): a prepared row, the object with the intent as its source, then registered. An 'extend' row
    # (origin without a Claim) over a synthetic parent keeps the seed free of Claims.
    parent = hexid('ops-parent')
    for cid, intent, extra in ((parent, 'ops-parent-intent', {}), (ids['Commitment'], 'ops-commitment-intent', {'supersedes': parent})):
        row = props | extra
        admin.execute("""insert into runtime.nexloop_commitments(tenant_id,world,commitment_id,dedupe_key,origin,consumer_id,supersedes,intent_id,properties)
            values(%s,'real',%s,%s,'extend',%s,%s,%s,%s)""", (TENANT, cid, 'ops-' + intent, consumer, extra.get('supersedes') or hexid('ops-root'), intent, Jsonb(row)))
        obj(admin, 'Commitment', cid, row, source_ref=intent)
        admin.execute("update runtime.nexloop_commitments set registered_at=clock_timestamp() where tenant_id=%s and commitment_id=%s", (TENANT, cid))
    return ids


def members(w, *extra):
    pairs = ((w['owner'], 'owner'), (w['operator'], 'operator'), *extra)
    configure(w, *pairs)
    write_facts(w['admin'], compile_members(ROLES, members_file(*pairs)))


def read(w, member, type_name, object_id, fields):
    client, inspected = staff_session(w, member)
    try:
        return reader_for(w, inspected).read_object(type_name, object_id, list(fields), 'world_model')
    finally:
        client.__exit__(None, None, None)


def test_owner_and_operator_read_operations_objects_within_the_field_lists(workbench):
    w = workbench
    members(w)
    admin = w['admin']
    ids = seed(w)
    before = admin.execute("select count(*) from runtime.nexloop_workbench_read_audit where read_purpose='world_model'").fetchone()[0]
    for member in (w['owner'], w['operator']):
        assert read(w, member, 'Goal', ids['Goal'], ['consumer_id', 'state', 'valid_until'])['state'] == 'active'
        assert read(w, member, 'PlanStep', ids['PlanStep'], ['consumer_id', 'goal_id', 'control_id', 'action_name', 'state'])['goal_id'] == ids['Goal']
        got = read(w, member, 'Commitment', ids['Commitment'], ['made_to', 'due_at', 'status', 'late', 'fulfillment_basis'])
        assert got['made_to'] == w['f']['consumer'] and got['status'] == 'open'
        assert read(w, member, 'ServiceOffering', ids['ServiceOffering'], OFFERING_FIELDS)['active'] is True
        # Outside the field lists: the promise's customer content and who made it; a step's submitter principals. The operator stays
        # inside them; the owner reads them through its roles v52 read list (I35: every declared property of a listed type).
        for type_name, field in (('Commitment', 'condition'), ('Commitment', 'content_ref'), ('Commitment', 'made_by'), ('Commitment', 'fulfillment_evidence'),
                                 ('PlanStep', 'submitter_principals')):
            got = read(w, member, type_name, ids[type_name], [field])
            # This fixture publishes no PlanStep type version: the list opens only declared properties of a published version.
            assert (got is not None) is (member is w['owner'] and type_name == 'Commitment'), (type_name, field)
    # One audit row per object read, with the new kinds.
    kinds = admin.execute("""select object_kind,count(*) from runtime.nexloop_workbench_read_audit where read_purpose='world_model' group by 1 order by 1""").fetchall()
    assert {k for k, _ in kinds} == {'goal', 'plan_step', 'commitment', 'service_offering'}
    # 4 object reads per member, and the owner's 4 Commitment reads beyond the field lists (I35).
    assert admin.execute("select count(*) from runtime.nexloop_workbench_read_audit where read_purpose='world_model'").fetchone()[0] - before == 8 + 4


def test_restricted_groups_reviewers_other_tenants_and_erasure_get_nothing(workbench):
    w = workbench
    members(w, (w['outsider'], 'reviewer'))
    admin = w['admin']
    ids = seed(w)
    # A reviewer (ADR-035 §2.1) derives nothing of these types.
    assert read(w, w['outsider'], 'Goal', ids['Goal'], []) is None and read(w, w['outsider'], 'ServiceOffering', ids['ServiceOffering'], ['title']) is None
    # An owner-restricted group is never derived, even for a listed field; the other group stays readable.
    admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
        (TENANT, 'property_group_restriction', ['Commitment'], Jsonb({'tenant_id': TENANT, 'type_name': 'Commitment', 'restricted_groups': ['commitment_lifecycle'],
                                                                         'decision': 'owner 2026-10-19 (synthetic)'})))
    assert read(w, w['owner'], 'Commitment', ids['Commitment'], ['status']) is None
    assert read(w, w['owner'], 'Commitment', ids['Commitment'], ['due_at'])['due_at']
    # Another tenant's object of the same type: never derived.
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_B,))
    other = hexid('ops-other-goal')
    obj(admin, 'Goal', other, {'consumer_id': hexid('someone'), 'state': 'active', 'valid_until': '2099-01-01T00:00:00Z'}, tenant=TENANT_B)
    assert read(w, w['owner'], 'Goal', other, []) is None
    # The Consumer being erased (NX-029): its Goal, PlanStep and Commitment are no longer derived; the catalog offering still is.
    mark_erasing(admin, TENANT, w['f']['consumer'])
    for type_name in ('Goal', 'PlanStep', 'Commitment'):
        assert read(w, w['owner'], type_name, ids[type_name], []) is None, type_name
    assert read(w, w['owner'], 'ServiceOffering', ids['ServiceOffering'], ['title'])['title'] == 'x'
