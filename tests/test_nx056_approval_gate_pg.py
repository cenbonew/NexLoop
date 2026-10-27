"""NX-056 A2 (docs/design/unified-approvals.md v5 §2, §4): the governed entry under an approval policy, end to end.

Production create_app over HTTP, members applied by the real configurator (test_real_dispatch_switch_pg's `switching`), a second owner
as approver, the policy "opening real dispatch (enabled=true) needs one other owner's approval". The request waits; the approver decides
through the governed approvals.decide; the requester executes by resubmitting the same body (a new request id); the handler runs once.
Synthetic data only.
"""
import threading

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from nexloop_eios.http_api import create_app
from nexloop_eios.workbench_roles import apply_members, compile_members
from support.vote_cooldown import ripen_approvers
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import owner_sql, switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import ROLES, add_human, members_file, workbench_login, write_facts

PLAN = pytest.mark.parametrize("execution_plan", ["commitment-service"], indirect=True)
OPEN = {'scope_kind': 'tenant', 'enabled': True, 'data_mode': 'real', 'action_type': None, 'reason': 'synthetic opening'}


@pytest.fixture
def gated(switching, admin):
    c = switching
    second = add_human(admin, 'second')
    members = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'), (second, 'owner'))
    apply_members(ROLES, members, database_url_file=c['configurator'])
    write_facts(admin, compile_members(ROLES, members))
    ripen_approvers(admin, TENANT)  # NX-058 slice D §2: these members vote at once
    publish_registry_actions(admin, ('nexloop.approvals.decide', 'nexloop.approvals.cancel'))
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,published_by) values('real_dispatch.set',1,1,%s,1,'{owner}',86400,'{scope_kind,enabled}','synthetic-config')""",
                  (Jsonb({'field': 'enabled', 'equals': True}),))
    c['second'] = second
    c['members'] = members

    def call(member, operation, body):
        with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
            csrf = workbench_login(client, member).json()['csrf_token']
            return act(client, csrf, operation, body)
    c['call'] = call
    c['request'] = lambda member=None, **body: call(member or c['owner_member'], 'set_real_dispatch', {**OPEN, **body})
    c['decide'] = lambda approval, member=None, decision='approve': call(member or second, 'decide_approval',
                                                                          {'approval_id': approval, 'decision': decision, 'reason': 'synthetic check'})
    return c


def tenant_switch(admin):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    return owner_sql(admin, "select (select enabled from control.nexloop_real_dispatch_switches where tenant_id=%s and world='real' and scope_kind='tenant')", (TENANT,))


def claim_status(admin, response):
    """The action claim's terminal outcome of one workbench call (the outcome id is derived from its intent)."""
    return admin.execute("select claim->'terminal_outcome'->>'status' from runtime.nexloop_action_claims where claim->'terminal_outcome'->>'outcome_id'=%s",
                         (response.json()['outcome_id'] if hasattr(response, 'json') else response['outcome_id'],)).fetchone()[0]


def age(admin, approval, hours):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute('set local session_replication_role=replica')
        admin.execute('update control.nexloop_approval_requests set requested_at=requested_at-make_interval(hours=>%s),due_by=due_by-make_interval(hours=>%s) '
                      'where approval_id=%s', (hours, hours, approval))


def status(admin, approval):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    return admin.execute('select status from control.nexloop_approval_requests where approval_id=%s', (approval,)).fetchone()[0]


def consumptions(admin):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    return admin.execute('select result from control.nexloop_approval_consumptions order by consumed_at').fetchall()


@PLAN
def test_opening_waits_for_another_owner_and_runs_once_on_the_requesters_resubmission(gated, admin):
    c = gated
    first = c['request']()
    assert first.status_code == 200 and first.json()['status'] == 'waiting_approval' and first.json()['approval'] == 'required'
    approval = first.json()['approval_id']
    assert tenant_switch(admin) is None  # nothing executed
    assert c['request']().json()['approval_id'] == approval  # resubmitting before the decision answers the same waiting request
    assert c['decide'](approval, member=c['owner_member']).status_code == 409  # never the requester
    assert c['decide'](approval).json()['status'] == 'approved'
    executed = c['request']()
    assert executed.status_code == 200 and executed.json()['approval'] == 'consumed' and executed.json()['approval_id'] == approval
    assert tenant_switch(admin) is True and consumptions(admin) == [('executed',)], executed.json()
    audit = admin.execute("select payload from runtime.audit_events where action='approval.consumed'").fetchone()[0]
    assert audit['actor'] == c['owner_member']['subject_id'] and audit['approved_by'] == [c['second']['subject_id']]
    # Consumed once: the same body again within the hour answers that consumption, never a second execution nor a new request.
    again = c['request']()
    assert again.json()['status'] == 'approval_consumed' and again.json()['approval_id'] == approval and again.json()['result'] == 'executed'
    assert consumptions(admin) == [('executed',)] and claim_status(admin, again) == 'permanent_failure'
    assert claim_status(admin, executed) == 'succeeded' and claim_status(admin, first) == 'permanent_failure'  # waiting is no success
    # Closing is outside the policy's condition: it runs at once.
    assert c['request'](enabled=False).json().get('approval') is None and tenant_switch(admin) is False


@PLAN
def test_an_approver_who_lost_the_role_no_longer_counts(gated, admin):
    c = gated
    approval = c['request']().json()['approval_id']
    c['decide'](approval)
    demoted = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'), (c['second'], 'operator'))
    facts = compile_members(ROLES, demoted)
    keep = {tuple(f['key'] if isinstance(f, dict) else f[1]) for f in facts}
    for kind, key in admin.execute('select fact_kind,entity_key from authz.nexloop_authority_facts where tenant_id=%s and payload::text like %s',
                                   (TENANT, f"%{c['second']['principal_id']}%")).fetchall():
        if tuple(key) not in keep:
            admin.execute('delete from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s', (TENANT, kind, key))
    write_facts(admin, facts)
    apply_members(ROLES, demoted, database_url_file=c['configurator'])
    reopened = c['request']()
    assert reopened.json() == {**reopened.json(), 'status': 'waiting_approval', 'reason': 'approver_no_longer_eligible', 'approval_id': approval}
    assert tenant_switch(admin) is None and consumptions(admin) == []


@PLAN
def test_a_refusing_handler_consumes_the_approval_and_a_pause_stops_the_execution(gated, admin):
    c = gated
    approval = c['request'](data_mode=None).json()['approval_id']  # the handler refuses a tenant scope without its data mode
    c['decide'](approval)
    refused = c['request'](data_mode=None)
    assert refused.json()['status'] == 'execution_refused' and refused.json()['approval'] == 'consumed'
    assert consumptions(admin) == [(f"execution_refused:{refused.json()['code']}",)] and tenant_switch(admin) is None
    assert claim_status(admin, refused) == 'permanent_failure'
    again = c['request'](data_mode=None).json()  # never a retry of the consumed one
    assert again['status'] == 'approval_consumed' and again['result'] == consumptions(admin)[0][0]
    # A paused tenant stops the execution itself (L6 I1): refused, nothing consumed.
    other = c['request']().json()['approval_id']
    c['decide'](other)
    admin.execute("insert into control.nexloop_control_scopes(tenant_id,world,scope_kind,scope_ref,paused,revision,reason) values(%s,'real','tenant','*',true,1000,'synthetic pause')",
                  (TENANT,))
    paused = c['request']()
    assert paused.status_code == 503 and tenant_switch(admin) is None
    assert len(consumptions(admin)) == 1


@PLAN
def test_a_transient_error_consumes_nothing_and_concurrent_executions_run_once(gated, admin, pg):
    c = gated
    approval = c['request']().json()['approval_id']
    c['decide'](approval)
    admin.execute("alter role nexloop_api set lock_timeout='300ms'")  # this test's own cluster (the pg fixture is per test)
    try:
        with psycopg.connect(pg, autocommit=False) as blocker:
            blocker.execute('lock table control.nexloop_real_dispatch_switches in access exclusive mode')
            transient = c['request']()
            blocker.rollback()
        assert transient.status_code >= 500 and consumptions(admin) == []  # 55P03 re-raised (L6 N-2a)
    finally:
        admin.execute('alter role nexloop_api reset lock_timeout')
    results = []
    threads = [threading.Thread(target=lambda: results.append(c['request']().json() or {})) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # The loser waits on the row lock and then answers the winner's consumption: no second execution and no new request.
    assert sorted(r.get('approval') or r.get('status') or str(r) for r in results) == ['approval_consumed', 'consumed'], results
    assert consumptions(admin) == [('executed',)] and tenant_switch(admin) is True  # the handler ran once (L6 N-1)


@PLAN
def test_an_approval_past_due_ends_at_execution_and_the_body_opens_anew(gated, admin):
    c = gated
    approval = c['request']().json()['approval_id']
    c['decide'](approval)
    age(admin, approval, 25)
    late = c['request']().json()
    assert late['status'] == 'waiting_approval' and late['approval'] == 'required' and late['approval_id'] != approval  # never blocked
    assert status(admin, approval) == 'expired' and tenant_switch(admin) is None and consumptions(admin) == []
    reason = admin.execute("select payload->>'reason' from runtime.audit_events where action='approval.expired'").fetchone()
    assert reason == ('due_by_passed',)


@PLAN
def test_a_new_policy_version_answers_policy_changed_once(gated, admin):
    c = gated
    approval = c['request']().json()['approval_id']
    c['decide'](approval)
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,published_by) values('real_dispatch.set',2,1,%s,1,'{owner}',86400,'{scope_kind,enabled}','synthetic-config')""",
                  (Jsonb({'field': 'enabled', 'equals': True}),))
    changed = c['request']().json()
    assert changed == {**changed, 'status': 'policy_changed', 'approval_id': approval, 'policy_version': 2}
    assert status(admin, approval) == 'expired' and tenant_switch(admin) is None
    renewed = c['request']().json()
    assert renewed['status'] == 'waiting_approval' and renewed['approval_id'] != approval


@PLAN
def test_a_body_without_the_condition_field_needs_approval(gated, admin):
    c = gated
    body = {k: v for k, v in OPEN.items() if k != 'enabled'}
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        response = act(client, csrf, 'set_real_dispatch', body)
    # The workbench refuses a body without 'enabled' itself (422) or the gate holds it for approval; it never runs unapproved.
    assert response.status_code == 422 or response.json()['approval'] == 'required'
    assert tenant_switch(admin) is None
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    gate = admin.execute("select control.nexloop_approval_gate(%s,'real','real_dispatch.set','eios:action:nexloop.real_dispatch.set:1',%s,%s)",
                         (TENANT, c['owner_member']['principal_id'], Jsonb({'request_id': 'g-1', 'operation': 'set_real_dispatch', 'scope_kind': 'tenant'}))).fetchone()[0]
    assert gate['proceed'] is False and gate['approval'] == 'required'


@PLAN
def test_another_requesters_waiting_request_and_rejection_never_answer_or_block_mine(gated, admin):
    c = gated
    mine = c['request']().json()['approval_id']
    c['decide'](mine, decision='reject')
    assert c['request']().status_code == 409  # my own rejection: the hour's cooldown
    theirs = c['request'](member=c['second']).json()
    assert theirs['status'] == 'waiting_approval' and theirs['approval_id'] != mine  # another person, the same body: neither cooldown nor reuse


@PLAN
def test_a_waiting_request_under_an_older_policy_ends_with_policy_changed_and_a_consumed_one_never_moves(gated, admin):
    c = gated
    waiting = c['request']().json()['approval_id']
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,published_by) values('real_dispatch.set',2,1,%s,1,'{owner}',86400,'{scope_kind,enabled}','synthetic-config')""",
                  (Jsonb({'field': 'enabled', 'equals': True}),))
    assert c['request']().json()['status'] == 'policy_changed' and status(admin, waiting) == 'expired'
    current = c['request']().json()['approval_id']
    c['decide'](current)
    assert c['request']().json()['approval'] == 'consumed'
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    with pytest.raises(psycopg.Error, match='consumed'), admin.transaction():
        admin.execute("update control.nexloop_approval_requests set status='waiting_approval',revision=revision+1 where approval_id=%s", (current,))


@PLAN
def test_a_requester_who_lost_the_role_is_refused_by_the_entry_and_consumes_nothing(gated, admin):
    c = gated
    approval = c['request']().json()['approval_id']
    c['decide'](approval)
    demoted = members_file((c['owner_member'], 'operator'), (c['operator_member'], 'operator'), (c['second'], 'owner'))
    facts = compile_members(ROLES, demoted)
    keep = {tuple(f['key'] if isinstance(f, dict) else f[1]) for f in facts}
    for kind, key in admin.execute('select fact_kind,entity_key from authz.nexloop_authority_facts where tenant_id=%s and payload::text like %s',
                                   (TENANT, f"%{c['owner_member']['principal_id']}%")).fetchall():
        if tuple(key) not in keep:
            admin.execute('delete from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s', (TENANT, kind, key))
    write_facts(admin, facts)
    apply_members(ROLES, demoted, database_url_file=c['configurator'])
    assert c['request']().status_code == 403  # the normal entry refuses before any approval is looked at
    # C-B1 (ADR-039 R3 §8.5): the configurator's member apply (a role's stop) ended the old approval at once - it can never execute
    assert status(admin, approval) == 'expired' and consumptions(admin) == [] and tenant_switch(admin) is None


@PLAN
def test_a_changed_body_never_consumes_the_approval_of_another(gated, admin):
    c = gated
    approval = c['request']().json()['approval_id']
    c['decide'](approval)
    other = c['request'](reason='another reason').json()  # one field differs: another digest
    assert other['status'] == 'waiting_approval' and other['approval_id'] != approval
    assert status(admin, approval) == 'approved' and consumptions(admin) == [] and tenant_switch(admin) is None


@PLAN
def test_approval_refusals_keep_their_status_and_name_only_the_fixed_reason_code(gated, admin):
    """Workbench reason codes (NX-056): an approval refusal answers with its unchanged status (409 for a state refusal, 403 for an
    authority refusal) and details.reason = the fixed code; nothing else of the database message reaches the client."""
    c = gated
    mine = c['request']().json()['approval_id']
    own = c['decide'](mine, member=c['owner_member'])                      # the requester's own vote
    assert own.status_code == 409 and own.json()['details'] == {'reason': 'self_approval'}, own.text
    rejected = c['decide'](mine, decision='reject')
    assert rejected.status_code == 200, rejected.text
    again = c['request']()                                                  # the same request within the hour: cooldown, still 409
    assert again.status_code == 409 and again.json()['code'] == 'not_allowed_in_state' and again.json()['details'] == {'reason': 'cooldown'}
    assert set(again.json()) == {'code', 'message', 'trace_id', 'retryable', 'details'} and 'approval' not in again.json()['message']
    theirs = c['request'](member=c['second']).json()['approval_id']
    withdraw = c['call'](c['owner_member'], 'cancel_approval', {'approval_id': theirs, 'reason': 'not mine'})
    assert (withdraw.status_code, withdraw.json()['details']) == (403, {'reason': 'not_requester'}), withdraw.text


@PLAN
def test_the_view_says_consumed_once_executed_and_to_execute_drops_it(gated, admin):
    """0368 (day 1 drill, L6 S1): the status stays 'approved' after execution; every row carries consumed / consumed_at from the
    consumption record, and to_execute no longer lists the executed approval."""
    c = gated

    def view(member):
        with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
            workbench_login(client, member)
            got = client.get('/api/v1/workbench/approvals', headers={'Origin': ORIGIN})
            assert got.status_code == 200, got.text
            return got.json()
    approval = c['request']().json()['approval_id']
    for row in view(c['second'])['to_decide']:
        assert (row['consumed'], row['consumed_at']) == (False, None)
    c['decide'](approval)
    before = view(c['owner_member'])
    mine = {r['approval_id']: r for r in before['mine']}
    assert (mine[approval]['status'], mine[approval]['consumed'], mine[approval]['consumed_at']) == ('approved', False, None)
    assert [r['approval_id'] for r in before['to_execute']] == [approval] and before['to_execute'][0]['consumed'] is False
    assert c['request']().json()['approval'] == 'consumed'
    after = view(c['owner_member'])
    row = {r['approval_id']: r for r in after['mine']}[approval]
    assert row['status'] == 'approved' and row['consumed'] is True and row['result'] == 'executed' and row['consumed_at'] is not None
    assert approval not in [r['approval_id'] for r in after['to_execute']]


@PLAN
def test_consumed_belongs_to_the_rows_own_tenant_and_world(gated, admin):
    """0368 (L6 S1 on 8e7e4c1e): the consumption looked up for a row is that row's own tenant and world. A request executed in
    tenant A / world real; the same approval_id as a row of tenant B, and as a row of A's world 'test', is not consumed - even when
    the lookup runs with A's tenant setting, where row security alone would let A's consumption through."""
    from support.tenants import TENANT_B
    c = gated
    approval = c['request']().json()['approval_id']
    c['decide'](approval)
    assert c['request']().json()['approval'] == 'consumed'

    def row_as(tenant, world):
        with admin.transaction():
            admin.execute('set local role nexloop_owner')
            admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
            return admin.execute("""select runtime.nexloop_approval_view_row(
                    jsonb_populate_record(r, jsonb_build_object('tenant_id',%s::text,'world',%s::text)), pol)
                from control.nexloop_approval_requests r join control.nexloop_approval_policies pol on pol.capability=r.capability and pol.version=r.policy_version
                where r.tenant_id=%s and r.world='real' and r.approval_id=%s""", (tenant, world, TENANT, approval)).fetchone()[0]
    own = row_as(TENANT, 'real')
    assert own['consumed'] is True and own['consumed_at'] is not None
    for tenant, world in ((TENANT_B, 'real'), (TENANT, 'test')):
        other = row_as(tenant, world)
        assert (other['approval_id'], other['consumed'], other['consumed_at']) == (approval, False, None), (tenant, world)
