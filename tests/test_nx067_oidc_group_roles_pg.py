"""NX-067 IdP groups only narrow (docs/design/nx067-oidc-group-roles-and-disable-sync.md §2, Z1, Z2, Z5) on PostgreSQL through the actual
app and the in-process IdP (nothing leaves the process): the owner's governed mapping; the ceiling a login computes from the ID token's
groups (an intersection with the manifest role, read from the current link row on every check); refusals that never widen anything; a
changed mapping forcing a new SSO login with an empty ceiling and only a cleared one restoring null; owner promotion removing the ceiling;
the ceiling enforced both where the workbench entry picks the Action and again where the governed execution reads the grant."""
import json

import pytest
from fastapi.testclient import TestClient

from commercial_fixture import publish_registry_actions
from test_browser_http import ORIGIN
from test_workbench_actions_http import act
from test_workbench_oidc_pg import ACTIONS, app, link, sign_in, sso  # noqa: F401
from test_workbench_read_pg import ROLES, members_file, workbench_login
from nexloop_eios.workbench_roles import apply_members
from test_real_dispatch_switch_pg import switching  # noqa: F401
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

SUBJECT = 'idp-sub-nx067-groups'
OPERATOR_ACTION = 'eios:action:nexloop.human_question.answer:1'
QUESTION = '00000000-0000-4000-8000-000000000067'


@pytest.fixture
def groups(sso, admin):
    publish_registry_actions(admin, ('nexloop.workbench_member.set_sso_group_mapping', 'nexloop.human_question.answer'))
    return sso


def mapping(client, csrf, mappings, revision, *, claim='groups', provider='corp'):
    return act(client, csrf, 'set_sso_group_mapping', {'provider_id': provider, 'group_claim': claim, 'mappings': mappings, 'expected_revision': revision})


def operator_act(client):
    """An operator Action: 403 when the ceiling excludes it, any other answer (the question does not exist) when the grant holds."""
    csrf = client.post('/api/v1/workbench/auth/csrf', headers={'Origin': ORIGIN}).json()['csrf_token']
    return act(client, csrf, 'answer_human_question', {'question_id': QUESTION, 'answer_kind': 'withdraw', 'option_index': None, 'text': None}).status_code


def ceiling(admin, principal):
    return admin.execute('select role_ceiling from control.nexloop_workbench_external_identities where principal_id=%s', (principal,)).fetchone()


def permitted(admin, principal, resource=OPERATOR_ACTION):
    return admin.execute('select control.nexloop_workbench_action_permitted(%s,%s,%s)', (TENANT, principal, resource)).fetchone()[0]


def events(admin):
    return [(r, f, t) for r, f, t in admin.execute(
        'select reason,from_ceiling,to_ceiling from control.nexloop_workbench_role_ceiling_events order by created_at,event_id')]


def failures(admin):
    return [r for (r,) in admin.execute("select payload->>'reason' from runtime.audit_events where action='nexloop.workbench_member.sso_login_failed' order by created_at")]


def login(member, idp, groups_claim):
    idp.tamper = {} if groups_claim is ... else {'groups': groups_claim}
    try:
        return sign_in(member, idp, SUBJECT).headers['location']
    finally:
        idp.tamper = {}


def test_without_a_mapping_nothing_changes(groups, admin):
    c = groups
    operator = c['operator_member']['principal_id']
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(owner, c['owner_member']).json()['csrf_token']
        assert link(owner, csrf, operator, SUBJECT).status_code == 200
        assert login(member, c['idp'], ['anything']) == '/workbench'
        assert ceiling(admin, operator) == (None,) and permitted(admin, operator) is True
        assert operator_act(member) != 403
    assert events(admin) == []


def test_mapped_groups_set_the_ceiling_and_unmapped_ones_lock_every_session(groups, admin):
    c, idp = groups, groups['idp']
    operator = c['operator_member']['principal_id']
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member, TestClient(app(c), base_url=ORIGIN) as password:
        csrf = workbench_login(owner, c['owner_member']).json()['csrf_token']
        assert link(owner, csrf, operator, SUBJECT).status_code == 200
        set_ = mapping(owner, csrf, {'corp-ops': 'operator', 'corp-review': 'reviewer'}, 0)
        assert set_.status_code == 200 and set_.json()['revision'] == 1 and set_.json()['links_affected'] == 1, set_.json()
        assert ceiling(admin, operator) == ([],)  # Z2: the first mapping too forces a new SSO login
        # Mapped: the ceiling is the mapped roles; the operator keeps its operator Actions.
        assert login(member, idp, ['corp-ops', 'unrelated']) == '/workbench'
        assert ceiling(admin, operator) == (['operator'],) and operator_act(member) != 403
        assert workbench_login(password, c['operator_member']).status_code == 200
        # Groups that map only to reviewer: operator and reviewer share no Action, so the intersection is empty - refused, ceiling {}.
        assert login(member, idp, ['corp-review']) == '/workbench?sso=failed'
        assert ceiling(admin, operator) == ([],)
        assert operator_act(password) == 403  # the password session is narrowed too (L6 point 1)
        assert permitted(admin, operator, 'eios:action:nexloop.workbench.read:1') is False
        # No mapped group at all: refused, ceiling stays {}.
        assert login(member, idp, ['nobody-knows']) == '/workbench?sso=failed'
        # Mapped again: back to operator.
        assert login(member, idp, ['corp-ops']) == '/workbench'
        assert ceiling(admin, operator) == (['operator'],) and operator_act(member) != 403
    assert failures(admin) == ['no_mapped_role', 'no_mapped_role']
    assert events(admin) == [('mapping_changed', None, []), ('sso_login', [], ['operator']), ('no_mapped_role', ['operator'], []),
                             ('sso_login', [], ['operator'])]
    # Audit and events: roles and the mapping revision only, never group names or the IdP subject.
    audit = json.dumps([p for (p,) in admin.execute("select payload from runtime.audit_events where action='nexloop.workbench_member.sso_role_ceiling_changed'")])
    rows = json.dumps([list(map(str, r)) for r in admin.execute('select * from control.nexloop_workbench_role_ceiling_events')])
    for secret in ('corp-ops', 'corp-review', 'nobody-knows', SUBJECT):
        assert secret not in audit and secret not in rows


@pytest.mark.parametrize('claim', [..., 'corp-ops', [1, 2], [''], ['x' * 257], None])
def test_a_missing_or_malformed_groups_claim_is_refused_and_leaves_the_ceiling(groups, admin, claim):
    c, idp = groups, groups['idp']
    operator = c['operator_member']['principal_id']
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(owner, c['owner_member']).json()['csrf_token']
        assert link(owner, csrf, operator, SUBJECT).status_code == 200
        assert mapping(owner, csrf, {'corp-ops': 'operator'}, 0).status_code == 200
        assert login(member, idp, ['corp-ops']) == '/workbench'
        assert login(member, idp, claim) == '/workbench?sso=failed'
        assert ceiling(admin, operator) == (['operator'],)  # unchanged: nothing can be inferred
    assert failures(admin) == ['groups_invalid']


def test_the_mapping_is_the_owners_governed_action_and_never_maps_to_owner(groups, admin):
    c = groups
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as other:
        csrf = workbench_login(owner, c['owner_member']).json()['csrf_token']
        assert mapping(owner, csrf, {'corp-admins': 'owner'}, 0).status_code == 422  # the entry refuses it
        assert mapping(owner, csrf, {'corp-ops': 'operator'}, 0, provider='unknown').json()['details'] == {'reason': 'provider_unknown'}
        first = mapping(owner, csrf, {'corp-ops': 'operator'}, 0)
        assert first.status_code == 200 and first.json()['revision'] == 1
        stale = mapping(owner, csrf, {'corp-ops': 'reviewer'}, 0)
        assert stale.status_code == 409 and stale.json()['details'] == {'reason': 'revision_conflict'}
        key = 'wb-' + '0' * 20 + '67'
        replay = [act(owner, csrf, 'set_sso_group_mapping', {'provider_id': 'corp', 'group_claim': 'groups', 'mappings': {'a': 'reviewer'},
                                                              'expected_revision': 1}, key=key) for _ in range(2)]
        assert [r.status_code for r in replay] == [200, 200], [r.json() for r in replay]
        assert replay[0].json()['revision'] == 2 and replay[0].json()['replayed'] is False and replay[1].json()['replayed'] is True
        operator_csrf = workbench_login(other, c['operator_member']).json()['csrf_token']
        assert mapping(other, operator_csrf, {'corp-ops': 'operator'}, 2).status_code == 403
    # SQL refuses an owner mapping even when called past the entry.
    with pytest.raises(Exception, match='workbench member: invalid'):
        admin.execute("select control.nexloop_governed_set_sso_group_mapping(%s,'real',%s,'s','i',%s::jsonb)",
                      (TENANT, c['owner_member']['principal_id'], json.dumps({'operation': 'set_sso_group_mapping', 'request_id': 'direct-1',
                       'provider_id': 'corp', 'group_claim': 'groups', 'mappings': {'corp-admins': 'owner'}, 'expected_revision': 2})))
    assert [r for (r,) in admin.execute('select revision from control.nexloop_workbench_oidc_group_mappings order by revision')] == [1, 2]
    audit = admin.execute("select payload from runtime.audit_events where action='nexloop.workbench_member.set_sso_group_mapping' order by created_at").fetchall()
    assert [a['revision'] for (a,) in audit] == [1, 2] and audit[1][0]['previous_mappings'] == {'corp-ops': 'operator'}


def test_z2_a_changed_mapping_forces_a_new_sso_login_and_only_clearing_restores_null(groups, admin):
    c, idp = groups, groups['idp']
    operator = c['operator_member']['principal_id']
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member:
        csrf = workbench_login(owner, c['owner_member']).json()['csrf_token']
        assert link(owner, csrf, operator, SUBJECT).status_code == 200
        assert mapping(owner, csrf, {'corp-ops': 'operator'}, 0).status_code == 200
        assert login(member, idp, ['corp-ops']) == '/workbench' and ceiling(admin, operator) == (['operator'],)
        changed = mapping(owner, csrf, {'corp-ops': 'operator', 'corp-review': 'reviewer'}, 1)
        assert changed.status_code == 200 and changed.json()['cleared'] is False
        assert ceiling(admin, operator) == ([],)  # never null after a change
        assert member.get('/api/v1/workbench/auth/session').status_code == 401  # the session ended
        assert login(member, idp, ['corp-ops']) == '/workbench' and ceiling(admin, operator) == (['operator'],)
        cleared = mapping(owner, csrf, {}, 2)
        assert cleared.status_code == 200 and cleared.json()['cleared'] is True
        assert ceiling(admin, operator) == (None,)
        assert member.get('/api/v1/workbench/auth/session').status_code == 200  # clearing only gives back the manifest role
        # Without a mapping the groups claim is not read at all.
        assert login(member, idp, ...) == '/workbench' and ceiling(admin, operator) == (None,)
    assert [e[0] for e in events(admin)] == ['mapping_changed', 'sso_login', 'mapping_changed', 'sso_login', 'mapping_cleared']


def test_z1_an_owner_promotion_removes_the_ceiling_and_a_logout_token_for_its_subject_changes_nothing(groups, admin):
    c, idp = groups, groups['idp']
    owner_m, operator_m = c['owner_member'], c['operator_member']
    providers = json.loads(c['providers'].read_text())
    providers['providers'][0]['backchannel_logout'] = True
    c['providers'].write_text(json.dumps(providers))
    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as member, TestClient(app(c), base_url=ORIGIN) as idp_server:
        csrf = workbench_login(owner, owner_m).json()['csrf_token']
        assert link(owner, csrf, operator_m['principal_id'], SUBJECT).status_code == 200
        assert mapping(owner, csrf, {'corp-review': 'reviewer'}, 0).status_code == 200
        assert login(member, idp, ['corp-review']) == '/workbench?sso=failed' and ceiling(admin, operator_m['principal_id']) == ([],)
        apply_members(ROLES, members_file((owner_m, 'owner'), (operator_m, 'owner')), database_url_file=c['configurator'])
        assert ceiling(admin, operator_m['principal_id']) is None  # the link (and its ceiling) is gone
        assert permitted(admin, operator_m['principal_id'], 'eios:action:nexloop.workbench_member.link_external:1') is True
        promoted = TestClient(app(c), base_url=ORIGIN)
        with promoted:
            assert workbench_login(promoted, operator_m).status_code == 200
            answer = idp_server.post('/api/v1/workbench/auth/oidc/backchannel/corp', content='logout_token=' + idp.logout_token(SUBJECT),
                                     headers={'content-type': 'application/x-www-form-urlencoded'})
            assert answer.status_code == 200
            assert promoted.get('/api/v1/workbench/auth/session').status_code == 200  # the owner's session is not touched


def test_z5_the_ceiling_is_checked_again_at_execution_and_at_dispatch(groups, admin):
    """Planning says yes, execution says no: with the entry's Action pick swapped back to its unchecked body the browser authorization
    finds no grant fact and fails closed (503, nothing runs); with the grant read swapped back too, the governed dispatch's own SQL
    transaction refuses (403). Without the ceiling the same call passes all three."""
    c = groups
    operator = c['operator_member']['principal_id']

    def unchecked(name, signature):
        admin.execute(f'alter function authz.{name}({signature}) rename to {name}_checked')
        admin.execute(f'alter function authz.{name}_before_role_ceiling({signature}) rename to {name}')
        admin.execute(f'grant execute on function authz.{name}({signature}) to nexloop_api')

    with TestClient(app(c), base_url=ORIGIN) as owner, TestClient(app(c), base_url=ORIGIN) as password:
        csrf = workbench_login(owner, c['owner_member']).json()['csrf_token']
        assert link(owner, csrf, operator, SUBJECT).status_code == 200
        assert mapping(owner, csrf, {'corp-review': 'reviewer'}, 0).status_code == 200
        assert ceiling(admin, operator) == ([],)
        assert workbench_login(password, c['operator_member']).status_code == 200
        assert operator_act(password) == 403  # the entry
        unchecked('nexloop_workbench_action_for', 'text,text,text')
        assert operator_act(password) == 503  # the grant read: withheld, the decision cannot be made
        unchecked('nexloop_browser_authority_fact_snapshot', 'text,text,text,text[]')
        assert operator_act(password) == 403  # the dispatch
        admin.execute('update control.nexloop_workbench_external_identities set role_ceiling=null where principal_id=%s', (operator,))
        assert operator_act(password) not in (403, 503)  # the control: without the ceiling the same call passes all three
    assert admin.execute("select count(*) from runtime.audit_events where action like 'nexloop.human_question%'").fetchone() == (0,)
