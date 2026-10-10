"""NX-028: the two login realms together in one production app (slice 1 reads + slices 2/3 writes, merged).

One workbench cookie (__Host-nexloop_workbench) both reads (GET /api/v1/workbench/*) and writes (POST /api/v1/workbench/actions/*),
and the write shows up in the next read; the WebChat customer cookie is refused for both (401), and the customer logged into the
workbench realm holds nothing there (403 for both). Synthetic data; same setup as L4's realm test (test_workbench_actions_http).
"""
import secrets

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app
from nexloop_eios.workbench_roles import apply_members, compile_members
from commitment_fixture import TENANT, commitments, publish_request_actions  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN, login
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import active_plan
from test_workbench_actions_http import act, publish_goal_actions
from test_workbench_read_pg import CALLER, ROLES, WORKBENCH_APP, add_human, members_file, workbench_login, write_facts

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


def test_one_workbench_cookie_reads_and_writes_and_the_customer_cookie_is_refused_for_both(commitments, identity, uow, admin, pg, tmp_path):
    c = commitments
    publish_request_actions(admin)
    publish_goal_actions(admin)
    plan = active_plan(c)

    def private(name, content):
        path = tmp_path / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(0o600)
        return path
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    owner = add_human(admin, 'owner')
    admin.execute('alter role nexloop_configurator login')
    members = members_file((owner, 'owner'))
    apply_members(ROLES, members, database_url_file=private('configurator-dsn', make_conninfo(pg, user='nexloop_configurator')))
    write_facts(admin, compile_members(ROLES, members))
    signer = c['signer']
    (tmp_path / 'app-artifacts').mkdir(mode=0o700)
    config = ApiConfiguration(private('business-dsn', make_conninfo(pg, user='nexloop_api')), private('authority-key', signer.material),
        tmp_path / 'app-artifacts', signer.key_id, BrowserConfiguration(private('identity-dsn', identity[0]), private('rate-key', secrets.token_hex(32)),
        TENANT, 'synthetic-browser-app', ORIGIN), execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, ORIGIN))
    pause = {'scope_kind': 'consumer', 'scope_ref': c['consumer'], 'paused': True, 'reason': '暂停'}

    def paused(client):
        scopes = client.get('/api/v1/workbench/goals').json()['control']['scopes']
        return [s['paused'] for s in scopes if s['scope_kind'] == 'consumer' and s['scope_ref'] == c['consumer']]

    with TestClient(create_app(config), base_url=ORIGIN) as client:
        # The WebChat customer cookie: neither a workbench read nor a workbench write (no workbench session at all).
        customer = login(client, identity)
        assert customer.status_code == 200
        assert client.get('/api/v1/workbench/overview').status_code == 401
        assert client.get('/api/v1/workbench/goals').status_code == 401
        assert act(client, customer.json()['csrf_token'], 'set_control', pause).status_code == 401
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        # The same customer logged into the workbench realm: authenticated there, but holds nothing (customer principal).
        csrf = workbench_login(client, {'username': identity[3].username, 'password': identity[-1]}).json()['csrf_token']
        assert client.get('/api/v1/workbench/goals').status_code == 403
        assert act(client, csrf, 'request_plan_reevaluation', {'plan_id': str(plan), 'reason': 'x'}).status_code == 403
    assert admin.execute('select count(*) from control.nexloop_control_events').fetchone() == (0,)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        # One owner cookie: read, write, and the write is visible on the next read.
        csrf = workbench_login(client, owner).json()['csrf_token']
        assert client.cookies.get('__Host-nexloop_workbench') and not client.cookies.get('__Host-nexloop_session')
        assert client.get('/api/v1/workbench/overview').status_code == 200
        assert paused(client) == []
        written = act(client, csrf, 'set_control', pause)
        assert written.status_code == 200, written.text
        assert paused(client) == [True]
        assert act(client, csrf, 'request_plan_reevaluation', {'plan_id': str(plan), 'reason': '客户情况有变'}).status_code == 200
        assert client.get('/api/v1/workbench/contact').status_code == 200
    assert admin.execute('select count(*) from control.nexloop_control_events').fetchone() == (1,)
