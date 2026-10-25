"""NX-058 slice E (design §3): every table with a column that may name a workbench member (principal, subject, username, source digest) is
written by the staff erasure (or its replay) or listed below with the reason it needs none. A new such table fails here until it does one
or the other. Real PostgreSQL on a clean catalog."""
import re

import pytest

from nexloop_eios.bootstrap import bootstrap

STAFF_COLUMNS = ('principal_id', 'subject_id', 'issued_by_principal', 'issued_by_subject', 'target_principal', 'target_subject', 'accepting_subject',
                 'accepted_principal', 'username', 'issuer_source', 'acceptor_source', 'completer_source', 'requester_principal', 'requester_subject',
                 'actor_principal', 'actor_subject', 'consumed_by_principal', 'consumed_by_subject', 'erased_by_principal', 'operator_principal_id')
STAFF_ERASURE_BODIES = """select string_agg(p.prosrc,' ') from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='control' and p.proname in ('nexloop_workbench_member_erase','nexloop_governed_erase_workbench_member')"""
_ACT = 'who did what: the principal of a staff or service action, kept for audit traceability (design §2.7); no username or digest'
_CUSTOMER = 'a customer principal (NX-029 Consumer erasure), never a workbench member'
_SERVICE = 'a service or runtime principal, never a workbench member'
STAFF_KEPT = {
    'control.nexloop_alert_silences': _ACT, 'control.nexloop_approval_consumptions': _ACT, 'control.nexloop_approval_decisions': _ACT,
    'control.nexloop_budget_consumption': _ACT, 'control.nexloop_control_events': _ACT, 'control.nexloop_remediation_switches': _ACT,
    'control.nexloop_run_goal_bindings': _ACT, 'control.nexloop_sim_clock_events': _ACT, 'runtime.nexloop_commitment_events': _ACT,
    'runtime.nexloop_effect_query_admissions': _ACT, 'runtime.nexloop_effect_submissions': _ACT, 'runtime.nexloop_human_requests': _ACT,
    'runtime.nexloop_incentive_issues': _ACT, 'runtime.nexloop_outreach_sends': _ACT, 'runtime.nexloop_workbench_read_audit': _ACT + ' (the member who read)',
    'runtime.nexloop_action_claims': _ACT + ' (claims bind a request digest, never the body)',
    'control.nexloop_consumer_identity_link_receipts': _CUSTOMER, 'control.nexloop_consumer_owners': _CUSTOMER, 'runtime.nexloop_conversations': _CUSTOMER,
    'runtime.nexloop_native_web_events': _CUSTOMER, 'runtime.nexloop_native_web_transports': _CUSTOMER,
    'authz.nexloop_evo_principals': _SERVICE, 'runtime.nexloop_local_artifacts': _SERVICE, 'runtime.nexloop_message_relay_leases': _SERVICE,
    'runtime.nexloop_orphan_sweeps': _SERVICE,
    'control.nexloop_browser_rate_policies': 'the identity operator (a service) per limiter; no member',
    'control.nexloop_browser_rate_counters': 'client digests in short rate windows (minutes); no username',
    'control.nexloop_browser_memberships': 'the revoked membership rows stay: principal and subject kept for audit (design §2.7)',
    'control.nexloop_browser_login_events': 'the subject stays; the source and username digests are cleared 90 days after the erasure by the retention service (slice E2)',
    'control.nexloop_workbench_grant_instances': 'grant audit (principal and digests only); revoked members hold none current - staff erasure: NX-058 slice E (E5)',
    'control.nexloop_workbench_role_history': 'vote eligibility and audit (principal only); its stop is written at revocation - staff erasure: NX-058 slice E (E5)',
}


@pytest.fixture
def db(admin):
    bootstrap(admin)
    return admin


def tables(db):
    return sorted(t for (t,) in db.execute("""select distinct c.oid::regclass::text from pg_class c join pg_namespace n on n.oid=c.relnamespace
        join pg_attribute a on a.attrelid=c.oid and not a.attisdropped and a.attname=any(%s)
        where c.relkind in ('r','p') and c.relpersistence<>'t' and n.nspname in ('control','runtime','authz')""", (list(STAFF_COLUMNS),)).fetchall())


def written(bodies):
    return {m.group(1).lower() for m in re.finditer(r'(?:update|delete\s+from|insert\s+into)\s+(?:only\s+)?([a-z_]+\.[a-z_0-9]+)', bodies, re.I)}


def test_every_staff_identity_table_is_erased_or_has_a_reason(db):
    reached = written(db.execute(STAFF_ERASURE_BODIES).fetchone()[0])
    missing = [t for t in tables(db) if t not in reached and t not in STAFF_KEPT]
    assert missing == [], '\n'.join(missing)
    assert all(STAFF_KEPT.values()) and set(STAFF_KEPT) <= set(tables(db))
    # A reason for a table the staff erasure now writes is stale.
    assert sorted(t for t in STAFF_KEPT if t in reached) == []
    # What the erasure writes (design §2): the account, the subject, sessions and token realms, invitations, reset codes, invite_owner
    # bodies and its own ledger.
    assert {'control.nexloop_browser_accounts', 'control.nexloop_browser_subjects', 'control.nexloop_browser_sessions', 'control.nexloop_workbench_invitations',
            'control.nexloop_workbench_reset_codes', 'control.nexloop_approval_requests', 'control.nexloop_workbench_member_erasures',
            'control.nexloop_initial_identity_receipts'} <= reached  # the receipt's password fingerprint is overwritten
