"""Deterministic scheduling gaps in the test-only Host observation deadline model."""
from concurrent.futures import TimeoutError
from types import SimpleNamespace

import pytest

import load_chain


class Clock:
    def __init__(self): self.now = 0.0
    def monotonic(self): return self.now
    def advance(self, seconds): self.now += seconds


class Future:
    def __init__(self, clock, operation, body, work=0.0, wake=0.0):
        self.clock, self.operation, self.body = clock, operation, body
        self.work, self.wake = work, wake
        self.done, self.callbacks, self.timeouts, self.error = False, [], [], None
    def complete(self):
        self.clock.advance(self.work)
        try: self.answer = self.operation(self.body)
        except Exception as error: self.error = error
        self.done = True
        for callback in self.callbacks: callback(self)
    def add_done_callback(self, callback):
        if self.done: callback(self)
        else: self.callbacks.append(callback)
    def result(self, timeout):
        self.timeouts.append(timeout)
        if not self.done:
            if self.work > timeout:
                self.clock.advance(timeout)
                raise TimeoutError()
            self.complete()
        self.clock.advance(self.wake)
        if self.error: raise self.error
        return self.answer


class Executor:
    def __init__(self, clock, *plans): self.clock, self.plans, self.futures = clock, list(plans), []
    def submit(self, operation, body):
        plan = dict(self.plans.pop(0))
        submit = plan.pop('submit', 0.0)
        ready = plan.pop('ready', False)
        future = Future(self.clock, operation, body, **plan)
        if ready: future.complete()
        self.clock.advance(submit)
        self.futures.append(future)
        return future


def invoke(monkeypatch, *plans, call=None):
    clock = Clock()
    monkeypatch.setattr(load_chain, 'time', SimpleNamespace(monotonic=clock.monotonic))
    executor = Executor(clock, *plans)
    answer = load_chain.submit_like_the_host(call or (lambda body: {'receipt': {'state': 'accepted'}}), 2.0, executor)
    return answer, executor


def test_submit_overhead_is_deducted_from_wait(monkeypatch):
    result, executor = invoke(monkeypatch, {'submit': .75, 'work': 1.5}, {'work': .2})
    assert executor.futures[0].timeouts == [pytest.approx(1.25)]
    assert result['outcome'] == 'ok_after_resend'
    assert result['attempts'] == 2 and len(result['abandoned']) == 1
    assert result['bodies'][0] == result['bodies'][1] == load_chain.EFFECT_BODY


def test_already_expired_submission_does_not_wait_another_limit(monkeypatch):
    result, executor = invoke(monkeypatch, {'submit': 2.1}, {'work': .1})
    assert executor.futures[0].timeouts == []
    assert result['outcome'] == 'ok_after_resend'
    assert len(result['abandoned']) == 1


@pytest.mark.parametrize('ready', [False, True])
def test_completed_answer_observed_after_deadline_is_ambiguous(monkeypatch, ready):
    result, _ = invoke(monkeypatch, {'ready': ready, 'work': .1, 'wake': 2.0}, {'work': .1})
    assert result['outcome'] == 'ok_after_resend'
    assert result['timings'][0] == pytest.approx(2.1)
    assert result['timings'][-1] <= 2.0
    assert len(result['abandoned']) == 1


def test_two_late_answers_end_unavailable_without_a_third_request(monkeypatch):
    result, executor = invoke(monkeypatch, {'work': .1, 'wake': 2.0}, {'work': .1, 'wake': 2.0})
    assert result['outcome'] == 'runtime_effect_unavailable'
    assert result['attempts'] == len(executor.futures) == 2
    assert len(result['abandoned']) == 2
    assert result['bodies'] == [load_chain.EFFECT_BODY, load_chain.EFFECT_BODY]


def test_two_actual_timeouts_preserve_bounded_same_body_retry(monkeypatch):
    result, executor = invoke(monkeypatch, {'work': 3.0}, {'work': 3.0})
    assert result['outcome'] == 'runtime_effect_unavailable'
    assert result['attempts'] == 2 and len(result['abandoned']) == 2
    assert all(f.timeouts == [2.0] for f in executor.futures)


def test_on_time_success_and_definite_refusal_keep_original_meaning(monkeypatch):
    result, _ = invoke(monkeypatch, {'work': .2})
    assert result['outcome'] == 'ok' and result['attempts'] == 1
    def refused(body): raise ValueError('synthetic refusal')
    result, executor = invoke(monkeypatch, {'work': .1}, call=refused)
    assert result['outcome'] == 'refused' and result['code'] == 'ValueError'
    assert result['attempts'] == len(executor.futures) == 1


def test_deadline_wins_over_a_refusal_first_observed_late(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(load_chain, 'time', SimpleNamespace(monotonic=clock.monotonic))
    executor = Executor(clock, {'work': .1}, {'work': .1})
    calls = []
    def call(body):
        calls.append(body)
        if len(calls) == 1:
            clock.advance(2.0)
            raise ValueError('synthetic refusal observed after the deadline')
        return {'receipt': {'state': 'accepted'}}
    result = load_chain.submit_like_the_host(call, 2.0, executor)
    assert result['outcome'] == 'ok_after_resend'
    assert result['attempt_traces'][0]['outcome'] == 'late_refusal'
    assert result['attempts'] == 2 and len(result['abandoned']) == 1
    assert calls == [load_chain.EFFECT_BODY, load_chain.EFFECT_BODY]
