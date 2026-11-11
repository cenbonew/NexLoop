"""NX-067 Z5: every SQL function that reads or writes grant facts is registered here with why the workbench role ceiling does or does
not apply to it. A new function reading grants fails this test until it is classified; the three enforcement points must call
control.nexloop_workbench_action_permitted. The ceiling narrows workbench Action grants of browser members only."""

ENFORCEMENT = {
    # Where the ceiling is applied (each calls control.nexloop_workbench_action_permitted).
    'authz.nexloop_workbench_action_for(text,text,text)': 'the workbench entry picks the Action: granted false',
    'authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[])': 'every browser authorization reads a grant here: withheld',
    'authz.nexloop_goal_governed_action(text,text,text,text,text)': 'the governed dispatch transaction: 42501',
}
WRAPPED = 'wrapped by an enforcement point of the same name (nx067_oidc_gaps) and callable only through it'
SERVICE = 'service, Agent or Run credentials only; a browser member is authorized through the browser snapshot'
OBJECT_BASIS = ('object and property read grants (eios:object / eios:property), never a workbench Action grant; a member reaches them only '
                'after the workbench read Action, whose grant the browser snapshot withholds under an empty ceiling')
# NX-068 (0449): both read the grant facts of SERVICE holders only (subject_kind 'service') as data about a Schema publication - whether
# every holder of a registrar or create Action carries register's follow marker (gate) or holds the covering version (smoke round 2).
# They authorize nobody: no subject's own grant is consulted and their result can only add refusals or smoke problems. The gate is
# reached by the owner through the workbench (F3 publish transaction via the gates wrapper, and the readiness precheck route), but the
# owner is authorized there by the review read guard (authz.nexloop_assert_review_read), not by a grant read here; the smoke checks are
# reached only by the alert evaluator's maintenance. A browser member's workbench Action grant - the thing the ceiling narrows - is never
# read, so the ceiling has nothing to apply to; test_publication_holder_inspection_reads_service_grants_only pins that.
HOLDER_INSPECTION = ('reads service holders\' grant facts (subject_kind service) as publication data, authorizes no subject; reachable '
                     'only inside another function (no direct EXECUTE)')
CONFIGURATION = 'writes or checks the configured grants (trusted configuration), decides no request'
REGISTRY = {
    'authz.nexloop_browser_authority_fact_snapshot_before_role_ceiling(text,text,text,text[])': WRAPPED,
    'authz.nexloop_workbench_action_for_before_role_ceiling(text,text,text)': WRAPPED,
    'authz.nexloop_workbench_action_for_single_version(text,text,text)': WRAPPED + ' (the wrapped body calls it for one-version Actions)',
    'authz.nexloop_workbench_action_for_before_notes(text,text,text)': WRAPPED + ' (NX-062 M5c, 0443: the notes wrapper sits between the ceiling and this body)',
    'authz.nexloop_machine_authority_fact_snapshot(text,text,text,text[])': 'machine clients only (NX-062): a machine subject is no person and never has an SSO link',
    'authz.nexloop_load_authority_fact(text,text,text,text[])': SERVICE,
    'authz.nexloop_load_authority_fact_with_identity(jsonb,text,text,text,text[])': SERVICE,
    'authz.nexloop_claim_match_settled(text,text,text,bigint)': SERVICE + ' (claim matcher)',
    # ADR-046 (0446): both choose a version only and decide nothing - the registrar command (0179) and the governed create re-authorize
    # the exact resource. Real callers are service credentials: effect_contexts (relay_planner, role_planner, business_owner configure)
    # and create_versions.create_current (business_setup, enterprise_setup, the relay and fallback planners, role_assembly); no
    # workbench route reaches either, and a browser member's grant is only ever read through the browser snapshot.
    'authz.nexloop_effect_registrar_version(text,text,text,jsonb)': SERVICE + ' (advisory registrar version; 0179 re-checks the exact resource)',
    'authz.nexloop_create_action_version(text,text,text)': SERVICE + ' (advisory create version; the governed create re-checks the exact resource)',
    'authz.nexloop_evo_l1_open(text,uuid,text)': SERVICE + ' (evolution L1 test world)',
    'control.nexloop_revoke_service_credential(text,jsonb,text,bytea)': SERVICE + ' (service credential rotation)',
    'control.nexloop_consumer_webchat_grants(text,text,text,text)': 'WebChat customer grants; a customer principal is never a workbench member',
    'authz.nexloop_assert_derived_message_read_actor_body_v0080(text,text,jsonb)': OBJECT_BASIS,
    'authz.nexloop_assert_derived_message_read_evidence_v0086(text,text,jsonb)': OBJECT_BASIS,
    'authz.nexloop_assert_derived_message_read_inbound_v0077(text,text,jsonb)': OBJECT_BASIS,
    'authz.nexloop_assert_derived_property_access_before_erasure_v0142(text,text,jsonb,text)': OBJECT_BASIS,
    'authz.nexloop_assert_purpose_message_read(text,text,jsonb)': OBJECT_BASIS,
    'authz.nexloop_assert_relay_source_property_read(text,text,jsonb,text,jsonb)': OBJECT_BASIS,
    'authz.nexloop_assert_relay_source_read(text,text,jsonb)': OBJECT_BASIS,
    'authz.nexloop_assert_role_source_read(text,text,jsonb)': OBJECT_BASIS,
    'authz.nexloop_conversation_read_basis(text,text,text)': OBJECT_BASIS,
    'authz.nexloop_message_purpose_basis_for(text,text,text,text,text,text)': OBJECT_BASIS,
    'authz.nexloop_message_read_basis(text,text,text)': OBJECT_BASIS,
    'authz.nexloop_property_access_basis(text,text,text,text)': OBJECT_BASIS,
    'authz.nexloop_property_access_basis_before_relay_source_v0161(text,text,text,text)': OBJECT_BASIS,
    'authz.nexloop_property_access_basis_before_role_source_v0211(text,text,text,text)': OBJECT_BASIS,
    'authz.nexloop_schema_publish_gate(text,text,jsonb)': HOLDER_INSPECTION + ' (F3 gates wrapper and readiness precheck)',
    'runtime.nexloop_schema_publish_smoke_checks(text,text,text,boolean)': HOLDER_INSPECTION + ' (smoke rounds of the alert maintenance)',
    'authz.nexloop_workbench_grant_guard()': CONFIGURATION,
    'control.nexloop_configure_workbench(text,jsonb,jsonb)': CONFIGURATION,
    'control.nexloop_workbench_fact_instance(jsonb,text,text)': CONFIGURATION,
    'control.nexloop_workbench_granted(jsonb,text[])': CONFIGURATION,
}
READS_GRANTS = r"""(fact_kind\s*=\s*'grants'|when 'grants'|p_kind\s*=\s*'grants'|->'grants'|nexloop_workbench_granted\()"""


def functions(admin, pattern):
    return {r for (r,) in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname in ('authz','control','runtime','ontology') and p.prosrc ~ %s""", (pattern,))}


def test_every_function_reading_grants_is_classified(admin):
    found = functions(admin, READS_GRANTS)
    assert found - set(REGISTRY) == set(), 'unclassified grant readers: decide whether the workbench role ceiling applies'
    assert set(REGISTRY) - found == set(), 'stale registry entries'


def test_the_enforcement_points_check_the_ceiling_and_the_wrapped_bodies_are_reachable_only_through_them(admin):
    checking = functions(admin, r'control\.nexloop_workbench_action_permitted\(')
    assert set(ENFORCEMENT) <= checking, set(ENFORCEMENT) - checking
    for wrapped in [name for name, why in REGISTRY.items() if why == WRAPPED]:
        holders = admin.execute("""select coalesce(array_agg(r.rolname order by r.rolname),'{}') from pg_proc p cross join aclexplode(p.proacl) a
            join pg_roles r on r.oid=a.grantee where p.oid=%s::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner""", (wrapped,)).fetchone()[0]
        assert holders == [], (wrapped, holders)
    # The wrappers hold what the wrapped functions held before nx067_oidc_gaps (each was callable by the API).
    for name in ENFORCEMENT:
        assert admin.execute("select has_function_privilege('nexloop_api',%s,'execute')", (name,)).fetchone()[0], name


def test_a_machine_command_passes_the_ceiling_check_untouched(admin):
    """L6 C1 on ba4f4e32: a machine command reaches the M5b dispatcher through the ceiling check. It names no person (the signed machine
    command carries a client), and a machine subject never has an SSO link: the check is true for both, so machine writes are unaffected
    (tests/test_machine_writes_pg.py commits through this same entry)."""
    tenant = 'tenant-nx067-machine'
    for principal in (None, 'nexloop-machine:acme-agent'):
        assert admin.execute('select control.nexloop_workbench_action_permitted(%s,%s,%s)',
                             (tenant, principal, 'eios:action:nexloop.object.add_note:1')).fetchone()[0] is True
    entry = admin.execute("select prosrc from pg_proc where oid='authz.nexloop_goal_governed_action(text,text,text,text,text)'::regprocedure").fetchone()[0]
    assert "a->>'principal_id'" in entry and 'authz.nexloop_goal_governed_action_before_role_ceiling(' in entry


def test_publication_holder_inspection_reads_service_grants_only(admin):
    """NX-068 functions classified HOLDER_INSPECTION: every holder loop over grant facts is restricted to subject_kind 'service' (the
    inner per-holder lookups are keyed by such a holder), no role but the owner may execute them directly, and their callers authorize
    the human by the review read guard, not by a grant."""
    import re
    names = [name for name, why in REGISTRY.items() if why.startswith(HOLDER_INSPECTION)]
    assert len(names) == 2
    for name in names:
        src = admin.execute('select prosrc from pg_proc where oid=%s::regprocedure', (name,)).fetchone()[0]
        loops = re.findall(r"for holder in select distinct f\.entity_key\[1\] from authz\.nexloop_authority_facts f where (.*?) loop", src, re.S)
        assert loops and all("f.payload->>'subject_kind'='service'" in w for w in loops), name
        reads = len(re.findall(r"fact_kind='grants'", src))
        keyed = len(re.findall(r"fact_kind='grants' and g\.entity_key\[1\]=holder", src))
        assert reads == len(loops) + keyed, (name, reads, len(loops), keyed)
        assert 'nexloop_browser_authority_fact_snapshot' not in src and 'nexloop_load_authority_fact' not in src
        # proacl NULL means PostgreSQL's default (PUBLIC may execute) and aclexplode(NULL) is empty; PUBLIC is grantee 0, not in pg_roles.
        acl, grantees = admin.execute("""select p.proacl is not null,coalesce((select array_agg(a.grantee::regrole::text order by a.grantee::regrole::text)
            from aclexplode(p.proacl) a where a.privilege_type='EXECUTE' and a.grantee<>p.proowner),'{}') from pg_proc p where p.oid=%s::regprocedure""",
                                      (name,)).fetchone()
        assert acl is True and grantees == [], (name, acl, grantees)
    readiness = admin.execute("select prosrc from pg_proc where oid='authz.nexloop_read_schema_publish_readiness(text,text,text,text,text)'::regprocedure").fetchone()[0]
    assert 'authz.nexloop_assert_review_read(' in readiness and 'authz.nexloop_schema_publish_gate(' in readiness
