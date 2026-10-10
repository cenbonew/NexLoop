"""NX-028 slice 1: same-origin owner workbench reads, GET /api/v1/workbench/* (design §3, §9, §15.2).

Only an actual, unrestricted Human session of the workbench browser application (its own login realm and cookie) reaches
the ports; tenant, world and principal come from that session, never from the request. Every request re-authenticates in
PostgreSQL, so a revoked grant or membership is a 403 on the very next request. Status codes are exact (AT-045):
401 no/expired session, 403 no grant or not a member, 404 unknown object, 422 malformed request, 503 dependency unavailable.
"""
import re
import uuid
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from eios.identity.errors import CredentialInvalid
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.sessions import BrowserSessionService

from nexloop_eios.browser_http import WORKBENCH_COOKIE

_ID = re.compile(r'[a-f0-9]{64}')
_STATES = ('accepted', 'dispatching', 'unknown', 'fulfilled', 'failed', 'confirmed')
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


def router(config, *, ports_for_workbench):
    """ports_for_workbench(request, inspected_session) must re-authenticate in PostgreSQL."""
    if not callable(ports_for_workbench):
        raise ValueError('governed workbench ports required')
    routes = APIRouter(prefix='/api/v1/workbench')
    origin = urlsplit(config.origin)

    def error(code, status):
        return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()),
            'retryable': status == 503, 'details': {}}, status_code=status, headers={'Cache-Control': 'no-store'})

    def ports(request):
        if request.headers.get('host') != origin.netloc or request.headers.get('origin') not in (None, config.origin):
            raise PermissionError()
        store = getattr(request.app.state, 'workbench_store', None)
        if store is None:
            raise RuntimeError()
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        session = BrowserSessionService(store, store, application_id=config.application_id, operator=op).inspect(request.cookies.get(WORKBENCH_COOKIE, ''))
        if session.restricted:
            raise PermissionError()
        return ports_for_workbench(request, session)

    async def invoke(request, method, **arguments):
        from nexloop_eios.workbench_reads import WorkbenchForbidden
        try:
            value = await run_in_threadpool(lambda: getattr(ports(request), method)(**arguments))
            if value is None:
                return error('not_found', 404)
            return JSONResponse(value, headers=HEADERS)
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (WorkbenchForbidden, PermissionError, psycopg.errors.InsufficientPrivilege):
            return error('forbidden', 403)
        except (ValueError, psycopg.errors.InvalidParameterValue):
            return error('invalid_request', 422)
        except Exception:
            return error('dependency_unavailable', 503)

    def limit_of(raw):
        if not re.fullmatch(r'[1-9][0-9]{0,2}', raw) or not 1 <= int(raw) <= 200:
            raise ValueError()
        return int(raw)

    def flag(raw):
        if raw not in ('true', 'false'):
            raise ValueError()
        return raw == 'true'

    def query(request, allowed):
        if set(request.query_params) - set(allowed) or any(len(request.query_params.getlist(k)) > 1 for k in request.query_params):
            raise ValueError()
        return request.query_params

    @routes.get('/overview')
    async def overview(request: Request):
        try:
            query(request, ())
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'overview')

    @routes.get('/goals')
    async def goals(request: Request):
        try:
            q = query(request, ('limit',))
            arguments = {'limit': limit_of(q['limit'])} if 'limit' in q else {}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'goals', **arguments)

    @routes.get('/consumers')
    async def consumers(request: Request):
        try:
            q = query(request, ('after', 'limit'))
            if 'after' in q and not _ID.fullmatch(q['after']):
                raise ValueError()
            arguments = {'after': q.get('after'), 'limit': limit_of(q['limit']) if 'limit' in q else 50}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'consumers', **arguments)

    @routes.get('/consumers/{consumer_id}')
    async def consumer(consumer_id: str, request: Request):
        if not _ID.fullmatch(consumer_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'consumer', consumer_id=consumer_id)

    @routes.get('/conversations/{conversation_id}')
    async def conversation(conversation_id: str, request: Request):
        if not _ID.fullmatch(conversation_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'conversation', conversation_id=conversation_id)

    @routes.get('/plans')
    async def plans(request: Request):
        try:
            q = query(request, ('consumer_id',))
            if 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']):
                raise ValueError()
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'plans', consumer_id=q.get('consumer_id'))

    @routes.get('/actions')
    async def actions(request: Request):
        try:
            q = query(request, ('state', 'consumer_id', 'limit'))
            if 'state' in q and q['state'] not in _STATES or 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']):
                raise ValueError()
            arguments = {'state': q.get('state'), 'consumer_id': q.get('consumer_id'), 'limit': limit_of(q['limit']) if 'limit' in q else 50}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'actions', **arguments)

    @routes.get('/commitments')
    async def commitments(request: Request):
        try:
            q = query(request, ('consumer_id', 'all'))
            if 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']):
                raise ValueError()
            arguments = {'consumer_id': q.get('consumer_id'), 'include_closed': flag(q['all']) if 'all' in q else False}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'commitments', **arguments)

    @routes.get('/commitments/{commitment_id}')
    async def commitment(commitment_id: str, request: Request):
        if not _ID.fullmatch(commitment_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'commitment', commitment_id=commitment_id)

    @routes.get('/contact')
    async def contact(request: Request):
        try:
            q = query(request, ('consumer_id', 'all'))
            if 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']):
                raise ValueError()
            arguments = {'consumer_id': q.get('consumer_id'), 'include_released': flag(q['all']) if 'all' in q else False}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'contact', **arguments)

    @routes.get('/takeovers')
    async def takeovers(request: Request):
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'takeovers')

    @routes.get('/settings')
    async def settings(request: Request):
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'settings')

    @routes.get('/audit')
    async def audit(request: Request):
        # ADR-025 §2.3: the member-read audit, owner only (SQL refuses everyone else with 403).
        try:
            q = query(request, ('limit', 'before'))
            if 'before' in q and not re.fullmatch(r'[1-9][0-9]{0,18}', q['before']):
                raise ValueError()
            limit = limit_of(q['limit']) if 'limit' in q else 100
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'audit', limit=limit, before=q.get('before'))

    return routes
