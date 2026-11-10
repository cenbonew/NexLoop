"""NX-062 M3 (docs/implementation/NX-062-design.md §2.1, §3.1; ADR-041 §2.4-2.5, §2.7; L6 C1, C2, C4): the machine_key realm.

/machine/v1/* accepts exactly one credential: ``Authorization: Bearer nxm_...``. Every other credential (a cookie session, a service or
Run token, a signature) is not looked at and answers 401; a machine key on any other route is not a credential there either (the other
realms never read it). Per request:

1. no query string at all (400 query_not_allowed; a key must never travel in a URL - the gateway logs no URI, C9);
2. the key's shape (machine_keys.parse), then the row's pepper version by prefix and one HMAC with that version (C2);
3. authz.nexloop_machine_admit: identity at clock_timestamp(), the rate policy in force (none = refused and counted, never unlimited, C4),
   per-key and per-tenant minute windows (429 with Retry-After), every outcome counted for the alert rules;
4. the route's own SQL port (the capability list: scope ∩ allowlist ∩ grants; the read verbs; the M5 writes - prepare, commit and the
   intent query through nexloop_eios.machine_writes, where the API signs the commit command and SQL re-runs every check).

Tenant, world and actor come only from the key (SQL). Request headers naming an identity are never read. A small process-wide
concurrency limit keeps machine calls from taking the workbench's connections (T-8). Responses carry error codes only, never a value of
the request, and Cache-Control: no-store.
"""
import asyncio
import json
import threading
import uuid

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from nexloop_eios import machine_keys as K

PREFIX = '/machine/v1'
CONCURRENCY = 8
BODY_LIMIT = 4096
WRITE_BODY_LIMIT = 16384  # a note of 2000 characters is up to 8000 bytes of UTF-8 plus its envelope
# ADR-041 §2.5 / T-5: identity is derived from the key only; a body naming one is refused (422), never read.
IDENTITY_FIELDS = frozenset({'tenant_id', 'world', 'world_id', 'actor', 'actor_id', 'principal_id', 'subject_id', 'on_behalf_of', 'client_id'})
_UNKNOWN_DIGEST = '0' * 64  # an unknown prefix is still admitted (and counted) as denied, with a digest no row holds


def _error(code, status, headers=None):
    return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()), 'retryable': status in (429, 503), 'details': {}},
                        status_code=status, headers={'Cache-Control': 'no-store', **(headers or {})})


def router(*, peppers_for, pool_for, signer_for=lambda request: None):
    """peppers_for(request) -> machine_keys.Peppers | None; pool_for(request) -> the API's business pool | None;
    signer_for(request) -> the API's authority signer | None (M5 commit only)."""
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

    def _identity_named(value):
        if isinstance(value, dict):
            return any(k in IDENTITY_FIELDS or _identity_named(v) for k, v in value.items())
        if isinstance(value, list):
            return any(_identity_named(v) for v in value)
        return False

    async def _body(request, limit):
        if request.url.query:
            raise ValueError()
        data = b''
        async with asyncio.timeout(5):
            async for part in request.stream():
                data += part
                if len(data) > limit:
                    raise ValueError()
        return json.loads(data)

    @routes.post('/read')
    async def read(request: Request):
        """NX-062 M4a: {"verb": <read verb>, "params": {...}} - the verb's SQL entry decides (authz.nexloop_machine_read)."""
        if request.url.query:
            return _error('query_not_allowed', 400)
        try:
            data = b''
            async with asyncio.timeout(5):
                async for part in request.stream():
                    data += part
                    if len(data) > BODY_LIMIT:
                        raise ValueError()
            body = json.loads(data)
            if type(body) is not dict or set(body) != {'verb', 'params'} or type(body['verb']) is not str or type(body['params']) is not dict:
                raise ValueError()
        except (ValueError, TimeoutError, UnicodeError):
            return _error('invalid_request', 422)
        if _identity_named(body):
            return _error('invalid_request', 422)
        return await run_in_threadpool(lambda: _read(request, body))

    def _read(request, body):
        if not gate.acquire(blocking=False):
            return _error('busy', 503, {'Retry-After': '1'})
        try:
            credential, refusal = admitted(request)
            if refusal is not None:
                return refusal
            try:
                with pool_for(request).connection() as db:
                    value = db.execute('select authz.nexloop_machine_read(%s,%s,%s,%s::jsonb)',
                                       (*credential, body['verb'], json.dumps(body['params']))).fetchone()[0]
            except psycopg.errors.NoDataFound:
                return _error('not_found', 404)
            except psycopg.errors.InvalidParameterValue:
                return _error('invalid_request', 422)
            except psycopg.errors.InsufficientPrivilege as refused:
                return _error('not_permitted' if 'not_permitted' in (refused.diag.message_primary or '') else 'unauthenticated',
                              403 if 'not_permitted' in (refused.diag.message_primary or '') else 401)
            except (psycopg.OperationalError, psycopg.InterfaceError, psycopg.errors.ObjectNotInPrerequisiteState):
                return _error('unavailable', 503)
            return JSONResponse(value, headers={'Cache-Control': 'no-store'})
        finally:
            gate.release()

    # ---- NX-062 M5: writes (design §4, R4, R5) ---------------------------------------------------------------------------------
    WRITE_KEYS = {'capability', 'intent_id', 'params'}

    def _write_shape(body, commit):
        keys = WRITE_KEYS | ({'prepare_digest'} if commit else set())
        return (type(body) is dict and set(body) == keys and type(body['capability']) is str and type(body['intent_id']) is str
                and type(body['params']) is dict and (not commit or type(body['prepare_digest']) is str) and not _identity_named(body['params']))

    def _write_error(error):
        message = getattr(error.diag, 'message_primary', None) or ''
        code = message.split(': ', 1)[1] if message.startswith('machine write: ') or message.startswith('object note: ') else None
        state = getattr(error, 'sqlstate', None)
        if state == 'P0002':
            return _error('not_found', 404)
        if state == '22023':
            return _error('invalid_request', 422)
        if state == 'NX409':
            return _error(code if code in ('intent_conflict', 'intent_committed', 'digest_mismatch') else 'conflict', 409)
        if state == 'NX410':
            return _error('prepare_expired', 410)
        if state == 'NX429':
            return _error(code if code in ('note_limit', 'note_limit_policy_missing') else 'limit', 429, {'Retry-After': '3600'})
        if state == '42501':
            if code in ('not_permitted', 'approval_policy_required'):
                return _error(code, 403)
            return _error('unauthenticated', 401)
        return _error('unavailable', 503)

    def _write(request, body, run):
        if not gate.acquire(blocking=False):
            return _error('busy', 503, {'Retry-After': '1'})
        try:
            credential, refusal = admitted(request)
            if refusal is not None:
                return refusal
            try:
                value, status = run(pool_for(request), *credential)
            except psycopg.Error as error:
                if isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError)):
                    return _error('unavailable', 503)
                return _write_error(error)
            return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})
        finally:
            gate.release()

    @routes.post('/prepare')
    async def prepare(request: Request):
        """NX-062 M5c: {"capability", "intent_id", "params"} - the SQL entry decides (authz.nexloop_machine_prepare, via machine_writes)."""
        from nexloop_eios import machine_writes as MW
        if request.url.query:
            return _error('query_not_allowed', 400)
        try:
            body = await _body(request, WRITE_BODY_LIMIT)
        except (ValueError, TimeoutError, UnicodeError):
            return _error('invalid_request', 422)
        if not _write_shape(body, False):
            return _error('invalid_request', 422)
        return await run_in_threadpool(lambda: _write(request, body, lambda pool, prefix, digest: (MW.prepare(
            pool, prefix=prefix, digest=digest, capability=body['capability'], intent_id=body['intent_id'], params=body['params']), 200)))

    @routes.post('/commit')
    async def commit(request: Request):
        """NX-062 M5c: the prepared write plus its prepare_digest; the API signs, the SQL entry re-runs every check
        (authz.nexloop_machine_commit, via machine_writes)."""
        from nexloop_eios import machine_writes as MW
        if request.url.query:
            return _error('query_not_allowed', 400)
        try:
            body = await _body(request, WRITE_BODY_LIMIT)
        except (ValueError, TimeoutError, UnicodeError):
            return _error('invalid_request', 422)
        if not _write_shape(body, True):
            return _error('invalid_request', 422)
        signer = signer_for(request)
        if signer is None:
            return _error('unavailable', 503)

        def run(pool, prefix, digest):
            value = MW.commit(pool, signer, prefix=prefix, digest=digest, capability=body['capability'], intent_id=body['intent_id'],
                              params=body['params'], prepare_digest=body['prepare_digest'])
            return value, 202 if value.get('phase') == 'pending_approval' else 200
        return await run_in_threadpool(lambda: _write(request, body, run))

    @routes.get('/intents/{intent_id}')
    def intent(request: Request, intent_id: str):
        """NX-062 M5c: one intent's state (authz.nexloop_machine_intent, via machine_writes)."""
        from nexloop_eios import machine_writes as MW
        if not 8 <= len(intent_id) <= 128 or not all(c.isalnum() and c.isascii() or c in '._:-' for c in intent_id):
            return _error('invalid_request', 422)
        return _write(request, None, lambda pool, prefix, digest: (MW.intent(pool, prefix=prefix, digest=digest, intent_id=intent_id), 200))

    return routes
