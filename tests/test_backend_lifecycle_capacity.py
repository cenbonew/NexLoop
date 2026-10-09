"""LifecycleLock request capacity vs connection pool (pool starvation fix).

Synthetic disposable PG only. Default pool_max_size=4 -> capacity 2.
"""
import threading
import time
import pytest
from nexloop_eios.backend import BackendBusy, LifecycleLock, request_depth
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from role_run_fixture import role_runtime_plan  # noqa: F401


def test_capacity_bounds_outer_requests_but_not_reentry():
    lock = LifecycleLock(capacity=1, wait_seconds=0.3)
    entered = threading.Event(); release = threading.Event(); outcome = []
    def holder():
        with lock:
            with lock:  # same-thread reentry never waits for capacity
                assert request_depth() == 2
                entered.set(); release.wait(5)
    thread = threading.Thread(target=holder); thread.start(); assert entered.wait(5)
    started = time.monotonic()
    def contender():
        try:
            with lock:outcome.append('entered')
        except BackendBusy as error:outcome.append(str(error))
    other = threading.Thread(target=contender); other.start(); other.join(5)
    assert outcome == ['backend request capacity unavailable'] and time.monotonic() - started < 2
    release.set(); thread.join(5)
    with lock:assert request_depth() == 1
    assert request_depth() == 0


def test_admission_is_fifo_and_timed_out_waiters_leave_the_queue():
    lock = LifecycleLock(capacity=1, wait_seconds=5)
    release = threading.Event(); order = []
    def holder():
        with lock:release.wait(5)
    first = threading.Thread(target=holder); first.start(); time.sleep(0.1)
    def waiter(name):
        with lock:order.append(name)
    threads = []
    for name in range(6):
        thread = threading.Thread(target=waiter, args=(name,)); thread.start(); threads.append(thread); time.sleep(0.05)
    release.set(); first.join(5)
    for thread in threads:thread.join(5)
    assert order == list(range(6))
    # A waiter that times out leaves the queue; the next one is admitted when capacity frees.
    short = LifecycleLock(capacity=1, wait_seconds=0.3); gate = threading.Event(); results = []
    def hold():
        with short:gate.wait(5)
    keeper = threading.Thread(target=hold); keeper.start(); time.sleep(0.1)
    def wait_once(name):
        try:
            with short:results.append(name)
        except BackendBusy:results.append('busy-' + name)
    timed = threading.Thread(target=wait_once, args=('early',)); timed.start(); timed.join(5)
    assert results == ['busy-early']
    later = threading.Thread(target=wait_once, args=('later',)); later.start(); time.sleep(0.05); gate.set(); keeper.join(5); later.join(5)
    assert results == ['busy-early', 'later'] and short._active == 0 and not short._queue


def test_late_arrival_cannot_barge_ahead_of_an_earlier_waiter():
    lock = LifecycleLock(capacity=1, wait_seconds=5)
    release = threading.Event(); order = []
    def holder():
        with lock:release.wait(5)
    first = threading.Thread(target=holder); first.start(); time.sleep(0.1)
    def waiter(name, hold=0.0):
        with lock:
            order.append(name); time.sleep(hold)
    early = threading.Thread(target=waiter, args=('early',)); early.start(); time.sleep(0.2)
    # Capacity frees and a brand-new request arrives in the same instant: it must queue
    # behind the earlier waiter instead of winning the wake-up race.
    late = threading.Thread(target=waiter, args=('late',))
    release.set(); late.start()
    first.join(5); early.join(5); late.join(5)
    assert order == ['early', 'late']


def test_waiters_after_a_timed_out_head_are_admitted_in_order():
    lock = LifecycleLock(capacity=1, wait_seconds=0.6)
    gate = threading.Event(); results = []
    def hold():
        with lock:gate.wait(5)
    keeper = threading.Thread(target=hold); keeper.start(); time.sleep(0.1)
    def wait_once(name):
        try:
            with lock:
                results.append(name); time.sleep(0.05)
        except BackendBusy:results.append('busy-' + name)
    head = threading.Thread(target=wait_once, args=('head',)); head.start(); time.sleep(0.4)
    second = threading.Thread(target=wait_once, args=('second',)); second.start(); time.sleep(0.05)
    third = threading.Thread(target=wait_once, args=('third',)); third.start()
    head.join(5)                      # head times out (0.6 s) while the keeper still holds
    assert results == ['busy-head']
    gate.set(); keeper.join(5); second.join(5); third.join(5)
    assert results == ['busy-head', 'second', 'third'] and lock._active == 0 and not lock._queue


def test_shutdown_waits_for_in_flight_request():
    lock = LifecycleLock(capacity=2); inside = threading.Event(); finish = threading.Event(); order = []
    def request():
        with lock:
            inside.set(); finish.wait(5); order.append('request done')
    thread = threading.Thread(target=request); thread.start(); assert inside.wait(5)
    def shutdown():
        with lock.exclusive():order.append('shutdown')
    closer = threading.Thread(target=shutdown); closer.start(); time.sleep(0.2)
    assert order == []
    finish.set(); thread.join(5); closer.join(5)
    assert order == ['request done', 'shutdown']


def test_capacity_follows_pool_size(role_runtime_plan):
    backend = role_runtime_plan['backend_worker']
    assert backend._pool.max_size == 4 and backend._lock._capacity == 2


BUSY = []


def retrying(call, attempts=5):
    """Caller semantics for overload: BackendBusy is explicit and retryable; nothing else is retried."""
    for attempt in range(attempts):
        try:return call()
        except BackendBusy:
            BUSY.append(attempt)
            if attempt == attempts - 1:raise
            time.sleep(0.2 * (attempt + 1))


def run_threads(functions, timeout=180):
    barrier = threading.Barrier(len(functions)); outcomes = [None] * len(functions)
    def wrap(index, function):
        barrier.wait()
        try:function();outcomes[index] = 'ok'
        except Exception as error:outcomes[index] = type(error).__name__
    threads = [threading.Thread(target=wrap, args=pair) for pair in enumerate(functions)]
    started = time.monotonic()
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout); assert not thread.is_alive(), 'request hung'
    return outcomes, time.monotonic() - started


def test_eight_concurrent_requests_complete_on_pool_of_four(role_runtime_plan, admin):
    plan = role_runtime_plan
    BUSY.clear()
    def submit():
        services = retrying(lambda: plan['backend_worker'].authenticate(plan['worker_token'], world='real'))
        retrying(lambda: services.runtime_effect_tool(activation_ref=plan['activations'][0], command=plan['commands'][0], tool_operation='submit', parameters={'message': 'one business intent'}))
    def authorize(operation):
        def call():
            services = retrying(lambda: plan['backend_worker'].authenticate(plan['worker_token'], world='real'))
            assert retrying(lambda: services.authorize_runtime_activation(activation_ref=plan['activations'][1], command=plan['commands'][1], operation=operation))['authorized']
        return call
    outcomes, elapsed = run_threads([submit] * 4 + [authorize('model'), authorize('inspect'), authorize('model'), authorize('inspect')])
    # All eight finished (no hang, no PoolTimeout starvation). Overload on a slow host may
    # surface as the explicit retryable BackendBusy (FIFO-bounded); after retry every
    # authorize on the other Run succeeds.
    assert outcomes[4:] == ['ok'] * 4, (outcomes, BUSY)
    # Same-Run concurrent identical submits all receive the one stable intent: same-Run tool
    # requests serialize before the first guard, so no lock cycle can abort a caller.
    assert outcomes[:4] == ['ok'] * 4, (outcomes, BUSY)
    assert 'BackendBusy' not in outcomes and elapsed < 150, (outcomes, BUSY, elapsed)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s', (plan['tenant'],)).fetchone() == (1,)


def test_request_waiting_for_capacity_still_meets_final_deadline(role_runtime_plan, admin):
    plan = role_runtime_plan; backend = plan['backend_worker']
    occupy = threading.Event(); release = threading.Event()
    def hold():
        with backend._lock:
            occupy.set(); release.wait(10)
    holders = [threading.Thread(target=hold) for _ in range(backend._lock._capacity)]
    for thread in holders:thread.start()
    assert occupy.wait(5); time.sleep(0.2)
    outcome = []
    def submit():
        services = plan['backend_worker'].authenticate(plan['worker_token'], world='real')
        try:
            services.runtime_effect_tool(activation_ref=plan['activations'][0], command=plan['commands'][0], tool_operation='submit', parameters={'message': 'waited for capacity'})
            outcome.append('ok')
        except Exception as error:outcome.append(type(error).__name__)
    # authenticate() itself is a request and waits too: build the caller on its own thread.
    waiter = threading.Thread(target=submit); waiter.start(); time.sleep(0.5)
    assert outcome == []  # still waiting, not failed and not running
    # While it waits, the Run's governed deadline passes (disposable fault injection on the
    # Run credential ledger); the request must be denied after it acquires capacity.
    admin.execute("update authz.nexloop_run_credentials set expires_at=clock_timestamp() where run_id=%s", (plan['commands'][0]['run_id'],))
    release.set()
    for thread in holders:thread.join(10)
    waiter.join(30); assert not waiter.is_alive()
    assert outcome and outcome[0] != 'ok', outcome
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s', (plan['tenant'],)).fetchone() == (0,)


def test_capacity_wait_times_out_with_explicit_error(role_runtime_plan, monkeypatch):
    plan = role_runtime_plan; backend = plan['backend_worker']
    monkeypatch.setattr(backend._lock, '_wait_seconds', 0.5)
    occupy = threading.Event(); release = threading.Event()
    def hold():
        with backend._lock:
            occupy.set(); release.wait(10)
    holders = [threading.Thread(target=hold) for _ in range(backend._lock._capacity)]
    for thread in holders:thread.start()
    assert occupy.wait(5); time.sleep(0.2)
    started = time.monotonic()
    with pytest.raises(BackendBusy, match='backend request capacity unavailable'):
        plan['backend_worker'].authenticate(plan['worker_token'], world='real')
    assert time.monotonic() - started < 3
    release.set()
    for thread in holders:thread.join(10)


def test_run_request_is_a_bounded_lifecycle_request(role_runtime_plan):
    backend = role_runtime_plan['backend_worker']
    def operation(pool, signer):
        assert request_depth() == 1 and pool is backend._pool and signer is backend._signer
        with pool.connection() as connection:
            return connection.execute('select 1').fetchone()[0]
    assert backend.run_request(operation) == 1
    with pytest.raises(TypeError):backend.run_request(None)
