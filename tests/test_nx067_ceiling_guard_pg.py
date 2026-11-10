"""NX-067 Z5: every SQL function that reads or writes grant facts is registered here with why the workbench role ceiling does or does
not apply to it. A new function reading grants fails this test until it is classified; the three enforcement points must call
control.nexloop_workbench_action_permitted. The ceiling narrows workbench Action grants of browser members only."""

ENFORCEMENT = {
    # Where the ceiling is applied (each calls control.nexloop_workbench_action_permitted).
    'authz.nexloop_workbench_action_for(text,text,text)': 'the workbench entry picks the Action: granted false',
    'authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[])': 'every browser authorization reads a grant here: withheld',
    'authz.nexloop_goal_governed_action(text,text,text,text,text)': 'the governed dispatch transaction: 42501',
}
WRAPPED = 'wrapped by an enforcement point of the same name (0446) and callable only through it'
SERVICE = 'service, Agent or Run credentials only; a browser member is authorized through the browser snapshot'
OBJECT_BASIS = ('object and property read grants (eios:object / eios:property), never a workbench Action grant; a member reaches them only '
                'after the workbench read Action, whose grant the browser snapshot withholds under an empty ceiling')
CONFIGURATION = 'writes or checks the configured grants (trusted configuration), decides no request'
REGISTRY = {
    'authz.nexloop_browser_authority_fact_snapshot_before_role_ceiling(text,text,text,text[])': WRAPPED,
    'authz.nexloop_workbench_action_for_before_role_ceiling(text,text,text)': WRAPPED,
    'authz.nexloop_workbench_action_for_single_version(text,text,text)': WRAPPED + ' (the wrapped body calls it for one-version Actions)',
    'authz.nexloop_load_authority_fact(text,text,text,text[])': SERVICE,
    'authz.nexloop_load_authority_fact_with_identity(jsonb,text,text,text,text[])': SERVICE,
    'authz.nexloop_claim_match_settled(text,text,text,bigint)': SERVICE + ' (claim matcher)',
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
    # The wrappers hold what the wrapped functions held before 0446 (each was callable by the API).
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
