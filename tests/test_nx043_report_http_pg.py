"""NX-043 X7 stage finding (S7, release 38): read_experiment_report answered 422 through the workbench - its field table named kind only
while every report kind carries more fields, and the entry requires the exact key set. On PostgreSQL through the actual workbench app
(create_app, real login realm, the owner's HTTP request): each kind's body (shadow, experiment, pooled) is accepted and reported; a
missing, an extra or another kind's field, and an unknown kind, are refused 422 before any handler runs."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from nexloop_eios.http_api import create_app
from commercial_fixture import publish_registry_actions
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_nx043_pooled_pg import allocate as pool_allocate, online, people
from test_nx043_report_action_pg import TODAY, both, shadow_body  # noqa: F401
from test_nx043_reports_pg import EXPERIMENT, pay
from test_nx043_shadow_pg import x3  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login


@pytest.fixture
def reports(switching, both):  # noqa: F811
    admin = both['admin']
    publish_registry_actions(admin, ('nexloop.experiment.report',))
    for exp in ('pool.r1', 'pool.r2'):
        online(admin, exp)
        pool_allocate(admin, exp, 'candidate', people(exp + '-c', 20))
        pool_allocate(admin, exp, 'control', people(exp + '-k', 20))
    for c in people('pool.r1-c', 8) + people('pool.r2-c', 8):
        pay(admin, c, allocated_days_ago=16)
    return switching, both


def bodies(f):
    return {'shadow': shadow_body(f),
            'experiment': {'kind': 'experiment', 'experiment_id': EXPERIMENT, 'as_of': TODAY.isoformat()},
            'pooled': {'kind': 'pooled', 'experiment_ids': ['pool.r1', 'pool.r2'], 'as_of': TODAY.isoformat()}}


def test_every_report_kind_is_read_through_the_workbench_and_a_wrong_key_set_is_refused(reports, admin):
    switching, f = reports
    ok = bodies(f)
    with TestClient(create_app(switching['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, switching['owner_member']).json()['csrf_token']
        read = lambda body: act(client, csrf, 'read_experiment_report', body)  # noqa: E731
        shadow = read(ok['shadow'])
        assert shadow.status_code == 200, shadow.json()
        assert shadow.json()['cells'] == [{'shadow': 'reply', 'real': 'reply', 'events': 31, 'consumers': 31}]
        experiment = read(ok['experiment'])
        assert experiment.status_code == 200 and experiment.json()['scores']['candidate']['successes'] == 8, experiment.json()
        pooled = read(ok['pooled'])
        assert pooled.status_code == 200 and (pooled.json()['level'], pooled.json()['experiment_ids']) == ('pooled', ['pool.r1', 'pool.r2']), pooled.json()
        before = admin.execute("select count(*) from runtime.audit_events where action='nexloop.experiment.report_read'").fetchone()[0]
        wrong = [{'kind': 'shadow'}, {'kind': 'experiment'}, {'kind': 'pooled'}, {'kind': 'other'}, {},  # the kind alone (the stage body), no kind
                 {k: v for k, v in ok['shadow'].items() if k != 'until'}, {**ok['experiment'], 'tenant_id': 'x'},
                 {**ok['pooled'], 'world': ok['shadow']['world']}, {**ok['experiment'], 'kind': 'pooled'},
                 {**ok['shadow'], 'since': '2026-10-22T00:00:00'}, {**ok['shadow'], 'strategy_version': '1'},
                 {**ok['pooled'], 'experiment_ids': ['pool.r1']}, {**ok['experiment'], 'kind': ['experiment']}]
        for body in wrong:
            refused = read(body)
            assert (refused.status_code, refused.json()['code']) == (422, 'invalid_request'), body
        assert admin.execute("select count(*) from runtime.audit_events where action='nexloop.experiment.report_read'").fetchone()[0] == before
        # An operator holds no grant on the owner's report.
        operator = workbench_login(client, switching['operator_member']).json()['csrf_token']
        assert act(client, operator, 'read_experiment_report', ok['experiment']).status_code == 403


def test_a_window_the_handler_refuses_answers_with_its_reason(reports):
    switching, f = reports
    with TestClient(create_app(switching['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, switching['owner_member']).json()['csrf_token']
        off_grid = shadow_body(f, since=TODAY + timedelta(hours=3))
        refused = act(client, csrf, 'read_experiment_report', off_grid)
        assert refused.status_code == 409 and refused.json()['code'] == 'not_allowed_in_state', refused.json()
