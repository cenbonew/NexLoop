"""NX-058 workbench UI: the owner's members view (GET /api/v1/workbench/members, verb 'members' of the workbench observe read) on
PostgreSQL through the actual app - members with label, username, role, statuses and the vote cooldown's end; unexpired invitations; the
last 90 days of reset codes - for the owner only, and never a token, code, HMAC, source digest or email. Synthetic members only."""
import json

from fastapi.testclient import TestClient

from nexloop_eios import workbench_member_secrets as W
from nexloop_eios.http_api import create_app
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_owner_member_actions_pg import owners, with_policies  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login


def read(c, member):
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        workbench_login(client, member)
        return client.get('/api/v1/workbench/members', headers={'Origin': ORIGIN})


def test_the_owner_sees_members_invitations_and_resets_without_any_secret(owners, admin):
    c = owners
    with_policies(c)
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        token = act(client, csrf, 'invite_workbench_member', {'username': 'viewed-reviewer', 'role': 'reviewer', 'expires_in_hours': 24}).json()['token']
        code = act(client, csrf, 'issue_workbench_reset', {'target_principal': c['operator_member']['principal_id']}).json()['code']
    response = read(c, c['owner_member'])
    assert response.status_code == 200, response.text
    view = response.json()
    by = {m['principal_id']: m for m in view['members']}
    assert by[c['operator_member']['principal_id']]['role'] == 'operator' and by[c['third']['principal_id']]['role'] == 'owner'
    owner = by[c['owner_member']['principal_id']]
    assert set(owner) == {'principal_id', 'label', 'username', 'role', 'member_status', 'account_status', 'vote_eligible_from', 'erased'}
    assert owner['label'].startswith('p-') and len(owner['label']) == 10 and owner['username'] == c['owner_member']['username'] and owner['erased'] is False
    assert [(i['username'], i['role']) for i in view['invitations']] == [('viewed-reviewer', 'reviewer')]
    assert [(r['target_principal'], r['state'], r['kind']) for r in view['resets']] == [(c['operator_member']['principal_id'], 'issued', 'member')]
    key = W.load_key(c['config'].workbench.identity_token_key_file)
    text = json.dumps(view)
    for secret in (token, code, W.digest(key, W.INVITE, TENANT, token), W.digest(key, W.RESET, TENANT, code), '@example.invalid', 'argon2'):
        assert secret not in text


def test_only_the_owner_reads_the_members_view(owners):
    c = owners
    assert read(c, c['operator_member']).status_code == 403
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        assert client.get('/api/v1/workbench/members', headers={'Origin': ORIGIN}).status_code == 401  # no session
        workbench_login(client, c['owner_member'])
        assert client.get('/api/v1/workbench/members?x=1', headers={'Origin': ORIGIN}).status_code == 422
