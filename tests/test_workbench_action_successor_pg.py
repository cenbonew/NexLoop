"""A Schema publication's successor Actions coexist with their predecessors (migration schema_publication_successor_coexistence; stage
R73 2026-11-09: Consumer v2 through NX-040 F3 added :2 beside the active :1 of all 98 Consumer-bound Actions, and every workbench
write failed with 'workbench capability published more than once').

Real workbench over HTTP (the alert silencing fixture: production create_app, members by the real configurator, grants compiled from
the role manifest naming :1). The successors are inserted exactly as authz.nexloop_schema_proposal_publish inserts them (active, the
same capability, version+1, built by the publication backend's successors()) for every Consumer-bound Action of the fixture; the F3 transaction itself is tests/test_nx040_*.
- the owner's write succeeds and executes the granted :1;
- roles granting :2 move it to :2;
- a member without a grant is forbidden;
- the governed entry still checks the exact resource: naming :2 while only :1 is granted is refused (no widening);
- two different Actions carrying one human capability are still refused.
Synthetic data."""
import copy
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.goal_controls import GoalGovernedActions
from nexloop_eios.schema_publication import successors
from nexloop_eios.workbench_roles import application_facts, compile_members
from test_alert_silence_pg import TENANT, silenced, until  # noqa: F401
from system_catalog_fixture import publish_successor_actions
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import CALLER, ROLES, members_file, workbench_login, write_facts

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
SILENCE = 'eios:action:nexloop.alert.silence:'


def publish_successors(admin):
    """What the F3 publication inserts: Consumer v2 (here only its version moves) and, built by the publication backend's own
    nexloop_eios.schema_publication.successors, the next version of each Action bound to Consumer - active, the same capability."""
    consumer = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=1", (TENANT,)).fetchone()[0]
    rows = admin.execute("""select definition,capability from control.nexloop_action_definitions where tenant_id=%s and world='real' and active
        and definition->'object_types' @> '[{"stable_name":"Consumer"}]'""", (TENANT,)).fetchall()
    assert any(d['stable_name'] == 'nexloop.alert.silence' for d, _ in rows)
    v2 = {**consumer, 'version': 2}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',2,%s)", (TENANT, Jsonb(v2)))
    publish_successor_actions(admin, TENANT, successors({'actions': [{'definition': d, 'capability': c} for d, c in rows]}, v2))
    return len(rows)


@pytest.fixture
def executed(monkeypatch):
    """The Action version each governed silence names; `force` replaces it (the no-widening probe)."""
    seen, force = [], {}
    original = GoalGovernedActions.silence_alert

    def recording(self, *, action_version, **kwargs):
        seen.append(action_version)
        try:
            return original(self, action_version=force.get('version', action_version), **kwargs)
        except Exception as failure:
            force.setdefault('errors', []).append(type(failure).__name__)
            raise
    monkeypatch.setattr(GoalGovernedActions, 'silence_alert', recording)
    return seen, force


def body(reason):
    return {'rule_id': 'queue_dead_letter', 'selector': 'operations', 'until': until(24), 'reason': reason}


def silences(admin):
    return admin.execute('select count(*) from control.nexloop_alert_silences').fetchone()[0]


def test_after_a_publication_the_owner_still_writes_with_the_granted_version(silenced, executed):  # noqa: F811
    s = silenced
    seen, _ = executed
    assert publish_successors(s['admin']) >= 1
    assert s['admin'].execute("select count(*) from control.nexloop_action_definitions where tenant_id=%s and active and resource_id like %s",
                              (TENANT, SILENCE + '%')).fetchone()[0] == 2
    with s['app']() as client:
        csrf = workbench_login(client, s['owner']).json()['csrf_token']
        done = act(client, csrf, 'silence_alert', body('发布后仍可静默'), key='wb-successor-silence-0001')
        assert done.status_code == 200 and done.json()['silenced'] is True, done.text
    assert seen == [1] and silences(s['admin']) == 1


def test_roles_granting_the_successor_move_the_write_to_it(silenced, executed):  # noqa: F811
    s = silenced
    seen, _ = executed
    publish_successors(s['admin'])
    roles = copy.deepcopy(ROLES)
    for name, actions in roles['roles'].items():
        roles['roles'][name] = [SILENCE + '2' if a == SILENCE + '1' else a for a in actions]
    write_facts(s['admin'], compile_members(roles, members_file((s['owner'], 'owner'), (s['operator'], 'operator'))))
    # The roles' caller application is restricted to the Actions the roles carry (as the configurator publishes it).
    write_facts(s['admin'], [application_facts(roles, TENANT, CALLER, valid_from=datetime.now(UTC))])
    with s['app']() as client:
        csrf = workbench_login(client, s['owner']).json()['csrf_token']
        done = act(client, csrf, 'silence_alert', body('角色改到第二版'), key='wb-successor-silence-0002')
        assert done.status_code == 200, done.text
    assert seen == [2] and silences(s['admin']) == 1


def test_without_a_grant_on_any_version_the_member_is_forbidden(silenced, executed):  # noqa: F811
    s = silenced
    seen, _ = executed
    publish_successors(s['admin'])
    with s['app']() as client:
        csrf = workbench_login(client, s['operator']).json()['csrf_token']
        denied = act(client, csrf, 'silence_alert', body('无授权'))
        assert denied.status_code == 403 and denied.json()['code'] == 'forbidden', denied.text
    assert seen == [] and silences(s['admin']) == 0


def test_the_governed_entry_still_checks_the_exact_version_it_executes(silenced, executed):  # noqa: F811
    """No widening: the owner holds :1 only; a write naming the successor :2 is refused by the governed entry and writes nothing."""
    s = silenced
    _, force = executed
    publish_successors(s['admin'])
    force['version'] = 2
    with s['app']() as client:
        csrf = workbench_login(client, s['owner']).json()['csrf_token']
        refused = act(client, csrf, 'silence_alert', body('越权到第二版'), key='wb-successor-silence-0003')
        assert refused.status_code == 503 and refused.json()['code'] == 'unavailable', refused.text
    # The caller has no authority facts for :2: the authorization fails closed before anything is written.
    assert force['errors'] == ['AuthorizationUnavailable'] and silences(s['admin']) == 0


def test_two_actions_with_one_human_capability_are_still_refused(silenced):  # noqa: F811
    s = silenced
    admin = s['admin']
    # The silence Action active a second time under another name (the same capability), through the catalog fixture's helper.
    definition, capability = admin.execute("select definition,capability from control.nexloop_action_definitions where tenant_id=%s and resource_id=%s",
                                           (TENANT, SILENCE + '1')).fetchone()
    publish_successor_actions(admin, TENANT, [{'definition': {**definition, 'stable_name': 'nexloop.alert.silence_copy'}, 'capability': capability}])
    assert admin.execute("select count(*) from control.nexloop_action_definitions where tenant_id=%s and active and resource_id=%s",
                         (TENANT, 'eios:action:nexloop.alert.silence_copy:1')).fetchone()[0] == 1
    with s['app']() as client:
        csrf = workbench_login(client, s['owner']).json()['csrf_token']
        failed = act(client, csrf, 'silence_alert', body('两个不同动作'), key='wb-successor-silence-0004')
        assert failed.status_code >= 400, failed.text
    assert silences(admin) == 0
