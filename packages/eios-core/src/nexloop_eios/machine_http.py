"""NX-062 M3 (docs/implementation/NX-062-design.md §2.1, §3.1; ADR-041 §2.4-2.5, §2.7; L6 C1, C2, C4): the machine_key realm.

/machine/v1/* accepts exactly one credential: ``Authorization: Bearer nxm_...``. Every other credential (a cookie session, a service or
Run token, a signature) is not looked at and answers 401; a machine key on any other route is not a credential there either (the other
realms never read it). Per request:

1. no query string at all (400 query_not_allowed; a key must never travel in a URL - the gateway logs no URI, C9);
2. the key's shape (machine_keys.parse), then the row's pepper version by prefix and one HMAC with that version (C2);
3. authz.nexloop_machine_admit: identity at clock_timestamp(), the rate policy in force (none = refused and counted, never unlimited, C4),
   per-key and per-tenant minute windows (429 with Retry-After), every outcome counted for the alert rules;
4. the route's own SQL port (the capability list: scope ∩ allowlist ∩ grants - empty until the M5 grants).

Tenant, world and actor come only from the key (SQL). Request headers naming an identity are never read. A small process-wide
concurrency limit keeps machine calls from taking the workbench's connections (T-8). Responses carry error codes only, never a value of
the request, and Cache-Control: no-store.
"""
import threading
import uuid

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nexloop_eios import machine_keys as K

PREFIX = '/machine/v1'
CONCURRENCY = 8
_UNKNOWN_DIGEST = '0' * 64  # an unknown prefix is still admitted (and counted) as denied, with a digest no row holds


def _error(code, status, headers=None):
    return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()), 'retryable': status in (429, 503), 'details': {}},
                        status_code=status, headers={'Cache-Control': 'no-store', **(headers or {})})


def router(*, peppers_for, pool_for):
    """peppers_for(request) -> machine_keys.Peppers | None; pool_for(request) -> the API's business pool | None."""
    routes = APIRouter(prefix=PREFIX)
    gate = threading.BoundedSemaphore(CONCURRENCY)

    def admitted(request):
        """(prefix, digest) of an admitted key, or the refusal response."""
        if request.url.query:
            return None, _error('query_not_allowed', 400)
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or len(header) > 256:
            return None, _error('unauthenticated', 401)
        try:
            key = K.parse(header[7:])
        except K.MachineKeyInvalid:
            return None, _error('unauthenticated', 401)
        peppers, pool = peppers_for(request), pool_for(request)
        if peppers is None or pool is None:
            return None, _error('unavailable', 503)
        try:
            with pool.connection() as db:
                version = db.execute('select authz.nexloop_machine_key_pepper_version(%s)', (key.prefix_id,)).fetchone()[0]
                digest = _UNKNOWN_DIGEST
                if version is not None and version in peppers.versions:
                    digest = K.digest(key, peppers, version).hex()
                result = db.execute('select authz.nexloop_machine_admit(%s,%s)', (key.prefix_id, digest)).fetchone()[0]
        except (psycopg.OperationalError, psycopg.InterfaceError):
            return None, _error('unavailable', 503)
        status = result.get('status')
        if status == 'ok':
            return (key.prefix_id, digest), None
        if status == 'rate_limited':
            return None, _error('rate_limited', 429, {'Retry-After': str(int(result['retry_after']))})
        if status == 'rate_policy_missing':
            return None, _error('rate_policy_missing', 503)
        return None, _error('unauthenticated', 401)

    @routes.get('/capabilities')
    def capabilities(request: Request):
        if not gate.acquire(blocking=False):
            return _error('busy', 503, {'Retry-After': '1'})
        try:
            credential, refusal = admitted(request)
            if refusal is not None:
                return refusal
            try:
                with pool_for(request).connection() as db:
                    listed = db.execute('select authz.nexloop_machine_capability_list(%s,%s)', credential).fetchone()[0]
            except psycopg.errors.InsufficientPrivilege:
                return _error('unauthenticated', 401)  # revoked or expired between the two statements
            except (psycopg.OperationalError, psycopg.InterfaceError):
                return _error('unavailable', 503)
            return JSONResponse({'capabilities': listed}, headers={'Cache-Control': 'no-store'})
        finally:
            gate.release()

    return routes
