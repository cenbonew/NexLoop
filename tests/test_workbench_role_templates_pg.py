"""NX-058 design v7 §1c (Q-D = C, ADR-020 §3 amendment): the configurator publishes per role a template of the Human authority facts;
governed SQL instantiates it for one member by replacing the placeholders and recomputing every member-dependent digest. Golden tests:
for every role, the SQL instance equals compile_member byte for byte (canonical JSON of each payload, the keys, the kinds); the revoke
form equals revoke_member; a re-published template rewrites the members it was instantiated for (AA4); the publication is the
configurator's alone. Synthetic members; the admin connection calls the internal function as the governed transactions do."""
import json
from datetime import UTC, datetime

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


def facts_of(admin, principal):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    return {(k, tuple(key)): canonical_payload(p) for k, key, p in admin.execute(
        'select fact_kind,entity_key,payload from authz.nexloop_authority_facts where tenant_id=%s and %s=any(entity_key)', (TENANT, principal)).fetchall()}


def matches_compile(admin, roles, member, role):
    want = compile_member(roles, TENANT, {**member, 'role': role}, valid_from=datetime(2026, 10, 10, tzinfo=UTC))
    for kind, key, fact in want:
        row = admin.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',
                            (TENANT, kind, list(key))).fetchone()
        assert row and canonical_payload(row[0]) == canonical_payload(fact.model_dump(mode='json')), (kind, key)


@pytest.mark.parametrize('direction', ['narrower', 'wider'])
def test_a_republished_template_rewrites_its_instances_through_the_configurator_path(workbench, admin, direction):
    """AA4 and L6 slice A review A-3: a changed role is a new template version; the configurator's own apply (no table written by the
    test) first revokes the previous version from every member holding the role through an instantiation, then configures, then grants
    the new version - so a narrower role passes the grant check and a wider one is granted after it."""
    from nexloop_eios.workbench_roles import apply_members
    from test_workbench_read_pg import members_file
    w = workbench
    configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    write(admin, w['operator']['principal_id'], w['operator']['subject_id'], 'operator', 'grant')
    operator = sorted(ROLES['roles']['operator'])
    extra = next(a for a in sorted(ROLES['roles']['reviewer']) if a not in operator)  # a reviewer Action: never owner-only
    changed = operator[:-1] if direction == 'narrower' else operator + [extra]
    roles = {**ROLES, 'manifest_version': ROLES['manifest_version'] + 1, 'roles': {**ROLES['roles'], 'operator': changed}}
    result = apply_members(roles, members_file((w['owner'], 'owner'), (w['operator'], 'operator')), database_url_file=w['configurator'])
    assert {'role': 'operator', 'version': 2} in result['role_templates'] and result['instances_rewritten'] == 1
    sources = admin.execute("select direction,template_version,source from control.nexloop_workbench_grant_instances where principal_id=%s order by instance_id",
                            (w['operator']['principal_id'],)).fetchall()
    assert sources == [('grant', 1, 'action:golden'), ('revoke', 1, 'template-pending:2'), ('grant', 2, 'template:2')]
    matches_compile(admin, roles, w['operator'], 'operator')
    if direction == 'narrower':  # the removed Action is an empty grant set now, exactly revoke_member's
        removed = operator[-1]
        row = admin.execute("select payload->'grants' from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key=%s",
                            (TENANT, [w['operator']['principal_id'], removed])).fetchone()
        assert row == ([],)



def test_the_publication_is_recorded_replayed_and_checked_against_the_expected_revision(workbench, admin, pg):
    """L6 slice A review D-1: as 0050 and the NX-056 policy publication - the text's sha256, the roles manifest version and the expected
    revision are recorded; the same publication again changes nothing; a stale expected revision or another text under a recorded
    version is refused."""
    import hashlib
    from psycopg.conninfo import make_conninfo
    w = workbench
    configure(w, (w['owner'], 'owner'))
    configure(w, (w['owner'], 'owner'))  # a rerun: the recorded publication is replayed
    rows = admin.execute('select roles_manifest_version,manifest_sha256,expected_revision from control.nexloop_workbench_template_publications').fetchall()
    text = canonical_payload(role_templates(ROLES, TENANT))
    assert rows == [(ROLES['manifest_version'], hashlib.sha256(text.encode()).hexdigest(), 0)]
    with psycopg.connect(make_conninfo(pg, user='nexloop_configurator'), autocommit=True) as c:
        with pytest.raises(psycopg.errors.SerializationFailure, match='expected_revision_mismatch'):
            other = {**ROLES, 'manifest_version': ROLES['manifest_version'] + 1}
            c.execute('select control.nexloop_publish_workbench_role_templates(%s,%s,%s)', (TENANT, canonical_payload(role_templates(other, TENANT)), 0))
        forged = json.loads(text)
        forged['operator']['grant_facts'] = forged['operator']['grant_facts'][:-1]
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='manifest_conflict'):
            c.execute('select control.nexloop_publish_workbench_role_templates(%s,%s,%s)', (TENANT, canonical_payload(forged), ROLES['manifest_version'] - 1))


@pytest.mark.parametrize('bad', ['x","grants":[{"evil":1}],"y":"', 'a"b', 'a\\b', 'a b', '', 'nexloop-template-principal', '{"a":1}', 'a' * 321])
def test_a_member_identifier_is_a_value_never_text(workbench, admin, bad):
    """L6 slice A review A-1: the placeholders are replaced as JSON values, and an identifier with a quote, a backslash, a space or JSON
    is refused before anything is written - a stolen nexloop_identity cannot shape grants beyond the template."""
    w = workbench
    configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    before = admin.execute('select count(*) from authz.nexloop_authority_facts').fetchone()[0]
    for principal, subject in ((bad, 'synthetic-subject'), ('synthetic-principal', bad)):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='workbench member: invalid'):
            write(admin, principal, subject, 'operator', 'grant')
    assert admin.execute('select count(*) from authz.nexloop_authority_facts').fetchone()[0] == before


def test_an_instance_differs_from_its_template_only_in_the_member_values(workbench, admin):
    w = workbench
    configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    member = w['operator']
    write(admin, member['principal_id'], member['subject_id'], 'operator', 'grant')
    template = admin.execute("select grant_facts from control.nexloop_workbench_role_templates where role='operator'").fetchone()[0]
    digest_keys = {'digest', 'snapshot_digest'}

    def walk(t, v, path=()):
        if isinstance(t, dict):
            assert set(t) == set(v), path
            for k in t:
                if k not in digest_keys:
                    walk(t[k], v[k], path + (k,))
        elif isinstance(t, list):
            assert len(t) == len(v), path
            for i, (a, b) in enumerate(zip(t, v)):
                walk(a, b, path + (i,))
        elif t != v:
            assert (t, v) in {('nexloop-template-principal', member['principal_id']), ('nexloop-template-subject', member['subject_id'])}, (path, t, v)
    for f in template:
        row = admin.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',
                            (TENANT, f['kind'], [member['principal_id'] if k == 'nexloop-template-principal' else k for k in f['key']])).fetchone()
        walk(f['payload'], row[0])


def test_a_configurator_rerun_naming_the_member_rewrites_nothing(workbench, admin):
    """L6 slice A review A-5 (the point of option C): the instantiated facts equal compile_member's, so a configurator run that names
    the member writes the same bytes - no instance record, no change of the tenant's authority revision from the member's facts."""
    from nexloop_eios.workbench_roles import apply_members, compile_members
    from test_workbench_read_pg import members_file
    w = workbench
    configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    write(admin, w['operator']['principal_id'], w['operator']['subject_id'], 'operator', 'grant')
    before_facts = facts_of(admin, w['operator']['principal_id'])
    revision = admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT,)).fetchone()[0]
    instances = admin.execute('select count(*) from control.nexloop_workbench_grant_instances').fetchone()[0]
    apply_members(ROLES, members_file((w['owner'], 'owner'), (w['operator'], 'operator')), database_url_file=w['configurator'])
    # 0050 writes a fact only when its payload differs: the configurator's compile of this member is byte-identical to the instance.
    for item in compile_members(ROLES, members_file((w['operator'], 'operator'))):
        if item['kind'] == 'application':
            continue
        row = admin.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',
                            (TENANT, item['kind'], item['key'])).fetchone()
        assert row and canonical_payload(row[0]) == canonical_payload(item['payload']), item['key']
    assert facts_of(admin, w['operator']['principal_id']) == before_facts
    assert admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT,)).fetchone()[0] == revision
    assert admin.execute('select count(*) from control.nexloop_workbench_grant_instances').fetchone()[0] == instances
def test_only_the_configurator_publishes_templates_and_nobody_calls_the_instantiation(workbench, admin, pg):
    w = workbench
    configure(w, (w['owner'], 'owner'))
    from psycopg.conninfo import make_conninfo
    for role in ('nexloop_api', 'nexloop_identity'):
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('select control.nexloop_publish_workbench_role_templates(%s,%s,%s)', (TENANT, canonical_payload(role_templates(ROLES, TENANT)), 0))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select control.nexloop_workbench_grants_write(%s,'x','y','operator','grant','action:x',null)", (TENANT,))
    versions = admin.execute("select role,version from control.nexloop_workbench_role_templates order by 1").fetchall()
    assert versions == [('operator', 1), ('owner', 1), ('reviewer', 1)]  # the configuration published them once; a rerun adds none
    configure(w, (w['owner'], 'owner'))
    assert admin.execute("select count(*) from control.nexloop_workbench_role_templates").fetchone() == (3,)
