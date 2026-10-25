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
from test_nx056_approval_gate_pg import gated  # noqa: F401
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401


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


def test_the_backfill_starts_configured_active_members_once(store):
    """The deployment start (0339 calls it once): a configured active member without an open holding gets one start; an open holding, a
    suspended membership and a second call write nothing."""
    from psycopg.types.json import Jsonb
    f = store
    admin = f['admin']
    fresh, paused = add_human(admin, 'backfill-fresh'), add_human(admin, 'backfill-paused')
    admin.execute("update control.nexloop_browser_memberships set payload=jsonb_set(payload,'{status}','\"suspended\"') where principal_id=%s", (paused['principal_id'],))
    admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
        select tenant_id,manifest_version+1,roles_version,application_id,roles,members||%s,roles_digest,'synthetic-configurator'
        from control.nexloop_workbench_configurations where tenant_id=%s order by manifest_version desc limit 1""",
                  (Jsonb({fresh['principal_id']: 'owner', paused['principal_id']: 'owner'}), TENANT_A))
    rows = lambda person: admin.execute("select role,event,source from control.nexloop_workbench_role_history where subject_id=%s order by entry_id",  # noqa: E731
                                        (person['subject_id'],)).fetchall()
    assert admin.execute("select control.nexloop_workbench_role_history_backfill('deployment')").fetchone()[0] == 1
    assert rows(fresh) == [('owner', 'start', 'deployment')] and rows(paused) == []
    assert rows(f['approver'])[-1][2] == 'synthetic-ripened'  # an open holding: untouched
    assert admin.execute("select control.nexloop_workbench_role_history_backfill('deployment')").fetchone()[0] == 0
    assert 22 <= eligible_from_hours(admin, fresh, ['owner']) <= 24  # 24 hours from now


def age_history(admin, person, hours):
    """Move every role history row of a person back (the append-only trigger is a replica-mode exception for the test only)."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update control.nexloop_workbench_role_history set recorded_at=recorded_at-make_interval(hours=>%s) where subject_id=%s",
                      (hours, person['subject_id']))


def sql_decide(admin, member, approval, decision='approve'):
    import uuid
    from psycopg.types.json import Jsonb
    from commitment_fixture import TENANT
    return admin.execute('select control.nexloop_governed_approval_decide(%s,%s,%s,%s,%s,%s)', (
        TENANT, 'real', member['principal_id'], 'human', 'intent-' + uuid.uuid4().hex[:12],
        Jsonb({'operation': 'decide_approval', 'request_id': uuid.uuid4().hex, 'approval_id': approval, 'decision': decision, 'reason': '核对过'}))).fetchone()[0]


def test_a_member_suspended_and_resumed_by_the_handler_votes_24_hours_later_and_then_the_request_runs(gated, admin):
    """Through the actual app: an operator (an approver role of this policy version) suspended and resumed by the slice C handler cannot
    approve nor reject within 24 hours; once the holding is 24 hours old its approval counts and the requester's resubmission executes."""
    from commercial_fixture import publish_registry_actions
    from test_nx056_approval_gate_pg import consumptions, tenant_switch
    from test_workbench_actions_http import act
    from test_workbench_read_pg import workbench_login
    from test_browser_http import ORIGIN
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import create_app
    c = gated
    publish_registry_actions(admin, ('nexloop.workbench_member.suspend', 'nexloop.workbench_member.resume'))
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,published_by) values('real_dispatch.set',2,1,%s,1,'{owner,operator}',86400,'{scope_kind,enabled}','synthetic-config')""",
                  (__import__('psycopg').types.json.Jsonb({'field': 'enabled', 'equals': True}),))
    operator = c['operator_member']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        body = {'target_principal': operator['principal_id'], 'reason': 'synthetic leave'}
        assert act(client, csrf, 'suspend_workbench_member', body).status_code == 200
        assert act(client, csrf, 'resume_workbench_member', body).status_code == 200
    approval = c['request']().json()['approval_id']
    refused('approver_unauthorized', sql_decide, admin, operator, approval)
    refused('approver_unauthorized', sql_decide, admin, operator, approval, 'reject')  # rejecting is a vote too
    age_history(admin, operator, 25)
    assert sql_decide(admin, operator, approval)['status'] == 'approved'
    executed = c['request']()
    assert executed.json()['approval'] == 'consumed' and tenant_switch(admin) is True and consumptions(admin) == [('executed',)], executed.json()


def test_an_owner_added_by_the_configurator_votes_24_hours_later(gated, admin):
    from nexloop_eios.workbench_roles import apply_members, compile_members
    from test_workbench_read_pg import ROLES, members_file, write_facts
    c = gated
    third = add_human(admin, 'third-owner')
    members = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'), (c['second'], 'owner'), (third, 'owner'))
    apply_members(ROLES, members, database_url_file=c['configurator'])
    write_facts(admin, compile_members(ROLES, members))
    approval = c['request']().json()['approval_id']
    refused('approver_unauthorized', sql_decide, admin, third, approval)
    assert sql_decide(admin, c['second'], approval)['status'] == 'approved'  # the ripe owner decides
