"""Private hard-deadline HTTP process boundary (vendored from NEX-EIOS src/eios/adapters/_deadline_http.py; PROVENANCE.json).

NexLoop uses it for its side-effect-free outbound calls (model inference, embeddings): nexloop_eios.deadline_http."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import multiprocessing
import os
import signal
import time
from typing import Any, Callable

import httpx


_FORKSERVER_PRELOAD = ("eios.adapters._deadline_http",)
_REQUEST_CONTEXT: Any = None


def request_runtime_context() -> Any:
    """Return the process factory used for request children.

    A fresh interpreter per request costs 0.126s on the production collector
    against 0.006s for a forkserver fork, and its start-up tail is charged to
    the cycle's shared hard deadline: one slow start could push a single date
    past the gate. forkserver keeps the isolation the hard timeout needs (the
    child is still its own killable process group) without paying for a cold
    interpreter every time.
    """

    global _REQUEST_CONTEXT
    if _REQUEST_CONTEXT is None:
        try:
            context: Any = multiprocessing.get_context("forkserver")
            context.set_forkserver_preload(list(_FORKSERVER_PRELOAD))
        except ValueError:  # pragma: no cover - non-POSIX fallback
            context = multiprocessing.get_context("spawn")
        _REQUEST_CONTEXT = context
    return _REQUEST_CONTEXT


def ensure_request_runtime_ready() -> None:
    """Start the forkserver before the process holds anything worth leaking.

    Every request child is forked from this template, so it inherits whatever
    the template held: start it at boot, ahead of credentials, sessions and
    database handles, and the template stays clean for the process lifetime.
    Calling this late is a correctness problem, not a slow path.
    """

    context = request_runtime_context()
    if getattr(context, "_name", "") != "forkserver":
        return
    from multiprocessing import forkserver

    forkserver.set_forkserver_preload(list(_FORKSERVER_PRELOAD))
    forkserver.ensure_running()


@dataclass(frozen=True, repr=False)
class DeadlineHttpOutcome:
    kind: str
    status: int | None = None
    headers: dict[str, str] | None = None
    body: bytes = b""
    header_limit: bool = False

    def __repr__(self) -> str:
        return f"DeadlineHttpOutcome(kind={self.kind!r}, status={self.status!r})"


def _request_child(sender: Any, request: dict[str, Any]) -> None:
    try:
        os.setsid()
        sender.send(("ready", None))
        observation_channel = (
            sender if request.pop("observe_transport", False) is True else None
        )
        sender.send(
            (
                "outcome",
                asyncio.run(_request(request, observation_channel)),
            )
        )
    except ValueError:
        try:
            sender.send(("outcome", DeadlineHttpOutcome("body_limit")))
        except (BrokenPipeError, EOFError, OSError):
            pass
    except httpx.TimeoutException:
        try:
            sender.send(("outcome", DeadlineHttpOutcome("transport")))
        except (BrokenPipeError, EOFError, OSError):
            pass
    except BaseException:
        try:
            sender.send(("outcome", DeadlineHttpOutcome("transport")))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        request.clear()
        sender.close()


async def _request(
    request: dict[str, Any], observation_channel: Any | None = None
) -> DeadlineHttpOutcome:
    maximum = int(request["maximum"])
    body = bytearray()
    started = False
    try:
        async with httpx.AsyncClient(
            base_url=request["base_url"],
            headers=request["client_headers"],
            follow_redirects=False,
            verify=True,
            timeout=httpx.Timeout(
                connect=request["remaining"],
                read=request["remaining"],
                write=request["remaining"],
                pool=request["remaining"],
            ),
        ) as client:
            if observation_channel is not None:
                observation_channel.send(("started", time.monotonic()))
                started = True
                kind, remaining = observation_channel.recv()
                if (
                    kind != "deadline"
                    or type(remaining) is not float
                    or remaining <= 0
                ):
                    return DeadlineHttpOutcome("transport")
                request["remaining"] = remaining
            # NexLoop: no 5 s cap per operation - a model completion may send its first byte after 30 s; the parent's hard deadline
            # still ends the whole request.
            request_timeout = httpx.Timeout(float(request["remaining"]))
            async with client.stream(
                request["method"],
                request["path"],
                params=request.get("params"),
                headers=request["headers"],
                json=request.get("json"),
                # NexLoop: a prepared request body (bytes) besides json.
                content=request.get("content"),
                follow_redirects=False,
                timeout=request_timeout,
            ) as response:
                safe_headers = {
                    name: value
                    for name, value in response.headers.items()
                    if name.lower()
                    in {"content-type", "content-length", "content-encoding"}
                }
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    stripped = content_length.strip()
                    if (
                        not stripped
                        or not stripped.isascii()
                        or not stripped.isdigit()
                        or int(stripped) > maximum
                    ):
                        return DeadlineHttpOutcome(
                            "body_limit",
                            response.status_code,
                            safe_headers,
                            header_limit=True,
                        )
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > maximum:
                        return DeadlineHttpOutcome(
                            "body_limit", response.status_code, safe_headers
                        )
                    body.extend(chunk)
                return DeadlineHttpOutcome(
                    "response",
                    response.status_code,
                    safe_headers,
                    bytes(body),
                )
    finally:
        if observation_channel is not None and started:
            try:
                observation_channel.send(("finished", None))
            except (BrokenPipeError, EOFError, OSError):
                pass


def request_until(
    request: dict[str, Any],
    *,
    deadline_monotonic: float,
    on_http_start: Callable[[float], float] | None = None,
    on_http_finish: Callable[[], None] | None = None,
    _process_target: Any = _request_child,
) -> DeadlineHttpOutcome:
    """Run one request in a child process with a typed, sanitized outcome."""

    context = request_runtime_context()
    observed = on_http_start is not None or on_http_finish is not None
    receiver, sender = context.Pipe(duplex=observed)
    request["observe_transport"] = observed
    process: multiprocessing.Process | None = None
    group_ready = False
    active_deadline = deadline_monotonic
    http_started = False
    http_finished = False

    def finish_once() -> bool:
        nonlocal http_finished
        if not http_started or http_finished:
            return True
        http_finished = True
        if on_http_finish is None:
            return True
        try:
            on_http_finish()
        except Exception:
            return False
        return True

    try:
        process = context.Process(target=_process_target, args=(sender, request))
        process.start()
        sender.close()
        while True:
            remaining = active_deadline - time.monotonic()
            if remaining <= 0 or not receiver.poll(remaining):
                _terminate(process, group_ready=group_ready)
                return DeadlineHttpOutcome("deadline")
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                return DeadlineHttpOutcome("transport")
            if kind == "ready":
                group_ready = True
                continue
            if kind == "started":
                if type(payload) is not float:
                    _terminate(process, group_ready=group_ready)
                    return DeadlineHttpOutcome("transport")
                http_started = True
                if on_http_start is not None:
                    try:
                        observed_deadline = on_http_start(payload)
                    except Exception:
                        # The observer refused to register this start; that is
                        # an evidence fault, not a slow source. Reporting it as
                        # a deadline used to hide the real cause behind a
                        # timeout code that the timings plainly contradicted.
                        _terminate(process, group_ready=group_ready)
                        return DeadlineHttpOutcome("observer")
                    if (
                        type(observed_deadline) is not float
                        or observed_deadline <= time.monotonic()
                    ):
                        _terminate(process, group_ready=group_ready)
                        return DeadlineHttpOutcome("deadline")
                    active_deadline = observed_deadline
                try:
                    receiver.send(
                        ("deadline", active_deadline - time.monotonic())
                    )
                except (BrokenPipeError, EOFError, OSError):
                    _terminate(process, group_ready=group_ready)
                    return DeadlineHttpOutcome("transport")
                continue
            if kind == "finished":
                if not finish_once():
                    _terminate(process, group_ready=group_ready)
                    return DeadlineHttpOutcome("transport")
                continue
            process.join(timeout=max(0.0, active_deadline - time.monotonic()))
            if process.is_alive():
                _terminate(process, group_ready=group_ready)
                return DeadlineHttpOutcome("deadline")
            return (
                payload
                if kind == "outcome" and isinstance(payload, DeadlineHttpOutcome)
                else DeadlineHttpOutcome("transport")
            )
    except Exception:
        return DeadlineHttpOutcome("transport")
    finally:
        finish_once()
        try:
            sender.close()
        except OSError:
            pass
        receiver.close()
        request.clear()
        if process is not None and process.pid is not None and process.is_alive():
            _terminate(process, group_ready=group_ready)
        if process is not None and process.pid is not None:
            process.join()
            process.close()


def _terminate(
    process: multiprocessing.Process, *, group_ready: bool
) -> None:
    if process.pid is None:
        return
    try:
        if group_ready:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    process.join(timeout=0.01)
    if process.is_alive():
        try:
            if group_ready:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.join()
