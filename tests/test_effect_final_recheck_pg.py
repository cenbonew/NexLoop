"""ADR-028 (0173): the guard's effect tool path rechecks the Run after the intent with the activation core. When the task lease
or the Run credential runs out while the intent is being written, the final recheck refuses and the whole transaction rolls
back: no intent, no outbox item, no reserved unit. The same payload submitted again returns the same intent and reserves
nothing again; a different payload is a conflict; find changes nothing. Actual PG authority, guard port only (no Host)."""
import time

import pytest
from eios.authz.errors import AuthorizationUnavailable
from test_context_artifacts import active_worker, context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401


def counts(admin):
    return admin.execute("""select (select count(*) from runtime.nexloop_effect_intents),(select count(*) from runtime.nexloop_effect_outbox),
        (select coalesce(sum(reserved_units),0) from control.nexloop_effect_contexts),
        (select coalesce(sum(reserved_units),0) from control.nexloop_effect_control_ledger)""").fetchone()


def slow_intent(monkeypatch, seconds):
    from nexloop_eios.effect_intents import EffectIntentPort
    original = EffectIntentPort._execute_in_transaction

    def delayed(self, db, *args, **kwargs):
        result = original(self, db, *args, **kwargs)
        time.sleep(seconds)  # the intent is written; the final recheck comes after this
        return result
    monkeypatch.setattr(EffectIntentPort, '_execute_in_transaction', delayed)


@pytest.mark.parametrize('expiring', ['task_lease', 'run_credential'])
def test_final_recheck_refuses_and_rolls_back_the_intent(context_message, admin, tmp_path, monkeypatch, expiring):
    from nexloop_eios.effect_intents import EffectIntentUnavailable
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        assert worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation='model')['authorized'] is True
        before = counts(admin)
        statement = {'task_lease': "update runtime.jobs set lease_until=clock_timestamp()+interval '0.6 seconds' where job_id=(select task_id from authz.nexloop_runtime_activations where activation_ref=%s)",
                     'run_credential': "update authz.nexloop_run_credentials set expires_at=clock_timestamp()+interval '0.6 seconds' where run_id=(select run_id from authz.nexloop_runtime_activations where activation_ref=%s)"}[expiring]
        admin.execute(statement, (activation['activation_ref'],))
        slow_intent(monkeypatch, 1.0)
        with pytest.raises((EffectIntentUnavailable, AuthorizationUnavailable)):
            worker.runtime_effect_tool(activation_ref=activation['activation_ref'], command=command, tool_operation='submit', parameters={'message': 'final recheck'})
        assert counts(admin) == before


def test_same_payload_returns_the_same_intent_and_reserves_once(context_message, admin, tmp_path):
    from nexloop_eios.effect_intents import EffectIntentConflict
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        before = counts(admin)
        call = lambda message: worker.runtime_effect_tool(activation_ref=activation['activation_ref'], command=command, tool_operation='submit', parameters={'message': message})
        first = call('same governed request')
        again = call('same governed request')
        assert again['receipt']['intent_id'] == first['receipt']['intent_id'] and again['receipt']['receipt_id'] == first['receipt']['receipt_id']
        after = counts(admin)
        assert after[:2] == (before[0] + 1, before[1] + 1) and after[2] - before[2] <= 1 and after[3] - before[3] <= 1
        assert (after[2] - before[2]) + (after[3] - before[3]) >= 1  # reserved exactly once in the ledger in use
        with pytest.raises(EffectIntentConflict):
            call('a different request')
        assert counts(admin) == after
        found = worker.runtime_effect_tool(activation_ref=activation['activation_ref'], command=command, tool_operation='find', intent_id=first['receipt']['intent_id'])
        found_again = worker.runtime_effect_tool(activation_ref=activation['activation_ref'], command=command, tool_operation='find', intent_id=first['receipt']['intent_id'])
        assert found['receipt'] == found_again['receipt'] and found['receipt']['intent_id'] == first['receipt']['intent_id']
        assert counts(admin) == after


def test_final_recheck_is_refused_without_a_prior_execution_and_with_context(context_message, admin, tmp_path):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        port = worker._backend
        from nexloop_eios.runtime_activation import RuntimeActivationPort, _command
        ra = RuntimeActivationPort(port._pool, worker._session, port._signer)
        text_, digest = _command(command)
        binding = dict(activation_ref=activation['activation_ref'], command_text=text_, command_digest=digest, input_digest=None, operation='tool')
        import psycopg
        with port._pool.connection() as db, db.transaction():
            queue = db.execute('select authz.nexloop_runtime_activation_hint(%s,%s,%s)', (worker._session.token_digest, worker._session.world, activation['activation_ref'])).fetchone()[0]
            resolved = ra._execute(db, ra._signed(queue, 'resolve', **binding))
            from nexloop_eios.authorization import _identity
            run = _identity(db, resolved['_run_digest'], worker._session.world)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):  # no execution marker yet for this Run
                ra._execute(db, ra._signed(queue, 'authorize', **binding, final_recheck=True, run_proofs=ra._run_proofs(run)))
