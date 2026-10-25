"""NX-058 slice D3 (docs/design/nx058-owner-actions.md v2.8 §4, §5; ADR-039 R3 §8) on PostgreSQL through the actual app, with three ripe
owners and an operator (the slice D2 fixture) and approval policies version 3:
- issue_owner_reset: the waiting submission draws a code that is never shown nor stored; the frozen body holds no HMAC; the execution
  shows the code once, stores its HMAC and only then ends the target's sessions; a retry of the execution with the same Idempotency-Key
  replays (no second code) instead of conflicting (L6 I-C2, R3 §8.4 option a);
- invite_owner: a username the tenant holds is refused before any approval; the token is shown only by the execution; acceptance v3 needs
  the approval executed and flags an acceptance from the issuer's source (approval.same_person_flagged);
- requester_no_longer_eligible through the D2 handler (L6 I-C1): an owner demoted, suspended or revoked loses its approved, unconsumed
  requests at once. Synthetic members only."""
import uuid

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import workbench_member_secrets as W
from nexloop_eios.http_api import create_app
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_owner_member_actions_pg import approved_run, owners, with_policies  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_invitations_pg import accept
from test_workbench_read_pg import workbench_login


def key_of(c):
    return W.load_key(c['config'].workbench.identity_token_key_file)


def frozen(admin, approval):
    return admin.execute('select body from control.nexloop_approval_requests where approval_id=%s', (approval,)).fetchone()[0]


def test_an_owner_reset_code_appears_only_at_execution_and_a_retry_replays(owners, admin):
    c = owners
    with_policies(c)
    t = c['third']['principal_id']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client, TestClient(create_app(c['config']), base_url=ORIGIN) as target:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert workbench_login(target, c['third']).status_code == 200
        waiting = act(client, csrf, 'issue_workbench_owner_reset', {'target_principal': t})
        assert waiting.status_code == 200 and waiting.json()['status'] == 'waiting_approval' and 'code' not in waiting.json(), waiting.json()
        approval = waiting.json()['approval_id']
        assert set(frozen(admin, approval)) == {'operation', 'request_id', 'target_principal'}  # no HMAC in the frozen body (R3 §8.1)
        assert admin.execute('select count(*) from control.nexloop_workbench_reset_codes').fetchone() == (0,)
        assert target.get('/api/v1/workbench/auth/session').status_code == 200  # R5: requesting ends no session
        assert c['decide'](c['second'], approval).json()['status'] == 'approved'
        key = 'wb-' + uuid.uuid4().hex[:24]
        executed = act(client, csrf, 'issue_workbench_owner_reset', {'target_principal': t}, key=key)
        assert executed.status_code == 200 and executed.json()['approval'] == 'consumed' and W.valid_code(executed.json()['code']), executed.json()
        code = executed.json()['code']
        assert admin.execute('select code_hmac,state from control.nexloop_workbench_reset_codes').fetchall() == [(W.digest(key_of(c), W.RESET, TENANT, code), 'issued')]
        assert target.get('/api/v1/workbench/auth/session').status_code == 401  # only now
        # The response was lost: the same Idempotency-Key again (a new HMAC is drawn) replays the claim - no second code, no conflict.
        again = act(client, csrf, 'issue_workbench_owner_reset', {'target_principal': t}, key=key)
        assert again.status_code == 200 and again.json().get('replayed') is True and 'code' not in again.json(), again.json()
        assert admin.execute('select count(*) from control.nexloop_workbench_reset_codes').fetchone() == (1,)
    dump = str(admin.execute('select body from control.nexloop_approval_requests').fetchall()) + str(
        admin.execute("select payload from runtime.audit_events where action like 'nexloop.workbench_member.%' or action like 'approval.%'").fetchall())
    assert code not in dump and W.digest(key_of(c), W.RESET, TENANT, code) not in dump


def test_an_owner_invitation_is_shown_at_execution_needs_its_approval_and_flags_the_same_source(owners, admin):
    c = owners
    with_policies(c)
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        taken = act(client, csrf, 'invite_workbench_owner', {'username': c['third']['username'], 'expires_in_hours': 24})
        assert taken.status_code == 409 and taken.json()['details']['reason'] == 'username_taken', taken.json()
        assert admin.execute("select count(*) from control.nexloop_approval_requests where capability='workbench_member.invite_owner'").fetchone() == (0,)
        body = {'username': 'new-owner', 'expires_in_hours': 24}
        waiting = act(client, csrf, 'invite_workbench_owner', body)
        assert waiting.json()['status'] == 'waiting_approval' and 'token' not in waiting.json(), waiting.json()
        assert set(frozen(admin, waiting.json()['approval_id'])) == {'operation', 'request_id', 'username', 'expires_in_hours'}
        assert c['decide'](c['second'], waiting.json()['approval_id']).json()['status'] == 'approved'
        executed = act(client, csrf, 'invite_workbench_owner', body)
        token = executed.json()['token']
        assert W.valid_token(token) and executed.json()['role'] == 'owner', executed.json()
        assert admin.execute("select role,approval_id::text from control.nexloop_workbench_invitations where username='new-owner'").fetchone() == ('owner', executed.json()['approval_id'])
        done = accept(client, {'token': token, 'password': 'a-good-password-1'})
        assert (done.status_code, done.json()) == (200, {'code': 'invitation_accepted'})
    principal = admin.execute("select accepted_principal from control.nexloop_workbench_invitations where username='new-owner'").fetchone()[0]
    assert admin.execute('select members->>%s from control.nexloop_workbench_configurations order by manifest_version desc limit 1', (principal,)).fetchone() == ('owner',)
    # The test client is one source: the acceptance comes from the issuer's source (ADR-039 R1 §6.6, design S1).
    flagged = admin.execute("select payload from runtime.audit_events where action='approval.same_person_flagged'").fetchall()
    assert [p['reason'] for (p,) in flagged] == ['same_source_as_issuer'] and flagged[0][0]['approval_id'] == executed.json()['approval_id']


def test_acceptance_v3_refuses_an_owner_invitation_without_an_executed_approval(owners, admin):
    c = owners
    token = W.new_token()
    admin.execute("""insert into control.nexloop_workbench_invitations(tenant_id,request_id,username,role,token_hmac,issued_by_principal,issued_by_subject,
        issued_at,expires_at,state) values(%s,'forged','forged-owner','owner',%s,%s,%s,clock_timestamp(),clock_timestamp()+interval '1 hour','issued')""",
                  (TENANT, W.digest(key_of(c), W.INVITE, TENANT, token), c['owner_member']['principal_id'], c['owner_member']['subject_id']))
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        refused = accept(client, {'token': token, 'password': 'a-good-password-1'})
    assert (refused.status_code, refused.json()) == (400, {'code': 'invitation_invalid'})
    assert admin.execute("select state from control.nexloop_workbench_invitations where username='forged-owner'").fetchone() == ('issued',)


@pytest.mark.parametrize('change', ['demote_workbench_owner', 'suspend_workbench_owner', 'revoke_workbench_owner'])
def test_an_owner_who_loses_standing_loses_its_approved_requests_at_once(owners, admin, change):
    """L6 I-C1, through the D2 handler: the third owner holds an approved, unconsumed request; demoting, suspending or revoking it ends
    that request (requester_no_longer_eligible) in the same transaction - the role history's stop or the membership leaving active."""
    c = owners
    with_policies(c)
    pending = c['call'](c['third'], 'promote_workbench_member', {'target_principal': c['operator_member']['principal_id'], 'reason': 'synthetic'})
    approval = pending.json()['approval_id']
    assert c['decide'](c['second'], approval).json()['status'] == 'approved'
    body = {'target_principal': c['third']['principal_id'], 'reason': 'synthetic', **({'new_role': 'reviewer'} if change == 'demote_workbench_owner' else {})}
    assert approved_run(c, c['owner_member'], c['second'], change, body).json()['approval'] == 'consumed'
    assert admin.execute('select status from control.nexloop_approval_requests where approval_id=%s', (approval,)).fetchone() == ('expired',)
    ended = [p for (p,) in admin.execute("select payload from runtime.audit_events where action='approval.expired'").fetchall() if p['approval_id'] == approval]
    assert [p['reason'] for p in ended] == ['requester_no_longer_eligible']


def test_the_claim_digest_leaves_out_exactly_the_registered_execution_time_fields(admin):
    """goal_controls.EXECUTION_FIELDS mirrors control.nexloop_approval_capability_settings (R3 §8.4 option a relies on both sides agreeing)."""
    from nexloop_eios.bootstrap import bootstrap
    from nexloop_eios.goal_controls import EXECUTION_FIELDS
    bootstrap(admin)
    rows = admin.execute("select capability,execution_fields from control.nexloop_approval_capability_settings where cardinality(execution_fields)>0").fetchall()
    assert {cap: tuple(fields) for cap, fields in rows} == EXECUTION_FIELDS
