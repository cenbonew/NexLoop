"""Structural check (NX-043 review 3.1, dispatcher 2026-10-21): every table with a consumer_id column is reached by the NX-029 erasure
propagation - named in the body of an erasure function or trigger - or is listed below with the reason it needs none. A new table with
a consumer_id that does neither fails here. Real PostgreSQL on a clean catalog."""
import pytest

from nexloop_eios.bootstrap import bootstrap

# Tables with a consumer_id that no erasure body names, each with its reason.
_SIM = 'simulation or shadow world only (world check), synthetic Consumers; deleted with the whole world (runtime.nexloop_sim_world_targets, NX-041 D2)'
NO_ERASURE_NEEDED = {
    'runtime.nexloop_sim_consumer_events': _SIM, 'runtime.nexloop_sim_consumer_observations': _SIM, 'runtime.nexloop_sim_context_bindings': _SIM,
    'runtime.nexloop_sim_session_events': _SIM, 'runtime.nexloop_twin_event_provenance': _SIM, 'runtime.nexloop_twin_observations': _SIM,
    'runtime.nexloop_twin_samples': _SIM,
}
# KNOWN GAPS (found by this check, 2026-10-21; outside NX-043): real tables with a consumer_id that the NX-029 erasure does not reach.
# They are listed so the check passes today and so the list can only shrink: a table leaves it when the erasure names it (the test
# then demands its removal), and no new table may join it. Several hold the Consumer's own text (marked *). Follow-up task, not a
# reason: none of these is claimed to need no erasure.
KNOWN_GAPS = {
    'authz.nexloop_role_run_bindings', 'control.nexloop_consumer_identity_link_receipts', 'control.nexloop_consumer_owners',
    'control.nexloop_contact_refusal_hits',  # * matched_text
    'control.nexloop_data_exports',  # until its expiry retention class
    'control.nexloop_effect_contexts', 'control.nexloop_effect_control_ledger', 'control.nexloop_relationship_context_recipes',
    'control.nexloop_reply_escalations',  # * detail
    'control.nexloop_takeovers',  # * reason
    'ontology.nexloop_extraction_runs',
    'runtime.nexloop_commitment_exceptions', 'runtime.nexloop_commitments',  # until the commitment_closed retention class
    'runtime.nexloop_evo_patch_sources', 'runtime.nexloop_marketing_consents',  # * reason
    'runtime.nexloop_marketing_optouts',  # * matched_text
    'runtime.nexloop_message_relay_leases', 'runtime.nexloop_native_web_events',
    'runtime.nexloop_outreach_sends',  # * body
    'runtime.nexloop_relay_handoffs', 'runtime.nexloop_scope_denials',
}

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


def test_every_consumer_id_table_is_erased_or_has_a_reason(db):
    bodies = db.execute(ERASURE_BODIES).fetchone()[0]
    missing = [t for t in tables_with_consumer_id(db) if t not in NO_ERASURE_NEEDED and t not in KNOWN_GAPS and t.split('.')[1] not in bodies]
    assert missing == [], "\n".join(missing)
    assert all(NO_ERASURE_NEEDED.values())
    # A reason for a table the propagation now names is stale and leaves the list.
    assert sorted(t for t in NO_ERASURE_NEEDED if t.split('.')[1] in bodies) == []
    assert set(NO_ERASURE_NEEDED) <= set(tables_with_consumer_id(db))
    # The known gaps can only shrink: one the erasure now names must leave the list.
    assert sorted(t for t in KNOWN_GAPS if t.split('.')[1] in bodies) == [] and KNOWN_GAPS <= set(tables_with_consumer_id(db))
    assert len(KNOWN_GAPS) <= 21


def test_the_experiment_allocations_are_in_the_propagation(db):
    assert 'nexloop_experiment_allocations' in db.execute(ERASURE_BODIES).fetchone()[0]
