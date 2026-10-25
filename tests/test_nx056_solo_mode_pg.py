"""NX-056 A7 (docs/design/unified-approvals.md v5 §15-17; 0332): the single-person mode on a clean catalog.

A tenant whose only eligible approver is the requester: the owner switches the mode on (append-only events), and a self vote counts
only while the mode takes effect - the switch on, exactly the requester eligible now, and the policy's eligibility timeline covering
the last 24 hours with at most one eligible subject throughout (S-C1 option A). It is computed again at consumption (S-I1); a second
eligible member ends it at once. The governed handlers and the gate are called as the governed entry calls them; admin seeds members
and configuration, and moves recorded timeline times back (triggers off for that statement) to stand for elapsed time. Synthetic ids.
"""
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A
from support.vote_cooldown import ripen_approvers
from test_workbench_read_pg import add_human

CAP = 'real_dispatch.set'
ACTION = 'eios:action:nexloop.real_dispatch.set:1'


def configure(admin, version, members):
    admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
        values(%s,%s,%s,'synthetic-workbench-app',%s,%s,%s,'synthetic-configurator')""",
                  (TENANT_A, version, version, Jsonb({'owner': [], 'operator': [], 'reviewer': []}),
                   Jsonb({p['principal_id']: role for p, role in members}), 'c' * 64))


def policy(admin, version, quorum=1, roles=('owner',), solo='allowed'):
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,solo_mode,published_by) values(%s,%s,1,null,%s,%s,86400,'{}',%s,'synthetic-config')""", (CAP, version, quorum, list(roles), solo))


@pytest.fixture
def solo(admin):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    people = {k: add_human(admin, 'solo-' + k) for k in ('owner', 'other', 'operator')}
    f = {'admin': admin, **people, 'config': 1}
    configure(admin, 1, [(people['owner'], 'owner'), (people['operator'], 'operator')])  # the owner is the only eligible approver
    ripen_approvers(admin, TENANT_A)  # NX-058 slice D §2: the vote cooldown is not what these tests are about
    policy(admin, 1)
    return f


def reconfigure(f, *members):
    f['config'] += 1
    configure(f['admin'], f['config'], members)


def age_timeline(f, hours):
    with f['admin'].transaction():
        f['admin'].execute('set local session_replication_role=replica')
        f['admin'].execute("update control.nexloop_approval_eligibility_events set recorded_at=recorded_at-make_interval(hours=>%s)", (hours,))


def timeline(f):
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    return f['admin'].execute('select policy_version,eligible_count from control.nexloop_approval_eligibility_events order by event_id').fetchall()


def switch(f, enabled, who='owner'):
    return f['admin'].execute('select control.nexloop_governed_set_approval_solo_mode(%s,%s,%s,%s,%s,%s)', (
        TENANT_A, 'real', f[who]['principal_id'], 'human', 'intent-' + uuid.uuid4().hex[:12],
        Jsonb({'operation': 'set_approval_solo_mode', 'request_id': uuid.uuid4().hex, 'enabled': enabled, 'reason': '公司只有我一人'}))).fetchone()[0]


def body(request_id='r-1', **extra):
    return {'operation': 'set_real_dispatch', 'request_id': request_id, 'scope_kind': 'tenant', 'enabled': True, 'data_mode': 'real', 'reason': '打开', **extra}


def open_request(f, request_id='r-1', **extra):
    return f['admin'].execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)',
                              (TENANT_A, 'real', CAP, ACTION, f['owner']['principal_id'], Jsonb(body(request_id, **extra)))).fetchone()[0]


def decide(f, approval, who='owner', decision='approve'):
    return f['admin'].execute('select control.nexloop_governed_approval_decide(%s,%s,%s,%s,%s,%s)', (
        TENANT_A, 'real', f[who]['principal_id'], 'human', 'intent-' + uuid.uuid4().hex[:12],
        Jsonb({'operation': 'decide_approval', 'request_id': uuid.uuid4().hex, 'approval_id': approval, 'decision': decision, 'reason': '核对过'}))).fetchone()[0]


def gate(f, request_id='r-2'):
    """The governed entry's question at the requester's resubmission (the same body, a new request id)."""
    return f['admin'].execute('select control.nexloop_approval_gate(%s,%s,%s,%s,%s,%s)',
                              (TENANT_A, 'real', CAP, ACTION, f['owner']['principal_id'], Jsonb(body(request_id)))).fetchone()[0]


def refused(code, fn, *args, **kw):
    with pytest.raises(psycopg.Error, match=code):
        fn(*args, **kw)


def test_a_single_person_tenant_self_approves_once_the_timeline_covers_24_hours(solo):
    f = solo
    assert timeline(f) == [(1, 1)]  # the policy's first record: one eligible approver
    approval = open_request(f)['approval_id']
    refused('self_approval', decide, f, approval)  # the switch is off
    assert switch(f, True)['enabled'] is True
    refused('self_approval', decide, f, approval)  # the timeline is younger than 24 hours (just deployed)
    age_timeline(f, 25)
    decided = decide(f, approval)
    assert decided['status'] == 'approved'
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    assert f['admin'].execute('select self_approved from control.nexloop_approval_decisions where approval_id=%s', (approval,)).fetchone() == (True,)
    audit = f['admin'].execute("select payload from runtime.audit_events where action='approval.decided'").fetchone()[0]
    assert audit['self_approved'] is True  # every self-approval can be filtered out of the audit
    assert gate(f) == {'proceed': True, 'approval_id': approval, 'approved_by': [f['owner']['subject_id']]}
    metrics = f['admin'].execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'approvals'", (TENANT_A,)).fetchone()[0]
    assert (metrics['solo_mode_enabled'], metrics['self_approvals_24h']) == (1, 1)


def test_switching_off_or_a_second_approver_ends_the_self_vote_at_consumption(solo):
    f = solo
    switch(f, True)
    age_timeline(f, 25)
    first = open_request(f)['approval_id']
    decide(f, first)
    switch(f, False)  # S-I1: the self vote no longer counts - the request is back to waiting at once (L6 review 11)
    off = gate(f)
    assert off['proceed'] is False and off['approval_id'] == first and off['status'] == 'waiting_approval', off
    refused('self_approval', decide, f, open_request(f, request_id='r-3', reason='另一个')['approval_id'])  # nor can a new one be made
    switch(f, True)
    decide(f, first)  # the mode takes effect again: the same person approves once more
    reconfigure(f, (f['owner'], 'owner'), (f['other'], 'owner'), (f['operator'], 'operator'))  # a second eligible approver joins
    assert gate(f, 'r-5')['reason'] == 'approver_no_longer_eligible'  # the self vote does not count any more
    refused('approver_unauthorized', decide, f, first, who='other')  # NX-058 slice D §2: the new approver votes 24 hours after joining
    ripen_approvers(f['admin'], TENANT_A)
    assert decide(f, first, who='other')['status'] == 'approved' and gate(f, 'r-6')['proceed'] is True  # the other person decides


def test_removing_the_other_approver_waits_24_hours_from_the_removal(solo):
    f = solo
    reconfigure(f, (f['owner'], 'owner'), (f['other'], 'owner'), (f['operator'], 'operator'))
    age_timeline(f, 48)  # two approvers for two days
    reconfigure(f, (f['owner'], 'owner'), (f['operator'], 'operator'))  # S-C1: the other's role removed now
    assert [count for _, count in timeline(f)] == [1, 2, 1]
    switch(f, True)
    approval = open_request(f)['approval_id']
    refused('self_approval', decide, f, approval)  # two approvers inside the window
    age_timeline(f, 23)
    refused('self_approval', decide, f, approval)
    age_timeline(f, 2)  # 25 hours after the removal
    assert decide(f, approval)['status'] == 'approved'
    reduced = f['admin'].execute("select payload from runtime.audit_events where action='approval.eligibility_reduced'").fetchone()[0]
    assert (reduced['from'], reduced['to']) == (2, 1)


def test_the_count_is_the_union_of_the_policys_approver_roles(solo):
    f = solo
    policy(f['admin'], 2, roles=('owner', 'operator'))  # T-C1: the owner holds one role, the operator person the other
    assert timeline(f)[-1] == (2, 2)
    switch(f, True)
    age_timeline(f, 25)
    approval = open_request(f)['approval_id']
    refused('self_approval', decide, f, approval)  # two eligible under the union
    assert decide(f, approval, who='operator')['status'] == 'approved'


def test_quorum_two_and_not_applicable_policies_are_never_satisfied_alone(solo):
    f = solo
    switch(f, True)
    age_timeline(f, 25)
    policy(f['admin'], 2, quorum=2)
    refused('quorum_unreachable_in_solo_mode', open_request, f)
    policy(f['admin'], 3, solo='not_applicable')
    age_timeline(f, 25)
    refused('quorum_unreachable_in_solo_mode', open_request, f, request_id='r-9')


def test_only_the_owner_switches_and_the_records_are_append_only(solo):
    f = solo
    refused('owner_only', switch, f, True, who='operator')
    switch(f, True)
    admin = f['admin']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    for table in ('control.nexloop_approval_eligibility_events', 'control.nexloop_approval_solo_mode_events'):
        for sql in (f'update {table} set tenant_id=tenant_id', f'delete from {table}', f'truncate {table}'):
            with pytest.raises(psycopg.Error), admin.transaction():
                admin.execute(sql)
        for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_configurator'):
            assert admin.execute('select has_table_privilege(%s,%s,%s)', (role, table, 'insert')).fetchone() == (False,)


def test_a_decision_waits_for_the_tenant_lock_a_member_change_holds(solo, pg):
    """Lock order (design §16): a member change and a decision of one tenant never interleave."""
    f = solo
    approval = open_request(f)['approval_id']
    with psycopg.connect(pg, autocommit=False) as other:
        other.execute('select control.nexloop_approval_tenant_lock(%s)', (TENANT_A,))  # as a member change's trigger takes it
        with pytest.raises(psycopg.errors.LockNotAvailable), f['admin'].transaction():
            f['admin'].execute("set local lock_timeout='200ms'")
            decide(f, approval, who='operator')
        other.rollback()


def test_a_new_sole_approver_waits_24_hours_even_when_the_count_never_exceeded_one(solo):
    """L6 review 11: the only approver A is removed and B (the requester) becomes the only one - the count stays 1, the subject changes;
    B approves alone only after 24 hours of being that one approver."""
    f = solo
    reconfigure(f, (f['other'], 'owner'), (f['owner'], 'operator'), (f['operator'], 'operator'))  # A (other) alone for two days
    age_timeline(f, 48)
    reconfigure(f, (f['owner'], 'owner'), (f['operator'], 'operator'))  # A removed, B (owner) the one approver now
    assert [count for _, count in timeline(f)][-2:] == [1, 1]  # the count never went above one; the digest changed
    switch(f, True)
    approval = open_request(f)['approval_id']
    refused('self_approval', decide, f, approval)
    age_timeline(f, 23)
    refused('self_approval', decide, f, approval)
    age_timeline(f, 2)
    assert decide(f, approval)['status'] == 'approved'


def test_switching_off_reopens_self_approved_requests_at_once(solo):
    f = solo
    switch(f, True)
    age_timeline(f, 25)
    approval = open_request(f)['approval_id']
    decide(f, approval)
    switch(f, False)
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    assert f['admin'].execute('select status from control.nexloop_approval_requests where approval_id=%s', (approval,)).fetchone() == ('waiting_approval',)
    reopened = f['admin'].execute("select payload->>'reason' from runtime.audit_events where action='approval.reopened'").fetchone()
    assert reopened == ('solo_mode_off',)


def test_a_direct_member_change_and_a_member_action_in_opposite_order_never_deadlock(solo, pg):
    """L6 review 11: an approved member Action holds the tenant lock and then changes a member row; a direct change (trusted
    configuration, restore) of that row takes the tenant lock before the row (BEFORE STATEMENT), so the two only queue."""
    import threading
    import time
    f = solo
    member = f['operator']['principal_id']
    errors, done = [], []
    change = """update control.nexloop_browser_memberships set payload=jsonb_set(payload,'{revision}',to_jsonb((payload->>'revision')::integer+1))
        where tenant_id=%s and principal_id=%s"""
    with psycopg.connect(pg, autocommit=False) as action, psycopg.connect(pg, autocommit=False) as direct:
        action.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        action.execute('select control.nexloop_approval_tenant_lock(%s)', (TENANT_A,))  # the Action path: tenant lock first

        def direct_change():
            try:
                direct.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
                direct.execute(change, (TENANT_A, member))  # waits for the tenant lock before it locks the row
                direct.commit()
                done.append('direct')
            except psycopg.Error as error:
                errors.append(error.sqlstate)
                direct.rollback()
        thread = threading.Thread(target=direct_change)
        thread.start()
        time.sleep(1.5)  # longer than deadlock_timeout: a cycle would have been detected by now
        action.execute(change, (TENANT_A, member))  # then its row: never held by the waiting direct change
        action.commit()
        done.append('action')
        thread.join(10)
    assert errors == [] and sorted(done) == ['action', 'direct']  # no 40P01, both committed
