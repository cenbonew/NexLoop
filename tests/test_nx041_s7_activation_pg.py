"""NX-041 S7 review 2.2 (fix verify): the SQL activation entry refuses a correctly signed command whose world is not the credential's
world. The real runtime-effect plan (runtime_effect_fixture) gives a genuine worker session and the signing key. A fresh Run is
issued, bound, accepted and claimed; the worker signs a create-activation for it naming a simulation (or shadow, or test) world - mode
set to match, so the Python command check passes and SQL is what refuses - and then the same create with the real world, which
creates the activation (positive control: a new creation, not a replay). Both signatures are valid.

The refusal is located by its PL/pgSQL frame, not by its text: 0037 raises 'activation command denied' at two places (the command's
tenant/digest/world/not_after check, and the owner epoch); the world refusal is the first one, line 28 of the 0037 function body
(renamed authz.nexloop_runtime_activation_command_v0037 by later migrations), and the negative differs from the positive control by
its world only."""
import uuid

import psycopg
import pytest

import runtime_effect_fixture as fixture
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401

# The command check's RAISE (0312 added the mode line inside that condition: line 28 -> 29; the same refusal point).
WORLD_CHECK_FRAME = 'nexloop_runtime_activation_command_v0037(text,text,text,text,text) line 29 at RAISE'


def fresh_claimed_run(plan, label):
    """As the fixture does for its own Runs: issue, bind, accept, claim."""
    source = plan['sources'][0]
    run = source.issue_run_credential(action_resources=['eios:action:' + fixture.EFFECT + ':1'])
    plan['planner'].bind_effect_context(step_id=plan['step'], step_revision=1, goal_revision=1, consumer_revision=1, control_revision=1,
                                        run_id=run.run_id, run_token=run.token, executor_token=plan['executor_token'])
    command = {**plan['commands'][0], 'run_id': run.run_id, 'request_id': 'runtime-effect-' + str(uuid.uuid4()), 'trigger_event_id': str(uuid.uuid4()),
               'credential_ref': 'run_credential:' + run.run_id, 'not_after': run.expires_at.isoformat(), 'role_ref': 'role:' + label}
    accepted = plan['owner'].accept_runtime_event(queue='operations', source_id='runtime-effect', event_id='event-' + label, run_token=run.token,
                                                  command=command, input='persist one service intent')
    job = plan['worker'].claim_task(queue='operations', lease_seconds=60)
    assert job['task_id'] == accepted['task_id']
    return command, job


def activation(plan, job, command):
    return plan['worker'].create_runtime_activation(queue='operations', task_id=job['task_id'], fence=job['fence'], run_id=command['run_id'],
                                                    command=command, input='persist one service intent', owner_epoch=1)


def sql_error(error):
    while error is not None:
        if isinstance(error, psycopg.Error):
            return error
        error = error.__cause__ or error.__context__
    return None


def test_a_signed_activation_naming_another_world_is_refused(runtime_effect_plan):  # noqa: F811
    plan = runtime_effect_plan
    command, job = fresh_claimed_run(plan, 'world-probe')
    for world, mode in (('sim:' + str(uuid.UUID(int=0x5117)), 'simulation'), ('shadow:' + str(uuid.UUID(int=0x5118)), 'shadow'), ('test', 'test')):
        with pytest.raises(Exception) as refused:
            activation(plan, job, {**command, 'world_id': world, 'mode': mode})
        error = sql_error(refused.value)
        assert error is not None and error.diag.message_primary == 'activation command denied', refused.value
        assert WORLD_CHECK_FRAME in (error.diag.context or ''), error.diag.context
    # Positive control: the same signed command with its real world creates the activation (a new one).
    created = activation(plan, job, command)
    assert created['activation_ref'].startswith('activation_') and created['activation_ref'] not in plan['activations']
