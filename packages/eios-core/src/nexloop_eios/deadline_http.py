"""Hard-deadline outbound HTTP for side-effect-free calls (eios-capability-gap D2, dispatcher 2026-10-22).

A call runs in a forkserver child (its own process group, eios.adapters._deadline_http vendored from NEX-EIOS); the parent waits
for one total deadline - connection, TLS, writing and reading together, not a timeout per step - and kills the child's group when
it passes. So DNS, TLS or a provider that stops mid-response never holds a worker thread, a database connection or a pool slot past
the deadline.

Only for calls without an external effect: model inference (Claim extraction, matching, decision evaluation) and embeddings. A
timeout there is a recognizable failure (DeadlineExceeded) that the caller may retry within its own bounds. Calls with an external
effect (nexloop_eios.effect_provider, the ledgers) never use it: an unknown result there is queried and reconciled first, never
killed and retried blindly.
"""
import time
from urllib.parse import urlsplit

from eios.adapters._deadline_http import ensure_request_runtime_ready, request_until

MAXIMUM_BODY = 4 * 1024 * 1024
MAXIMUM_DEADLINE = 120.0


class DeadlineExceeded(TimeoutError):
    """The total deadline passed; the request's process group was killed. No answer exists (the call had no effect)."""


class DeadlineTransportError(OSError):
    """The request failed before an answer (connection, TLS, protocol, or the answer was larger than allowed)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def post(url, headers, body, *, deadline_seconds, ca, maximum=MAXIMUM_BODY):
    """POST `body` (bytes) to an https URL within `deadline_seconds` in total; returns (status, body bytes). `ca` keeps each
    caller's CA source ('system', 'certifi' or a CA file path); the environment (proxies, SSL_CERT_*) is never used."""
    if ca not in ('system', 'certifi') and not (type(ca) is str and ca.startswith('/')):
        raise ValueError('CA source required')
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('https URL required')
    if type(deadline_seconds) not in (int, float) or not 0 < deadline_seconds <= MAXIMUM_DEADLINE:
        raise ValueError('deadline_seconds 0..120')
    if type(body) is not bytes or type(maximum) is not int or not 0 < maximum <= 16 * 1024 * 1024:
        raise ValueError('body bytes and a bounded response size required')
    base = f'{parts.scheme}://{parts.netloc}'
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    outcome = request_until({'base_url': base, 'client_headers': {}, 'method': 'POST', 'path': path, 'headers': dict(headers),
                             'content': body, 'maximum': maximum, 'remaining': float(deadline_seconds), 'ca': ca},
                            deadline_monotonic=time.monotonic() + float(deadline_seconds))
    if outcome.kind == 'response':
        return outcome.status, outcome.body
    if outcome.kind == 'deadline':
        raise DeadlineExceeded('deadline')
    raise DeadlineTransportError(outcome.kind)  # transport, body_limit, observer


__all__ = ['DeadlineExceeded', 'DeadlineTransportError', 'MAXIMUM_BODY', 'ensure_request_runtime_ready', 'post']
