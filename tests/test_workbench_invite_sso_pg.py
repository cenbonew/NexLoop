"""NX-058 slice F (docs/design/account-self-service.md §5): an invitation names the member SSO only, on PostgreSQL through the actual app
and the in-process IdP of tests/support/mock_oidc_idp.py: the acceptance takes no password; until an owner links the member's IdP subject
the member has no way in (no password, no reset code, no SSO); the link must be the invitation's provider and sso_only; then SSO signs the
member in. Owners are never invited SSO only; an unconfigured provider is refused. Synthetic members, keys and secrets only."""
import pytest
from fastapi.testclient import TestClient

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_invitations_pg import accept
from test_workbench_oidc_pg import app, failures, link, sign_in, sso  # noqa: F401
from test_workbench_read_pg import workbench_login


@pytest.fixture
def invited(sso, admin):
    publish_registry_actions(admin, ('nexloop.workbench_member.invite', 'nexloop.workbench_member.invite_owner'))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'invitation_accept','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    return sso


def invite(client, csrf, **extra):
    return act(client, csrf, 'invite_workbench_member', {'username': 'sso-operator', 'role': 'operator', 'expires_in_hours': 24, **extra})


def test_an_sso_only_invitee_has_no_way_in_until_linked_then_signs_in_by_sso(invited, admin):
    c, idp = invited, invited['idp']
    with TestClient(app(c), base_url=ORIGIN) as client, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        # Only a configured provider; an owner invitation has no such field.
        unknown = invite(client, csrf, sso_provider='other')
        assert unknown.status_code == 409 and unknown.json()['details'] == {'reason': 'provider_unknown'}
        owner_invite = act(client, csrf, 'invite_workbench_owner', {'username': 'sso-owner', 'expires_in_hours': 24, 'sso_provider': 'corp'})
        assert owner_invite.status_code == 422
        issued = invite(client, csrf, sso_provider='corp')
        assert issued.status_code == 200, issued.json()
        assert issued.json()['sso_provider'] == 'corp'
        token = issued.json()['token']
        # The acceptance sets no password: a body with one is refused (the same answer as any refusal); without one it is accepted.
        refused = accept(member, {'token': token, 'password': 'a-good-password-1'})
        assert (refused.status_code, refused.json()) == (400, {'code': 'invitation_invalid'})
        done = accept(member, {'token': token})
        assert (done.status_code, done.json()) == (200, {'code': 'invitation_accepted'})
        admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
        principal, subject = admin.execute("select accepted_principal,accepting_subject from control.nexloop_workbench_invitations where username='sso-operator'").fetchone()
        assert admin.execute('select provider_id from control.nexloop_workbench_sso_pending where principal_id=%s', (principal,)).fetchall() == [('corp',)]
        # The owner's members view marks the member pending (a boolean only); nobody else is.
        view = admin.execute('select runtime.nexloop_workbench_members_view(%s)', (TENANT,)).fetchone()[0]['members']
        assert {m['principal_id']: m['sso_pending'] for m in view}[principal] is True and sum(m['sso_pending'] for m in view) == 1
        assert 'corp' not in str([m for m in view if m['principal_id'] == principal])
        # Pending: the password path refuses (whatever password), no reset code, and SSO finds no link.
        assert workbench_login(member, {'username': 'sso-operator', 'password': 'a-good-password-1'}).status_code == 401
        reset = act(client, csrf, 'issue_workbench_reset', {'target_principal': principal})
        assert reset.status_code == 409 and reset.json()['details'] == {'reason': 'target_sso_only'}
        assert sign_in(member, idp, 'idp-sub-invitee').headers['location'] == '/workbench?sso=failed'
        assert member.get('/api/v1/workbench/auth/session').status_code == 401
        # The link must be sso_only (and the invitation's provider); a refused link leaves the member pending.
        mismatch = link(client, csrf, principal, 'idp-sub-invitee', mode='sso_or_password')
        assert mismatch.status_code == 409 and mismatch.json()['details'] == {'reason': 'sso_invitation_mismatch'}
        assert admin.execute('select count(*) from control.nexloop_workbench_sso_pending where principal_id=%s', (principal,)).fetchone()[0] == 1
        linked = link(client, csrf, principal, 'idp-sub-invitee', mode='sso_only')
        assert linked.status_code == 200, linked.json()
        assert admin.execute('select count(*) from control.nexloop_workbench_sso_pending where principal_id=%s', (principal,)).fetchone()[0] == 0
        assert not any(m['sso_pending'] for m in admin.execute('select runtime.nexloop_workbench_members_view(%s)', (TENANT,)).fetchone()[0]['members'])
        # Linked: SSO signs the member in; the password still does not.
        assert sign_in(member, idp, 'idp-sub-invitee').headers['location'] == '/workbench'
        session = member.get('/api/v1/workbench/auth/session').json()
        assert session['principal_id'] == principal and session['authentication_methods'] == ['oidc']
        with TestClient(app(c), base_url=ORIGIN) as third:
            assert workbench_login(third, {'username': 'sso-operator', 'password': 'a-good-password-1'}).status_code == 401
    assert failures(admin) == ['not_linked']
    assert admin.execute("select payload->>'sso_provider' from runtime.audit_events where action='nexloop.workbench_member.invite'").fetchall() == [('corp',)]
    assert subject


def test_a_password_invitation_still_takes_a_password_and_pending_ends_with_revocation(invited, admin):
    c = invited
    with TestClient(app(c), base_url=ORIGIN) as client, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        plain = invite(client, csrf, username='pw-operator')
        assert plain.status_code == 200 and plain.json()['sso_provider'] is None
        assert accept(member, {'token': plain.json()['token']}).status_code == 400  # a password invitation needs one
        assert accept(member, {'token': plain.json()['token'], 'password': 'a-good-password-1'}).status_code == 200
        assert workbench_login(member, {'username': 'pw-operator', 'password': 'a-good-password-1'}).status_code == 200
        issued = invite(client, csrf, sso_provider='corp')
        assert accept(member, {'token': issued.json()['token']}).status_code == 200
        admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
        principal = admin.execute("select accepted_principal from control.nexloop_workbench_invitations where username='sso-operator'").fetchone()[0]
        revoked = act(client, csrf, 'revoke_workbench_member', {'target_principal': principal, 'reason': 'test revocation'})
        assert revoked.status_code == 200, revoked.json()
        assert admin.execute('select count(*) from control.nexloop_workbench_sso_pending').fetchone()[0] == 0
