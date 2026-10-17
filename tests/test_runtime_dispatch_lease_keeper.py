"""10/18 stage (Run 73fb5aec): the dispatch renewed its task lease only before a Host request; one slow step let it expire while
the Run was finishing, and the task was dead-lettered. The keeper renews it every third of the lease on its own until stopped,
stops for good when the lease is lost, and keeps trying after a failed renewal."""
import time

from nexloop_eios.durable_queue import StaleQueueLease
from nexloop_eios.runtime_dispatch import _LeaseKeeper


class Worker:
    def __init__(self, fail=(), lost_at=None):
        self.calls, self.fail, self.lost_at = [], set(fail), lost_at

    def renew_task(self, *, queue, task_id, fence, lease_seconds):
        self.calls.append((queue, task_id, fence, lease_seconds))
        n = len(self.calls)
        if self.lost_at is not None and n >= self.lost_at:
            raise StaleQueueLease('lease lost')
        if n in self.fail:
            raise RuntimeError('database slow')


def test_the_keeper_renews_on_its_own_until_stopped():
    worker = Worker(fail={2})  # a failed renewal is retried at the next interval
    keeper = _LeaseKeeper(worker, 'operations', 'task-1', 7, 3)  # every second
    keeper.start();time.sleep(3.4);keeper.stop()
    assert len(worker.calls) == 3 and set(worker.calls) == {('operations', 'task-1', 7, 3)}
    time.sleep(1.2)
    assert len(worker.calls) == 3  # stopped: no renewal after stop


def test_the_keeper_stops_for_good_when_the_lease_is_lost():
    worker = Worker(lost_at=1)
    keeper = _LeaseKeeper(worker, 'operations', 'task-1', 7, 3)
    keeper.start();time.sleep(2.4)
    assert len(worker.calls) == 1  # no renewal after the loss
    keeper.stop()
