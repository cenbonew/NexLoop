"""NX-058 slice E2 (docs/design/nx058-member-erasure.md v2.5 §2.5; owner Q-A "A+90天", E6 = A) on PostgreSQL: 90 days after a staff
erasure, the plaintext username and email in the member's failed-login result snapshots become the account's erased- pseudonym - nothing
before 90 days, only those two keys, once per subject, for this tenant only; redone after a restore that lost the clearance; run by the
retention keeper's tick through its signed port (a failure is counted, never stops the tick). Synthetic members only."""
import uuid

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B
from test_workbench_read_pg import add_human

SNAPSHOT_KEYS = {'username', 'verified_email', 'status', 'revision', 'failed_attempts'}


def erased_member(admin, tenant, key, days_ago):
    """A member as slice E1 leaves it (account pseudonymised, ledger row) erased days_ago, with one failed-login event holding the old
    plaintext username and email in its result snapshot, and one event without a snapshot."""
    person = add_human(admin, key) if tenant == TENANT_A else None
    subject = person['subject_id'] if person else f'synthetic-staff-{key}'
    admin.execute("select set_config('eios.tenant_id',%s,false)", (tenant,))
    if person is None:
        admin.execute("insert into control.nexloop_browser_subjects values(%s,%s) on conflict do nothing",
                      (subject, Jsonb({'subject_id': subject, 'kind': 'human', 'status': 'disabled', 'revision': 2})))
    account = f'synthetic-account-{key}-{tenant}'
    pseudonym = 'erased-' + uuid.uuid4().hex[:16]
    if person is None:
        plain = f'staff-{key}'  # inserted plain, then pseudonymised as the erasure does (no new account may start erased-)
        admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,null,%s,%s)', (tenant, account, subject, plain, [], Jsonb({
            'tenant_id': tenant, 'local_account_id': account, 'subject_id': subject, 'username': plain, 'status': 'disabled', 'revision': 2, 'session_epoch': 2})))
        admin.execute("update control.nexloop_browser_accounts set username=%s,payload=payload||jsonb_build_object('username',%s::text) where tenant_id=%s and local_account_id=%s",
                      (pseudonym, pseudonym, tenant, account))
    else:
        admin.execute("""update control.nexloop_browser_accounts set username=%s,payload=payload||jsonb_build_object('username',%s::text,'status','disabled')
            where subject_id=%s""", (pseudonym, pseudonym, subject))
    snapshot = {'username': f'staff-{key}', 'verified_email': f'staff-{key}@example.invalid', 'status': 'active', 'revision': 3, 'failed_attempts': 1}
    for details in ({'failed_attempts': 1, 'lockout_level': 0, 'account_id': account, 'account_revision': 3, 'result_snapshot': snapshot},
                    {'failed_attempts': 2, 'lockout_level': 0}):
        admin.execute("""insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
            values(%s,%s,'local_authentication','failure','nexloop_identity',%s,'trace',%s,clock_timestamp()-make_interval(days=>%s))""",
                      (tenant, subject, uuid.uuid4().hex, Jsonb(details), (days_ago or 0) + 1))
    if days_ago is not None:
        admin.execute("insert into control.nexloop_workbench_member_erasures(tenant_id,subject_id,principals,erased_at) values(%s,%s,%s,clock_timestamp()-make_interval(days=>%s))",
                      (tenant, subject, [f'{subject}-principal'], days_ago))
    return {'subject': subject, 'pseudonym': pseudonym, 'snapshot': snapshot, 'tenant': tenant}


def snapshots(admin, member):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (member['tenant'],))
    return [d for (d,) in admin.execute("select details->'result_snapshot' from control.nexloop_browser_login_events where tenant_id=%s and subject_id=%s order by created_at",
                                        (member['tenant'], member['subject'])).fetchall()]


def clear(admin, tenant=TENANT_A, limit=100):
    return admin.execute('select control.nexloop_workbench_member_login_clear(%s,%s)', (tenant, limit)).fetchone()[0]


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for tenant in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    return admin


def test_only_the_two_keys_of_members_erased_90_days_ago_change_once(db):
    admin = db
    due = erased_member(admin, TENANT_A, 'due', 91)
    recent = erased_member(admin, TENANT_A, 'recent', 30)
    never = erased_member(admin, TENANT_A, 'never', None)
    elsewhere = erased_member(admin, TENANT_B, 'elsewhere', 120)
    assert clear(admin) == {'subjects': 1, 'events': 1}
    cleared, plain = snapshots(admin, due)
    assert plain is None and set(cleared) == SNAPSHOT_KEYS
    assert (cleared['username'], cleared['verified_email']) == (due['pseudonym'], due['pseudonym'] + '@erased.invalid')
    assert {k: v for k, v in cleared.items() if k not in ('username', 'verified_email')} == {k: v for k, v in due['snapshot'].items() if k not in ('username', 'verified_email')}
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    assert admin.execute("select details->>'account_id',details->>'account_revision' from control.nexloop_browser_login_events where subject_id=%s and details ? 'result_snapshot'",
                         (due['subject'],)).fetchone() == (f'synthetic-account-due-{TENANT_A}', '3')
    for member in (recent, never, elsewhere):
        assert snapshots(admin, member)[0] == member['snapshot'], member['subject']
    assert clear(admin) == {'subjects': 0, 'events': 0}  # once per subject
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    assert admin.execute('select subject_id,events from control.nexloop_workbench_member_login_clearances').fetchall() == [(due['subject'], 1)]
    assert clear(admin, TENANT_B) == {'subjects': 1, 'events': 1}  # the other tenant on its own call
    assert snapshots(admin, elsewhere)[0]['username'] == elsewhere['pseudonym']


def test_a_restore_that_lost_the_clearance_is_redone_by_the_next_run(db):
    """A database restored from before the clearing has the plaintext snapshots back and no clearance row; the erasure ledger (replayed
    with its original time, slice E1) makes the subject due again and the next run clears it again."""
    admin = db
    due = erased_member(admin, TENANT_A, 'restored', 100)
    assert clear(admin)['subjects'] == 1
    with admin.transaction():  # the restored state (replica mode skips the append-only trigger; test only)
        admin.execute('set local session_replication_role=replica')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute('delete from control.nexloop_workbench_member_login_clearances')
        admin.execute("update control.nexloop_browser_login_events set details=jsonb_set(details,'{result_snapshot}',%s) where subject_id=%s and details ? 'result_snapshot'",
                      (Jsonb(due['snapshot']), due['subject']))
    assert snapshots(admin, due)[0]['username'] == due['snapshot']['username']
    assert clear(admin) == {'subjects': 1, 'events': 1}
    assert snapshots(admin, due)[0]['username'] == due['pseudonym']


def test_the_clearing_is_bounded_and_oldest_first(db):
    admin = db
    members = [erased_member(admin, TENANT_A, f'batch-{n}', 120 - n) for n in range(3)]
    assert clear(admin, limit=2) == {'subjects': 2, 'events': 2}
    assert [snapshots(admin, m)[0]['username'] == m['pseudonym'] for m in members] == [True, True, False]
    assert clear(admin, limit=2) == {'subjects': 1, 'events': 1}


@pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
def test_the_retention_keeper_tick_runs_the_clearing(erasing, admin):
    from test_workbench_read_pg import TENANT
    c = erasing
    assert TENANT == TENANT_A
    due = erased_member(admin, TENANT_A, 'keeper', 95)
    summary = keeper(c).run_once()
    assert (summary['member_logins_cleared'], summary['member_login_events'], summary['member_login_failed']) == (1, 1, 0)
    assert snapshots(admin, due)[0]['username'] == due['pseudonym']
    broken = keeper(c)
    broken.port.member_login_clear = lambda: (_ for _ in ()).throw(RuntimeError('synthetic'))
    assert broken.run_once()['member_login_failed'] == 1  # counted; the tick goes on
    assert not admin.execute("select has_function_privilege('nexloop_domain_worker','control.nexloop_workbench_member_login_clear(text,integer)','execute')").fetchone()[0]


from test_erasure_pg import erasing, keeper  # noqa: E402,F401
from commitment_fixture import commitments  # noqa: E402,F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: E402,F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: E402,F401
