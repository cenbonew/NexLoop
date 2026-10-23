"""NX-058 design v7 §1c (Q-D = C, ADR-020 §3 amendment): the configurator publishes per role a template of the Human authority facts;
governed SQL instantiates it for one member by replacing the placeholders and recomputing every member-dependent digest. Golden tests:
for every role, the SQL instance equals compile_member byte for byte (canonical JSON of each payload, the keys, the kinds); the revoke
form equals revoke_member; a re-published template rewrites the members it was instantiated for (AA4); the publication is the
configurator's alone. Synthetic members; the admin connection calls the internal function as the governed transactions do."""
import json

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.workbench_roles import compile_member, revoke_member, role_templates
from test_workbench_read_pg import ROLES, TENANT, configure, workbench  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401


def write(admin, principal, subject, role, direction, source='action:golden'):
    with admin.transaction():
        return admin.execute('select control.nexloop_workbench_grants_write(%s,%s,%s,%s,%s,%s)', (TENANT, principal, subject, role, direction, source)).fetchone()[0]


@pytest.mark.parametrize('role', ['owner', 'operator', 'reviewer'])
def test_the_sql_instance_is_compile_member_byte_for_byte(workbench, admin, role):
    w = workbench
    configure(w, (w['owner'], 'owner'), (w['operator'], role if role != 'owner' else 'operator'))
    target = w['owner'] if role == 'owner' else w['operator']
    write(admin, target['principal_id'], target['subject_id'], role, 'grant')
    want = compile_member(ROLES, TENANT, {'principal_id': target['principal_id'], 'subject_id': target['subject_id'], 'role': role},
                          valid_from=__import__('datetime').datetime(2026, 10, 10, tzinfo=__import__('datetime').UTC))
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    for kind, key, fact in want:
        row = admin.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',
                            (TENANT, kind, list(key))).fetchone()
        assert row is not None, (kind, key)
        assert canonical_payload(row[0]) == canonical_payload(fact.model_dump(mode='json')), (kind, key)
    # The revoke form is revoke_member's empty grant sets.
    write(admin, target['principal_id'], target['subject_id'], role, 'revoke')
    for kind, key, fact in revoke_member(ROLES, TENANT, target['principal_id'], ROLES['roles'][role]):
        row = admin.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',
                            (TENANT, kind, list(key))).fetchone()
        assert canonical_payload(row[0]) == canonical_payload(fact.model_dump(mode='json')), (kind, key)
    records = admin.execute("select direction,role,template_version,source from control.nexloop_workbench_grant_instances where principal_id=%s order by instance_id",
                            (target['principal_id'],)).fetchall()
    assert records == [('grant', role, 1, 'action:golden'), ('revoke', role, 1, 'action:golden')]


def test_a_republished_template_rewrites_its_instances(workbench, admin):
    """AA4: a changed role (here the operator role loses an Action) is a new template version; the same configurator transaction
    rewrites every member holding the role through an instantiation - here to compile_member of the new roles."""
    w = workbench
    configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    write(admin, w['operator']['principal_id'], w['operator']['subject_id'], 'operator', 'grant')
    from nexloop_eios.workbench_roles import apply_members
    from test_workbench_read_pg import members_file
    removed = sorted(ROLES['roles']['operator'])[-1]
    narrower = {**ROLES, 'manifest_version': ROLES['manifest_version'] + 1, 'roles': {**ROLES['roles'], 'operator': [a for a in ROLES['roles']['operator'] if a != removed]}}
    # The member's grant of the removed Action is outside the new role: revoke first (as G-WB-REVOKE does), then configure.
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    admin.execute("""update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{grants}','[]') where tenant_id=%s and fact_kind='grants'
        and entity_key=%s""", (TENANT, [w['operator']['principal_id'], removed]))
    result = apply_members(narrower, members_file((w['owner'], 'owner'), (w['operator'], 'operator')), database_url_file=w['configurator'])
    assert {'role': 'operator', 'version': 2} in result['role_templates']
    record = admin.execute("""select template_version,source from control.nexloop_workbench_grant_instances where principal_id=%s order by instance_id desc limit 1""",
                           (w['operator']['principal_id'],)).fetchone()
    assert record == (2, 'template:2')
    want = compile_member(narrower, TENANT, {**w['operator'], 'role': 'operator'},
                          valid_from=__import__('datetime').datetime(2026, 10, 10, tzinfo=__import__('datetime').UTC))
    for kind, key, fact in want:
        row = admin.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',
                            (TENANT, kind, list(key))).fetchone()
        assert canonical_payload(row[0]) == canonical_payload(fact.model_dump(mode='json')), (kind, key)


def test_only_the_configurator_publishes_templates_and_nobody_calls_the_instantiation(workbench, admin, pg):
    w = workbench
    configure(w, (w['owner'], 'owner'))
    from psycopg.conninfo import make_conninfo
    for role in ('nexloop_api', 'nexloop_identity'):
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('select control.nexloop_publish_workbench_role_templates(%s,%s)', (TENANT, Jsonb(role_templates(ROLES, TENANT))))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select control.nexloop_workbench_grants_write(%s,'x','y','operator','grant','action:x')", (TENANT,))
    versions = admin.execute("select role,version from control.nexloop_workbench_role_templates order by 1").fetchall()
    assert versions == [('operator', 1), ('owner', 1), ('reviewer', 1)]  # the configuration published them once; a rerun adds none
    configure(w, (w['owner'], 'owner'))
    assert admin.execute("select count(*) from control.nexloop_workbench_role_templates").fetchone() == (3,)
