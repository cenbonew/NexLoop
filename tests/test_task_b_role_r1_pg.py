"""R1 audit: identical no-admission Consumer v2 Role bind on batch170 and candidate.
The JSON diagnostic contains only stage/function/error class and server error code/message, never locals or credentials.
"""
import json
import sys
import pytest
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_role_launcher_pg import role_planning, role_settings, within_ceiling  # noqa: F401
from nexloop_eios.plan_reevaluation import run_budget
from task_b_fixture import bump_consumer_v2
from nexloop_eios.effect_contexts import EffectContextUnavailable


def fresh_role_spec(f, spec):
    """A governed fresh PlanStep and explicit synthetic READ/policy setup; no old registrar claim is reused."""
    admin, plan = f['admin'], f['plan']
    import uuid
    from test_effect_scope_successor_proof_pg import follow_facts
    from nexloop_eios.role_runs import STEP_FIELDS
    old_step = spec['recipe']['step_id']
    properties = admin.execute('select properties from ontology.objects where tenant_id=%s and object_id=%s',
                               (plan['tenant'], old_step)).fetchone()[0]
    planner = plan['api'].authenticate(plan['planner_token'], world='real')
    new_step = planner.create_object(action_name='PlanStep.create', action_version=1, intent_id='r1-fresh-step',
        type_name='PlanStep', properties=properties)['object_id']
    # Synthetic fixture provisions exact READ authority and explicitly governed Role policy for the fresh PlanStep.
    follow_facts(admin, plan['tenant'], 'eios:object:PlanStep/' + old_step, 'eios:object:PlanStep/' + new_step)
    for field in STEP_FIELDS:
        follow_facts(admin, plan['tenant'], f'eios:property:PlanStep/{old_step}/{field}', f'eios:property:PlanStep/{new_step}/{field}')
    plan['edit_policy'](0, 'RoleExecutionCeiling', {'step_ids': [old_step, new_step]})
    plan['edit_policy'](0, 'RoleAssignmentScope', {'step_ids': [old_step, new_step]})
    spec['plan_id'] = str(uuid.uuid4())
    spec['recipe']['step_id'] = new_step
    f['worker']().port.establish(spec)
    assert admin.execute('select count(*) from control.nexloop_effect_plan_bindings where step_id=%s', (new_step,)).fetchone() == (0,)
    return spec


@pytest.mark.parametrize('fresh_step', [False, True])
def test_r1_no_admission_consumer_v2_role_bind(role_planning, fresh_step):  # noqa: F811
    f = role_planning
    admin, plan = f['admin'], f['plan']
    spec = f['establish'](steps=[within_ceiling()])
    if fresh_step:
        spec = fresh_role_spec(f, spec)
    bump_consumer_v2(admin, plan['tenant'], admitted=False)
    assert admin.execute('select count(*) from control.nexloop_effect_action_admissions where tenant_id=%s and retired_at is null',
                         (plan['tenant'],)).fetchone() == (0,)
    worker = f['worker']()
    decision = worker.port.precheck(spec['plan_id'], [])
    budget = run_budget(worker.launcher.settings, decision['plan_steps'])
    run = worker.launcher.issue(decision, budget)
    # All Source / Planner / executor / Run sessions are freshly authenticated after publication/follow facts.
    planner = worker.launcher._current(worker.launcher._planner)
    refs = decision['revisions']
    prior_keys = {r[0] for r in admin.execute("select claim->'key'->>'idempotency_key' from runtime.nexloop_action_claims where action_name='nexloop.plan.bind_effect_context'").fetchall()}
    events = []
    def trace(frame, event, arg):
        if event == 'exception' and frame.f_code.co_filename.endswith('/nexloop_eios/effect_contexts.py'):
            _, error, _ = arg
            events.append(dict(function=frame.f_code.co_name, line=frame.f_lineno, error=type(error).__name__,
                               sqlstate=getattr(error, 'sqlstate', None),
                               disposition=getattr(getattr(frame.f_locals.get('reservation'), 'disposition', None), 'value', None),
                               same_prior_intent=(getattr(getattr(frame.f_locals.get('command'), 'key', None), 'idempotency_key', None) in prior_keys),
                               action_version=getattr(getattr(getattr(frame.f_locals.get('command'), 'binding', None), 'action_reference', None), 'version', None),
                               message=getattr(getattr(error, 'diag', None), 'message_primary', None)))
        return trace
    succeeded = False
    rejected = False
    try:
        sys.settrace(trace)
        planner.bind_effect_context(step_id=decision['plan']['recipe']['step_id'], step_revision=refs['PlanStep'],
            goal_revision=refs['Goal'], consumer_revision=refs['Consumer'], control_revision=refs['EffectControl'],
            run_id=run.run_id, run_token=run.token, executor_token=worker.launcher._current(worker.launcher.executor_token))
        succeeded = True
    except EffectContextUnavailable:
        rejected = True
    finally:
        sys.settrace(None)
        print('R1_DIAGNOSTIC=' + json.dumps(dict(fresh_step=fresh_step, succeeded=succeeded, events=events), sort_keys=True))
    if fresh_step:
        assert succeeded and not rejected
        assert admin.execute('select count(*) from control.nexloop_effect_run_contexts where run_id=%s', (run.run_id,)).fetchone() == (1,)
    else:
        assert rejected and not succeeded
        assert any(e['disposition'] == 'conflict' and e['same_prior_intent'] and e['action_version'] == 2 for e in events), events
        assert admin.execute('select count(*) from control.nexloop_effect_run_contexts where run_id=%s', (run.run_id,)).fetchone() == (0,)
