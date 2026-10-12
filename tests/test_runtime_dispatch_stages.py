"""NX-031 ladder (500 customers): the runtime worker names an unexpected dispatch failure by where it happened and refuses a
Run queued past its not_after (= its credential's expiry) as terminal runtime_command_expired, before any activation or Host
request. Fake worker and Host client (no PG, no Host): only the dispatcher's own classification is under test."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nexloop_eios import goal_controls
from nexloop_eios.runtime_dispatch import RuntimeDispatcher


class Worker:
    def __init__(self, not_after, *, activation_error=None):
        self.finished, self.activations, self.activation_error = [], 0, activation_error
        self._backend, self._session = SimpleNamespace(_pool=None), None
        command = {'run_id': '11111111-1111-4111-8111-111111111111', 'request_id': 'r', 'not_after': not_after.isoformat(), 'runtime_owner_epoch': 1}
        self.job = {'task_id': 't', 'fence': 1, 'payload': {'run_command': command, 'input': 'x'}}

    def claim_task(self, **_):
        job, self.job = self.job, None
        return job

    def assert_task_lease(self, **_):
        pass

    def renew_task(self, **_):
        pass

    def create_runtime_activation(self, **_):
        self.activations += 1
        if self.activation_error:
            raise self.activation_error
        return {'activation_ref': 'activation_x', 'ever_execution_authorized': False}

    def finish_task(self, *, status, result, **_):
        self.finished.append((status, result))
        return {'status': status}


class Client:
    def __init__(self):
        self.calls = []

    def request(self, operation, body, *, deadline):
        self.calls.append(operation)
        raise RuntimeError('unexpected Host failure')


@pytest.fixture(autouse=True)
def no_control(monkeypatch):
    monkeypatch.setattr('nexloop_eios.runtime_dispatch._command', lambda command: ('{}', 'd'))
    monkeypatch.setattr('nexloop_eios.runtime_dispatch._input_digest', lambda value: 'd')
    monkeypatch.setattr(goal_controls.ControlPlane, '__init__', lambda self, *a, **k: None)
    monkeypatch.setattr(goal_controls.ControlPlane, 'prepare_run_dispatch', lambda self, **k: None)


def dispatcher(worker):
    d = RuntimeDispatcher.__new__(RuntimeDispatcher)
    d.worker, d.client, d.queue, d.cache_wakeup = worker, Client(), 'operations', None
    d.lease_seconds, d.total_timeout, d.request_timeout, d.poll_seconds = 30, 5, 2, .05
    return d


def test_a_run_queued_past_its_not_after_is_expired_before_activation():
    worker = Worker(datetime.now(UTC) - timedelta(seconds=1))
    d = dispatcher(worker)
    d.run_once()
    (status, result), = worker.finished
    assert status == 'failed' and result['code'] == 'runtime_command_expired'
    assert worker.activations == 0 and d.client.calls == []


def test_an_activation_failure_is_named_and_terminal():
    worker = Worker(datetime.now(UTC) + timedelta(seconds=120), activation_error=RuntimeError('activation denied'))
    dispatcher(worker).run_once()
    (status, result), = worker.finished
    assert status == 'failed' and result['code'] == 'runtime_activation_unavailable'


def test_a_control_failure_is_named(monkeypatch):
    monkeypatch.setattr(goal_controls.ControlPlane, 'prepare_run_dispatch', lambda self, **k: (_ for _ in ()).throw(RuntimeError('control store down')))
    worker = Worker(datetime.now(UTC) + timedelta(seconds=120))
    dispatcher(worker).run_once()
    (status, result), = worker.finished
    assert result['code'] == 'runtime_control_unavailable' and worker.activations == 0
