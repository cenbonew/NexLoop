"""Experiment space slice 1 (owner 2026-10-28; temporary migration 0370): the owner's experiments view (GET /api/v1/workbench/experiments,
verb 'experiments' of the workbench observe read) on PostgreSQL through the actual app - registration fields, status and times, stop reason,
spend, allocation counts, the budget-drill flag and notice, the twin's status and calibration - for the owner only, the tenant's own only,
and never a Consumer id. Synthetic tenant and data only."""
import json
import uuid

from fastapi.testclient import TestClient

from nexloop_eios.http_api import create_app
from support.tenants import TENANT_A, TENANT_B
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_owner_member_actions_pg import owners, with_policies  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_nx043_experiments_pg import publish, publish_twin, register, start, strategy
from test_nx043_budget_drill_pg import NOTICE, drill, reserve, soon
from test_nx043_experiments_pg import registration
from test_nx041_sim_worlds_pg import create as create_world
from test_workbench_read_pg import workbench_login

TWIN = {'twin_id': 'trial.service_consumer', 'version': 1}


def read(c, member, query=''):
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        workbench_login(client, member)
        return client.get('/api/v1/workbench/experiments' + query, headers={'Origin': ORIGIN})


def seed(admin):
    """A budget drill exhausted by the guard, and a plain screening registered but not started, in one simulation world."""
    publish(admin, strategy('followup.later', follow_up_delay_hours=48), request='s-candidate')
    publish(admin, strategy('followup.default', follow_up_delay_hours=24), request='s-control')
    publish_twin(admin, request='twin-1')
    world = create_world(admin)['world']
    register(admin, drill(world))
    start(admin, 'budget.drill')
    reserve(admin, world, '1')  # reaches the cap: the guard stops it
    register(admin, registration(world, TWIN, experiment_id='plain.screening', budget={'model_cny': 1, 'incentive_cny': 0}, ends_at=soon()))
    return world


def test_the_owner_reads_each_experiment_with_its_status_spend_and_drill_flag(owners, admin):
    c = owners
    with_policies(c)
    world = seed(admin)
    response = read(c, c['owner_member'])
    assert response.status_code == 200, response.text
    by = {x['experiment_id']: x for x in response.json()['experiments']}
    assert set(by) == {'budget.drill', 'plain.screening'}
    d = by['budget.drill']
    assert d['world'] == world and d['world_kind'] == 'simulation' and d['stage'] == 'screening'
    assert d['status'] == 'stopped' and d['started_at'] and d['stopped_at']
    assert d['stop_reason'] == 'guardrail: budget_exhausted' and d['stopped_by_guard'] is True
    assert d['budget_drill'] is True and d['notice'] == NOTICE
    assert float(d['spent']['model_cny']) == 1 and d['budget']['model_cny'] == 1
    assert d['candidate'] == {'strategy_id': 'followup.later', 'version': 1} and d['control']['strategy_id'] == 'followup.default'
    assert d['twin'] == {'twin_id': 'trial.service_consumer', 'version': 1, 'status': 'published', 'calibration': 'uncalibrated'}
    assert d['allocation'] == {'candidate': 0, 'control': 0, 'excluded': 0} and d['report'] is None
    assert len(d['registration_digest']) == 64
    p = by['plain.screening']
    assert p['status'] == 'registered' and p['started_at'] is None and p['stop_reason'] is None and p['stopped_by_guard'] is False
    assert p['budget_drill'] is False and p['notice'] is None
    assert set(d) == set(p)


def test_no_consumer_id_leaves_the_view(owners, admin):
    c = owners
    with_policies(c)
    seed(admin)
    consumer = uuid.uuid4().hex * 2
    admin.execute("""insert into control.nexloop_experiment_allocations(tenant_id,experiment_id,consumer_id,cluster_key,arm)
        values(%s,'budget.drill',%s,%s,'candidate')""", (TENANT_A, consumer, consumer))
    view = read(c, c['owner_member']).json()
    assert {x['experiment_id']: x['allocation'] for x in view['experiments']}['budget.drill'] == {'candidate': 1, 'control': 0, 'excluded': 0}
    assert consumer not in json.dumps(view)


def test_only_the_owner_of_this_tenant_reads_the_experiments_view(owners, admin):
    c = owners
    with_policies(c)
    seed(admin)
    assert read(c, c['operator_member']).status_code == 403
    assert read(c, c['owner_member'], '?x=1').status_code == 422
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        assert client.get('/api/v1/workbench/experiments', headers={'Origin': ORIGIN}).status_code == 401  # no session
    # Another tenant's experiments are not this tenant's: the view function reads one tenant only.
    assert admin.execute('select runtime.nexloop_workbench_experiments_view(%s)', (TENANT_B,)).fetchone()[0] == {'experiments': []}


def test_an_empty_tenant_reads_an_empty_list(owners):
    c = owners
    with_policies(c)
    response = read(c, c['owner_member'])
    assert response.status_code == 200 and response.json() == {'experiments': []}


def test_over_http_an_owner_reads_only_the_session_tenant_s_experiments(owners, admin):
    """L6 S1 on a51d0ad4: the tenant comes from the session, never the request. Another tenant's experiment (seeded directly, its
    registration as this tenant's) does not reach this tenant's owner over HTTP, and this tenant's does."""
    c = owners
    with_policies(c)
    world = seed(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_B,))
    theirs = registration(world, TWIN, experiment_id='other.tenant', budget={'model_cny': 1, 'incentive_cny': 0}, ends_at=soon())
    admin.execute("""insert into control.nexloop_experiments(tenant_id,experiment_id,registration,registration_digest,request_id,registered_by)
        values(%s,'other.tenant',%s,%s,'other-tenant-request','other-owner')""", (TENANT_B, json.dumps(theirs), 'e' * 64))
    response = read(c, c['owner_member'])
    assert response.status_code == 200
    ids = {x['experiment_id'] for x in response.json()['experiments']}
    assert ids == {'budget.drill', 'plain.screening'} and 'other.tenant' not in json.dumps(response.json())
    assert [x['experiment_id'] for x in admin.execute('select runtime.nexloop_workbench_experiments_view(%s)', (TENANT_B,)).fetchone()[0]['experiments']] == ['other.tenant']
