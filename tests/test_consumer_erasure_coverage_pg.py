"""Structural check (NX-043 review 3.1, dispatcher 2026-10-21): every table with a consumer_id column is reached by the NX-029 erasure
propagation - the target of an update or delete statement in an erasure function or trigger (L6 erasure-coverage-1 review 1.1: a table
only read there, as the conversations were, is not reached) - or is listed below with the reason it needs none. A new table with
a consumer_id that does neither fails here. Real PostgreSQL on a clean catalog."""
import pytest

from nexloop_eios.bootstrap import bootstrap

# Tables with a consumer_id that no erasure body names, each with its reason.
_SIM = 'simulation or shadow world only (world check), synthetic Consumers; deleted with the whole world (runtime.nexloop_sim_world_targets, NX-041 D2)'
NO_ERASURE_NEEDED = {
    'runtime.nexloop_sim_consumer_events': _SIM, 'runtime.nexloop_sim_consumer_observations': _SIM, 'runtime.nexloop_sim_context_bindings': _SIM,
    'runtime.nexloop_sim_session_events': _SIM, 'runtime.nexloop_twin_event_provenance': _SIM, 'runtime.nexloop_twin_observations': _SIM,
    'runtime.nexloop_twin_samples': _SIM,
    # The erasure writes it (0132, blocking) and keeps the original id on purpose: the erased Consumer can never be contacted again;
    # its release refuses while the erasure exists, and the row cannot be deleted (nx025_no_delete). No text: matched_text ''.
    'control.nexloop_contact_restrictions': 'the erasure\'s own permanent contact restriction (0132): original id kept on purpose, no text',
}
# KNOWN GAPS (found by this check, 2026-10-21; outside NX-043): real tables with a consumer_id that the NX-029 erasure does not reach.
# They are listed so the check passes today and so the list can only shrink: a table leaves it when the erasure names it (the test
# then demands its removal), and no new table may join it. Several hold the Consumer's own text (marked *). Follow-up task, not a
# reason: none of these is claimed to need no erasure.
KNOWN_GAPS = set()  # erasure coverage batches 1-3 (0305, 0306): every real table with a consumer_id is written by the erasure

ERASURE_BODIES = """select string_agg(p.prosrc,' ') from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname in ('runtime','control','authz','ontology') and (p.proname ~ 'erasure|erase|purge|pseudonym' or p.proname in ('nexloop_sim_world_targets'))"""


@pytest.fixture
def db(admin):
    bootstrap(admin)
    return admin


def tables_with_consumer_id(db):
    return sorted(t for (t,) in db.execute("""select c.oid::regclass::text from pg_class c join pg_namespace n on n.oid=c.relnamespace
        join pg_attribute a on a.attrelid=c.oid and a.attname='consumer_id' and not a.attisdropped
        where c.relkind in ('r','p') and n.nspname not in ('pg_catalog','information_schema')""").fetchall())


def written(bodies):
    """The tables an erasure body updates or deletes from (schema-qualified, as every erasure statement names them)."""
    import re
    return {m.group(1).lower() for m in re.finditer(r'(?:update|delete\s+from)\s+(?:only\s+)?([a-z_]+\.[a-z_0-9]+)', bodies, re.I)}


def test_every_consumer_id_table_is_erased_or_has_a_reason(db):
    bodies = db.execute(ERASURE_BODIES).fetchone()[0]
    reached = written(bodies)
    missing = [t for t in tables_with_consumer_id(db) if t not in NO_ERASURE_NEEDED and t not in KNOWN_GAPS and t not in reached]
    assert missing == [], "\n".join(missing)
    assert all(NO_ERASURE_NEEDED.values())
    # A reason for a table the propagation now writes is stale and leaves the list (the contact restriction is written on purpose).
    assert sorted(t for t in NO_ERASURE_NEEDED if t in reached and t != 'control.nexloop_contact_restrictions') == []
    assert set(NO_ERASURE_NEEDED) <= set(tables_with_consumer_id(db))
    # The known gaps can only shrink: one the erasure now writes must leave the list.
    assert sorted(t for t in KNOWN_GAPS if t in reached) == [] and KNOWN_GAPS <= set(tables_with_consumer_id(db))
    # 21 found (2026-10-21); batches 1-3 (0305, 0306) reach all of them (and the conversations and plans the write-based check found).
    assert len(KNOWN_GAPS) == 0  # locked: a new table with a consumer_id is erased or gets a reason


def test_the_experiment_allocations_are_in_the_propagation(db):
    assert 'control.nexloop_experiment_allocations' in written(db.execute(ERASURE_BODIES).fetchone()[0])


# Batch 4 (L6 review of batch 3): the Consumer is not only a consumer_id. Every table with a column that may hold a principal, an actor
# or a reference to a person is written by the erasure or listed here with the reason it needs none. The whole-database search
# (tests/test_erasure_whole_database_pg.py) is the backstop for what no column name tells.
IDENTITY_COLUMNS = ('principal_id', 'actor_id', 'actor', 'subject_principal_id', 'source_principal', 'owner_principal', 'relay_principal',
                    'executor_principal', 'consumer_ref', 'subject_ref', 'scope_ref', 'subject_key', 'member_ref')
_STAFF = 'a staff member, the owner or a service principal - never the customer'
_KEPT = 'kept on purpose with the erasure itself (see tests/test_erasure_whole_database_pg.py KEPT_ON_PURPOSE)'
IDENTITY_KEPT = {
    'authz.nexloop_evo_principals': _STAFF, 'control.nexloop_alert_silences': _STAFF, 'control.nexloop_budget_consumption': _STAFF,
    'control.nexloop_run_goal_bindings': _STAFF, 'control.nexloop_sim_clock_events': _STAFF, 'runtime.nexloop_effect_attempts': _STAFF,
    'runtime.nexloop_effect_query_admissions': _STAFF, 'runtime.nexloop_effect_submissions': _STAFF, 'runtime.nexloop_human_requests': _STAFF,
    'runtime.nexloop_workbench_read_audit': _STAFF + ' (the member who read)', 'runtime.audit_events': _STAFF + ' (the actor of an audited command)',
    'runtime.nexloop_context_artifact_bindings': _STAFF + ' (the relay that bound the context)',
    'runtime.nexloop_fallback_context_bindings': _STAFF + ' (the relay that bound the context)',
    'runtime.nexloop_role_context_artifacts': _STAFF + ' (the role runtime)', 'runtime.nexloop_role_trigger_events': _STAFF + ' (the role runtime)',
    'runtime.nexloop_orphan_sweeps': _STAFF + ' (the artifact sweeper)',
    # control.nexloop_initial_identity_receipts: staff and owner initial accounts, never a customer; written by the staff erasure
    # (NX-058 slice E: its password fingerprint), so no reason here.
    'control.nexloop_browser_memberships': 'identity layer: the customer account itself is not this erasure\'s (owner decision 5: deactivate on erasure, delete by the identity layer)',
    'control.nexloop_real_dispatch_switches': 'scope_ref names the tenant or an Action type, never a person',
    'runtime.nexloop_commercial_exceptions': 'subject_ref is a commercial record or event reference, never a person',
    'runtime.nexloop_commitment_events': 'subject_ref is a commitment or Claim reference; principal_id the staff member who acted',
    'runtime.nexloop_native_web_transports': 'principal_id follows runtime.nexloop_native_web_events through ON UPDATE CASCADE (0306)',
    'control.nexloop_deletion_ledger': _KEPT, 'runtime.nexloop_erasure_tombstones': _KEPT,
    # NX-058: workbench members' role grants and role history - staff, never a Consumer (staff erasure: NX-058 slice E,
    # tests/test_member_erasure_coverage_pg.py; kept there with their reason, E5).
    'control.nexloop_workbench_grant_instances': _STAFF + ' (a workbench member\'s instantiated role grants)',
    'control.nexloop_workbench_role_history': _STAFF + ' (when a workbench member held a role)',
    # NX-058 slice F: single sign-on of workbench members - staff, never a Consumer (staff erasure: tests/test_member_erasure_coverage_pg.py).
    # The link holds the member's IdP subject in clear (external_subject, not an HMAC); a trigger deletes it when the membership is revoked.
    'control.nexloop_workbench_external_identities': _STAFF + ' (a workbench member\'s SSO link: provider, issuer and IdP subject in clear; deleted on revocation)',
    'control.nexloop_workbench_sso_pending': _STAFF + ' (an SSO-only invitee awaiting the link: principal, provider and invitation id; subject_id is the internal subject id, not the IdP sub; deleted on link or revocation)',
    # NX-057: who switched or resolved a remediation (the owner or the planner's service principal); the remediation holds no consumer id.
    'control.nexloop_remediation_switches': _STAFF + ' (the owner who paused or resumed remediation)',
    'runtime.nexloop_remediation_events': _STAFF + ' (the planner service or the owner who moved a remediation)',
}
# Known JSON paths that name the Consumer or their principal, and how the erasure treats each (a table of IDENTITY_KEPT or one the
# erasure writes; the whole-database search proves the values are gone).
JSON_PATHS = {
    ('runtime.nexloop_conversation_messages', 'record', 'actor'): 'erased: pseudonym (0307)',
    ('runtime.jobs', 'normalized_input', 'run_command.consumer_ref'): 'erased: pseudonym (0307)',
    ('runtime.nexloop_action_claims', 'claim', '*'): 'erased: pseudonym (0307)',
    ('runtime.nexloop_effect_dispatch_controls', 'snapshot', 'scopes[].ref'): 'erased: {"erased":true} (0307)',
    ('ontology.objects', 'properties', 'consumer_ref (CommercialRecord)'): 'erased: derived again, unlinked (0307)',
    ('control.nexloop_effect_contexts', 'expected_versions', '[].resource_ref'): 'erased: {"erased":true} (0306)',
    ('runtime.nexloop_work_feed', 'payload', 'consumer_id / object_id'): 'erased: deleted, except the transient recall index deletion (0307)',
    ('control.nexloop_restore_journal', 'entry', 'subject_ref / consumer_id'): 'kept: NX-035 replay of the erasure after a restore',
}


def test_every_identity_column_table_is_erased_or_has_a_reason(db):
    bodies = db.execute(ERASURE_BODIES).fetchone()[0]
    reached = written(bodies)
    tables = sorted(t for (t,) in db.execute("""select distinct c.oid::regclass::text from pg_class c join pg_namespace n on n.oid=c.relnamespace
        join pg_attribute a on a.attrelid=c.oid and not a.attisdropped and a.attname=any(%s)
        where c.relkind in ('r','p') and c.relpersistence<>'t' and n.nspname not in ('pg_catalog','information_schema')""", (list(IDENTITY_COLUMNS),)).fetchall())
    missing = [t for t in tables if t not in reached and t not in IDENTITY_KEPT and t not in NO_ERASURE_NEEDED]
    assert missing == [], '\n'.join(missing)
    assert all(IDENTITY_KEPT.values()) and set(IDENTITY_KEPT) <= set(tables)
    # A reason for a table the erasure now writes is stale.
    assert sorted(t for t in IDENTITY_KEPT if t in reached and t not in ('control.nexloop_deletion_ledger',)) == []
    for (table, column, _path), how in JSON_PATHS.items():
        assert how.startswith(('erased:', 'kept:')), (table, how)
        if how.startswith('erased:'):
            assert table in reached, (table, column)
