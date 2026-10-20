"""NX-040 F3 (design §4, §5, §6; D22, divergence 1 decided 2026-10-20) on PostgreSQL: the Schema publication transaction.

A human reviewer (browser session with ontology.schema.review) publishes a frozen proposal; the backend judges it with F1 and builds
the next version and successor Actions; SQL re-derives the version, re-checks the successors and proves that no permission changed.
Whitelist positives, each refusal class, the permission proof, the three AT-024 negatives (a new type with an escalating Action, a
changed existing Action, a new type or property re-exposing an owner-restricted property) and the rollback as the next Schema
version. Proposals are seeded by admin (F2's freeze is tested in tests/test_nx040_schema_proposals_pg.py)."""
import copy
import hashlib
import json
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.schema_publication import PROTOCOL, SchemaPublicationPort, build
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_browser_session_reads import authenticate, create
from test_claim_matching_pg import env  # noqa: F401
from test_evolution_e4_pg import evo  # noqa: F401
import psycopg.conninfo
from test_review_decisions_pg import review  # noqa: F401
from test_review_http import REVIEW, grant_human
from test_review_decisions_pg import TENANT

pytestmark = pytest.mark.parametrize('env', [TENANT], indirect=True)


def port(f):
    grant_human(f['admin'], f['uow'], f['identity'], REVIEW)
    issued = create(f['uow'], f['identity'], authenticate(f['uow'], f['identity']).evidence)
    return SchemaPublicationPort(f['api'], authenticate_browser_business(f['api'], issued.session, world='real'), f['signer'])


def proposal(f, operation, type_name, change, *, parent_version=None):
    admin, tenant = f['admin'], f['tenant']
    if parent_version is None:
        parent_version = 0 if operation == 'add_object_type' else admin.execute(
            'select max(version) from ontology.object_type_versions where tenant_id=%s and type_name=%s', (tenant, type_name)).fetchone()[0]
    pid = str(uuid.uuid4())
    target = 'eios:object_type:' + type_name if operation == 'add_object_type' else f"eios:property:{type_name}.{change['property_name']}"
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # the evolution candidate behind it is F2's concern
        admin.execute("""insert into ontology.nexloop_schema_proposals(tenant_id,world,proposal_id,candidate_id,operation,type_name,target_ref,change,change_digest,
            parent_schema_version,parent_schema_digest,input_snapshot_digest,compatibility_report_digest,permission_proof_digest,frozen_by)
            values(%s,'real',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'synthetic')""",
            (tenant, pid, str(uuid.uuid4()), operation, type_name, target, Jsonb(change), 'a' * 64, parent_version, 'b' * 64, 'c' * 64, 'd' * 64, 'e' * 64))
    return pid


def counts(f):
    a, t = f['admin'], f['tenant']
    return (a.execute('select count(*) from ontology.object_type_versions where tenant_id=%s', (t,)).fetchone()[0],
            a.execute('select count(*) from control.nexloop_action_definitions where tenant_id=%s', (t,)).fetchone()[0],
            a.execute('select count(*) from ontology.nexloop_schema_publications').fetchone()[0])


def forged(p, pid, publication, report, kind='publish', **extra):
    """A backend that skips F1 or builds something else: SQL decides on its own."""
    payload = {'publication_id': str(uuid.uuid4()), 'proposal_id': pid, 'kind': kind, 'rationale': 'synthetic', 'publication': publication,
               'compatibility_report': report, **extra}
    return p._call('nexloop_schema_proposal_publish', payload, PROTOCOL)


ALLOW = lambda op: {'op': op, 'allowed': True, 'change_class': 'minor', 'reason': None, 'codes': []}  # noqa: E731
KEY = 'synthetic-publication-0001'


def test_an_optional_property_is_published_with_its_successor_actions_and_no_permission_change(review):  # noqa: F811
    f = review
    admin, tenant = f['admin'], f['tenant']
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    p = port(f)
    migrations = admin.execute('select count(*) from public.schema_migrations').fetchone()[0] if admin.execute("select to_regclass('public.schema_migrations')").fetchone()[0] else None
    fingerprint = admin.execute('select authz.nexloop_tenant_authority_fingerprint(%s)', (tenant,)).fetchone()[0]
    perm = admin.execute("select authz.nexloop_schema_permission_digest(%s,'{}')", (tenant,)).fetchone()[0]
    out = p.publish(pid, rationale='顾客常提到门店', idempotency_key=KEY)
    # §5: the proof - recorded before and after in the transaction, and re-computable right after (the successors aside).
    recomputed = admin.execute('select authz.nexloop_schema_permission_digest(%s,%s)', (tenant, out['successor_actions'])).fetchone()[0]
    assert out['published'] is True and out['version_after'] == 2 and out['successor_actions'] == ['eios:action:Consumer.edit:2']
    assert out['objects_migrated'] >= 1 and out['permission_digest'] == recomputed
    assert port(f).publish(pid, rationale='顾客常提到门店', idempotency_key=KEY)['replayed'] is True  # a fresh session: the Schema moved
    schema = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=2", (tenant,)).fetchone()[0]
    assert schema['properties'][-1]['property_name'] == 'preferred_store' and schema['properties'][-1]['required'] is False
    assert admin.execute('select authz.nexloop_tenant_authority_fingerprint(%s)', (tenant,)).fetchone()[0] == fingerprint
    row = admin.execute('select permission_digest_before,permission_digest_after,kind,published_by from ontology.nexloop_schema_publications').fetchone()
    assert row[0] == row[1] == recomputed == perm and row[2] == 'publish' and row[3].startswith('human:')
    assert admin.execute('select status,permit_ref from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'published'
    assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Consumer' and schema_version=1", (tenant,)).fetchone()[0] == 0
    if migrations is not None:  # no DDL: the catalog lineage is unchanged
        assert admin.execute('select count(*) from public.schema_migrations').fetchone()[0] == migrations
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute('delete from ontology.nexloop_schema_publications')


def test_a_vocabulary_value_is_published(review):  # noqa: F811
    f = review
    pid = proposal(f, 'add_vocabulary_value', 'Consumer', {'property_name': 'budget_level', 'value': '一万元以上'})
    out = port(f).publish(pid, rationale='新的档位', idempotency_key=KEY)
    assert out['published'] is True
    schema = f['admin'].execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=2", (f['tenant'],)).fetchone()[0]
    assert next(p for p in schema['properties'] if p['property_name'] == 'budget_level')['type_descriptor']['enum'][-1] == '一万元以上'


def test_a_new_type_without_key_or_actions_is_published_by_hand(review):  # noqa: F811
    f = review
    pid = proposal(f, 'add_object_type', 'Warranty', {'properties': [{'property_name': 'months', 'value_type': 'integer'}]})
    out = port(f).publish(pid, rationale='保修期', idempotency_key=KEY)
    assert out['published'] is True and out['successor_actions'] == [] and out['version_after'] == 1


@pytest.mark.parametrize('label,operation,type_name,change,code', [
    ('json value type', 'add_optional_property', 'Consumer', {'property_name': 'raw_blob', 'value_type': 'json'}, 'value_type_not_allowed'),
    ('existing property', 'add_optional_property', 'Consumer', {'property_name': 'favorite_sport', 'value_type': 'string'}, 'property_exists'),
    ('existing value', 'add_vocabulary_value', 'Consumer', {'property_name': 'budget_level', 'value': '两千元以内'}, 'vocabulary_value_exists'),
    ('open vocabulary', 'add_vocabulary_value', 'Consumer', {'property_name': 'favorite_sport', 'value': '网球'}, 'not_closed_vocabulary'),
    # Seeded past F2 (which now refuses a key): SQL still closes it on its own.
    ('new type with a key', 'add_object_type', 'Coupon', {'properties': [{'property_name': 'code', 'value_type': 'string', 'required': True}], 'primary_key': ['code']},
     'breaking_primary_key'),
])
def test_each_refusal_writes_nothing(review, label, operation, type_name, change, code):  # noqa: F811
    f = review
    pid = proposal(f, operation, type_name, change)
    before = counts(f)
    out = port(f).publish(pid, rationale='synthetic', idempotency_key=KEY)
    assert out['published'] is False and code in out['codes'], (label, out)
    assert counts(f) == before
    assert f['admin'].execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'frozen'


def test_a_moved_parent_withdraws_the_proposal(review):  # noqa: F811
    f = review
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    other = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'shoe_size', 'value_type': 'integer'})
    p = port(f)
    assert p.publish(other, rationale='first', idempotency_key=KEY)['published'] is True
    out = port(f).publish(pid, rationale='second', idempotency_key='synthetic-publication-0002')  # a published Schema makes earlier sessions stale
    assert (out['published'], out['codes']) == (False, ['parent_moved'])
    assert f['admin'].execute('select status,withdraw_reason from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone() == ('withdrawn', 'parent_moved')


def test_at024_a_new_type_never_publishes_an_action(review):  # noqa: F811
    f = review
    pid = proposal(f, 'add_object_type', 'Voucher', {'properties': [{'property_name': 'amount', 'value_type': 'number'}]})
    p = port(f)
    basis = p.basis(pid)
    publication, _ = build(basis, 'publish')
    escalating = json.loads(json.dumps(f['admin'].execute("select definition,capability from control.nexloop_action_definitions where tenant_id=%s and resource_id='eios:action:Consumer.edit:1'",
                                                          (f['tenant'],)).fetchone()))
    action = {'definition': {**escalating[0], 'stable_name': 'Voucher.grant', 'version': 1}, 'capability': escalating[1]}
    before = counts(f)
    out = forged(p, pid, {**publication, 'actions': [action]}, ALLOW('add_object_type'))
    assert out['published'] is False and 'authority_content' in out['codes'] and counts(f) == before


def test_at024_an_existing_action_cannot_change_its_permission(review):  # noqa: F811
    f = review
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    p = port(f)
    publication, _ = build(p.basis(pid), 'publish')
    before = counts(f)
    widened = copy.deepcopy(publication)
    widened['actions'][0]['capability'] = {**widened['actions'][0]['capability'], 'granted_scopes': ['ontology.schema.review']}
    out = forged(p, pid, widened, ALLOW('add_optional_property'))
    assert out['published'] is False and 'tool_signature_changed' in out['codes']
    scoped = copy.deepcopy(publication)
    scoped['actions'][0]['definition']['required_scopes'] = sorted(set(scoped['actions'][0]['definition'].get('required_scopes') or []) | {'ontology.schema.review'})
    assert 'tool_signature_changed' in forged(p, pid, scoped, ALLOW('add_optional_property'))['codes']
    extra = copy.deepcopy(publication)
    extra['actions'].append({**copy.deepcopy(extra['actions'][0]), 'definition': {**extra['actions'][0]['definition'], 'stable_name': 'Consumer.export'}})
    assert {'authority_content', 'successor_not_one_to_one'} <= set(forged(p, pid, extra, ALLOW('add_optional_property'))['codes'])
    missing = {**publication, 'actions': []}
    assert 'successor_not_one_to_one' in forged(p, pid, missing, ALLOW('add_optional_property'))['codes']
    assert counts(f) == before


def test_at024_a_new_type_or_property_cannot_reexpose_a_restricted_property(review):  # noqa: F811
    f = review
    admin, tenant = f['admin'], f['tenant']
    admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
                  (tenant, 'property_group_restriction', ['Consumer'], Jsonb({'tenant_id': tenant, 'type_name': 'Consumer', 'restricted_groups': ['spending_power'],
                                                                             'decision': 'synthetic owner restriction'})))
    p = port(f)
    wallet = proposal(f, 'add_object_type', 'Wallet', {'properties': [{'property_name': 'budget_amount', 'value_type': 'number'}]})
    before = counts(f)
    out = p.publish(wallet, rationale='synthetic', idempotency_key=KEY)  # F1 refuses in the backend
    assert out['published'] is False and 'property_security_bypass' in out['codes']
    publication, _ = build(p.basis(wallet), 'publish')
    assert 'property_security_bypass' in forged(p, wallet, publication, ALLOW('add_object_type'))['codes']  # and SQL on its own
    sneak = proposal(f, 'add_optional_property', 'Product', {'property_name': 'budget_level', 'value_type': 'string'})
    publication, _ = build(p.basis(sneak), 'publish')
    assert 'property_security_bypass' in forged(p, sneak, publication, ALLOW('add_optional_property'))['codes']
    assert counts(f) == before


def test_the_rollback_is_the_next_schema_version_with_the_property_hidden(review):  # noqa: F811
    f = review
    admin, tenant = f['admin'], f['tenant']
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    p = port(f)
    assert p.publish(pid, rationale='first', idempotency_key=KEY)['version_after'] == 2
    p = port(f)
    out = p.hide(pid, rationale='抽取错误率上升', idempotency_key='synthetic-publication-hide-01')
    assert out['published'] is True and out['version_after'] == 3 and out['successor_actions'] == ['eios:action:Consumer.edit:3']
    schema = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=3", (tenant,)).fetchone()[0]
    assert next(x for x in schema['properties'] if x['property_name'] == 'preferred_store')['visibility'] == 'hidden'
    assert [r[0] for r in admin.execute('select kind from ontology.nexloop_schema_publications order by published_at')] == ['publish', 'hide']
    # Hiding a property no publication added is refused.
    other = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'shoe_size', 'value_type': 'integer'})
    p = port(f)
    publication, _ = build(p.basis(other), 'hide', 'favorite_sport')
    with pytest.raises(psycopg.Error, match='cannot be hide'):
        forged(p, other, publication, {'op': 'hide_property', 'allowed': True, 'codes': []}, kind='hide', property_name='favorite_sport')


def test_publication_is_a_person_s_never_a_service(review):  # noqa: F811
    from multi_authority_fixture import seed_multi_authority
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    f = review
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    service, _ = seed_multi_authority(f['admin'], f['api'], [(REVIEW, ResourceType.ACTION, Operation.EXECUTE)],
                                      identity_suffix='-schema-service', tenant=f['tenant'])
    with pytest.raises(PermissionError):
        SchemaPublicationPort(f['api'], service, f['signer'])
    fake = object.__new__(SchemaPublicationPort)
    fake.pool, fake.session, fake.signer, fake.action = f['api'], service, f['signer'], 'ontology.schema.review'
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='human session'):
        forged(fake, pid, {'schema': {}, 'actions': []}, ALLOW('add_optional_property'))
    assert f['admin'].execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'frozen'


def test_end_to_end_f2_freezes_f1_judges_and_f3_publishes(review, evo):  # noqa: F811
    """The gap of the seeded proposals: a new type and an optional property, each frozen by F2 from an ontology_schema candidate
    (evolution writer), judged by F1 before freezing (its report digest is what F2 freezes) and at publication, then published by F3."""
    import hashlib as _h
    from nexloop_eios.evolution.schema_compat import judge
    from test_evolution_e4_pg import body_of, opened, propose_raw
    f, e = review, evo
    assert e['admin'] is f['admin'] and f['tenant'] == TENANT
    opened(e)

    def candidate(scope, kind, target):
        cid = str(uuid.uuid4())
        out = propose_raw(e, body_of(e, candidate={'candidate_id': cid, 'dimension': 'ontology_schema', 'allowed_change_scope': scope},
                                     patch={'change_kind': kind, 'target_refs': [target]}))
        assert out['proposed'] is True
        return cid

    consumer = f['admin'].execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' order by version desc limit 1",
                                  (TENANT,)).fetchone()[0]
    parent = {'types': {'Consumer': consumer}, 'actions': [], 'object_counts': {'Consumer': 1}}
    cases = [
        ('add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'},
         {'op': 'add_optional_property', 'type_name': 'Consumer', 'property': {'property_name': 'preferred_store', 'value_type': 'string', 'nullable': True}},
         ['eios:object_type:Consumer', 'eios:property:Consumer.preferred_store']),
        ('add_object_type', 'Warranty', {'properties': [{'property_name': 'months', 'value_type': 'integer'}]},
         {'op': 'add_object_type', 'type_name': 'Warranty', 'definition': {'type_name': 'Warranty', 'version': 1, 'only_edit_via_actions': True,
                                                                         'properties': [{'property_name': 'months', 'value_type': 'integer', 'nullable': True}]}},
         ['eios:object_type:Warranty'])]
    for operation, type_name, change, f1_change, scope in cases:
        verdict = judge(f1_change, parent if operation != 'add_object_type' else {'types': {}})
        assert verdict['allowed'] is True and verdict['change_class'] == 'minor', verdict
        report_digest = _h.sha256(json.dumps({k: verdict[k] for k in ('op', 'allowed', 'change_class', 'reason', 'codes')}, sort_keys=True).encode()).hexdigest()
        cid = candidate(scope, operation, scope[0])
        body = {'proposal_id': str(uuid.uuid4()), 'candidate_id': cid, 'operation': operation, 'type_name': type_name, 'change': change,
                'claim_ids': [], 'candidate_definition_ids': [], 'compatibility_report_digest': report_digest, 'permission_proof_digest': 'b' * 64}
        with psycopg.connect(psycopg.conninfo.make_conninfo(e['pg'], user='nexloop_evo_writer'), autocommit=True) as db:
            frozen = db.execute('select authz.nexloop_schema_proposal_freeze(%s,%s)', (e['digest'], json.dumps(body))).fetchone()[0]
        assert frozen['frozen'] is True and frozen['proposal']['status'] == 'frozen'
        pid = body['proposal_id']
        out = port(f).publish(pid, rationale='端到端：F2 冻结、F1 判定、F3 发布', idempotency_key='synthetic-e2e-' + operation.replace('_', '-'))
        assert out['published'] is True, out
        assert f['admin'].execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'published'
    versions = dict(f['admin'].execute("select type_name,max(version) from ontology.object_type_versions where tenant_id=%s and type_name in ('Consumer','Warranty') group by 1",
                                       (TENANT,)).fetchall())
    assert versions == {'Consumer': 2, 'Warranty': 1}
    assert f['admin'].execute('select count(*),bool_and(permission_digest_before=permission_digest_after) from ontology.nexloop_schema_publications').fetchone() == (2, True)
