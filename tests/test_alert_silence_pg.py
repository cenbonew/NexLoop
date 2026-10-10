"""NX-030 UI part: owner-only alert silencing through the governed entry (0124 registry row, 0150 handler), over real cookies.

Production create_app, the workbench login realm, members by the real configurator, grants compiled from the role manifest
(v4: nexloop.alert.silence for owner only), the Action compiled from business-actions (v10). A firing alert state is seeded by
admin (the evaluator itself is covered by test_observability_pg). Synthetic data.
"""
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios import business_actions
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app
from nexloop_eios.observability import apply_rules, load_rules
from nexloop_eios.workbench_roles import apply_members, compile_members
from commitment_fixture import ROOT, TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_postgres_action_claims import governance_inputs
from test_browser_http import ORIGIN, login
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import CALLER, ROLES, WORKBENCH_APP, add_human, members_file, workbench_login, write_facts

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
RULES = ROOT / 'deploy/configuration/alert-rules.v1.json'


def publish_silence(admin):
    consumer = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=1", (TENANT,)).fetchone()[0]
    cap = governance_inputs()['capability_snapshot'].model_copy(update={'capability_name': 'alert.silence', 'has_side_effects': True})
    for row in business_actions.compile_actions(business_actions.load(ROOT / 'deploy/configuration/business-actions.v1.json'), tenant=TENANT,
            created_by='synthetic-configuration', created_at=datetime.now(UTC), object_types=[consumer], capabilities={'alert.silence': cap}, select=('nexloop.alert.silence',)):
        d = row['definition']
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (TENANT, 'real', f"eios:action:{d['stable_name']}:{d['version']}", Jsonb(d), Jsonb(row['capability'])))


@pytest.fixture
def silenced(commitments, identity, uow, admin, pg, tmp_path):
    c = commitments
    publish_silence(admin)

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
    apply_rules(load_rules(RULES), TENANT, database_url_file=configurator)
    now = datetime.now(UTC)
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("""insert into control.nexloop_alert_state(tenant_id,world,dedupe_key,rule_id,severity,selector,breached_since,firing,first_fired_at,last_fired_at,fire_count,
            last_value,last_evaluated_at,rules_version) values(%s,'real','queue_dead_letter:operations','queue_dead_letter','critical','operations',%s,true,%s,%s,1,2,%s,1)""",
            (TENANT, now, now, now, now))
    signer = c['signer']
    (tmp_path / 'app-artifacts').mkdir(mode=0o700)
    config = ApiConfiguration(private('business-dsn', make_conninfo(pg, user='nexloop_api')), private('authority-key', signer.material),
        tmp_path / 'app-artifacts', signer.key_id, BrowserConfiguration(private('identity-dsn', identity[0]), private('rate-key', secrets.token_hex(32)),
        TENANT, 'synthetic-browser-app', ORIGIN), execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, ORIGIN))
    return dict(app=lambda: TestClient(create_app(config), base_url=ORIGIN), owner=owner, operator=operator, identity=identity, admin=admin)


def until(hours):
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def test_owner_silences_an_alert_which_stays_evaluated_and_marked(silenced):
    s = silenced
    admin = s['admin']
    body = {'rule_id': 'queue_dead_letter', 'selector': 'operations', 'until': until(24), 'reason': '已知故障，修复中'}
    with s['app']() as client:
        csrf = workbench_login(client, s['owner']).json()['csrf_token']
        done = act(client, csrf, 'silence_alert', body, key='wb-silence-alert-0001')
        assert done.status_code == 200 and done.json()['silenced'] is True, done.text
        # Replay of the same request: the terminal outcome, one row.
        assert act(client, csrf, 'silence_alert', body, key='wb-silence-alert-0001').status_code == 200
        assert admin.execute('select count(*) from control.nexloop_alert_silences').fetchone() == (1,)
        alerts = client.get('/api/v1/workbench/alerts').json()
        assert alerts['firing'][0]['rule_id'] == 'queue_dead_letter' and alerts['firing'][0]['silenced_until'] is not None
        assert [x['rule_id'] for x in alerts['silences']] == ['queue_dead_letter']
        # The handler refuses a silence longer than 7 days or of an unknown rule (409, nothing written).
        assert act(client, csrf, 'silence_alert', {**body, 'until': until(24 * 8)}).json()['code'] == 'not_allowed_in_state'
        assert act(client, csrf, 'silence_alert', {**body, 'rule_id': 'no_such_rule'}).json()['code'] == 'not_allowed_in_state'
        assert act(client, csrf, 'silence_alert', {**body, 'selector': 'bad selector'}).status_code == 422
        audit = client.get('/api/v1/workbench/human-actions').json()['items']
        assert audit[0]['action'] == 'alert.silence' and audit[0]['target_ref'] == 'queue_dead_letter' and audit[0]['principal_id'] == s['owner']['principal_id']
    assert admin.execute('select count(*) from control.nexloop_alert_silences').fetchone() == (1,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute('delete from control.nexloop_alert_silences')


def test_operator_and_customer_cannot_silence(silenced):
    s = silenced
    body = {'rule_id': 'queue_dead_letter', 'selector': None, 'until': until(1), 'reason': '试图静默'}
    with s['app']() as client:
        csrf = workbench_login(client, s['operator']).json()['csrf_token']
        denied = act(client, csrf, 'silence_alert', body)
        assert denied.status_code == 403 and denied.json()['code'] == 'forbidden'
        assert client.get('/api/v1/workbench/alerts').status_code == 200  # the operator still reads alerts
    with s['app']() as client:
        customer = login(client, s['identity'])
        assert act(client, customer.json()['csrf_token'], 'silence_alert', body).status_code == 401
    assert s['admin'].execute('select count(*) from control.nexloop_alert_silences').fetchone() == (0,)
