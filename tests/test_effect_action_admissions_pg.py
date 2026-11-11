"""Option B M1 (ADR-046 §3; docs/planning-drafts/b-effect-action-follows-schema-version-20261111.md §2.1-§3.2): the tenant-level
admission of an effect Action successor, the selection of the effect Action version for a Consumer version and the exact resource of
an effect context, on actual PostgreSQL. The publication is set up as F3 leaves it (test_effect_scope_successor_proof_pg.publish_v2);
every refusal case of the A' proof is a refusal of the admission, which writes nothing. A' itself stays (M4). Synthetic data."""
import json
import uuid

import psycopg
import pytest
from eios.ontology.definitions import ActionDefinition

from system_catalog_fixture import insert_active_actions

from test_context_artifacts import context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_context_artifacts import active_worker  # noqa: F401
from test_effect_scope_successor_proof_pg import (EFFECT, appended, changed_first, consumer_ref_version, extra_type, proofs, publish_v2,
                                                  reply)

SLOT = 'service.request:primary'
ACTOR = 'human:synthetic-configurator'


def admit(admin, tenant, publication, version=2, *, name=EFFECT, actor=ACTOR, dry_run=False):
    admin.execute('set session authorization nexloop_configurator')
    try:
        return admin.execute('select control.nexloop_admit_effect_action_successor(%s,%s,%s,%s,%s,%s)',
                             (tenant, publication, name, version, actor, dry_run)).fetchone()[0]
    finally:
        admin.execute('reset session authorization')


def retire(admin, tenant, version=2, reason='合成退役'):
    admin.execute('set session authorization nexloop_configurator')
    try:
        return admin.execute('select control.nexloop_retire_effect_action_admission(%s,%s,%s,%s,%s)', (tenant, EFFECT, version, ACTOR, reason)).fetchone()[0]
    finally:
        admin.execute('reset session authorization')


def selected(admin, tenant, consumer_version, world='real', slot=SLOT):
    return admin.execute('select authz.nexloop_effect_action_version(%s,%s,%s,%s)', (tenant, world, slot, consumer_version)).fetchone()[0]


def admissions(admin):
    return admin.execute("""select resource_id,consumer_version,predecessor_resource,publication_id::text,admitted_by,retired_at is not null
        from control.nexloop_effect_action_admissions order by admitted_at""").fetchall()


def test_the_published_successor_is_admitted_once_and_then_selected(context_message, admin):  # noqa: F811
    f = context_message
    tenant = f['original']['tenant']
    published = publish_v2(f, admin)
    assert selected(admin, tenant, 1) == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='effect Action version unavailable'):
        selected(admin, tenant, 2)  # published, not admitted: never chosen from the definitions alone
    plan = admit(admin, tenant, published['publication'], dry_run=True)
    assert plan['would_admit'] is True and plan['admitted'] is False and admissions(admin) == []
    done = admit(admin, tenant, published['publication'])
    assert (done['admitted'], done['changed'], done['resource_id'], done['predecessor']) == (True, True, f'eios:action:{EFFECT}:2', f'eios:action:{EFFECT}:1')
    assert admissions(admin) == [(f'eios:action:{EFFECT}:2', 2, f'eios:action:{EFFECT}:1', published['publication'], ACTOR, False)]
    judgement = admin.execute('select judgement from control.nexloop_effect_action_admissions').fetchone()[0]
    assert judgement == {'admissible': True, 'code': None}
    again = admit(admin, tenant, published['publication'])
    assert (again['admitted'], again['changed']) == (True, False) and len(admissions(admin)) == 1
    assert selected(admin, tenant, 2) == 2 and selected(admin, tenant, 1) == 1
    # Another tenant's or world's selection is not reached by this admission.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        selected(admin, tenant, 2, world='shadow')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        selected(admin, str(uuid.uuid4()), 2)
    assert proofs(admin) == []  # the admission records no A' proof


@pytest.mark.parametrize('case', [
    'no_publication_record', 'publisher_not_human', 'successor_policy_differs', 'successor_scope_extra_type',
    'added_property_required', 'added_property_hidden', 'added_property_default', 'existing_property_changed',
    'added_property_required_nullable', 'added_property_not_nullable', 'other_type_key_changed', 'successor_scope_wrong_version',
    'publication_other_type', 'publication_other_version', 'successor_inactive', 'successor_missing', 'successor_other_world',
    'successor_other_tenant', 'successor_target_systems_differ', 'successor_capability_differs', 'successor_receipt_differs',
])
def test_the_admission_refuses_when_one_condition_fails_and_writes_nothing(context_message, admin, case):  # noqa: F811
    f = context_message
    tenant = f['original']['tenant']
    kwargs = {
        'no_publication_record': dict(record=False),
        'publisher_not_human': dict(publisher='service:synthetic'),
        'successor_policy_differs': dict(edit_successor=lambda row: row['definition']['input_schema']['properties']['message'].update(maxLength=4000)),
        'successor_scope_extra_type': dict(edit_successor=lambda row: extra_type(row)),
        'added_property_required': dict(v2=lambda old: appended(old, required=True, nullable=False)),
        'added_property_hidden': dict(v2=lambda old: appended(old, visibility='hidden')),
        'added_property_default': dict(v2=lambda old: appended(old, default_value='morning')),
        'existing_property_changed': dict(v2=lambda old: changed_first(old)),
        'added_property_required_nullable': dict(v2=lambda old: appended(old, required=True)),
        'added_property_not_nullable': dict(v2=lambda old: appended(old, nullable=False)),
        'other_type_key_changed': dict(v2=lambda old: {**appended(old), 'description': (old.get('description') or '') + ' changed'}),
        'successor_scope_wrong_version': dict(edit_successor=lambda row: consumer_ref_version(row, 1)),
        'publication_other_type': dict(record_as=('Goal', 1, 2)),
        'publication_other_version': dict(record_as=('Consumer', 4, 5)),
        'successor_inactive': dict(effect='inactive'),
        'successor_missing': dict(effect='skip'),
        'successor_other_world': dict(effect=('world', 'shadow')),
        'successor_other_tenant': dict(effect=('tenant', str(uuid.uuid4()))),
        'successor_target_systems_differ': dict(edit_successor=lambda row: row['definition']['governance']['change_scope'].update(
            target_systems=row['definition']['governance']['change_scope']['target_systems'] + ['postgres'])),
        'successor_capability_differs': dict(edit_successor=lambda row: row.update(capability={**row['capability'], 'idempotent': not row['capability']['idempotent']})),
        'successor_receipt_differs': dict(edit_successor=lambda row: row['definition']['governance'].update(
            idempotency={**row['definition']['governance']['idempotency'], 'key_fields': list(row['definition']['governance']['idempotency']['key_fields']) + ['message']})),
    }[case]
    published = publish_v2(f, admin, **kwargs)
    outcome = admit(admin, tenant, published.get('publication', str(uuid.uuid4())))
    assert outcome['admitted'] is False and outcome['changed'] is False and outcome['code'], outcome
    assert admissions(admin) == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        selected(admin, tenant, 2)


def test_the_admission_uses_the_shared_judgement_only(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    body = admin.execute("select pg_get_functiondef('control.nexloop_admit_effect_action_successor'::regproc)").fetchone()[0]
    assert 'authz.nexloop_effect_successor_admissible(' in body
    for fragment in ('nexloop_effect_definition_unversioned', "'properties'", 'permission_digest_before is distinct', 'human:%'):
        assert fragment not in body, fragment  # no second copy of the A' judgement


def test_admission_and_retirement_are_trusted_configuration_only(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    tenant = str(uuid.uuid4())
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='trusted configuration only'):
        admin.execute('select control.nexloop_admit_effect_action_successor(%s,%s,%s,2,%s,false)', (tenant, str(uuid.uuid4()), EFFECT, ACTOR))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='trusted configuration only'):
        admin.execute("select control.nexloop_retire_effect_action_admission(%s,%s,2,%s,'x')", (tenant, EFFECT, ACTOR))
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        admit(admin, tenant, str(uuid.uuid4()), actor='service:synthetic')
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_scheduler', 'nexloop_action_worker', 'nexloop_runtime', 'nexloop_identity',
                 'nexloop_metrics', 'nexloop_configurator'):
        for privilege in ('insert', 'update', 'delete', 'select'):
            assert admin.execute('select has_table_privilege(%s,%s,%s)', (role, 'control.nexloop_effect_action_admissions', privilege)).fetchone()[0] is False, (role, privilege)
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_runtime'):
        for fn in ('control.nexloop_admit_effect_action_successor(text,uuid,text,integer,text,boolean)',
                   'control.nexloop_retire_effect_action_admission(text,text,integer,text,text)',
                   'authz.nexloop_effect_action_version(text,text,text,integer)'):
            assert admin.execute('select has_function_privilege(%s,%s,%s)', (role, fn, 'execute')).fetchone()[0] is False, (role, fn)
    rls = admin.execute("select relrowsecurity,relforcerowsecurity from pg_class where oid='control.nexloop_effect_action_admissions'::regclass").fetchone()
    assert rls == (True, True)


def test_admissions_are_append_only_and_a_retired_one_is_not_selected(context_message, admin):  # noqa: F811
    f = context_message
    tenant = f['original']['tenant']
    published = publish_v2(f, admin)
    admit(admin, tenant, published['publication'])
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        for statement in ("update control.nexloop_effect_action_admissions set consumer_version=3",
                          "update control.nexloop_effect_action_admissions set admitted_by='human:other'",
                          'delete from control.nexloop_effect_action_admissions'):
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with admin.transaction():
                    admin.execute(statement)
    assert retire(admin, tenant)['retired'] is True
    assert retire(admin, tenant)['retired'] is False  # once
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            with admin.transaction():
                admin.execute("update control.nexloop_effect_action_admissions set retired_at=null,retired_by=null,retired_reason=null")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        selected(admin, tenant, 2)  # retired: back to the allow-list :1, which does not hold Consumer:2 (A' proves it until M4)
    readmitted = admit(admin, tenant, published['publication'])
    assert (readmitted['admitted'], readmitted['changed']) == (True, True)
    assert [r[5] for r in admissions(admin)] == [True, False] and selected(admin, tenant, 2) == 2


def test_another_version_for_an_admitted_consumer_version_is_refused(context_message, admin):  # noqa: F811
    f = context_message
    tenant = f['original']['tenant']
    published = publish_v2(f, admin)
    admit(admin, tenant, published['publication'])
    other = admit(admin, tenant, published['publication'], version=3)
    assert other['admitted'] is False and other['code'] in ('successor_not_in_publication', 'consumer_version_admitted'), other
    assert len(admissions(admin)) == 1


def test_two_candidates_for_one_consumer_version_are_refused_never_the_highest(context_message, admin):  # noqa: F811
    f = context_message
    tenant = f['original']['tenant']
    published = publish_v2(f, admin)
    admit(admin, tenant, published['publication'])
    with admin.transaction():  # a data error the admission rules exclude: a :3 whose scope also holds Consumer:2, admitted for v3
        admin.execute('set local session_replication_role=replica')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        definition, capability = admin.execute('select definition,capability from control.nexloop_action_definitions where tenant_id=%s and world=%s and resource_id=%s',
                                               (tenant, 'real', f'eios:action:{EFFECT}:2')).fetchone()
        body = {**definition, 'version': 3}
        body.pop('contract_digest', None)
        insert_active_actions(admin, tenant, [{'definition': ActionDefinition.model_validate_json(json.dumps(body)).model_dump(mode='json'), 'capability': capability}])
        admin.execute("""insert into control.nexloop_effect_action_admissions(tenant_id,world,stable_name,version,consumer_version,predecessor_resource,publication_id,admitted_by,judgement)
            values(%s,'real',%s,3,3,%s,%s,%s,'{"admissible":true}')""", (tenant, EFFECT, f'eios:action:{EFFECT}:2', published['publication'], ACTOR))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='effect Action version unavailable'):
        selected(admin, tenant, 2)


def test_each_context_records_its_exact_effect_action_resource(context_message, admin, tmp_path):  # noqa: F811
    f = context_message
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict), receipt
    rows = admin.execute('select context_id,action_resource from control.nexloop_effect_contexts').fetchall()
    assert rows and {r[1] for r in rows} == {f'eios:action:{EFFECT}:1'}
    tenant = f['original']['tenant']
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='immutable'):
            with admin.transaction():
                admin.execute('update control.nexloop_effect_contexts set action_resource=%s where context_id=%s', (f'eios:action:{EFFECT}:2', rows[0][0]))
    assert admin.execute('select count(*) from control.nexloop_effect_contexts where action_resource is null').fetchone()[0] == 0


def test_a_prime_stays_through_m1(context_message, admin, tmp_path):  # noqa: F811
    """M1 leaves A' in place (removed in M4): a Consumer at v2 whose successor is admitted is still answered at :1 through the A'
    proof until the bind selects the version (M2)."""
    f = context_message
    published = publish_v2(f, admin)
    admit(admin, f['original']['tenant'], published['publication'])
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict), receipt
    assert proofs(admin) == [(f'eios:action:{EFFECT}:1', f'eios:action:{EFFECT}:2', published['publication'], 2)]
    assert json.loads(json.dumps(admin.execute('select action_resource from control.nexloop_effect_contexts').fetchall())) == [[f'eios:action:{EFFECT}:1']]


def test_a_successor_the_publication_does_not_name_is_refused(context_message, admin):  # noqa: F811
    f = context_message
    tenant = f['original']['tenant']
    published = publish_v2(f, admin)
    with admin.transaction():  # the same :2 exists and is equivalent, but this publication record does not name it
        admin.execute('set local session_replication_role=replica')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute('update ontology.nexloop_schema_publications set successor_actions=array_remove(successor_actions,%s) where publication_id=%s',
                      (f'eios:action:{EFFECT}:2', published['publication']))
    outcome = admit(admin, tenant, published['publication'])
    assert (outcome['admitted'], outcome['code']) == (False, 'successor_not_in_publication') and admissions(admin) == []
