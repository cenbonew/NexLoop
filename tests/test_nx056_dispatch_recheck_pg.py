"""NX-056 A5 (docs/design/unified-approvals.md v5 §12, L6 A2 review test gap): an approval never bypasses the dispatch gates.

On the AT-043 incentive fixture (the owner issues through the governed entry; the issuer service dispatches through the effect gates)
a test-only policy holds nexloop.incentive.issue for a second owner. The approved issue is executed by the requester's resubmission,
and its external effect is still refused at dispatch while the incentive Action's real dispatch switch is closed, exactly
as without any approval (terminal, never sent). The approver is a second human member decided through the governed approvals.decide handler.
Synthetic data only.
"""
import uuid

from psycopg.types.json import Jsonb

from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from support.loopback_effects import make_external
from test_incentives_pg import incentives, intent, issue, seed_budget, seed_offering, tick  # noqa: F401
from test_real_dispatch_switch_pg import INCENTIVE, SEND, set_switch
from support.vote_cooldown import ripen_approvers
from test_workbench_read_pg import add_human


def approve_as_second_owner(admin, approval):
    """A second human member, owner in the workbench configuration, decides through the governed handler (as the entry calls it)."""
    second = add_human(admin, 'incentive-approver')
    admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
        values(%s,9001,9001,'synthetic-workbench-app',%s,%s,%s,'synthetic-configurator')""",
                  (TENANT, Jsonb({'owner': ['eios:action:nexloop.approvals.decide:1']}), Jsonb({second['principal_id']: 'owner'}), 'c' * 64))
    ripen_approvers(admin, TENANT)  # NX-058 slice D §2: the second owner votes at once
    return admin.execute('select control.nexloop_governed_approval_decide(%s,%s,%s,%s,%s,%s)', (
        TENANT, 'real', second['principal_id'], 'human', 'intent-' + uuid.uuid4().hex[:12],
        Jsonb({'operation': 'decide_approval', 'request_id': uuid.uuid4().hex, 'approval_id': approval, 'decision': 'approve', 'reason': '核对过'}))).fetchone()[0]


def test_an_approved_issue_is_still_refused_by_a_closed_switch(incentives, admin, monkeypatch):
    c = incentives
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
    make_external(admin, TENANT, INCENTIVE)
    set_switch(admin, TENANT, 'tenant', '*', True, 'real')
    set_switch(admin, TENANT, 'action_type', SEND, True)  # the incentive Action's own switch stays closed
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,published_by) values('incentive.issue',1,1,null,1,'{owner}',86400,'{amount_minor,currency}','synthetic-config')""")
    seed_budget(admin, 1000)
    offer = seed_offering(c, admin)
    waiting = issue(c, 'apr-1', offer, 100)
    assert waiting['status'] == 'waiting_approval' and intent(admin, 'apr-1') is None  # nothing issued yet
    assert approve_as_second_owner(admin, waiting['approval_id'])['status'] == 'approved'
    executed = issue(c, 'apr-2', offer, 100)  # the requester resubmits the same body (a new request id)
    assert executed['approval'] == 'consumed' and intent(admin, 'apr-2')[1] == 'accepted'
    assert tick(c)['refused'] == 1 and intent(admin, 'apr-2')[1:3] == ('refused', 'real_dispatch_closed') and c['ledger'].requests == []
