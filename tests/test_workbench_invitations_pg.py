"""NX-058 slice A (docs/design/account-self-service.md v7 §1, §1c) on PostgreSQL through the actual app (create_app, real workbench login
realm): the owner invites an operator; the token is shown once and stored by its HMAC only; the invitee accepts with the token and a
password only (no session; the tenant is the server's), becomes an active Human and member with the role's grants instantiated from the
configurator's template, and logs in; every refusal is one answer; a token is one-time, voided after five rule failures, revoked by a
reissue for the same username or by the owner, and refused once its issuer is no longer an active owner; owners are not invited by this
Action; the issuing owner is limited; without the key both ends are unavailable. Synthetic members only."""
from datetime import timedelta
import dataclasses
import json
import secrets

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import workbench_member_secrets as W
from nexloop_eios.http_api import create_app
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.workbench_roles import compile_member, load
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from support.single_clock import issued_once
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import ROLES, workbench_login

INVITE, REVOKE = 'nexloop.workbench_member.invite', 'nexloop.workbench_member.revoke_invitation'


@pytest.fixture
def invites(switching, admin, tmp_path):
    c = switching
    publish_registry_actions(admin, (INVITE, REVOKE))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'invitation_accept','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    key = tmp_path / 'identity-token-key'
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    c['config'] = dataclasses.replace(c['config'], workbench=dataclasses.replace(c['config'].workbench, identity_token_key_file=key))
    c['key'] = W.load_key(key)
    return c


def members_version(admin):
    return admin.execute('select max(manifest_version) from control.nexloop_workbench_configurations').fetchone()[0]


def accept(client, body):
    return client.post('/api/v1/workbench/auth/invitation/accept', headers={'Origin': ORIGIN}, json=body)


def owner_session(client, c):
    return workbench_login(client, c['owner_member']).json()['csrf_token']


def invite(client, csrf, username='new-operator', role='operator', hours=24):
    return act(client, csrf, 'invite_workbench_member', {'username': username, 'role': role, 'expires_in_hours': hours})


def test_an_invited_operator_joins_with_the_role_template_grants(invites, admin):
    c = invites
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = owner_session(client, c)
        issued = invite(client, csrf)
        assert issued.status_code == 200, issued.json()
        token = issued.json()['token']
        assert W.valid_token(token) and issued.json()['role'] == 'operator'
        invitation = issued.json()['invitation_id']
        issued_once(admin, TENANT, 'control.nexloop_workbench_invitations', 'invitation_id', invitation, timedelta(hours=24),
                    audit=('runtime.audit_events', 'created_at', 'event_id', 'workbench-invite:' + invitation))
        assert admin.execute('select token_hmac from control.nexloop_workbench_invitations').fetchall() == [(W.digest(c['key'], W.INVITE, TENANT, token),)]
        dump = json.dumps(admin.execute("select to_jsonb(i) from control.nexloop_workbench_invitations i").fetchall()) + json.dumps(
            admin.execute("select payload from runtime.audit_events where action like 'nexloop.workbench_member.%'").fetchall())
        assert token not in dump
        for body in ({'token': token[:-1] + ('A' if token[-1] != 'A' else 'B'), 'password': 'a-good-password-1'}, {'token': token},
                     {'token': token, 'password': 'short'}, {'token': token, 'password': 'a-good-password-1', 'username': 'x'},
                     {'token': token, 'password': 'a-good-password-1', 'tenant_id': TENANT}):
            refused = accept(client, body)
            assert (refused.status_code, refused.json()) == (400, {'code': 'invitation_invalid'}), body
        done = accept(client, {'token': token, 'password': 'a-good-password-1'})
        assert (done.status_code, done.json()) == (200, {'code': 'invitation_accepted'})
        assert accept(client, {'token': token, 'password': 'a-good-password-1'}).status_code == 400  # one-time
        joined = workbench_login(client, {'username': 'new-operator', 'password': 'a-good-password-1'})
        assert joined.status_code == 200, joined.json()
        principal = joined.json()['principal_id']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    members = admin.execute('select members,recorded_by from control.nexloop_workbench_configurations order by manifest_version desc limit 1').fetchone()
    assert members[0][principal] == 'operator' and members[1].startswith('invitation:')
    subject = admin.execute('select subject_id from control.nexloop_browser_memberships where principal_id=%s', (principal,)).fetchone()[0]
    want = compile_member(ROLES, TENANT, {'principal_id': principal, 'subject_id': subject, 'role': 'operator'},
                          valid_from=__import__('datetime').datetime(2026, 10, 10, tzinfo=__import__('datetime').UTC))
    for kind, key, fact in want:
        row = admin.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s', (TENANT, kind, list(key))).fetchone()
        assert row and canonical_payload(row[0]) == canonical_payload(fact.model_dump(mode='json')), (kind, key)
    assert admin.execute('select role,event from control.nexloop_workbench_role_history where principal_id=%s', (principal,)).fetchall() == [('operator', 'start')]
    assert admin.execute('select direction,source from control.nexloop_workbench_grant_instances where principal_id=%s', (principal,)).fetchone()[0] == 'grant'
    audits = admin.execute("select action,payload->>'issued_by' from runtime.audit_events where action in (%s,'nexloop.workbench_member.invitation_accepted') order by created_at",
                           (INVITE,)).fetchall()
    assert audits == [(INVITE, None), ('nexloop.workbench_member.invitation_accepted', c['owner_member']['principal_id'])]
    # A-8: the acceptance's detail and the instantiation's own event name the instance record and the configuration version.
    instance = admin.execute('select instance_id from control.nexloop_workbench_grant_instances where principal_id=%s', (principal,)).fetchone()[0]
    detail = admin.execute("select payload from runtime.audit_events where action='nexloop.workbench_member.invitation_accepted_detail'").fetchone()[0]
    assert detail['instance_id'] == instance and detail['configuration_version'] == members_version(admin)
    assert admin.execute("select count(*) from runtime.audit_events where action='nexloop.workbench_member.grants_written' and (payload->>'instance_id')::bigint=%s",
                         (instance,)).fetchone() == (1,)
    # X1 (slice C): a configurator apply from a member file without the invitee is refused, naming the member and its role.
    from nexloop_eios.workbench_roles import WorkbenchRolesRejected, apply_members
    from test_workbench_read_pg import members_file
    with pytest.raises(WorkbenchRolesRejected, match='members_changed_by_governed_actions: ' + principal + '=operator'):
        apply_members(ROLES, members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator')), database_url_file=c['configurator'])


def test_reissue_revoke_failures_owner_role_issuer_and_limits(invites, admin):
    c = invites
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = owner_session(client, c)
        assert invite(client, csrf, role='owner').status_code == 422  # owners: invite_owner, with approval (slice C)
        assert invite(client, csrf, username=c['operator_member']['username']).json()['details']['reason'] == 'username_taken'
        first, second = invite(client, csrf).json()['token'], invite(client, csrf).json()
        assert accept(client, {'token': first, 'password': 'a-good-password-1'}).status_code == 400  # revoked by the reissue
        revoked = act(client, csrf, 'revoke_workbench_invitation', {'invitation_id': second['invitation_id']})
        assert revoked.status_code == 200 and revoked.json()['state'] == 'revoked'
        assert accept(client, {'token': second['token'], 'password': 'a-good-password-1'}).status_code == 400
        third = invite(client, csrf, username='another-one').json()['token']
        for _ in range(5):
            assert accept(client, {'token': third, 'password': 'short'}).status_code == 400
        assert accept(client, {'token': third, 'password': 'a-good-password-1'}).status_code == 400  # voided
        fourth = invite(client, csrf, username='third-person').json()['token']
        for i in range(6):  # 4 issued so far in this window; the limit is 10 per 15 minutes
            invite(client, csrf, username=f'filler-{i}x')
        limited = invite(client, csrf, username='one-too-many')
        assert (limited.status_code, limited.headers['Retry-After'], limited.json()['code']) == (429, '900', 'rate_limited')
    admin.execute("""update control.nexloop_browser_memberships set payload=jsonb_set(payload,'{status}','"suspended"') where principal_id=%s""", (c['owner_member']['principal_id'],))
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        assert accept(client, {'token': fourth, 'password': 'a-good-password-1'}).status_code == 400  # its issuer is no longer an active owner
    assert admin.execute("select count(*) from control.nexloop_browser_accounts where username in ('new-operator','another-one','third-person')").fetchone() == (0,)


def test_without_the_key_and_for_an_operator(invites, switching, admin):
    c = invites
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert invite(client, csrf).status_code == 403
    plain = dataclasses.replace(c['config'], workbench=dataclasses.replace(c['config'].workbench, identity_token_key_file=None))
    with TestClient(create_app(plain), base_url=ORIGIN) as client:
        csrf = owner_session(client, c)
        assert invite(client, csrf).status_code == 503
        assert accept(client, {'token': 'A' * 43, 'password': 'a-good-password-1'}).status_code == 503


def test_an_expired_token_a_demoted_issuer_and_a_concurrent_acceptance(invites, admin):
    """L6 slice A review A-6: a token past its expiry is refused; a token whose issuer is no longer an owner (demoted, not only
    suspended) is refused; the same token accepted twice at once creates one Human."""
    import threading
    c = invites
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = owner_session(client, c)
        expired = invite(client, csrf, username='late-person', hours=1).json()['token']
        demoted = invite(client, csrf, username='orphan-person').json()['token']
        racing = invite(client, csrf, username='raced-person').json()['token']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    admin.execute("update control.nexloop_workbench_invitations set issued_at=issued_at-interval '2 hours',expires_at=expires_at-interval '2 hours' where username='late-person'")
    results = []
    def go():
        with TestClient(create_app(c['config']), base_url=ORIGIN) as other:
            results.append(accept(other, {'token': racing, 'password': 'a-good-password-1'}).status_code)
    # E-4: a barrier - the test holds the tenant's member lock until both acceptances wait on it, so they really meet inside the lock.
    import time
    import psycopg
    with psycopg.connect(admin.info.dsn, autocommit=True) as gate:
        with gate.transaction():
            gate.execute('select control.nexloop_approval_tenant_lock(%s)', (TENANT,))
            threads = [threading.Thread(target=go) for _ in range(2)]
            for t in threads:
                t.start()
            deadline = time.monotonic() + 30
            while admin.execute("select count(*) from pg_stat_activity where wait_event_type='Lock' and wait_event='advisory'").fetchone()[0] < 2:
                assert time.monotonic() < deadline, 'both acceptances never reached the member lock'
                time.sleep(0.05)
    for t in threads:
        t.join()
    assert sorted(results) == [200, 400]
    assert admin.execute("select count(*) from control.nexloop_browser_accounts where username='raced-person'").fetchone() == (1,)
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        assert accept(client, {'token': expired, 'password': 'a-good-password-1'}).status_code == 400
        # The issuer demoted to operator (a configuration version from a governed change, as slice C's demote_owner will append it).
        cur = admin.execute('select members from control.nexloop_workbench_configurations order by manifest_version desc limit 1').fetchone()[0]
        admin.execute("select control.nexloop_workbench_configuration_append(%s,%s,'action:test-demotion')",
                      (TENANT, json.dumps({**cur, c['owner_member']['principal_id']: 'operator'})))
        assert accept(client, {'token': demoted, 'password': 'a-good-password-1'}).status_code == 400
    assert admin.execute("select count(*) from control.nexloop_browser_accounts where username in ('late-person','orphan-person')").fetchone() == (0,)
