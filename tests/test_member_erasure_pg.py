"""NX-058 slice E1 (docs/design/nx058-member-erasure.md v2.4; ADR-039 R3 §8.1, §8.6, §8.7) on PostgreSQL through the actual app, with the
slice D fixture (three ripe owners, an operator) and approval policies version 4: an owner erases a departed member under another owner's
approval. Covers the erasure itself (account, subject, sessions, invitations, reset codes, the permanent ledger and the deletion ledger),
the subject scope (C1), the refusals (not revoked, already erased), the invite_owner frozen username (C3), the approval view's erased,
the replay after a restore (C2, I-E4), and the channels that must not bring an erased member back (0198, the configurator, erased-
usernames). Synthetic members only."""
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

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


def revoke(c, member):
    return approved_run(c, c['owner_member'], c['second'], 'revoke_workbench_owner' if member is not c['operator_member'] else 'revoke_workbench_member',
                        {'target_principal': member['principal_id'], 'reason': 'synthetic departure'})


def erase(c, member, reason='offboarding'):
    return approved_run(c, c['owner_member'], c['second'], 'erase_workbench_member', {'target_principal': member['principal_id'], 'reason': reason})


def account(admin, member):
    return admin.execute('select username,password_hash,password_history,payload from control.nexloop_browser_accounts where subject_id=%s',
                         (member['subject_id'],)).fetchone()


def test_an_erasure_of_a_revoked_owner(owners, admin):
    c = owners
    with_policies(c)
    t = c['third']
    key = W.load_key(c['config'].workbench.identity_token_key_file)
    assert c['call'](t, 'invite_workbench_member', {'username': 'pending-of-third', 'role': 'reviewer', 'expires_in_hours': 24}).status_code == 200
    with TestClient(create_app(c['config']), base_url=ORIGIN) as session:
        assert workbench_login(session, t).status_code == 200
    revoke(c, t)
    original = account(admin, t)[0]
    # A reset code aimed at the departed member (as an issue before the departure would leave it; written here directly).
    admin.execute("""insert into control.nexloop_workbench_reset_codes(tenant_id,request_id,target_principal,target_subject,code_hmac,issued_by_principal,
        issued_by_subject,issued_at,expires_at,state,revoked_reason) values(%s,'old-code',%s,%s,%s,%s,%s,clock_timestamp(),clock_timestamp()+interval '30 minutes','revoked','member_changed')""",
                  (TENANT, t['principal_id'], t['subject_id'], W.digest(key, W.RESET, TENANT, W.new_code()), c['owner_member']['principal_id'], c['owner_member']['subject_id']))
    erased = erase(c, t)
    assert erased.json()['approval'] == 'consumed' and erased.json()['erased'] is True, erased.json()
    username, password, history, payload = account(admin, t)
    assert username.startswith('erased-') and len(username) == 23 and payload['username'] == username and payload['verified_email'] == username + '@erased.invalid'
    assert (password, history, payload['status']) == (None, [], 'disabled')
    assert admin.execute("select payload->>'status' from control.nexloop_browser_subjects where subject_id=%s", (t['subject_id'],)).fetchone() == ('disabled',)
    assert admin.execute("select count(*) from control.nexloop_browser_sessions where payload->>'subject_id'=%s", (t['subject_id'],)).fetchone() == (0,)
    assert admin.execute('select count(*) from control.nexloop_workbench_reset_codes where target_subject=%s', (t['subject_id'],)).fetchone() == (0,)
    assert admin.execute("select issuer_source from control.nexloop_workbench_invitations where username='pending-of-third'").fetchone() == (None,)  # kept, digest cleared
    ledger = admin.execute('select principals,approval_id::text,erased_by_principal,erased_at from control.nexloop_workbench_member_erasures').fetchall()
    assert [row[:3] for row in ledger] == [([t['principal_id']], erased.json()['approval_id'], c['owner_member']['principal_id'])]  # S-E8: one row
    assert admin.execute("select world,subject_ref,request_ref,cutoff from control.nexloop_deletion_ledger where kind='workbench_member'").fetchall() == [
        ('real', t['subject_id'], erased.json()['approval_id'], ledger[0][3])]
    assert admin.execute('select count(*) from control.nexloop_browser_memberships where principal_id=%s', (t['principal_id'],)).fetchone() == (1,)  # kept
    audit = admin.execute("select payload from runtime.audit_events where action='nexloop.workbench_member.erase_member'").fetchone()[0]
    # principals and counts only: no reason, username or digest
    assert set(audit) == {'target_principal', 'approval_id', 'principals', 'sessions', 'invitations', 'reset_codes', 'frozen_bodies'}
    assert audit['approval_id'] == erased.json()['approval_id']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        assert workbench_login(client, {'username': original, 'password': t['password']}).status_code == 401
    # The approval view: an owner sees the erased member as erased (ADR-039 R3 §8.6).
    view = admin.execute("select runtime.nexloop_approval_view(%s,'real',%s)", (TENANT, c['owner_member']['principal_id'])).fetchone()[0]
    rows = [r for r in view['mine'] if r['approval_id'] == erased.json()['approval_id']]
    assert rows and rows[0]['target_member'] == {'kind': 'erased'}


def test_only_a_subject_with_every_membership_revoked_is_erased_once(owners, admin):
    c = owners
    with_policies(c)
    t = c['third']
    # Not revoked: the request never opens (target statuses {revoked}).
    refused = c['call'](c['owner_member'], 'erase_workbench_member', {'target_principal': t['principal_id'], 'reason': 'offboarding'})
    assert refused.status_code == 409 and admin.execute("select count(*) from control.nexloop_approval_requests where capability='workbench_member.erase_member'").fetchone() == (0,)
    # C1: the subject holds another, active principal - the revoked one opens, the execution refuses (target_not_revoked).
    payload = admin.execute('select payload from control.nexloop_browser_memberships where principal_id=%s', (t['principal_id'],)).fetchone()[0]
    other = t['principal_id'] + '-second'
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)', (TENANT, other, t['subject_id'], Jsonb({**payload, 'principal_id': other})))
    revoke(c, t)
    spent = erase(c, t)
    assert spent.json()['status'] == 'execution_refused' and spent.json()['reason'] == 'target_not_revoked', spent.json()
    assert account(admin, t)[3]['status'] == 'active' and admin.execute('select count(*) from control.nexloop_workbench_member_erasures').fetchone() == (0,)
    admin.execute("""update control.nexloop_browser_memberships set payload=payload||'{"status":"revoked"}' where principal_id=%s""", (other,))
    assert erase(c, t, reason='member_request').json()['erased'] is True
    assert admin.execute('select principals from control.nexloop_workbench_member_erasures').fetchone() == (sorted([t['principal_id'], other]),)
    twice = erase(c, t, reason='other')
    assert twice.json()['status'] == 'execution_refused' and twice.json()['reason'] == 'already_erased', twice.json()


def test_an_invite_owner_members_frozen_username_is_rewritten_and_shown_erased(owners, admin):
    c = owners
    with_policies(c)
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        token = approved_run(c, c['owner_member'], c['second'], 'invite_workbench_owner', {'username': 'joined-owner', 'expires_in_hours': 24}).json()['token']
        assert accept(client, {'token': token, 'password': 'a-good-password-1'}).status_code == 200
    invite_approval = admin.execute("select approval_id::text from control.nexloop_approval_requests where capability='workbench_member.invite_owner'").fetchone()[0]
    principal, subject = admin.execute("select m.principal_id,m.subject_id from control.nexloop_browser_accounts a join control.nexloop_browser_memberships m using (subject_id) where a.username='joined-owner'").fetchone()
    joined = {'principal_id': principal, 'subject_id': subject}
    approved_run(c, c['owner_member'], c['second'], 'revoke_workbench_owner', {'target_principal': principal, 'reason': 'synthetic departure'})
    erase(c, joined)
    pseudonym = account(admin, joined)[0]
    assert admin.execute('select body->>%s from control.nexloop_approval_requests where approval_id=%s', ('username', invite_approval)).fetchone() == (pseudonym,)
    assert admin.execute("select count(*) from control.nexloop_workbench_invitations where username='joined-owner'").fetchone() == (0,)
    view = admin.execute("select runtime.nexloop_approval_view(%s,'real',%s)", (TENANT, c['owner_member']['principal_id'])).fetchone()[0]
    assert [r['target_member'] for r in view['mine'] if r['approval_id'] == invite_approval] == [{'kind': 'erased'}]
    # E1 (owner: A): the original username is free again - a namesake may be invited (same_username_exists false).
    waiting = c['call'](c['owner_member'], 'invite_workbench_owner', {'username': 'joined-owner', 'expires_in_hours': 48})  # another body (NX-056 1-hour rule)
    assert waiting.json()['status'] == 'waiting_approval', waiting.json()
    row = [r for r in admin.execute("select runtime.nexloop_approval_view(%s,'real',%s)", (TENANT, c['second']['principal_id'])).fetchone()[0]['to_decide']
           if r['approval_id'] == waiting.json()['approval_id']]
    assert row[0]['target_member'] == {'kind': 'invitee', 'username': 'joined-owner', 'same_username_exists': False}


def test_a_replay_after_a_restore_erases_again_with_the_original_time_and_executor(owners, admin):
    """C2, I-E4: the deletion ledger's entry replays the erasure on a restored directory - here a revoked member never erased in this
    database, with the entry of an earlier erasure's approval - and the ledger row carries the entry's time and that approval's executor."""
    c = owners
    with_policies(c)
    revoke(c, c['third'])
    first = erase(c, c['third'])
    approval = first.json()['approval_id']
    admin.execute("""update control.nexloop_browser_memberships set payload=payload||'{"status":"revoked"}' where principal_id=%s""", (c['operator_member']['principal_id'],))
    at = '2026-10-01T08:00:00+00:00'
    admin.execute("select runtime.nexloop_replay_begin('synthetic-restore',0)")
    out = admin.execute('select runtime.nexloop_replay_deletions(%s,%s)', ('synthetic-restore', Jsonb([
        {'kind': 'workbench_member', 'tenant_id': TENANT, 'world': 'real', 'subject_ref': c['operator_member']['subject_id'], 'request_ref': approval, 'cutoff': at},
        {'kind': 'workbench_member', 'tenant_id': TENANT, 'world': 'real', 'subject_ref': c['third']['subject_id'], 'request_ref': approval, 'cutoff': at}]))).fetchone()[0]
    assert out['workbench_members'] == 2
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    assert admin.execute("select erased_at,erased_by_principal from control.nexloop_workbench_member_erasures where subject_id=%s", (c['operator_member']['subject_id'],)).fetchone() == (
        __import__('datetime').datetime.fromisoformat(at), c['owner_member']['principal_id'])
    assert account(admin, c['operator_member'])[0].startswith('erased-') and account(admin, c['operator_member'])[1] is None
    assert admin.execute('select count(*) from control.nexloop_workbench_member_erasures where subject_id=%s', (c['third']['subject_id'],)).fetchone() == (1,)  # idempotent


def test_no_channel_brings_an_erased_member_back(owners, admin, pg):
    from nexloop_eios.workbench_roles import WorkbenchRolesRejected, apply_members
    from test_workbench_read_pg import ROLES, members_file
    c = owners
    with_policies(c)
    t = c['third']
    revoke(c, t)
    erase(c, t)
    # The configurator (design §6): a member file naming the erased principal is refused.
    # (the subject is disabled, so the configuration's own active-Human check refuses first; the erased guard backs it up)
    with pytest.raises((WorkbenchRolesRejected, psycopg.Error), match='member_erased|existing active Human'):
        apply_members(ROLES, members_file((c['owner_member'], 'owner'), (c['second'], 'owner'), (t, 'owner'), (c['operator_member'], 'operator')),
                      database_url_file=c['configurator'], named_revocations=[t['principal_id']])  # past X1: the erased guard refuses
    # 0198: the operator reset refuses a disabled account and subject.
    account_id = admin.execute('select local_account_id from control.nexloop_browser_accounts where subject_id=%s', (t['subject_id'],)).fetchone()[0]
    revision = account(admin, t)[3]['revision']
    with psycopg.connect(make_conninfo(pg, user='nexloop_identity')) as identity_db, pytest.raises(psycopg.Error):
        identity_db.execute('select control.nexloop_operator_reset_browser_password(%s,%s,%s,%s,%s,%s,%s,%s,%s,false)', (
            TENANT, c['config'].workbench.application_id, 'nexloop_identity', account_id, revision, '$argon2id$v=19$m=65536,t=3,p=4$synthetic', 'req-' + uuid.uuid4().hex, 'trace', 'synthetic'))
    assert account(admin, t)[1] is None
    # erased- is reserved: no invitation of such a username (the workbench refuses it; the database too).
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert act(client, csrf, 'invite_workbench_member', {'username': 'erased-0123456789abcdef', 'role': 'operator', 'expires_in_hours': 24}).status_code == 422
    with pytest.raises(psycopg.Error, match='username_reserved'):
        admin.execute("""insert into control.nexloop_workbench_invitations(tenant_id,request_id,username,role,token_hmac,issued_by_principal,issued_by_subject,
            issued_at,expires_at,state) values(%s,'reserved','erased-0123456789abcdef','operator',repeat('a',64),'x','x',clock_timestamp(),clock_timestamp()+interval '1 hour','issued')""", (TENANT,))


def test_the_configuration_guard_refuses_an_erased_subjects_principal(owners, admin):
    """The second line behind the configurator (design §6): any configuration version naming a principal of an erased subject is refused."""
    c = owners
    with_policies(c)
    revoke(c, c['third'])
    erase(c, c['third'])
    with pytest.raises(psycopg.Error, match='member_erased'):
        admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
            select tenant_id,manifest_version+1,roles_version,application_id,roles,members||jsonb_build_object(%s::text,'operator'),roles_digest,'synthetic-configurator'
            from control.nexloop_workbench_configurations where tenant_id=%s order by manifest_version desc limit 1""", (c['third']['principal_id'], TENANT))
