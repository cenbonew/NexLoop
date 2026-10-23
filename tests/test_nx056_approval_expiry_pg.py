"""NX-056 A3 (docs/design/unified-approvals.md v5 §7, §8; 0323, alert rules v14): the expiry sweep and the approval alerts.

The sweep only ends waiting requests past due_by (never executes one; idempotent; the upstream ApprovalExpirySweeper's contract),
it runs in the retention keeper's tick through its signed port, and the 'approvals' metrics block (counts and ages only) drives
approval_waiting and approval_unconsumed. Real PostgreSQL; admin only moves recorded times back to stand for elapsed time.
Synthetic data only.
"""
import json
import uuid

import pytest

from psycopg.types.json import Jsonb

from nexloop_eios.observability import apply_rules, load_rules, validate_rules
from test_nx056_approvals_store_pg import decide, open_request, row, store  # noqa: F401
from test_observability_pg import RULES, observed  # noqa: F401
from test_workbench_read_pg import TENANT, add_human, browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401
from test_erasure_pg import erasing, keeper  # noqa: F401
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401

V13, V14 = RULES.parent / 'alert-rules.v13.json', RULES.parent / 'alert-rules.v14.json'
CAP = 'real_dispatch.set'
ACTION = 'eios:action:nexloop.real_dispatch.set:1'


def age(admin, approval, hours):
    """Explicit time injection (test only): one request's times moved back, its guard off for that one statement."""
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute('set local session_replication_role=replica')
        admin.execute('update control.nexloop_approval_requests set requested_at=requested_at-make_interval(hours=>%s),due_by=due_by-make_interval(hours=>%s) '
                      'where approval_id=%s', (hours, hours, approval))


def expire_due(admin, limit=100):
    return admin.execute("select control.nexloop_approval_expire_due(%s,'real',%s)", (TENANT, limit)).fetchone()[0]


def approvals(admin):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'approvals'", (TENANT,)).fetchone()[0]


def seed(admin, key):
    """A policy for real_dispatch.set and one requester (the catalog is already bootstrapped by the fixture)."""
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,published_by) values(%s,1,1,%s,1,'{owner}',86400,'{scope_kind,enabled}','synthetic-config') on conflict do nothing""",
                  (CAP, Jsonb({'field': 'enabled', 'equals': True})))
    requester = add_human(admin, key)
    return lambda request_id, **body: admin.execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)', (
        TENANT, 'real', CAP, ACTION, requester['principal_id'],
        Jsonb({'operation': 'set_real_dispatch', 'request_id': request_id, 'scope_kind': 'tenant', 'enabled': True, 'reason': 'x', **body}))).fetchone()[0]


def test_the_sweep_ends_waiting_and_unconsumed_approved_requests_past_due_and_is_idempotent(store):
    f = store
    admin = f['admin']
    late, fresh = open_request(f)['approval_id'], open_request(f, request_id='r-2', scope_kind='action_type')['approval_id']
    approved = open_request(f, request_id='r-3', reason='another')['approval_id']
    decide(f, 'approver', approved)
    for approval in (late, approved):
        age(admin, approval, 25)
    # Before the sweep both past-due requests show as overdue (the sweep's heartbeat), not as waiting or executable.
    assert {k: v for k, v in approvals(admin).items() if k != 'oldest_waiting_age_seconds'} == {'waiting': 1, 'approved_unconsumed': 0, 'overdue': 2,
        'solo_mode_enabled': 0, 'self_approvals_24h': 0, 'eligibility_reductions_24h': 0}
    assert expire_due(admin) == {'scanned': 2, 'expired': 2}
    # An approved one past due_by can no longer be executed (A2): it ends too instead of lingering (L6 A3 review important 2).
    assert [row(f, a)[0] for a in (late, fresh, approved)] == ['expired', 'waiting_approval', 'expired']
    assert expire_due(admin) == {'scanned': 0, 'expired': 0}  # terminal requests have left the set
    decision = admin.execute("select outcome,actor_subject,reason from control.nexloop_approval_decisions where approval_id=%s", (late,)).fetchall()
    assert decision == [('expired', 'system:expiry', 'due_by_passed')]
    assert admin.execute("select payload->>'from' from runtime.audit_events where action='approval.expired' order by 1").fetchall() == [('approved',), ('waiting_approval',)]
    assert approvals(admin)['overdue'] == 0 and approvals(admin)['waiting'] == 1


def test_the_sweep_is_bounded_and_oldest_first(store):
    f = store
    admin = f['admin']
    ids = [open_request(f, request_id=f'b-{n}', reason=f'r{n}')['approval_id'] for n in range(3)]
    for n, approval in enumerate(ids):
        age(admin, approval, 30 - n)
    assert expire_due(admin, limit=2) == {'scanned': 2, 'expired': 2}
    assert [row(f, a)[0] for a in ids] == ['expired', 'expired', 'waiting_approval']
    assert expire_due(admin) == {'scanned': 1, 'expired': 1}


@pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
def test_the_retention_keeper_tick_runs_the_sweep(erasing, admin):
    c = erasing
    open_ = seed(admin, 'expiry-requester')
    approval = open_('k-1')['approval_id']
    age(admin, approval, 25)
    assert keeper(c).run_once()['approvals_expired'] == 1
    assert keeper(c).run_once()['approvals_expired'] == 0
    # A failing sweep is counted and never stops the tick's erasure work (it runs last).
    broken = keeper(c)
    broken.port.approvals_expire = lambda: (_ for _ in ()).throw(RuntimeError('synthetic'))
    summary = broken.run_once()
    assert summary['approvals_failed'] == 1 and summary['approvals_expired'] == 0
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    assert admin.execute('select status from control.nexloop_approval_requests where approval_id=%s', (approval,)).fetchone() == ('expired',)
    # The keeper's principal holds no approval table privilege: the port is its only way in.
    assert not admin.execute("select has_function_privilege('nexloop_domain_worker','control.nexloop_approval_expire_due(text,text,integer)','execute')").fetchone()[0]


def test_rules_v14_are_v13_plus_the_approval_rules():
    v13, v14 = json.loads(V13.read_text()), load_rules(V14)
    assert v14['version'] == 14 and len(v13['rules']) == 61 and v14['rules'][:len(v13['rules'])] == v13['rules']  # L7's v13 is the prefix
    assert {r['rule_id']: r['metric'] for r in v14['rules'][len(v13['rules']):]} == {
        'approval_waiting': 'approvals.oldest_waiting_age_seconds', 'approval_unconsumed': 'approvals.approved_unconsumed',
        'approval_sweep_stale': 'approvals.overdue', 'approval_solo_mode': 'approvals.solo_mode_enabled',
        'approval_solo_self_approved': 'approvals.self_approvals_24h', 'approval_solo_eligibility_reduced': 'approvals.eligibility_reductions_24h'}
    validate_rules(v14)


def test_a_long_wait_raises_approval_waiting_and_the_expiry_clears_it(observed):  # noqa: F811
    o = observed
    admin = o['admin']
    assert apply_rules(load_rules(V14), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    open_ = seed(admin, 'alert-requester')
    approval = open_('a-' + uuid.uuid4().hex[:8])['approval_id']
    o['evaluate']()
    firing = lambda: admin.execute("select firing from control.nexloop_alert_state where rule_id='approval_waiting'").fetchone()
    assert firing() in (None, (False,))
    age(admin, approval, 2)
    assert approvals(admin)['waiting'] == 1 and approvals(admin)['oldest_waiting_age_seconds'] >= 7200
    assert o['evaluate']()['rules_version'] == 14 and firing() == (True,)
    age(admin, approval, 23)
    expire_due(admin)
    o['evaluate']()
    assert approvals(admin)['waiting'] == 0 and firing() == (False,)
