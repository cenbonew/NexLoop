"""NX-058 slice C without approval (docs/design/account-self-service.md v7 §1b, §1c, §4, §7) on PostgreSQL through the actual app: the
owner changes an operator's role, suspends, resumes and revokes a member. Each change revokes the old role's grants and grants the new
role's template (byte-identical to compile_member), appends a configuration version, writes the role history, ends the member's sessions
and unused reset codes, and is audited. Owner targets wait for the owner-only Actions (NX-056); the requester is never the target. The
configurator's apply refuses to undo a governed change (X1) except for named revocations (Z2), writes the role history of its own changes
and ends the sessions of a member it removes (S4). Synthetic members only."""
import dataclasses
import secrets
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import workbench_member_secrets as W
from nexloop_eios.http_api import create_app
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.workbench_roles import WorkbenchRolesRejected, apply_members, compile_member
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import ROLES, members_file, workbench_login

ACTIONS = ('nexloop.workbench_member.change_role', 'nexloop.workbench_member.suspend', 'nexloop.workbench_member.resume',
           'nexloop.workbench_member.revoke', 'nexloop.workbench_member.issue_reset')


@pytest.fixture
def changes(switching, admin, tmp_path):
    c = switching
    publish_registry_actions(admin, ACTIONS)
    key = tmp_path / 'identity-token-key'
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    c['config'] = dataclasses.replace(c['config'], workbench=dataclasses.replace(c['config'].workbench, identity_token_key_file=key))
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    return c


def grants_match(admin, member, role):
    for kind, key, fact in compile_member(ROLES, TENANT, {**member, 'role': role}, valid_from=datetime(2026, 10, 10, tzinfo=UTC)):
        row = admin.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s', (TENANT, kind, list(key))).fetchone()
        assert row and canonical_payload(row[0]) == canonical_payload(fact.model_dump(mode='json')), (kind, key)


def granted(admin, principal):
    return {key[1] for key, grants in admin.execute("select entity_key,payload->'grants' from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key[1]=%s",
                                                     (TENANT, principal)).fetchall() if grants}


def history(admin, principal):
    return admin.execute('select role,event from control.nexloop_workbench_role_history where principal_id=%s order by entry_id', (principal,)).fetchall()


def test_a_role_change_suspension_resumption_and_revocation(changes, admin):
    c = changes
    owner, operator = c['owner_member'], c['operator_member']
    p = operator['principal_id']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client, TestClient(create_app(c['config']), base_url=ORIGIN) as member:
        csrf = workbench_login(client, owner).json()['csrf_token']
        assert workbench_login(member, operator).status_code == 200
        code = act(client, csrf, 'issue_workbench_reset', {'target_principal': p}).json()['code']
        relogin = workbench_login(member, operator)
        assert relogin.status_code == 200
        old_csrf = relogin.json()['csrf_token']
        before = history(admin, p)
        # operator -> reviewer
        changed = act(client, csrf, 'change_workbench_member_role', {'target_principal': p, 'new_role': 'reviewer'})
        assert changed.status_code == 200 and changed.json()['role'] == 'reviewer', changed.json()
        assert member.get('/api/v1/workbench/auth/session').status_code == 401  # sessions ended
        # L6 slice C review S-5/S-a: the changed member's old session, with its own real CSRF token, is refused for an Action too.
        assert act(member, old_csrf, 'issue_workbench_reset', {'target_principal': owner['principal_id']}).status_code in (401, 403)
        assert admin.execute('select state,revoked_reason from control.nexloop_workbench_reset_codes').fetchone() == ('revoked', 'member_changed')
        assert W.valid_code(code)
        grants_match(admin, operator, 'reviewer')
        assert granted(admin, p) == set(ROLES['roles']['reviewer'])
        assert admin.execute('select members->>%s from control.nexloop_workbench_configurations order by manifest_version desc limit 1', (p,)).fetchone() == ('reviewer',)
        assert history(admin, p)[len(before):] == [('operator', 'stop'), ('reviewer', 'start')]
        same = act(client, csrf, 'change_workbench_member_role', {'target_principal': p, 'new_role': 'reviewer'})
        assert same.status_code == 409 and same.json()['details']['reason'] == 'role_unchanged'
        # suspend: no grants, no login; resume: the template again (AA1)
        assert act(client, csrf, 'suspend_workbench_member', {'target_principal': p, 'reason': 'synthetic leave'}).status_code == 200
        assert granted(admin, p) == set()
        assert workbench_login(member, operator).status_code == 401
        again = act(client, csrf, 'suspend_workbench_member', {'target_principal': p, 'reason': 'synthetic leave'})
        assert again.json()['details']['reason'] == 'target_not_active'
        assert act(client, csrf, 'resume_workbench_member', {'target_principal': p, 'reason': 'synthetic return'}).status_code == 200
        grants_match(admin, operator, 'reviewer')
        assert workbench_login(member, operator).status_code == 200
        # revoke: out of the configuration, membership revoked, no login, no grants
        assert act(client, csrf, 'revoke_workbench_member', {'target_principal': p, 'reason': 'synthetic departure'}).status_code == 200
        assert granted(admin, p) == set() and workbench_login(member, operator).status_code == 401
        assert admin.execute('select members ? %s from control.nexloop_workbench_configurations order by manifest_version desc limit 1', (p,)).fetchone() == (False,)
        gone = act(client, csrf, 'revoke_workbench_member', {'target_principal': p, 'reason': 'again'})
        assert gone.json()['details']['reason'] == 'target_not_active'
    assert history(admin, p)[len(before):] == [('operator', 'stop'), ('reviewer', 'start'), ('reviewer', 'stop'), ('reviewer', 'start'), ('reviewer', 'stop')]
    actions = [a for (a,) in admin.execute("select action from runtime.audit_events where action in ('nexloop.workbench_member.change_role','nexloop.workbench_member.suspend',"
                                            "'nexloop.workbench_member.resume','nexloop.workbench_member.revoke') order by created_at").fetchall()]
    assert actions == ['nexloop.workbench_member.change_role', 'nexloop.workbench_member.suspend', 'nexloop.workbench_member.resume', 'nexloop.workbench_member.revoke']
    assert admin.execute("select count(*) from runtime.audit_events where action='nexloop.workbench_member.grants_written'").fetchone()[0] == 5


def test_owner_targets_the_requester_and_an_operator(changes, admin):
    """An owner target waits for the owner-only Actions with approval (target_role_changed, slice C after NX-056); the requester is never
    the target; an operator holds none of these Actions."""
    from test_workbench_read_pg import add_human, write_facts
    from nexloop_eios.workbench_roles import compile_members
    c = changes
    second = add_human(admin, 'second-owner')
    members = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'), (second, 'owner'))
    apply_members(ROLES, members, database_url_file=c['configurator'])
    write_facts(admin, compile_members(ROLES, members))
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        for op, body in (('suspend_workbench_member', {'reason': 'x'}), ('revoke_workbench_member', {'reason': 'x'}),
                         ('change_workbench_member_role', {'new_role': 'operator'})):
            refused = act(client, csrf, op, {'target_principal': second['principal_id'], **body})
            assert refused.status_code == 409 and refused.json()['details']['reason'] == 'target_role_changed', (op, refused.json())
        self_target = act(client, csrf, 'suspend_workbench_member', {'target_principal': c['owner_member']['principal_id'], 'reason': 'x'})
        assert self_target.json()['details']['reason'] == 'target_is_requester'
        assert act(client, csrf, 'change_workbench_member_role', {'target_principal': c['operator_member']['principal_id'], 'new_role': 'owner'}).status_code == 422
        operator = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert act(client, operator, 'suspend_workbench_member', {'target_principal': second['principal_id'], 'reason': 'x'}).status_code == 403


def test_the_configurator_never_undoes_a_governed_change_unless_it_names_the_member(changes, admin):
    """X1 and Z2: after a governed role change, an apply whose member file still says operator is refused naming the member; the same
    file with the member named as a revocation passes the check (the drift is then the revocation's to settle)."""
    c = changes
    p = c['operator_member']['principal_id']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert act(client, csrf, 'change_workbench_member_role', {'target_principal': p, 'new_role': 'reviewer'}).status_code == 200
    stale = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'))
    with pytest.raises(WorkbenchRolesRejected, match=f'members_changed_by_governed_actions: {p}=reviewer'):
        apply_members(ROLES, stale, database_url_file=c['configurator'])
    import psycopg
    from psycopg.conninfo import make_conninfo
    with psycopg.connect(c['configurator'].read_text(), autocommit=True) as conf:
        named = conf.execute('select control.nexloop_workbench_governed_drift(%s,%s,%s)',
                             (TENANT, canonical_payload({c['owner_member']['principal_id']: 'owner'}), [p])).fetchone()[0]
    assert named == []
    current = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'reviewer'))
    assert apply_members(ROLES, current, database_url_file=c['configurator'])['configured'] is True  # the file follows: accepted


def test_a_configurator_removal_ends_the_member_sessions_and_writes_the_history(changes, admin):
    """S4: today a configurator revocation (G-WB-REVOKE: empty grant sets through 0050, then the member file without the member) only
    revoked grants; the role history sync now records the stop and ends the member's sessions."""
    from nexloop_eios.workbench_roles import revoke_member
    from test_workbench_read_pg import write_facts
    c = changes
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as member:
        assert workbench_login(member, operator).status_code == 200
        assert ('operator', 'start') in history(admin, operator['principal_id'])
        write_facts(admin, revoke_member(ROLES, TENANT, operator['principal_id']))  # what 0050 writes for the revocation manifest
        apply_members(ROLES, members_file((owner, 'owner')), database_url_file=c['configurator'], named_revocations=[operator['principal_id']])
        assert member.get('/api/v1/workbench/auth/session').status_code == 401
    assert history(admin, operator['principal_id'])[-1] == ('operator', 'stop')



def test_a_change_through_the_nx056_gate_runs_its_handler_in_the_savepoint_and_passes_the_recheck(changes, admin):
    """L6 slice C review I-1, on the code with the NX-056 gate (0327_nx056_approval_gate): under an approval policy the gate runs the
    member change's handler in a savepoint, so the tenant row's xmin is the subtransaction's. The effective revision does not test
    xmin, so the requester's recheck passes and the change executes once on the requester's resubmission."""
    from psycopg.types.json import Jsonb
    from test_workbench_read_pg import add_human, write_facts
    from nexloop_eios.workbench_roles import compile_members
    c = changes
    second = add_human(admin, 'second-owner')
    members = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'), (second, 'owner'))
    apply_members(ROLES, members, database_url_file=c['configurator'])
    write_facts(admin, compile_members(ROLES, members))
    publish_registry_actions(admin, ('nexloop.approvals.decide', 'nexloop.approvals.cancel'))
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,target_subject_field,published_by) values('workbench_member.suspend',1,1,null,1,'{owner}',86400,'{}','target_principal','synthetic-config')""")
    body = {'target_principal': c['operator_member']['principal_id'], 'reason': 'synthetic leave'}

    def call(member, operation, payload):
        with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
            csrf = workbench_login(client, member).json()['csrf_token']
            return act(client, csrf, operation, payload)
    first = call(c['owner_member'], 'suspend_workbench_member', body)
    assert first.status_code == 200 and first.json()['status'] == 'waiting_approval', first.json()
    assert call(second, 'decide_approval', {'approval_id': first.json()['approval_id'], 'decision': 'approve', 'reason': 'synthetic check'}).json()['status'] == 'approved'
    executed = call(c['owner_member'], 'suspend_workbench_member', body)
    assert executed.status_code == 200 and executed.json()['approval'] == 'consumed', executed.json()
    assert admin.execute("select result from control.nexloop_approval_consumptions").fetchall() == [('executed',)]
    assert admin.execute("select payload->>'status' from control.nexloop_browser_memberships where principal_id=%s", (c['operator_member']['principal_id'],)).fetchone() == ('suspended',)
    assert granted(admin, c['operator_member']['principal_id']) == set()


def test_a_governed_change_committed_while_the_configurator_waits_is_seen_by_its_check(changes, admin):
    """L6 slice C review N-1: the X1 check runs in the configurator's writing transaction under the member lock. A governed change that
    holds the lock while the configurator starts commits first; the check then sees it and refuses the stale member file (before, the
    check ran in its own transaction without the lock, and the write overwrote the change)."""
    import threading
    import time
    import psycopg
    c = changes
    p = c['operator_member']['principal_id']
    stale = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'))
    outcome = {}

    def run():
        try:
            outcome['result'] = apply_members(ROLES, stale, database_url_file=c['configurator'])
        except Exception as refused:  # noqa: BLE001 - the assertion below names the expected refusal
            outcome['result'] = refused
    with psycopg.connect(admin.info.dsn) as holder:  # what a governed member change holds: the tenant's approval lock
        holder.execute('select control.nexloop_approval_tenant_lock(%s)', (TENANT,))
        worker = threading.Thread(target=run)
        worker.start()
        for _ in range(100):
            if admin.execute("select count(*) from pg_stat_activity where usename='nexloop_configurator' and wait_event_type='Lock' and wait_event='advisory'").fetchone()[0]:
                break
            time.sleep(0.02)
        else:
            raise AssertionError('the configurator never waited for the member lock')
        # The governed change's configuration version (as nexloop_workbench_configuration_append writes it), committed while it waits.
        holder.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        holder.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
            select tenant_id,manifest_version+1,roles_version,application_id,roles,jsonb_set(members,array[%s],'"reviewer"'),roles_digest,'action:synthetic-change'
            from control.nexloop_workbench_configurations where tenant_id=%s order by manifest_version desc limit 1""", (p, TENANT))
        holder.commit()
    worker.join(15)
    assert isinstance(outcome.get('result'), WorkbenchRolesRejected) and f'{p}=reviewer' in str(outcome['result']), outcome
    assert admin.execute('select members->>%s,recorded_by from control.nexloop_workbench_configurations order by manifest_version desc limit 1', (p,)).fetchone() == ('reviewer', 'action:synthetic-change')


def test_revoking_a_suspended_member_also_revokes_grants_written_again(changes, admin):
    """L6 slice C review S-b: a configurator run may write a suspended member's grants again (it compiles every member of the file);
    revoking that member writes the revocation instance all the same, so no grant facts remain."""
    from test_workbench_read_pg import write_facts
    from nexloop_eios.workbench_roles import compile_member as compile_one
    c = changes
    operator = c['operator_member']
    p = operator['principal_id']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert act(client, csrf, 'suspend_workbench_member', {'target_principal': p, 'reason': 'synthetic leave'}).status_code == 200
        write_facts(admin, compile_one(ROLES, TENANT, {**operator, 'role': 'operator'}, valid_from=datetime(2026, 10, 10, tzinfo=UTC)))
        assert granted(admin, p) == set(ROLES['roles']['operator'])
        assert act(client, csrf, 'revoke_workbench_member', {'target_principal': p, 'reason': 'synthetic departure'}).status_code == 200
    assert granted(admin, p) == set()
    assert history(admin, p)[-1] == ('operator', 'stop')  # one stop, at the suspension


def test_the_x1_check_and_the_configuration_share_one_transaction(changes, admin, monkeypatch):
    """L6 slice C review S-f: the SQL lock alone also holds for a check in a transaction of its own, so this pins the Python side: the
    drift check and every write of apply_members run in one transaction (one transaction id), and the member lock is held at the write."""
    from nexloop_eios import trusted_configuration as T
    c = changes
    real_connect = T.configurator_connection
    seen = []

    class Spy:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *exc):
            return self.db.__exit__(*exc)

        def execute(self, sql, params=None):
            result = self.db.execute(sql, params)
            if 'governed_drift' in sql or 'nexloop_configure_workbench(' in sql:
                seen.append(self.db.execute("select pg_current_xact_id()::text,(select count(*) from pg_locks where pid=pg_backend_pid() "
                                            "and locktype='advisory' and granted)").fetchone())
            return result
    monkeypatch.setattr(T, 'configurator_connection', lambda path: Spy(real_connect(path)))
    apply_members(ROLES, members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator')), database_url_file=c['configurator'])
    assert len(seen) == 2 and seen[0][0] == seen[1][0] and all(locks >= 1 for _, locks in seen), seen


def test_a_held_member_lock_past_the_timeout_is_a_named_refusal(changes, admin):
    """L6 slice C review S-g: a governed change holding the member lock past lock_timeout ends the configurator run with a named
    refusal, not a bare LockNotAvailable."""
    import psycopg
    c = changes
    with psycopg.connect(admin.info.dsn) as holder:
        holder.execute('select control.nexloop_approval_tenant_lock(%s)', (TENANT,))
        with pytest.raises(WorkbenchRolesRejected, match='workbench_members_locked'):
            apply_members(ROLES, members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator')), database_url_file=c['configurator'])
