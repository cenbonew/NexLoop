"""NX-058 slice D §2 (docs/design/nx058-owner-actions.md v2.5; ADR-039 R1 §6.7): NX-056 counts a vote only 24 hours after the voter's
current holding of an approver role began, read from the role history, for every policy. On PostgreSQL:
- control.nexloop_workbench_vote_eligible_from: the start of the current continuous holding of the union of roles over all the subject's
  principals, plus 24 hours; a change inside the union keeps the clock; stopping all (a suspension) and starting again restarts it; null
  when nothing is held now, for another world, or for a member without history (fail closed);
- the decision refuses a vote within the cooldown (approver_unauthorized), and the consumption no longer counts a vote whose voter
  stopped holding the role after deciding."""
import psycopg
import pytest

from support.tenants import TENANT_A
from test_nx056_approvals_store_pg import decide, open_request, refused, store  # noqa: F401
from test_workbench_read_pg import add_human


def history(admin, person, role, event, hours_ago, principal=None):
    admin.execute("""insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source,recorded_at)
        values(%s,%s,%s,%s,%s,'synthetic',clock_timestamp()-make_interval(hours=>%s))""",
                  (TENANT_A, person['subject_id'], principal or person['principal_id'], role, event, hours_ago))


def eligible_from_hours(admin, person, roles, world='real'):
    """Hours from now to the eligible time (negative: already eligible); None when the function returns null."""
    v = admin.execute("select extract(epoch from control.nexloop_workbench_vote_eligible_from(%s,%s,%s,%s)-clock_timestamp())/3600",
                      (TENANT_A, world, person['subject_id'], list(roles))).fetchone()[0]
    return None if v is None else round(float(v))


def test_the_eligible_time_follows_the_current_holding_of_the_union(store):
    f = store
    admin = f['admin']
    a = add_human(admin, 'cooldown')  # not configured: only the rows written below
    assert eligible_from_hours(admin, a, ['owner']) is None  # no history: never eligible (fail closed)
    history(admin, a, 'operator', 'start', 100)
    assert eligible_from_hours(admin, a, ['owner']) is None and eligible_from_hours(admin, a, ['owner', 'operator']) == -76
    # operator -> owner within the union {owner, operator}: the clock keeps running; for {owner} alone it starts at the promotion
    with admin.transaction():  # one handler transaction: stop the old role, start the new one
        history(admin, a, 'operator', 'stop', 50)
        history(admin, a, 'owner', 'start', 50)
    assert eligible_from_hours(admin, a, ['owner', 'operator']) == -76 and eligible_from_hours(admin, a, ['owner']) == -26
    # the same two rows in separate transactions are a gap: everything stopped, then started again
    history(admin, a, 'owner', 'stop', 45)
    history(admin, a, 'owner', 'start', 45)
    assert eligible_from_hours(admin, a, ['owner', 'operator']) == -21
    # a suspension stops everything; resuming restarts the clock
    history(admin, a, 'owner', 'stop', 10)
    assert eligible_from_hours(admin, a, ['owner', 'operator']) is None
    history(admin, a, 'owner', 'start', 2)
    assert eligible_from_hours(admin, a, ['owner']) == 22
    # a second principal of the same person holding the role across the suspension: the holding never stopped (from 45 hours ago)
    history(admin, a, 'owner', 'start', 40, principal='synthetic-staff-cooldown-second-principal')
    assert eligible_from_hours(admin, a, ['owner']) == -21
    assert eligible_from_hours(admin, a, ['owner'], world='sim:x') is None


def test_a_vote_within_the_cooldown_is_refused_and_a_stopped_voters_vote_no_longer_counts(store):
    f = store
    admin = f['admin']
    newcomer = dict(f['operator'])
    admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
        select tenant_id,manifest_version+1,roles_version,application_id,roles,jsonb_set(members,array[%s],'"owner"'),roles_digest,'synthetic-configurator'
        from control.nexloop_workbench_configurations where tenant_id=%s order by manifest_version desc limit 1""", (newcomer['principal_id'], TENANT_A))
    history(admin, newcomer, 'operator', 'stop', 1)
    history(admin, newcomer, 'owner', 'start', 1)  # promoted an hour ago: the owner policy's union is {owner}
    approval = open_request(f)['approval_id']
    refused('approver_unauthorized', decide, f, 'operator', approval)
    # A ripe approver decides; once it stops holding the role (suspended), its vote no longer counts at consumption.
    assert decide(f, 'approver', approval)['status'] == 'approved'
    votes = lambda: admin.execute('select control.nexloop_approval_valid_votes(%s,%s,%s)', (TENANT_A, 'real', approval)).fetchone()[0]  # noqa: E731
    assert votes() == [f['approver']['subject_id']]
    history(admin, f['approver'], 'owner', 'stop', 0)
    assert votes() == []


def test_no_application_role_calls_the_eligibility_function(store, pg):
    from psycopg.conninfo import make_conninfo
    for role in ('nexloop_api', 'nexloop_identity'):
        with psycopg.connect(make_conninfo(pg, user=role)) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("select control.nexloop_workbench_vote_eligible_from('t','real','s','{owner}')")
