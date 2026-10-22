from __future__ import annotations

import asyncio
import multiprocessing
import os
import socketserver
import threading
import time
from typing import Any

import pytest

from eios.adapters._deadline_http import (
    DeadlineHttpOutcome,
    _request,
    ensure_request_runtime_ready,
    request_runtime_context,
    request_until,
)


def test_request_children_come_from_a_preloaded_forkserver() -> None:
    """A cold interpreter per request charged its start-up to the hard gate."""

    context = request_runtime_context()
    if not hasattr(multiprocessing, "get_all_start_methods"):  # pragma: no cover
        pytest.skip("start methods unavailable")
    if "forkserver" not in multiprocessing.get_all_start_methods():  # pragma: no cover
        pytest.skip("forkserver unavailable on this platform")

    assert context.get_start_method() == "forkserver"
    # Every child is forked from this template, so it must be taken before the
    # process holds credentials or database handles.
    ensure_request_runtime_ready()
    assert request_runtime_context() is context


def _blocked_resolver_child(sender: Any, request: dict[str, Any]) -> None:
    os.setsid()
    sender.send(("ready", None))
    request.clear()
    time.sleep(60)


def _duplicate_terminal_child(sender: Any, request: dict[str, Any]) -> None:
    os.setsid()
    sender.send(("ready", None))
    sender.send(("started", time.monotonic()))
    sender.send(("finished", None))
    sender.send(("finished", None))
    sender.send(("outcome", DeadlineHttpOutcome("transport")))
    request.clear()


def _started_then_blocked_child(sender: Any, request: dict[str, Any]) -> None:
    os.setsid()
    sender.send(("ready", None))
    sender.send(("started", time.monotonic()))
    request.clear()
    time.sleep(60)


def _slow_prestart_then_response_child(sender: Any, request: dict[str, Any]) -> None:
    os.setsid()
    sender.send(("ready", None))
    time.sleep(0.6)
    sender.send(("started", time.monotonic()))
    sender.recv()
    time.sleep(0.6)
    sender.send(("outcome", DeadlineHttpOutcome("response", 200)))
    request.clear()


def _handshake_then_response_child(sender: Any, request: dict[str, Any]) -> None:
    os.setsid()
    sender.send(("ready", None))
    sender.send(("started", time.monotonic()))
    sender.recv()
    sender.send(("finished", None))
    sender.send(("outcome", DeadlineHttpOutcome("response", 200)))
    request.clear()


def test_started_timeout_finishes_once_and_unstarted_timeout_never_finishes() -> None:
    started_finishes: list[str] = []
    unstarted_finishes: list[str] = []

    started = request_until(
        {"synthetic": "started-timeout"},
        # 外层 deadline 只是防挂死看门狗:满载轮子进程起动可超 1 秒,起动预算
        # 不是本测试的对象。真正的超时由 on_http_start 的相对 +0.1s 驱动。
        deadline_monotonic=time.monotonic() + 30.0,
        on_http_start=lambda observed_at: observed_at + 0.1,
        on_http_finish=lambda: started_finishes.append("finish"),
        _process_target=_started_then_blocked_child,
    )
    unstarted = request_until(
        {"synthetic": "unstarted-timeout"},
        deadline_monotonic=time.monotonic() + 0.1,
        on_http_start=lambda observed_at: observed_at + 5.0,
        on_http_finish=lambda: unstarted_finishes.append("finish"),
        _process_target=_blocked_resolver_child,
    )

    assert started.kind == "deadline"
    assert unstarted.kind == "deadline"
    assert started_finishes == ["finish"]
    assert unstarted_finishes == []


def test_prestart_watchdog_is_replaced_by_actual_http_deadline() -> None:
    started = time.monotonic()
    outcome = request_until(
        {"synthetic": "separate-deadlines"},
        # Process startup can be delayed by heavily parallel CI. Keep the
        # pre-start watchdog distinct from the one-second actual HTTP deadline
        # without making scheduler latency the subject of this test.
        deadline_monotonic=started + 3.0,
        on_http_start=lambda observed_at: observed_at + 1.0,
        _process_target=_slow_prestart_then_response_child,
    )

    assert outcome.kind == "response"
    assert time.monotonic() - started >= 1.2


def test_real_request_starts_observation_after_client_construction(monkeypatch) -> None:
    events: list[str] = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def aiter_bytes(self):
            if False:
                yield b""

    class Stream:
        async def __aenter__(self):
            events.append("stream_enter")
            return Response()

        async def __aexit__(self, *_args):
            pass

    class Client:
        def __init__(self, **_kwargs):
            events.append("client_constructed")

        async def __aenter__(self):
            events.append("client_entered")
            return self

        async def __aexit__(self, *_args):
            pass

        def stream(self, *_args, **_kwargs):
            events.append("stream_created")
            return Stream()

    class Channel:
        def send(self, message):
            events.append(message[0])

        def recv(self):
            events.append("deadline_received")
            return ("deadline", 1.0)

    monkeypatch.setattr("eios.adapters._deadline_http.httpx.AsyncClient", Client)
    outcome = asyncio.run(
        _request(
            {
                "base_url": "https://example.invalid",
                "client_headers": {},
                "method": "GET",
                "path": "/inventory",
                "headers": {},
                "remaining": 2.0,
                "maximum": 128,
            },
            Channel(),
        )
    )

    assert outcome.kind == "response"
    assert events.index("client_entered") < events.index("started")
    assert events.index("started") < events.index("stream_created")


def test_finish_only_observation_receives_deadline_handshake() -> None:
    finishes: list[str] = []
    outcome = request_until(
        {"synthetic": "finish-only"},
        deadline_monotonic=time.monotonic() + 1.0,
        on_http_finish=lambda: finishes.append("finish"),
        _process_target=_handshake_then_response_child,
    )

    assert outcome.kind == "response"
    assert finishes == ["finish"]


def test_duplicate_terminal_messages_finish_exactly_once() -> None:
    finishes: list[str] = []

    outcome = request_until(
        {"synthetic": "duplicate-terminal"},
        deadline_monotonic=time.monotonic() + 1.0,
        on_http_start=lambda observed_at: observed_at + 1.0,
        on_http_finish=lambda: finishes.append("finish"),
        _process_target=_duplicate_terminal_child,
    )

    assert outcome.kind == "transport"
    assert finishes == ["finish"]


def test_started_request_finishes_once_when_start_callback_raises() -> None:
    finishes: list[str] = []

    def reject_start(_observed_at: float) -> float:
        raise RuntimeError("private callback failure")

    outcome = request_until(
        {"synthetic": "start-callback-failure"},
        deadline_monotonic=time.monotonic() + 1.0,
        on_http_start=reject_start,
        on_http_finish=lambda: finishes.append("finish"),
        _process_target=_started_then_blocked_child,
    )

    # An observer that refuses the start is an evidence fault, not a slow
    # source: reporting it as "deadline" made cycles carry a timeout code that
    # their own sub-second timings contradicted.
    assert outcome.kind == "observer"
    assert finishes == ["finish"]


def test_blocked_resolver_process_is_killed_and_fully_reaped_at_deadline() -> None:
    request_until(
        {"synthetic": "resource-tracker-warmup"},
        deadline_monotonic=time.monotonic() + 0.05,
        _process_target=_blocked_resolver_child,
    )
    children_before = {child.pid for child in multiprocessing.active_children()}
    threads_before = {thread.ident for thread in threading.enumerate()}
    fds_before = len(os.listdir("/dev/fd"))
    started = time.monotonic()

    result = request_until(
        {"synthetic": "resolver-block"},
        deadline_monotonic=started + 0.1,
        _process_target=_blocked_resolver_child,
    )

    assert result.kind == "deadline"
    assert time.monotonic() - started <= 0.14
    assert {child.pid for child in multiprocessing.active_children()} == children_before
    assert {thread.ident for thread in threading.enumerate()} == threads_before
    assert len(os.listdir("/dev/fd")) == fds_before


def test_spawn_start_failure_clears_request_and_leaks_no_handles() -> None:
    children_before = {child.pid for child in multiprocessing.active_children()}
    fds_before = len(os.listdir("/dev/fd"))
    request = {"authorization": "private-start-failure-marker"}

    outcome = request_until(
        request,
        deadline_monotonic=time.monotonic() + 1,
        _process_target=lambda _sender, _request: None,
    )

    assert outcome.kind == "transport"
    assert request == {}
    assert {child.pid for child in multiprocessing.active_children()} == children_before
    assert len(os.listdir("/dev/fd")) == fds_before


def test_oversize_content_length_is_rejected_before_stalled_body() -> None:
    starts: list[float] = []
    finishes: list[str] = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(65536)
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 999999\r\n\r\n"
            )
            self.request.settimeout(60)
            try:
                self.request.recv(1)
            except OSError:
                pass

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        started = time.monotonic()
        try:
            outcome = request_until(
                {
                    "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                    "client_headers": {"Accept": "application/json"},
                    "method": "GET",
                    "path": "/inventory",
                    "headers": {},
                    "remaining": 4.0,
                    "maximum": 128,
                },
                deadline_monotonic=started + 4,
                on_http_start=lambda observed_at: (
                    starts.append(observed_at) or observed_at + 4.0
                ),
                on_http_finish=lambda: finishes.append("finish"),
            )
            assert outcome.kind == "body_limit"
            assert time.monotonic() - started < 3.5
            assert len(starts) == 1
            assert finishes == ["finish"]
        finally:
            server.shutdown()
            thread.join()


def test_outcome_repr_and_ipc_headers_are_redacted() -> None:
    marker = "private-cookie-and-body-marker"
    outcome = DeadlineHttpOutcome(
        "response",
        200,
        {"set-cookie": marker, "content-type": "application/json"},
        marker.encode(),
    )

    assert marker not in repr(outcome)
