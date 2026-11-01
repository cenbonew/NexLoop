"""S89 fix 1 (migration plan_denial_self_cause): a reevaluation Run denied at dispatch re-registers its plan as cause 'self', so the
0106 self-trigger throttle bounds it. Before, as 'external', a model budget exhausted (not checked by the precheck) re-armed the plan
at once and every worker tick issued another Run. Each round: the worker launches (FakeLauncher), then the Run's task is denied at
dispatch (fault-injected terminal state, as test_dispatch_denial_and_strategy_change_mark_the_plan does)."""
import uuid

import pytest
from psycopg.types.json import Jsonb

from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_reevaluation_pg import FakeLauncher, feed, make_due, planning, spec  # noqa: F401


def deny(admin, plan, run_id, code):
    """The Run's task, failed at dispatch with code (a copy of a fixture job bound to this Run)."""
    job = str(uuid.uuid4())
    src = plan['jobs'][0]['task_id']
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into runtime.jobs select (jsonb_populate_record(null::runtime.jobs, to_jsonb(j)||jsonb_build_object("
                      "'job_id',%s::text,'invocation_id',%s::text,'idempotency_key',%s::text,'status','running'))).* from runtime.jobs j where j.job_id=%s",
                      (job, str(uuid.uuid4()), str(uuid.uuid4()), src))
        admin.execute("insert into authz.nexloop_runtime_run_bindings(run_id,tenant_id,world,queue,task_id,run_digest,command_digest,input_digest,owner_epoch,enrolled_by,created_at) "
                      "select %s,tenant_id,world,queue,%s,(select token_digest from authz.nexloop_run_credentials where run_id=%s),command_digest,input_digest,owner_epoch,enrolled_by,clock_timestamp() "
                      "from authz.nexloop_runtime_run_bindings where task_id=%s", (run_id, job, run_id, src))
    admin.execute("update runtime.jobs set status='failed',result=%s where job_id=%s", (Jsonb({'code': code}), job))  # the trigger fires


CODES = ('control_paused', 'goal_version_stale', 'control_revision_stale', 'object_revision_stale', 'budget_exhausted', 'control_snapshot_missing')


@pytest.mark.parametrize('code', CODES)
def test_a_denied_reevaluation_is_throttled_like_any_self_trigger(planning, code):
    f = planning; admin = f['admin']; launcher = FakeLauncher(f['plan'])
    cap = 2
    s = spec(f['plan'], settings={'self_window_seconds': 600, 'self_trigger_cap': cap, 'min_reassess_seconds': 0}); f['port']().establish(s)
    admin.execute("select authz.nexloop_plan_feed_touch(%s,'real',%s,%s,null)", (f['tenant'], s['plan_id'], Jsonb({'kind': 'reassess_due', 'cause': 'self'})))
    launched, throttled = [], 0
    for _ in range(5):
        make_due(admin)
        summary = f['worker'](launcher).run_once()
        launched.append(summary['launched']); throttled += summary['throttled']
        if summary['launched']:
            (run_id,) = admin.execute('select run_id from runtime.nexloop_plan_runs where plan_id=%s order by created_at desc limit 1', (s['plan_id'],)).fetchone()
            deny(admin, f['plan'], run_id, code)
    runs = admin.execute('select count(*) from runtime.nexloop_plan_runs where plan_id=%s', (s['plan_id'],)).fetchone()[0]
    # At most self_trigger_cap Runs in the window (before the fix: one per round, five), the rest throttled and waiting.
    assert runs == cap and launched == [1, 1, 0, 0, 0] and throttled == 3
    (key, payload, status, future), = feed(admin)
    assert payload['triggers'][-1]['kind'] == 'dispatch_denied' and payload['triggers'][-1]['cause'] == 'self' and payload['triggers'][-1]['code'] == code
    assert status == 'pending' and future  # waiting for the window, not due now
    assert admin.execute('select last_code from runtime.nexloop_work_feed where item_key=%s', (key,)).fetchone()[0] == 'self_trigger_throttled'


def test_a_failure_that_is_not_a_dispatch_denial_re_registers_nothing(planning):
    f = planning; admin = f['admin']; launcher = FakeLauncher(f['plan'])
    s = spec(f['plan'], settings={'self_window_seconds': 600, 'self_trigger_cap': 2, 'min_reassess_seconds': 0}); f['port']().establish(s)
    admin.execute("select authz.nexloop_plan_feed_touch(%s,'real',%s,%s,null)", (f['tenant'], s['plan_id'], Jsonb({'kind': 'reassess_due', 'cause': 'self'})))
    assert f['worker'](launcher).run_once()['launched'] == 1
    (run_id,) = admin.execute('select run_id from runtime.nexloop_plan_runs where plan_id=%s', (s['plan_id'],)).fetchone()
    deny(admin, f['plan'], run_id, 'runtime_transport_timeout')
    assert feed(admin) == []
