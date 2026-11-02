"""NX-064 (migration effect_request_metrics, alert rules v29) on real PostgreSQL: the Host's effect counters as process samples
(kind 'host' totals, kind 'host_op' per operation) and the metrics snapshot's effect_requests block the v29 rules read.

Three items judged apart - effect_submit, effect_find (guard_op samples) and host (the 'host' samples carrying the effect counters):
each says present false and no_data 1 without a sample of the last 24 hours, so an empty or stopped source fires its own no-data
rule and never reads as healthy (L6 C3). enterprise_unavailable counts the tenant's failed Runs whose nexloop.enterprise.deliver ended
runtime_effect_unavailable with their run ids, for the owner's separate critical notice (L6 C2). Synthetic rows only."""
import json
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.observability import load_rules
from support.tenants import TENANT_A as TENANT, TENANT_B as OTHER

V29 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v29.json'
NEW = ('effect_submit_no_data', 'effect_find_no_data', 'host_effect_no_data', 'effect_submit_over_2s', 'effect_find_over_2s',
       'host_effect_unavailable', 'enterprise_deliver_unavailable')
NO_DATA = {'present': False, 'no_data': 1}


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for tenant in (TENANT, OTHER):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    return admin


def block(admin, tenant=TENANT, world='real'):
    return admin.execute('select runtime.nexloop_metrics_snapshot(%s,%s)', (tenant, world)).fetchone()[0].get('effect_requests')


def sample(admin, kind, service, value, age='1 minute'):
    admin.execute("""insert into runtime.nexloop_process_samples(kind,service,instance,window_start,window_seconds,sample)
        values(%s,%s,'synthetic:1',clock_timestamp()-%s::interval,60,%s)""", (kind, service, age, Jsonb(value)))


def failed_run(admin, tenant=TENANT, tool='nexloop.enterprise.deliver', detail='runtime_effect_unavailable', age='1 minute', status='failed'):
    run = str(uuid.uuid4())
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,
            actor_id,trace_id,request_id,normalized_input,plan_snapshot,status,result,created_at,updated_at,world,queue)
            values(%s,%s,'nexloop.runtime.run','1','runtime','async',%s,'synthetic','synthetic','synthetic','{}','{}',%s,%s,
                   clock_timestamp()-%s::interval,clock_timestamp()-%s::interval,'real','operations')""",
            ('job-' + run, 'inv-' + run, tenant, status, Jsonb({'run_id': run, 'runtime_outcome': 'failed', 'failure_code': 'runtime_tool_error',
                                                                  'failure_detail': detail, 'failure_tool': tool}), age, age))
    return run


def v29_fired(snapshot):
    """The evaluator's reading of a plain dotted metric (0131): a number above the threshold."""
    out = []
    for r in load_rules(V29)['rules']:
        if r['rule_id'] not in NEW:
            continue
        value = {'effect_requests': snapshot}
        for part in r['metric'].split('.'):
            value = value.get(part) if type(value) is dict else None
        if type(value) in (int, float) and not isinstance(value, bool) and value > r['threshold']:
            out.append(r['rule_id'])
    return sorted(out)


def test_sql_accepts_the_host_effect_counters_and_host_op_for_known_operations_only(db, pg):
    host = {'available': True, 'active_runs': 0, 'effect_calls': 3, 'effect_ambiguous': 1, 'effect_unavailable': 0}
    op = {'calls': 3, 'ambiguous': 1, 'unavailable': 0, 'h_74': 2, 'h_116': 1}
    with psycopg.connect(make_conninfo(pg, user='nexloop_domain_worker'), autocommit=True) as conn:
        record = lambda kind, service, value: conn.execute('select authz.nexloop_record_process_sample(%s,%s,%s,now(),60,%s)', (kind, service, 'synthetic:1', Jsonb(value)))
        record('host', 'agent-host', host)
        for operation in ('effect_submit', 'enterprise_submit', 'effect_find', 'incentive'):
            record('host_op', operation, op)
        for kind, service, value in (('host_op', 'outreach', op), ('host_op', 'effect_submit', {**op, 'timeouts': 1}),
                                     ('host_op', 'effect_submit', {'calls': 'many'}), ('host_op', 'effect_submit', {'h_117': 1}),
                                     ('host', 'agent-host', {**host, 'h_3': 1}), ('host', 'agent-host', {**host, 'effect_run_id': 1})):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                record(kind, service, value)
    assert db.execute("select count(*) from runtime.nexloop_process_samples where kind='host_op'").fetchone() == (4,)


def test_without_samples_each_item_says_no_data_and_fires_its_own_rule(db):
    value = block(db)
    assert value == {'effect_submit': NO_DATA, 'effect_find': NO_DATA, 'host': NO_DATA, 'enterprise_unavailable': {'runs_24h': 0, 'run_ids': []}}
    assert v29_fired(value) == ['effect_find_no_data', 'effect_submit_no_data', 'host_effect_no_data']


def test_each_item_is_judged_apart_and_samples_older_than_a_day_do_not_count(db):
    sample(db, 'guard_op', 'effect_submit', {'calls': 200, 'timeouts': 3, 'errors': 0, 'h_74': 197, 'h_81': 3})
    sample(db, 'guard_op', 'effect_find', {'calls': 10, 'timeouts': 0, 'errors': 0}, age='25 hours')
    sample(db, 'host', 'agent-host', {'available': True, 'active_runs': 0})  # a Host before NX-064: no effect counters
    value = block(db)
    assert value['effect_submit'] == {'present': True, 'no_data': 0, 'samples': 1, 'calls': 200, 'over_2s': 3, 'over_2s_ratio': 0.015}
    assert value['effect_find'] == NO_DATA and value['host'] == NO_DATA
    assert v29_fired(value) == ['effect_find_no_data', 'effect_submit_over_2s', 'host_effect_no_data']
    sample(db, 'guard_op', 'effect_find', {'calls': 10, 'timeouts': 0, 'errors': 0})
    sample(db, 'host', 'agent-host', {'available': True, 'effect_calls': 5, 'effect_ambiguous': 2, 'effect_unavailable': 1})
    sample(db, 'host', 'agent-host', {'available': True, 'effect_calls': 0, 'effect_ambiguous': 0, 'effect_unavailable': 0})
    sample(db, 'host_op', 'enterprise_submit', {'calls': 1, 'ambiguous': 1, 'unavailable': 1, 'h_80': 2})
    sample(db, 'host_op', 'effect_submit', {'calls': 4, 'ambiguous': 1, 'unavailable': 0, 'h_74': 4})
    value = block(db)
    assert value['effect_find'] == {'present': True, 'no_data': 0, 'samples': 1, 'calls': 10, 'over_2s': 0, 'over_2s_ratio': 0}
    assert value['host'] == {'present': True, 'no_data': 0, 'samples': 2, 'calls': 5, 'ambiguous': 2, 'unavailable': 1,
                             'operations': {'effect_submit': {'calls': 4, 'ambiguous': 1, 'unavailable': 0},
                                            'enterprise_submit': {'calls': 1, 'ambiguous': 1, 'unavailable': 1}}}
    assert v29_fired(value) == ['effect_submit_over_2s', 'host_effect_unavailable']
    # A quiet Host still reports zeros: present, no data 0, nothing fires for it.
    db.execute("delete from runtime.nexloop_process_samples where kind in ('host','host_op')")
    sample(db, 'host', 'agent-host', {'available': True, 'effect_calls': 0, 'effect_ambiguous': 0, 'effect_unavailable': 0})
    assert block(db)['host'] == {'present': True, 'no_data': 0, 'samples': 1, 'calls': 0, 'ambiguous': 0, 'unavailable': 0, 'operations': {}}


def test_enterprise_deliver_unavailable_names_the_runs_of_the_tenant_only(db):
    runs = [failed_run(db) for _ in range(2)]
    failed_run(db, tenant=OTHER)  # another tenant's Run
    failed_run(db, tool='nexloop.service.request')  # a reply: the reply guarantee covers it, not this notice
    failed_run(db, detail='invalid_effect_request')  # another failure of the tool
    failed_run(db, age='25 hours')  # older than a day
    failed_run(db, status='succeeded')
    value = block(db)['enterprise_unavailable']
    assert value['runs_24h'] == 2 and sorted(value['run_ids']) == sorted(runs)
    assert 'enterprise_deliver_unavailable' in v29_fired(block(db))
    assert block(db, tenant=OTHER)['enterprise_unavailable']['runs_24h'] == 1
    # At most 16 ids, the newest first; the count stays exact.
    for _ in range(16):
        failed_run(db)
    value = block(db)['enterprise_unavailable']
    assert value['runs_24h'] == 18 and len(value['run_ids']) == 16 and not set(runs) & set(value['run_ids'])


def test_v29_keeps_v28_and_adds_the_effect_rules():
    rules = load_rules(V29)
    v28 = json.loads((V29.parent / 'alert-rules.v28.json').read_text())
    assert rules['version'] == 29 and rules['rules'][:len(v28['rules'])] == v28['rules']
    added = {r['rule_id']: r for r in rules['rules'][len(v28['rules']):]}
    assert set(added) == set(NEW)
    assert added['enterprise_deliver_unavailable']['severity'] == 'critical'
    assert all(added[r]['severity'] == 'warning' for r in NEW if r != 'enterprise_deliver_unavailable')
    assert 'never re-order' in added['enterprise_deliver_unavailable']['purpose']


def test_the_alert_matches_the_literals_the_host_and_the_dispatcher_write():
    """L6 condition 2: the SQL matches the values a real failed enterprise Run carries (apps/agent-host/test/
    enterprise-unavailable-failure.test.ts runs the real tool to them): the Host's tool name and code, kept by the dispatcher."""
    import re
    from nexloop_eios.runtime_dispatch import _DETAIL, _TOOL
    root = Path(__file__).resolve().parents[1]
    sql = next((root / 'packages/eios-core/src/eios/migrations').glob('*_effect_request_metrics.sql')).read_text()
    host = (root / 'apps/agent-host/src/runtime-effect-tools.ts').read_text()
    host_test = (root / 'apps/agent-host/test/enterprise-unavailable-failure.test.ts').read_text()
    assert "j.result->>'failure_tool'='nexloop.enterprise.deliver' and j.result->>'failure_detail'='runtime_effect_unavailable'" in sql
    assert "defineTool({name:'nexloop.enterprise.deliver'," in host and "throw new RuntimeError('runtime_effect_unavailable');" in host
    assert "['runtime_tool_error','nexloop.enterprise.deliver','runtime_effect_unavailable']" in host_test
    assert _TOOL.fullmatch('nexloop.enterprise.deliver') and _DETAIL.fullmatch('runtime_effect_unavailable')
    dispatch = (root / 'packages/eios-core/src/nexloop_eios/runtime_dispatch.py').read_text()
    assert re.search(r"if failure_detail is not None:result\['failure_detail'\]=failure_detail", dispatch)
    assert re.search(r"if failure_tool is not None:result\['failure_tool'\]=failure_tool", dispatch)
