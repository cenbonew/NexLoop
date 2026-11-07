"""ADR-045 v3 (L6 C2): the global workbench route of set_synthetic_fault where the stage manifest was never published (production):
the owner's well-formed request answers 404 not_found (never 500, never a silent pass) and no table gains or loses a row - the
audit events, action claims, governed intents and receipts, approvals, the fault switch, its members, decisions and audit included.
What the request does write: the browser session's activity (last_seen_at / idle_expires_at, an update of an existing row, as every
authenticated workbench request does); there is no rate counter on this route. Production app, real login, real PostgreSQL."""
import pytest

from test_workbench_actions_http import act, session, workbench  # noqa: F401
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)

NAMED = ('runtime.audit_events', 'runtime.nexloop_action_claims', 'control.nexloop_synthetic_fault_switches', 'control.nexloop_synthetic_fault_members',
         'runtime.nexloop_synthetic_fault_decisions', 'runtime.nexloop_synthetic_fault_events')


def row_counts(admin):
    tables = [t for (t,) in admin.execute("""select n.nspname||'.'||c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind in ('r','p') and n.nspname in ('authz','control','runtime','ontology') order by 1""")]
    return {t: admin.execute(f'select count(*) from {t}').fetchone()[0] for t in tables}


BODY = {'state': 'open', 'consumer_ids': ['a' * 64], 'remaining': 3, 'duration_seconds': 600, 'reason': 'NX-038 S6 P5 (ADR-045, synthetic)'}


def test_the_unpublished_fault_action_answers_404_and_changes_no_row_count(workbench, admin):
    c = workbench
    assert admin.execute("select count(*) from control.nexloop_action_definitions where resource_id like 'eios:action:nexloop.synthetic_fault.set:%'").fetchone()[0] == 0
    with c['app']() as client:
        csrf = session(client, c['identity'])
        before = row_counts(admin)
        for body in (BODY, {**BODY, 'state': 'closed', 'consumer_ids': [], 'remaining': 0, 'duration_seconds': 0}):
            response = act(client, csrf, 'set_synthetic_fault', body)
            assert response.status_code == 404 and response.json()['code'] == 'not_found', response.text
        after = row_counts(admin)
    assert {t: (before[t], after[t]) for t in before if before[t] != after[t]} == {}
    assert all(t in before for t in NAMED) and all(after[t] == before[t] for t in NAMED)
    # The governed intent and receipt tables of the entry (whatever their names) are covered by the full comparison above.
    assert any('governed' in t or 'intent' in t for t in before)


def test_a_malformed_fault_request_is_422_before_any_lookup(workbench, admin):
    c = workbench
    with c['app']() as client:
        csrf = session(client, c['identity'])
        before = row_counts(admin)
        for wrong in ({**BODY, 'remaining': 51}, {**BODY, 'state': 'on'}, {**BODY, 'consumer_ids': ['x']}, {k: v for k, v in BODY.items() if k != 'reason'}):
            assert act(client, csrf, 'set_synthetic_fault', wrong).status_code == 422, wrong
        assert row_counts(admin) == before
