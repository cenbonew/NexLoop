"""NX-035 × AT-043: an incentive issue still open at the restore time is held by the fence, counted in the effect window
and paused (action type nexloop.incentive.issue); after its scope opens it is refused at admission, never issued.

The 'restored' state is the fixture database with a restore row written by the owner functions the restore tool calls (the
real point-in-time restore is test_restore_drill_pg). Incentive executor and loopback ledger as in test_incentives_pg.
Synthetic data only."""
import pytest
from psycopg.types.json import Jsonb

from commercial_fixture import TENANT
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_incentives_pg import incentives, intent, issue, seed_budget, seed_offering, tick  # noqa: F401

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
REF = 'restore-incentive-1'


def owner_sql(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute(sql, params).fetchone()[0]


def test_an_incentive_open_at_the_restore_is_fenced_paused_and_never_issued(incentives, admin):
    c = incentives
    seed_budget(admin, 1000)
    offer = seed_offering(c, admin)
    assert issue(c, 'inc-restore', offer, 300)['state'] == 'accepted'
    now = admin.execute('select clock_timestamp()').fetchone()[0]
    owner_sql(admin, 'select runtime.nexloop_restore_begin(%s,%s,%s,%s,1)', (REF, Jsonb({'target_name': 'drill-x'}), now, now))
    # Fenced: the admission rolls back (NXR01 is not a terminal refusal); still accepted, nothing sent or reserved.
    try:
        tick(c)
    except Exception:
        pass
    assert intent(admin, 'inc-restore')[1] == 'accepted' and c['ledger'].requests == []
    owner_sql(admin, 'select runtime.nexloop_restore_replay(%s,%s)', (REF, Jsonb([])))
    owner_sql(admin, 'select runtime.nexloop_restore_replayed(%s)', (REF,))
    window = owner_sql(admin, 'select runtime.nexloop_restore_reconcile(%s,clock_timestamp(),%s)', (REF, Jsonb([])))
    row = next(r for r in window['channels'] if r['tenant_id'] == TENANT and r['action_type'] == 'nexloop.incentive.issue')
    assert row == {'tenant_id': TENANT, 'world': 'real', 'action_type': 'nexloop.incentive.issue', 'time_queryable': False, 'open_at_restore': 1,
                   'external_in_window': 0, 'consistent': 0, 'unverifiable': 0, 'paused': True}
    owner_sql(admin, 'select runtime.nexloop_restore_verified(%s)', (REF,))
    # The scopes are opened (the owner's governed Action is covered by test_restore_fence_pg); the channel stays paused.
    with admin.transaction():
        admin.execute("update control.nexloop_restore_scopes set opened=true,opened_by='synthetic-owner',opened_at=now()")
    tick(c)
    assert intent(admin, 'inc-restore')[1:3] == ('refused', 'control_paused') and c['ledger'].requests == []
    assert admin.execute("select count(*) from control.nexloop_budget_consumption where budget_kind='incentive'").fetchone() == (0,)
