"""NX-041 S7 review 2.2 (fix verify item 4): the SQL activation entry refuses a correctly signed command whose world is not the
credential's world. The real runtime-effect plan (runtime_effect_fixture) gives a genuine worker session, a claimed task and the
signing key; the worker signs a create-activation command with the real world and, as the tampering, one whose command names a
simulation world (mode simulation, so the Python command check passes and the SQL check is what refuses). Both signatures are valid."""
import uuid

import pytest

from runtime_effect_fixture import runtime_effect_plan  # noqa: F401

SIM = 'sim:' + str(uuid.UUID(int=0x5117))


def activation(plan, command):
    job = plan['jobs'][0]
    return plan['worker'].create_runtime_activation(queue='operations', task_id=job['task_id'], fence=job['fence'], run_id=command['run_id'],
                                                 command=command, input='persist one service intent', owner_epoch=1)


def message_chain(error):
    out = []
    while error is not None:
        out.append(str(error))
        error = error.__cause__ or error.__context__
    return ' | '.join(out)


def test_a_signed_activation_naming_another_world_is_refused(runtime_effect_plan):  # noqa: F811
    plan = runtime_effect_plan
    command = dict(plan['commands'][0])
    # Positive control: the signed real command passes the SQL world check (the activation exists already: the same answer again).
    assert activation(plan, command)['activation_ref'] == plan['activations'][0]
    for world, mode in ((SIM, 'simulation'), ('shadow:' + str(uuid.UUID(int=0x5118)), 'shadow'), ('test', 'test')):
        with pytest.raises(Exception) as refused:
            activation(plan, {**command, 'world_id': world, 'mode': mode})
        chain = message_chain(refused.value)
        # The SQL entry's own refusal (0037: command world_id is not the credential's world), under the Python port's wrapper.
        assert 'activation command denied' in chain, chain
