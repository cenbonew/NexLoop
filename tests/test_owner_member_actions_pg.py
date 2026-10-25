"""NX-058 slice D2 (docs/design/nx058-owner-actions.md v2.8) on PostgreSQL through the actual app: the owner-only member Actions under
NX-056 approval - promote_owner, demote_owner, suspend_owner, resume_owner, revoke_owner - with the approval policies of the deployed file
(version 1 from the deployment history, then the current version 2), three ripe owners and an operator. Covers: request -> another
owner's approval -> the requester's resubmission executes once; the handler's fail-closed layers (no policy; an in-flight approval of
another body); the target's role and status rechecked at execution (a refusal consumes the approval as execution_refused); a suspended
owner demoted keeps nothing; approval_open's operation check; the target never approves its own request. Synthetic members only."""
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from nexloop_eios.approval_policies import apply_policies
from nexloop_eios.http_api import create_app
from nexloop_eios.workbench_roles import apply_members, compile_members
from support.vote_cooldown import ripen_approvers
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_nx056_first_policies_pg import CURRENT, POLICIES, configurator
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_member_changes_pg import grants_match, granted, history
from test_workbench_read_pg import ROLES, add_human, members_file, workbench_login, write_facts

ACTIONS = tuple(f'nexloop.workbench_member.{a}' for a in ('promote_owner', 'demote_owner', 'suspend_owner', 'resume_owner', 'revoke_owner')) + (
    'nexloop.approvals.decide', 'nexloop.approvals.cancel')


@pytest.fixture
def owners(switching, admin, pg, tmp_path):
    c = switching
    c['second'], c['third'] = add_human(admin, 'second-owner'), add_human(admin, 'third-owner')
    members = members_file((c['owner_member'], 'owner'), (c['second'], 'owner'), (c['third'], 'owner'), (c['operator_member'], 'operator'))
    apply_members(ROLES, members, database_url_file=c['configurator'])
    write_facts(admin, compile_members(ROLES, members))
    ripen_approvers(admin, TENANT)
    publish_registry_actions(admin, ACTIONS)
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    c['dsn'] = configurator(admin, pg, tmp_path)

    def call(member, operation, body):
        with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
            csrf = workbench_login(client, member).json()['csrf_token']
            return act(client, csrf, operation, body)
    c['call'] = call
    c['decide'] = lambda member, approval, decision='approve': call(member, 'decide_approval', {'approval_id': approval, 'decision': decision, 'reason': 'synthetic check'})
    return c


def with_policies(c):
    apply_policies(POLICIES.read_text(encoding='utf-8'), database_url_file=c['dsn'])
    return apply_policies(CURRENT.read_text(encoding='utf-8'), database_url_file=c['dsn'])


def approved_run(c, requester, approver, operation, body):
    """Request, another owner's approval, the requester's resubmission: the executed response."""
    first = c['call'](requester, operation, body)
    assert first.status_code == 200 and first.json()['status'] == 'waiting_approval', first.json()
    assert c['decide'](approver, first.json()['approval_id']).json()['status'] == 'approved'
    return c['call'](requester, operation, body)


def test_promotion_runs_once_after_another_owners_approval(owners, admin):
    c = owners
    applied = with_policies(c)
    assert applied['version'] == 2 and applied['appended'] == 5 and applied['unchanged'] == 3
    p = c['operator_member']['principal_id']
    body = {'target_principal': p, 'reason': 'synthetic promotion'}
    executed = approved_run(c, c['owner_member'], c['second'], 'promote_workbench_member', body)
    assert executed.status_code == 200 and executed.json()['approval'] == 'consumed' and executed.json()['role'] == 'owner', executed.json()
    assert admin.execute('select members->>%s from control.nexloop_workbench_configurations order by manifest_version desc limit 1', (p,)).fetchone() == ('owner',)
    grants_match(admin, c['operator_member'], 'owner')
    assert history(admin, p)[-2:] == [('operator', 'stop'), ('owner', 'start')]
    audit = admin.execute("select payload from runtime.audit_events where action='nexloop.workbench_member.promote_owner'").fetchone()[0]
    assert audit['approval_id'] == executed.json()['approval_id'] and audit['to_role'] == 'owner'
    assert c['call'](c['owner_member'], 'promote_workbench_member', body).json()['status'] == 'approval_consumed'  # once


def test_without_a_policy_the_handler_refuses_and_an_in_flight_approval_of_another_body_is_refused(owners, admin):
    c = owners
    body = {'target_principal': c['third']['principal_id'], 'reason': 'synthetic leave'}
    refused = c['call'](c['owner_member'], 'suspend_workbench_owner', body)  # no policy: the gate lets it through, the handler does not
    assert refused.status_code == 409 and refused.json()['details']['reason'] == 'approval_unavailable', refused.json()
    assert admin.execute("select payload->>'status' from control.nexloop_browser_memberships where principal_id=%s", (c['third']['principal_id'],)).fetchone() == ('active',)
    with_policies(c)
    # An approved request of another body: setting nexloop.approval_in_flight to it does not let this body run (layer 2).
    other = c['call'](c['owner_member'], 'suspend_workbench_owner', {**body, 'reason': 'another body'}).json()['approval_id']
    assert c['decide'](c['second'], other).json()['status'] == 'approved'
    with pytest.raises(psycopg.Error, match='approval_unavailable'), admin.transaction():
        admin.execute("select set_config('nexloop.approval_in_flight',%s,true)", (other,))
        admin.execute('select control.nexloop_governed_change_workbench_owner(%s,%s,%s,%s,%s,%s)', (
            TENANT, 'real', c['owner_member']['principal_id'], 'human', 'intent-' + uuid.uuid4().hex[:12],
            Jsonb({'operation': 'suspend_workbench_owner', 'request_id': uuid.uuid4().hex, **body})))


def test_a_suspended_owner_demoted_holds_nothing_and_a_changed_target_consumes_the_approval_as_refused(owners, admin):
    c = owners
    with_policies(c)
    t = c['third']['principal_id']
    assert approved_run(c, c['owner_member'], c['second'], 'suspend_workbench_owner', {'target_principal': t, 'reason': 'synthetic leave'}).json()['approval'] == 'consumed'
    assert granted(admin, t) == set()
    demoted = approved_run(c, c['owner_member'], c['second'], 'demote_workbench_owner', {'target_principal': t, 'new_role': 'reviewer', 'reason': 'synthetic'})
    assert demoted.json()['role'] == 'reviewer', demoted.json()
    assert admin.execute('select members->>%s from control.nexloop_workbench_configurations order by manifest_version desc limit 1', (t,)).fetchone() == ('reviewer',)
    assert admin.execute("select payload->>'status' from control.nexloop_browser_memberships where principal_id=%s", (t,)).fetchone() == ('suspended',)
    assert granted(admin, t) == set()
    # The target is no owner any more: an owner-only Action approved before is refused at execution, and its approval is spent.
    body = {'target_principal': c['operator_member']['principal_id'], 'reason': 'synthetic'}
    first = c['call'](c['owner_member'], 'revoke_workbench_owner', body)
    assert first.status_code == 200 and first.json()['status'] == 'waiting_approval'
    assert c['decide'](c['second'], first.json()['approval_id']).json()['status'] == 'approved'
    spent = c['call'](c['owner_member'], 'revoke_workbench_owner', body)
    assert spent.json()['status'] == 'execution_refused' and spent.json()['approval'] == 'consumed', spent.json()
    assert admin.execute('select result from control.nexloop_approval_consumptions where approval_id=%s', (first.json()['approval_id'],)).fetchone() == ('execution_refused:22023:target_role_changed',)  # the handler's code (ADR-039 §7.4, NX-056 B2)


def test_resume_and_revoke_an_owner_and_the_target_never_approves(owners, admin):
    c = owners
    with_policies(c)
    t = c['third']['principal_id']
    approved_run(c, c['owner_member'], c['second'], 'suspend_workbench_owner', {'target_principal': t, 'reason': 'synthetic leave'})
    resumed = approved_run(c, c['owner_member'], c['second'], 'resume_workbench_owner', {'target_principal': t, 'reason': 'synthetic return'})
    assert resumed.json()['approval'] == 'consumed' and granted(admin, t) == set(ROLES['roles']['owner'])
    # The member a request acts on never approves it (NX-056, AA2), not even as a ripe owner.
    body = {'target_principal': t, 'reason': 'synthetic departure'}
    first = c['call'](c['owner_member'], 'revoke_workbench_owner', body).json()['approval_id']
    assert c['decide'](c['third'], first).status_code == 403  # resumed just now: within the vote cooldown (approver_unauthorized)
    from test_vote_cooldown_pg import age_history
    age_history(admin, c['third'], 25)
    assert c['decide'](c['third'], first).status_code == 409  # ripe, and still the target: target_cannot_approve
    assert c['decide'](c['second'], first).json()['status'] == 'approved'
    revoked = c['call'](c['owner_member'], 'revoke_workbench_owner', body)
    assert revoked.json()['approval'] == 'consumed' and granted(admin, t) == set()
    assert admin.execute("select payload->>'status' from control.nexloop_browser_memberships where principal_id=%s", (t,)).fetchone() == ('revoked',)
    assert admin.execute('select members ? %s from control.nexloop_workbench_configurations order by manifest_version desc limit 1', (t,)).fetchone() == (False,)


def test_approval_open_refuses_a_body_of_another_capabilitys_operation(owners, admin):
    c = owners
    with_policies(c)
    for operation in ('invite_workbench_owner', 'suspend_workbench_owner'):
        with pytest.raises(psycopg.Error, match='operation_mismatch'):
            admin.execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)', (
                TENANT, 'real', 'workbench_member.demote_owner', 'eios:action:nexloop.workbench_member.demote_owner:1', c['owner_member']['principal_id'],
                Jsonb({'operation': operation, 'request_id': uuid.uuid4().hex, 'target_principal': c['third']['principal_id'], 'new_role': 'operator', 'reason': 'x'})))


def test_the_registry_matches_the_design(admin):
    """The five capabilities as registered (and as B1's hand-inserted rows would have to match; L6 review of B1)."""
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    rows = admin.execute("select capability,operation,subject_rule from control.nexloop_governed_capabilities where capability like 'workbench_member.%%_owner' order by 1").fetchall()
    assert rows == [('workbench_member.demote_owner', 'demote_workbench_owner', 'human'), ('workbench_member.promote_owner', 'promote_workbench_member', 'human'),
                    ('workbench_member.resume_owner', 'resume_workbench_owner', 'human'), ('workbench_member.revoke_owner', 'revoke_workbench_owner', 'human'),
                    ('workbench_member.suspend_owner', 'suspend_workbench_owner', 'human')]
