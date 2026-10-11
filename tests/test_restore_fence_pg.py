"""NX-035 local part: the restore fence (NXR01), the effect-window reconciliation and opening by scope (D8), on clean catalog PG.

The 'restored' database here is the fixture database with a restore row written by the owner functions the restore tool
calls (the real point-in-time restore is test_restore_drill_pg). Actual governed effect executor with the independent effect
Worker and loopback provider (commitment_fixture); production create_app with the workbench realm, members applied by the
real configurator, grants compiled from the role manifest (v7: nexloop.restore.open_scope owner only), the Action compiled
from business-actions (v13). Synthetic data.
"""
import secrets
import time
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app
from nexloop_eios.workbench_roles import apply_members, compile_members
from support.effect_provider import effect_provider
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import CALLER, ROLES, WORKBENCH_APP, add_human, members_file, workbench_login, write_facts

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
REF = 'restore-fence-1'


def owner_sql(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute(sql, params).fetchone()[0]


def refusals(admin, code):
    return admin.execute('select count(*) from runtime.nexloop_dispatch_refusals where code=%s', (code,)).fetchone()[0]


@pytest.fixture
def restoring(commitments, identity, uow, admin, pg, tmp_path):
    c = commitments
    c.configure_categories()
    publish_registry_actions(admin, ('nexloop.restore.open_scope',))

    def private(name, content):
        path = tmp_path / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(0o600)
        return path
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    owner, operator = add_human(admin, 'owner'), add_human(admin, 'operator')
    admin.execute('alter role nexloop_configurator login')
    configurator = private('configurator-dsn', make_conninfo(pg, user='nexloop_configurator'))
    members = members_file((owner, 'owner'), (operator, 'operator'))
    apply_members(ROLES, members, database_url_file=configurator)
    write_facts(admin, compile_members(ROLES, members))
    signer = c['signer']
    (tmp_path / 'app-artifacts').mkdir(mode=0o700)
    config = ApiConfiguration(private('business-dsn', make_conninfo(pg, user='nexloop_api')), private('authority-key', signer.material),
        tmp_path / 'app-artifacts', signer.key_id, BrowserConfiguration(private('identity-dsn', identity[0]), private('rate-key', secrets.token_hex(32)),
        TENANT, 'synthetic-browser-app', ORIGIN), execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, ORIGIN))
    c.update(app=lambda: TestClient(create_app(config), base_url=ORIGIN), owner_member=owner, operator_member=operator)
    return c


def test_fence_refuses_dispatch_until_the_owner_opens_the_scope_and_a_paused_channel_stays_blocked(restoring, admin, tmp_path):
    c = restoring
    queued = c.submit({'service': '后台退款'})['intent_id']
    action_type = admin.execute('select action_name from runtime.nexloop_effect_intents where intent_id=%s', (queued,)).fetchone()[0]
    assert owner_sql(admin, 'select runtime.nexloop_restore_readiness()') == {'ready': True, 'reads': True, 'dispatch': True, 'state': 'never_restored'}
    started = admin.execute('select clock_timestamp()').fetchone()[0]
    # The restore tool writes the fence before any application role may log in.
    owner_sql(admin, 'select runtime.nexloop_restore_begin(%s,%s,%s,%s,1)', (REF, Jsonb({'backup_id': '20261011-010000F', 'target_name': 'drill-x'}), started, started))
    assert owner_sql(admin, 'select runtime.nexloop_restore_readiness()') == {'ready': False, 'reads': False, 'dispatch': False, 'state': 'restored'}
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        assert c.dispatch(provider)['status'] == 'admission_unavailable' and provider.control('snapshot')['effects'] == 0
        assert refusals(admin, 'NXR01') == 1  # recorded by the existing effect refusal path (NX-030 D8)
        # Both dispatch checks (Run tasks and effects) go through the wrapped function.
        # (0220 keeps the intent check as …_before_real_dispatch_v0220 and adds the real dispatch switch after it.)
        for f in ('authz.nexloop_assert_task_dispatch_controls(text,text,text)', 'authz.nexloop_assert_intent_dispatch_controls_before_real_dispatch_v0220(text,text,uuid)'):
            assert 'authz.nexloop_assert_dispatch_controls(' in admin.execute('select prosrc from pg_proc where oid=%s::regprocedure', (f,)).fetchone()[0]
        # Opening is refused before verification.
        with c['app']() as client:
            csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
            assert act(client, csrf, 'open_restore_scope', {'restore_ref': REF}).status_code == 403
        # Replay (nothing newer in the journal here), end of replay, then the effect window: the queued intent was open at the
        # restore time, and a time-queryable channel reports an execution the database does not know.
        assert owner_sql(admin, 'select runtime.nexloop_restore_replay(%s,%s)', (REF, Jsonb([]))) == {
            'deletions': 0, 'deletion_result': {}, 'contact_restrictions': 0, 'erased_contact_keys': 0, 'service_credentials': 0, 'memberships': 0}
        replayed = owner_sql(admin, 'select runtime.nexloop_restore_replayed(%s)', (REF,))
        assert replayed['state'] == 'replayed' and replayed['run_credentials_revoked'] >= 0
        assert admin.execute("select count(*) from authz.nexloop_run_credentials where status='active'").fetchone() == (0,)
        unknown = str(uuid.uuid4())
        window = owner_sql(admin, 'select runtime.nexloop_restore_reconcile(%s,clock_timestamp(),%s)', (REF, Jsonb([
            {'tenant_id': TENANT, 'world': 'real', 'action_type': action_type, 'time_queryable': True, 'external_intent_ids': []},
            {'tenant_id': TENANT, 'world': 'real', 'action_type': 'synthetic.notify', 'time_queryable': True, 'external_intent_ids': [unknown]}])))
        report = {(r['tenant_id'], r['action_type']): r for r in window['channels']}
        assert report[(TENANT, action_type)] == {'tenant_id': TENANT, 'world': 'real', 'action_type': action_type, 'time_queryable': True,
                                                 'open_at_restore': 1, 'external_in_window': 0, 'consistent': 0, 'unverifiable': 0, 'paused': True}
        assert report[(TENANT, 'synthetic.notify')]['unverifiable'] == 1 and report[(TENANT, 'synthetic.notify')]['paused'] is True
        paused = dict(admin.execute("select scope_ref,paused from control.nexloop_control_scopes where tenant_id=%s and scope_kind='action_type'", (TENANT,)).fetchall())
        assert paused == {action_type: True, 'synthetic.notify': True}
        verified = owner_sql(admin, 'select runtime.nexloop_restore_verified(%s)', (REF,))
        assert verified['state'] == 'verified_readonly' and verified['rto_seconds'] >= 0
        assert owner_sql(admin, 'select runtime.nexloop_restore_readiness()') == {'ready': True, 'reads': True, 'dispatch': False, 'state': 'verified_readonly'}
        time.sleep(3.1)  # the refused worker's 3 s lease expires
        assert c.dispatch(provider)['status'] == 'admission_unavailable' and provider.control('snapshot')['effects'] == 0
        assert refusals(admin, 'NXR01') >= 1
        # Other tenants of this database (fixture tenants without a workbench) are opened by injection; ours by its owner.
        with admin.transaction():
            admin.execute("update control.nexloop_restore_scopes set opened=true,opened_by='synthetic-other-owner',opened_at=now() where tenant_id<>%s", (TENANT,))
        with c['app']() as client:
            csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
            assert act(client, csrf, 'open_restore_scope', {'restore_ref': REF}).status_code == 403  # owner only
        with c['app']() as client:
            csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
            assert act(client, csrf, 'open_restore_scope', {'restore_ref': 'another-restore'}).status_code == 403
            opened = act(client, csrf, 'open_restore_scope', {'restore_ref': REF}, key='wb-open-restore-0001')
            assert opened.status_code == 200, opened.text
            assert opened.json()['restore_open'] is True and opened.json()['paused_channels'] == sorted([action_type, 'synthetic.notify'])
            assert act(client, csrf, 'open_restore_scope', {'restore_ref': REF}, key='wb-open-restore-0001').status_code == 200  # replay
        state = admin.execute('select state,rpo_seconds,rto_seconds,opened_at is not null from control.nexloop_restore_state').fetchone()
        assert state[0] == 'open' and state[1] >= 0 and state[2] >= 0 and state[3] is True
        assert owner_sql(admin, 'select runtime.nexloop_restore_readiness()')['dispatch'] is True
        # Every Run credential of the old timeline was revoked at the end of replay; new Runs after the restore receive new
        # ones. The fixture reuses its one Run credential, so it is re-issued here by injection.
        with admin.transaction():
            admin.execute("update authz.nexloop_run_credentials set status='active'")
        # The scope is open, the channel is not: still refused (NXC01), nothing sent.
        time.sleep(3.1)
        assert c.dispatch(provider)['status'] == 'admission_unavailable' and provider.control('snapshot')['effects'] == 0
        assert refusals(admin, 'NXC01') >= 1, admin.execute('select code,count(*) from runtime.nexloop_dispatch_refusals group by 1').fetchall()
        # The owner resumes the channel after handling it (existing NX-022 control); new work then dispatches once.
        from test_nx022_dispatch_e2e import pause
        pause(admin, TENANT, 'action_type', action_type, paused=False)
        fresh = c.submit({'service': '后台退款（恢复后复评）'})['intent_id']
        outcome = [c.dispatch(provider) for _ in range(2)]
        assert provider.control('snapshot')['effects'] == 1 and any(r['claimed'] and r['status'] != 'admission_unavailable' for r in outcome), (fresh, outcome)
    # Owner-only state; no application role reaches the restore tables or steps.
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler'):
        assert admin.execute("select has_table_privilege(%s,'control.nexloop_restore_state','select')", (role,)).fetchone() == (False,)
        assert admin.execute("select has_function_privilege(%s,'runtime.nexloop_restore_verified(text)','execute')", (role,)).fetchone() == (False,)
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_restore_readiness()','execute')", (role,)).fetchone() == (True,)
