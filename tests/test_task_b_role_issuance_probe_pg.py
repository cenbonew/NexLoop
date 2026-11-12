"""Diagnostic only: M2.1 selects :2 while the production Role launcher still issues its configured :1.
Synthetic PostgreSQL; no Host changes, automatic grants, or production fixes. Keep the failure boundary observable.
"""
import pytest
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_role_launcher_pg import role_planning, role_settings, within_ceiling  # noqa: F401
from nexloop_eios.plan_reevaluation import run_budget
from nexloop_eios.effect_contexts import EffectContextUnavailable
from task_b_fixture import EFFECT, bump_consumer_v2
from test_task_b_role_r1_pg import fresh_role_spec


@pytest.mark.parametrize('admitted', [False, True])
def test_role_launcher_configured_v1_and_selected_consumer_version(role_planning, admitted):  # noqa: F811
    f = role_planning
    admin, plan = f['admin'], f['plan']
    spec = fresh_role_spec(f, f['establish'](steps=[within_ceiling()]))
    if admitted:
        bump_consumer_v2(admin, plan['tenant'])
    worker = f['worker']()
    decision = worker.port.precheck(spec['plan_id'], [])
    budget = run_budget(worker.launcher.settings, decision['plan_steps'])
    selected = admin.execute("select authz.nexloop_effect_bind_action_resource(%s,'real','service.request:primary',%s)",
                             (plan['tenant'], 2 if admitted else 1)).fetchone()[0]
    assert selected == f'eios:action:{EFFECT}:{2 if admitted else 1}'
    run = worker.launcher.issue(decision, budget)
    assert admin.execute('select allowed_resources from authz.nexloop_run_credentials where run_id=%s',
                         (run.run_id,)).fetchone()[0] == [f'eios:action:{EFFECT}:1']
    assert admin.execute('select count(*) from authz.nexloop_role_policy_bindings where run_id=%s', (run.run_id,)).fetchone() == (1,)
    if admitted:
        from eios.authz.errors import AuthorizationUnavailable
        from nexloop_eios.authorization import authenticate_service
        from nexloop_eios.effect_contexts import EffectContextRegistrar
        from nexloop_eios.run_credentials import AUDIENCE
        source = worker.launcher._current(worker.launcher._source)
        run_session = authenticate_service(source._backend._pool, run.token, world='real', run_id=run.run_id, audience=AUDIENCE)
        registrar = EffectContextRegistrar(source._backend._pool, source._session, source._backend._signer)
        assert registrar._proof(run_session, f'eios:action:{EFFECT}:1')['resource_id'].endswith(':1')
        with pytest.raises(AuthorizationUnavailable, match='PostgreSQL authority facts unavailable'):
            registrar._proof(run_session, selected)
        # The first production activation operation is Planner.bind_effect_context; its recomputation demands :2.
        with pytest.raises(EffectContextUnavailable):
            worker.launcher.activate(run, decision, budget, [])
        assert admin.execute('select count(*) from control.nexloop_effect_run_contexts where run_id=%s', (run.run_id,)).fetchone() == (0,)
    else:
        # Consumer v1 baseline: binding uses :1; do not exercise the Host here.
        planner = worker.launcher._current(worker.launcher._planner)
        refs = decision['revisions']
        bound = planner.bind_effect_context(step_id=decision['plan']['recipe']['step_id'], step_revision=refs['PlanStep'],
            goal_revision=refs['Goal'], consumer_revision=refs['Consumer'], control_revision=refs['EffectControl'],
            run_id=run.run_id, run_token=run.token, executor_token=worker.launcher._current(worker.launcher.executor_token))
        assert bound
