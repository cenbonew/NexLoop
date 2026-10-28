"""NX-056 budget approval (docs/design/NX-056-budget-approval.md v2, owner 2026-10-28 Q1-Q8 as recommended) on PostgreSQL through the
workbench entry of the actual app, with the approval policies of the deployed file (versions 1-4 from the deployment history, then the
current version 5) and three ripe owners: Budget.set only tightens (and renews the same amount once the old period has ended); every
loosening - a new budget, a higher limit, a changed unit, a later end, any change of period_start - is Budget.loosen, which another
owner approves. Covers the direction table (§2.2, L6 I1), the renewal timing (Q4, I1 (b)), the approval flow, the stale-view refusals
when the approval opens and when it executes, the handler's fail-closed layers and the entry's field and role checks (§5.2)."""
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from commitment_fixture import TENANT, commitments, publish_request_actions  # noqa: F401
from commercial_fixture import publish_registry_actions
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from support.budgets import approved_budget
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_owner_member_actions_pg import owners, with_policies  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import publish_goal_actions

NOW = datetime.now(UTC).replace(microsecond=0)
P0, P1 = NOW - timedelta(days=5), NOW + timedelta(days=25)


def iso(t):
    return t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def budget(kind='model', unit='USD', limit='100.00', start=P0, end=P1):
    return {'budget_kind': kind, 'unit': unit, 'limit_amount': limit, 'period_start': iso(start), 'period_end': iso(end)}


def row(admin, kind='model'):
    return admin.execute("select unit,limit_amount::text,period_start,period_end,revision from control.nexloop_budget_limits where budget_kind=%s",
                         (kind,)).fetchone()


def loosening(admin, change, kind='model'):
    """The loosen_budget body for `change` against the current row (as the requester read it)."""
    r = row(admin, kind)
    seen = {'from_unit': None, 'from_limit_amount': None, 'from_period_start': None, 'from_period_end': None, 'expected_revision': 0} if r is None else {
        'from_unit': r[0], 'from_limit_amount': r[1], 'from_period_start': iso(r[2]), 'from_period_end': iso(r[3]), 'expected_revision': r[4]}
    return {**change, **seen}


def consume(admin, consumption_id, amount, at, kind='model', unit='USD'):
    admin.execute("""insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id,recorded_at)
        values(%s,'real',%s,%s,%s,%s,'model-call:synthetic','synthetic-service',%s)""", (TENANT, kind, consumption_id, amount, unit, at))


def remaining(admin, kind='model'):
    lim = admin.execute("select limit_amount,period_start,period_end from control.nexloop_budget_limits where budget_kind=%s", (kind,)).fetchone()
    used = admin.execute("""select coalesce(sum(amount),0) from control.nexloop_budget_consumption where budget_kind=%s
        and recorded_at>=%s and recorded_at<%s""", (kind, lim[1], lim[2])).fetchone()[0]
    return lim[0] - used


def refused(response, reason):
    assert response.status_code == 409 and response.json()['details'] == {'reason': reason}, response.text


@pytest.fixture
def budgets(owners, admin):
    c = owners
    publish_registry_actions(admin, ('nexloop.budget.loosen',))
    publish_request_actions(admin)
    publish_goal_actions(admin)  # Budget.set (typed like the request Actions)
    c['set'] = lambda body, member=None: c['call'](member or c['owner_member'], 'set_budget', body)
    c['loosen'] = lambda body, member=None: c['call'](member or c['owner_member'], 'loosen_budget', body)
    return c


def test_budget_set_runs_only_tightenings_and_every_loosening_is_refused_unwritten(budgets, admin):
    """§2.2: new, higher, changed unit, later end, earlier start and later start are loosenings; lower, an earlier end and the same
    values are tightenings and run at once, with no approval."""
    c = budgets
    with_policies(c)
    refused(c['set'](budget()), 'budget_loosen_requires_approval')  # Q3: a new budget
    assert row(admin) is None
    approved_budget(admin, TENANT, 'model', 'USD', '100.00', P0, P1)
    before = row(admin)
    for change in (budget(limit='100.01'), budget(unit='EUR'), budget(end=P1 + timedelta(days=1)), budget(start=P0 - timedelta(days=1)),
                   budget(start=P0 + timedelta(hours=1)), budget(limit='50.00', start=P0 + timedelta(hours=1))):
        refused(c['set'](change), 'budget_loosen_requires_approval')
        assert row(admin) == before, change
    for change in (budget(), budget(limit='80.00'), budget(limit='80.00', end=P1 - timedelta(days=1))):
        done = c['set'](change)
        assert done.status_code == 200 and done.json()['operation'] == 'set_budget', done.text
    assert row(admin)[1:4] == ('80.00', P0, P1 - timedelta(days=1))
    assert admin.execute('select count(*) from control.nexloop_approval_requests').fetchone() == (0,)  # tightening opens no approval


def test_moving_the_start_later_never_drops_consumption_already_recorded(budgets, admin):
    """L6 I1 (a): 0068 counts consumption inside [period_start, period_end); a later start would leave the earlier consumption out -
    a loosening, refused on Budget.set; the remaining amount stays."""
    c = budgets
    with_policies(c)
    approved_budget(admin, TENANT, 'model', 'USD', '100.00', P0, P1)
    consume(admin, 'early-1', '70.00', P0 + timedelta(hours=1))
    assert remaining(admin) == 30
    refused(c['set'](budget(start=P0 + timedelta(days=1))), 'budget_loosen_requires_approval')
    assert row(admin)[2] == P0 and remaining(admin) == 30


def test_a_same_amount_renewal_runs_only_once_the_old_period_has_ended(budgets, admin):
    """Q4 (L6 I1 (b)): before the old end, a renewal would drop the old period's consumption and hand out a whole new amount early -
    refused; after it, the renewal of the same length, limit and unit runs on Budget.set (whole calendar months, or the exact duration)."""
    c = budgets
    with_policies(c)
    approved_budget(admin, TENANT, 'model', 'USD', '100.00', NOW - timedelta(days=5), NOW + timedelta(days=5))
    consume(admin, 'old-1', '90.00', NOW - timedelta(days=1))
    early = budget(start=NOW + timedelta(days=5), end=NOW + timedelta(days=15))
    refused(c['set'](early), 'budget_loosen_requires_approval')
    assert remaining(admin) == 10
    # Ended: the exact duration.
    approved_budget(admin, TENANT, 'evolution', 'CNY', '50.00', NOW - timedelta(days=20), NOW - timedelta(days=10))
    renewed = c['set'](budget(kind='evolution', unit='CNY', limit='50.00', start=NOW - timedelta(days=10), end=NOW))
    assert renewed.status_code == 200, renewed.text
    assert row(admin, 'evolution')[2:4] == (NOW - timedelta(days=10), NOW)
    refused(c['set'](budget(kind='evolution', unit='CNY', limit='50.00', start=NOW, end=NOW + timedelta(days=11))), 'budget_loosen_requires_approval')
    # Ended: whole calendar months (October has 31 days, November 30: the same one month).
    approved_budget(admin, TENANT, 'incentive', 'CNY', '1000', '2026-08-01T00:00:00Z', '2026-09-01T00:00:00Z')
    monthly = c['set'](budget(kind='incentive', unit='CNY', limit='1000', start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC)))
    assert monthly.status_code == 200, monthly.text
    for limit, end in (('1001', datetime(2026, 11, 1, tzinfo=UTC)), ('1000', datetime(2026, 11, 2, tzinfo=UTC))):  # a higher limit; a longer period
        refused(c['set'](budget(kind='incentive', unit='CNY', limit=limit, start=datetime(2026, 10, 1, tzinfo=UTC), end=end)), 'budget_loosen_requires_approval')
    assert c['set'](budget(kind='incentive', unit='CNY', limit='1000', start=datetime(2026, 10, 1, tzinfo=UTC), end=datetime(2026, 11, 1, tzinfo=UTC))).status_code == 200
    assert admin.execute('select count(*) from control.nexloop_approval_requests').fetchone() == (0,)


def test_a_loosening_waits_for_another_owner_and_runs_once(budgets, admin):
    """Q2/Q3/Q6: a new evolution budget and then a higher limit, each approved by another owner (never the requester), executed once by
    the requester's resubmission; the audit names the change, its direction and the approval."""
    c = budgets
    applied = with_policies(c)
    assert applied['version'] == 5
    assert admin.execute("select quorum,approver_roles,solo_mode,condition from control.nexloop_approval_policies where capability='goals.budget.loosen'").fetchone() == (
        1, ['owner'], 'allowed', None)
    body = loosening(admin, budget(kind='evolution', unit='CNY', limit='50.00'), 'evolution')
    first = c['loosen'](body)
    assert first.status_code == 200 and first.json()['status'] == 'waiting_approval', first.text
    assert row(admin, 'evolution') is None
    approval = first.json()['approval_id']
    assert c['decide'](c['owner_member'], approval).status_code == 409  # the requester never approves its own request
    assert c['decide'](c['second'], approval).json()['status'] == 'approved'
    executed = c['loosen'](body)
    assert executed.status_code == 200 and executed.json()['approval'] == 'consumed', executed.text
    assert row(admin, 'evolution')[:4] == ('CNY', '50.00', P0, P1)
    assert c['loosen'](body).json()['status'] == 'approval_consumed'  # once
    audit = admin.execute("select payload from runtime.audit_events where action='nexloop.goals.budget.loosen'").fetchone()[0]
    assert (audit['approval_id'], audit['direction'], audit['budget_kind'], audit['from']['limit_amount'], audit['to']['limit_amount']) == (
        approval, 'loosen', 'evolution', None, '50.00')
    # A higher limit names the row it raises; the summary shows the old and the new value.
    raised = loosening(admin, budget(kind='evolution', unit='CNY', limit='80.00'), 'evolution')
    second = c['loosen'](raised).json()['approval_id']
    summary = admin.execute('select body from control.nexloop_approval_requests where approval_id=%s', (second,)).fetchone()[0]
    assert (summary['from_limit_amount'], summary['limit_amount']) == ('50.00', '80.00')
    assert c['decide'](c['third'], second).json()['status'] == 'approved'
    assert c['loosen'](raised).json()['approval'] == 'consumed' and row(admin, 'evolution')[1] == '80.00'


def test_a_stale_view_never_opens_and_a_change_before_execution_consumes_the_approval_as_refused(budgets, admin):
    """§2.3: the body must name the current row when the approval opens (budget_changed, no approval) and when it executes (the
    approval is consumed as execution_refused:22023:budget_changed and the budget keeps the value set in between)."""
    c = budgets
    with_policies(c)
    approved_budget(admin, TENANT, 'model', 'USD', '100.00', P0, P1)
    current = loosening(admin, budget(limit='150.00'))
    for stale in ({**current, 'from_limit_amount': '90.00'}, {**current, 'expected_revision': current['expected_revision'] - 1},
                  {**current, 'from_unit': None, 'from_limit_amount': None, 'from_period_start': None, 'from_period_end': None, 'expected_revision': 0}):
        refused(c['loosen'](stale), 'budget_changed')
    assert admin.execute('select count(*) from control.nexloop_approval_requests').fetchone() == (0,)
    first = c['loosen'](current).json()['approval_id']
    assert c['decide'](c['second'], first).json()['status'] == 'approved'
    assert c['set'](budget(limit='60.00')).status_code == 200  # tightened in between
    spent = c['loosen'](current)
    assert spent.json()['status'] == 'execution_refused' and spent.json()['approval'] == 'consumed', spent.json()
    assert admin.execute('select result from control.nexloop_approval_consumptions where approval_id=%s', (first,)).fetchone() == (
        'execution_refused:22023:budget_changed',)
    assert row(admin)[1] == '60.00'


def test_without_its_policy_the_handler_refuses_and_an_approval_of_another_body_does_not_run_it(budgets, admin):
    """Fail closed, two layers: no goals.budget.loosen policy - the gate lets the request through and the handler refuses
    (approval_unavailable); an approved request of another body set as the in-flight approval does not run this body."""
    c = budgets
    body = loosening(admin, budget())
    refused(c['loosen'](body), 'approval_unavailable')
    assert row(admin) is None
    with_policies(c)
    other = c['loosen'](loosening(admin, budget(limit='1.00'))).json()['approval_id']
    assert c['decide'](c['second'], other).json()['status'] == 'approved'
    with pytest.raises(psycopg.Error, match='approval_unavailable'), admin.transaction():
        admin.execute("select set_config('nexloop.approval_in_flight',%s,true)", (other,))
        admin.execute('select control.nexloop_governed_loosen_budget(%s,%s,%s,%s,%s,%s)', (
            TENANT, 'real', c['owner_member']['principal_id'], 'human', 'intent-' + uuid.uuid4().hex[:12],
            Jsonb({'operation': 'loosen_budget', 'request_id': uuid.uuid4().hex, **body})))
    assert row(admin) is None


def test_the_entry_checks_the_fields_and_the_operator_holds_neither_budget_action(budgets, admin):
    c = budgets
    with_policies(c)
    body = loosening(admin, budget())
    for wrong in ({k: v for k, v in body.items() if k != 'expected_revision'}, {**body, 'expected_revision': -1}, {**body, 'expected_revision': '0'},
                  {**body, 'from_limit_amount': 1.5}, {**body, 'from_period_start': '2026-10-01T00:00:00+08:00'}, {**body, 'limit_amount': 50.0}):
        assert c['loosen'](wrong).status_code == 422, wrong
    for operation, payload in (('loosen_budget', body), ('set_budget', budget())):
        denied = c['call'](c['operator_member'], operation, payload)
        assert denied.status_code == 403 and denied.json()['code'] == 'forbidden', (operation, denied.text)
    # A partial view (some from_* set, others not) is refused by SQL too.
    refused(c['loosen']({**body, 'from_unit': 'USD'}), 'invalid')
    assert row(admin) is None and admin.execute('select count(*) from control.nexloop_approval_requests').fetchone() == (0,)


def test_the_registry_and_the_summary_fields(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    assert admin.execute("select operation,subject_rule from control.nexloop_governed_capabilities where capability='goals.budget.loosen'").fetchone() == (
        'loosen_budget', 'human')
    assert admin.execute("select control.nexloop_approval_summary_allowed('goals.budget.loosen')").fetchone()[0] == [
        'budget_kind', 'unit', 'from_limit_amount', 'limit_amount', 'from_period_start', 'from_period_end', 'period_start', 'period_end']
