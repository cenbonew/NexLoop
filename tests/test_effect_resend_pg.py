"""NX-064 (design v2 §4, L6 C1): the Host's one resend of the same body after an ambiguous effect submit is an idempotent resend on
the 0040 key - it finds the intent the first request wrote, or creates it the first time. Three cases on the guard port with actual PG
authority (no Host; the Host side is apps/agent-host/test/effect-metrics.test.ts):

1. the first request committed (the Host gave up waiting): the resend returns the same intent and reserves nothing again;
2. the first request rolled back: the resend creates the one intent;
3. the first request's transaction is still open, holding its locks, when the resend arrives: same-Run tool requests take the Run's
   advisory lock first (runtime_activation, 'nexloop-role-effect:<run>'), so the resend waits for it and then, as observed, finds
   the intent the first committed. Whatever it returns, there is never a second intent and never a second reservation.
"""
import threading
import time

import pytest
from test_context_artifacts import active_worker, context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_effect_final_recheck_pg import counts

BODY = {'message': 'one governed request'}


def submit(worker, activation, command):
    return worker.runtime_effect_tool(activation_ref=activation['activation_ref'], command=command, tool_operation='submit', parameters=dict(BODY))


def reserved_once(before, after):
    """One reservation: at most one unit in each ledger (the effect context and the control ledger record the same one), at least one."""
    return after[2] - before[2] <= 1 and after[3] - before[3] <= 1 and (after[2] - before[2]) + (after[3] - before[3]) >= 1


def test_resend_after_a_committed_first_request_returns_the_same_intent(context_message, admin, tmp_path):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        before = counts(admin)
        first = submit(worker, activation, command)  # committed; the Host's answer was lost
        again = submit(worker, activation, command)
        after = counts(admin)
        assert again['receipt'] == first['receipt']
        assert after[:2] == (before[0] + 1, before[1] + 1) and reserved_once(before, after)


def test_resend_after_a_rolled_back_first_request_creates_the_one_intent(context_message, admin, tmp_path, monkeypatch):
    from nexloop_eios.effect_intents import EffectIntentPort, EffectIntentUnavailable
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        before = counts(admin)
        original = EffectIntentPort._execute_in_transaction

        def lost(self, db, *args, **kwargs):
            original(self, db, *args, **kwargs)
            raise RuntimeError('synthetic: the guard transaction ends before its commit')
        monkeypatch.setattr(EffectIntentPort, '_execute_in_transaction', lost)
        with pytest.raises(EffectIntentUnavailable):
            submit(worker, activation, command)
        assert counts(admin) == before  # rolled back: no intent, no outbox item, nothing reserved
        monkeypatch.setattr(EffectIntentPort, '_execute_in_transaction', original)
        created = submit(worker, activation, command)
        after = counts(admin)
        assert after[:2] == (before[0] + 1, before[1] + 1) and reserved_once(before, after)
        assert submit(worker, activation, command)['receipt'] == created['receipt']


def test_resend_while_the_first_still_holds_its_locks_never_makes_a_second_intent(context_message, admin, tmp_path, monkeypatch):
    from nexloop_eios.effect_intents import EffectIntentPort, EffectIntentUnavailable
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        before = counts(admin)
        original = EffectIntentPort._execute_in_transaction
        written, release = threading.Event(), threading.Event()

        def held(self, db, *args, **kwargs):
            result = original(self, db, *args, **kwargs)
            if threading.current_thread().name == 'first':
                written.set()
                release.wait(10)  # the intent is written, the transaction still open and holding its locks
            return result
        monkeypatch.setattr(EffectIntentPort, '_execute_in_transaction', held)
        results = {}

        def run(name):
            try:
                results[name] = submit(worker, activation, command)
            except Exception as error:  # noqa: BLE001 - the outcome is asserted below
                results[name] = error
        first = threading.Thread(target=run, args=('first',), name='first')
        first.start()
        assert written.wait(10)
        second = threading.Thread(target=run, args=('second',), name='second')
        began = time.monotonic()
        second.start()
        second.join(1.5)
        assert second.is_alive()  # the resend waits on the Run's lock while the first holds it
        assert counts(admin) == before  # nothing committed yet
        release.set()
        first.join(20)
        second.join(20)
        assert not first.is_alive() and not second.is_alive()
        after = counts(admin)
        assert after[:2] == (before[0] + 1, before[1] + 1) and reserved_once(before, after)  # never a second intent or reservation
        assert type(results['first']) is dict
        # Observed: after the first commits, the resend's guard finds the intent it wrote (the lock wait stays within its bound).
        if isinstance(results['second'], EffectIntentUnavailable):
            pytest.fail('the resend ended ambiguous after %.1f s; expected it to find the committed intent' % (time.monotonic() - began))
        assert results['second']['receipt'] == results['first']['receipt']
