"""NX-064 (design v2 §3 item 6 and §4, L6 C5): the incentive tool's one resend of the same body after an ambiguous answer, on the
Run entry's key 'incentive-run:<run>:<offering>' (0145 runtime.nexloop_incentive_accept), with actual PostgreSQL. Three cases, as the
effect path's (test_effect_resend_pg):

1. the first request committed: the resend returns the stored intent (replayed);
2. the first request rolled back: the resend creates the one intent;
3. the first request's transaction is still open when the resend arrives. nexloop_incentive_accept reads before it inserts and takes
   no lock (L6): the resend does not see the uncommitted row, inserts, waits on the primary key and, once the first commits, fails
   with unique_violation (23505) - not NXB02. The guard answers that as 403 incentive_unavailable (runtime_control), so the Host ends
   the call incentive_unavailable without a third request. Never a second intent; the budget is reserved per intent row at dispatch.
The accept function is called directly (owner connection) with the parameters the Run entry passes."""
import threading
import uuid

import psycopg
import pytest
from test_incentives_pg import incentives, seed_budget, seed_offering  # noqa: F401
from commercial_fixture import TENANT
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
ACCEPT = 'select runtime.nexloop_incentive_accept(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'


def arguments(c, offer, run):
    return (TENANT, 'real', f"incentive-run:{run}:{offer['incentive_offering_id']}", c['consumer'], offer['incentive_offering_id'],
            offer['offering_revision'], offer['binding_id'], 300, 'CNY', '', 'agent', 'run:' + run, 'agent', run, None)


def rows(admin, run):
    return admin.execute('select count(*) from runtime.nexloop_incentive_issues where run_id=%s', (run,)).fetchone()[0]


@PLAN
def test_resend_after_a_committed_first_request_returns_the_stored_intent(incentives, admin, pg):
    c = incentives;seed_budget(admin, 1000);offer = seed_offering(c, admin);run = str(uuid.uuid4())
    with psycopg.connect(pg, autocommit=True) as one, psycopg.connect(pg, autocommit=True) as two:
        first = one.execute(ACCEPT, arguments(c, offer, run)).fetchone()[0]
        again = two.execute(ACCEPT, arguments(c, offer, run)).fetchone()[0]
    assert (first['replayed'], again['replayed']) == (False, True) and again['intent_id'] == first['intent_id']
    assert rows(admin, run) == 1


@PLAN
def test_resend_after_a_rolled_back_first_request_creates_the_one_intent(incentives, admin, pg):
    c = incentives;seed_budget(admin, 1000);offer = seed_offering(c, admin);run = str(uuid.uuid4())
    with psycopg.connect(pg) as one:
        one.execute(ACCEPT, arguments(c, offer, run))
        one.rollback()
    assert rows(admin, run) == 0
    with psycopg.connect(pg, autocommit=True) as two:
        created = two.execute(ACCEPT, arguments(c, offer, run)).fetchone()[0]
    assert created['replayed'] is False and rows(admin, run) == 1


@PLAN
def test_resend_while_the_first_is_uncommitted_fails_unique_violation_and_never_makes_a_second_intent(incentives, admin, pg):
    c = incentives;seed_budget(admin, 1000);offer = seed_offering(c, admin);run = str(uuid.uuid4())
    result = {}
    with psycopg.connect(pg) as one:
        first = one.execute(ACCEPT, arguments(c, offer, run)).fetchone()[0]  # inserted, not committed

        def resend():
            try:
                with psycopg.connect(pg, autocommit=True) as two:
                    two.execute("set lock_timeout='10s'")
                    result['value'] = two.execute(ACCEPT, arguments(c, offer, run)).fetchone()[0]
            except Exception as error:  # noqa: BLE001 - the outcome is asserted below
                result['value'] = error
        thread = threading.Thread(target=resend)
        thread.start()
        thread.join(1.5)
        assert thread.is_alive()  # waiting on the uncommitted row's key
        one.commit()
        thread.join(15)
    assert not thread.is_alive() and first['replayed'] is False
    # Observed (L6): the read found nothing, the insert waited, then failed on the key - not NXB02 (the guard's 409 conflict).
    assert isinstance(result['value'], psycopg.errors.UniqueViolation)
    assert rows(admin, run) == 1
    # A later resend (a new call) finds it.
    with psycopg.connect(pg, autocommit=True) as three:
        assert three.execute(ACCEPT, arguments(c, offer, run)).fetchone()[0]['replayed'] is True
    assert rows(admin, run) == 1


def test_the_guard_answers_a_unique_violation_as_incentive_unavailable_not_a_conflict():
    """runtime_control maps an accept failure by SQLSTATE: NXB02 -> 409 incentive_payload_conflict, anything else (23505 among them)
    -> 403 incentive_unavailable, which the Host ends at once (no further request)."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'packages/eios-core/src/nexloop_eios/runtime_control.py').read_text()
    assert "code={'NXB02':'incentive_payload_conflict','NXI01':'outside_catalog_terms'}.get(getattr(error,'sqlstate',None),'incentive_unavailable')" in source
    assert "self.send(409 if code=='incentive_payload_conflict' else 403,{'code':code})" in source
