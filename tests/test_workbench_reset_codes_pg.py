"""NX-058 slice B (docs/design/account-self-service.md §2, §1b, §4) on PostgreSQL through the actual app (create_app, real workbench login
realm): the owner issues a one-time reset code for an operator; the code is shown once, stored by its HMAC only, and every session of the
operator ends at once; the operator completes it with the code and a new password only (no username, no session, the tenant is the
server's); every refusal is the same 400; a reissue revokes the earlier code; five rule failures void a code; an issuer who is no longer
an active owner voids the completion; the notice shows on every login for 7 days and on the member's own history; audits name issuer and
completer, never the code. Synthetic members only."""
import dataclasses
import json
import secrets

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import workbench_member_secrets as W
from nexloop_eios.http_api import create_app
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

RESET = 'nexloop.workbench_member.issue_reset'


@pytest.fixture
def resets(switching, admin, tmp_path):
    c = switching
    publish_registry_actions(admin, (RESET,))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'password_reset_complete','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    key = tmp_path / 'identity-token-key'
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    c['config'] = dataclasses.replace(c['config'], workbench=dataclasses.replace(c['config'].workbench, identity_token_key_file=key))
    c['key'] = W.load_key(key)
    return c


def reset(client, body):
    return client.post('/api/v1/workbench/auth/password/reset', headers={'Origin': ORIGIN}, json=body)


def login(client, human, password=None):
    return workbench_login(client, {**human, 'password': password or human['password']})


def test_the_owner_issues_a_code_and_the_operator_resets_with_it(resets, admin):
    c = resets
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client, TestClient(create_app(c['config']), base_url=ORIGIN) as member:
        assert login(member, operator).status_code == 200 and member.get('/api/v1/workbench/auth/session').status_code == 200
        csrf = login(client, owner).json()['csrf_token']
        issued = act(client, csrf, 'issue_workbench_reset', {'target_principal': operator['principal_id']})
        assert issued.status_code == 200, issued.json()
        code = issued.json()['code']
        assert W.valid_code(code) and issued.json()['target_principal'] == operator['principal_id']
        # Every session of the operator ended at once; the code is in the database only as its HMAC.
        assert member.get('/api/v1/workbench/auth/session').status_code == 401
        stored = admin.execute("select code_hmac,state,issued_by_principal from control.nexloop_workbench_reset_codes").fetchall()
        assert stored == [(W.digest(c['key'], W.RESET, TENANT, code), 'issued', owner['principal_id'])]
        dump = json.dumps(admin.execute("select to_jsonb(r) from control.nexloop_workbench_reset_codes r").fetchall()) + json.dumps(
            admin.execute("select payload from runtime.audit_events where action like 'nexloop.workbench_member.%'").fetchall())
        assert code not in dump
        # Refusals: all one answer.
        for body in ({'code': code[:-1] + ('A' if code[-1] != 'A' else 'B'), 'password': 'a-new-password-1'}, {'code': code}, {'code': code, 'password': 'short'},
                     {'code': code, 'password': operator['password']}, {'code': code, 'password': 'a-new-password-1', 'username': 'staff-operator'},
                     {'code': code, 'password': 'a-new-password-1', 'tenant_id': TENANT}):
            refused = reset(client, body)
            assert (refused.status_code, refused.json()) == (400, {'code': 'reset_invalid'}), body
        assert admin.execute('select failures from control.nexloop_workbench_reset_codes').fetchone()[0] == 2  # short, and the current password
        done = reset(client, {'code': code, 'password': 'a-new-password-1'})
        assert (done.status_code, done.json()) == (200, {'code': 'password_reset'})
        assert reset(client, {'code': code, 'password': 'another-password-2'}).status_code == 400  # one-time
        assert login(member, operator).status_code == 401
        assert login(member, operator, 'a-new-password-1').status_code == 200
        notices = member.get('/api/v1/workbench/auth/session').json()['reset_notices']
        assert [n['issued_by'] for n in notices] == [owner['principal_id']]
        assert member.get('/api/v1/workbench/auth/reset-history').json()['resets'][0]['issued_by'] == owner['principal_id']
    audits = admin.execute("select action,actor_id,payload->>'issued_by' from runtime.audit_events where action like 'nexloop.workbench_member.%' order by created_at").fetchall()
    assert audits == [('nexloop.workbench_member.issue_reset', owner['principal_id'], None),
                      ('nexloop.workbench_member.reset_completed', operator['principal_id'], owner['principal_id'])]
    event = admin.execute("select details from control.nexloop_browser_login_events where event_type='owner_issued_password_reset'").fetchone()[0]
    assert event['same_source_as_issuer'] is True  # both requests came from the test client's address: a signal, not a refusal


def test_targets_reissue_failures_and_a_former_owner(resets, admin):
    c = resets
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = login(client, owner).json()['csrf_token']
        issue = lambda target: act(client, csrf, 'issue_workbench_reset', {'target_principal': target})  # noqa: E731
        assert (issue(owner['principal_id']).json()['details'] or {}).get('reason') == 'target_is_requester'
        assert issue('nobody-principal').json()['details']['reason'] == 'target_not_active'
        first, second = issue(operator['principal_id']).json()['code'], issue(operator['principal_id']).json()['code']
        assert reset(client, {'code': first, 'password': 'a-new-password-1'}).status_code == 400  # revoked by the reissue
        for _ in range(5):
            assert reset(client, {'code': second, 'password': 'short'}).status_code == 400
        assert admin.execute("select state from control.nexloop_workbench_reset_codes where failures=5").fetchone() == ('voided',)
        assert reset(client, {'code': second, 'password': 'a-new-password-1'}).status_code == 400
        third = issue(operator['principal_id']).json()['code']
    # The issuer is no longer an active owner when the code is used: the completion is refused like any other.
    admin.execute("""update control.nexloop_browser_memberships set payload=jsonb_set(payload,'{status}','"suspended"') where principal_id=%s""", (owner['principal_id'],))
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        assert reset(client, {'code': third, 'password': 'a-new-password-1'}).status_code == 400
    # Rate limited by source: the policy's window refuses with 429 once exhausted (here 50 a minute).
    assert admin.execute("select count(*) from control.nexloop_browser_rate_counters where action='password_reset_complete'").fetchone()[0] == 1


def test_without_the_key_both_ends_are_unavailable(switching, admin):
    c = switching
    publish_registry_actions(admin, (RESET,))
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = login(client, c['owner_member']).json()['csrf_token']
        assert act(client, csrf, 'issue_workbench_reset', {'target_principal': c['operator_member']['principal_id']}).status_code == 503
        assert reset(client, {'code': 'A' * 22, 'password': 'a-new-password-1'}).status_code == 503
    assert admin.execute('select count(*) from control.nexloop_workbench_reset_codes').fetchone() == (0,)


def test_the_operator_cannot_issue_and_an_owner_target_is_not_this_action(resets):
    c = resets
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = login(client, c['operator_member']).json()['csrf_token']
        assert act(client, csrf, 'issue_workbench_reset', {'target_principal': c['owner_member']['principal_id']}).status_code == 403


def test_another_owner_is_refused_by_the_role_recheck(resets, admin):
    """§1b (V1): the action is for operators and reviewers; the target's current role is read under the member lock, so another owner
    is refused (target_role_changed) whatever the request said - owners are reset by issue_owner_reset, with approval (slice C)."""
    from nexloop_eios.workbench_roles import apply_members, compile_members
    from test_workbench_read_pg import ROLES, add_human, members_file, write_facts
    c = resets
    second = add_human(admin, 'second-owner')
    members = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'), (second, 'owner'))
    apply_members(ROLES, members, database_url_file=c['configurator'])
    write_facts(admin, compile_members(ROLES, members))
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = login(client, c['owner_member']).json()['csrf_token']
        refused = act(client, csrf, 'issue_workbench_reset', {'target_principal': second['principal_id']})
        assert refused.status_code == 409 and refused.json()['details']['reason'] == 'target_role_changed'
    assert admin.execute('select count(*) from control.nexloop_workbench_reset_codes').fetchone() == (0,)
