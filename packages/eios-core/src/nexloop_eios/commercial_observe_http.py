"""NX-027 same-origin human reads (D09): GET /commercial-observations, /costs, /metrics (CommercialRecord.observe:1).

Only an actual, unrestricted Human browser session holding current EXECUTE on the observe Action receives content;
SQL (0134) refuses services, Agents and Run credentials before any grant is evaluated. Everyone else gets 401/403 and
no content. Read-only: GET only. Every answer names its world and each item its data_mode (D10:7).
"""
import re
import uuid
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from eios.identity.errors import CredentialInvalid

_HEX = re.compile(r'[0-9a-f]{64}')
_RUN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_SLUG = re.compile(r'[a-z0-9][a-z0-9._-]{0,127}')
_KINDS = ('model', 'channel', 'discount', 'service', 'labour')
_DENIED = ('ContextDenied', 'ActionAuthorizationDenied', 'AuthorizationUnavailable', 'AuthorizationFactDenied')


class ObservePorts:
    """Wiring for http_api; the observer runs on the human's own authenticated session."""

    def __init__(self, backend, inspected_session):
        self.backend, self.inspected_session = backend, inspected_session

    def observer(self):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.commercial import CommercialObserver
        pool, signer = self.backend._pool, self.backend._signer
        return CommercialObserver(pool, authenticate_browser_business(pool, self.inspected_session, world='real'), signer)


def _inspect(config, request):
    from eios.identity.ports import TrustedIdentityOperator
    from eios.identity.sessions import BrowserSessionService
    from nexloop_eios.browser_http import COOKIE
    store = getattr(request.app.state, 'browser_store', None)
    if store is None:
        raise RuntimeError()
    op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
    return BrowserSessionService(store, store, application_id=config.application_id, operator=op).inspect(request.cookies.get(COOKIE, ''))


def router(config, *, ports_for_browser, inspect=_inspect):
    """ports_for_browser(request, actual_session) must recheck live authority; ``inspect`` reads the browser session."""
    if not callable(ports_for_browser):
        raise ValueError('governed browser ports required')
    routes = APIRouter(prefix='/api/v1')
    origin = urlsplit(config.origin)

    def error(code, status):
        return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()),
            'retryable': status == 503, 'details': {}}, status_code=status, headers={'Cache-Control': 'no-store'})

    def observer(request):
        if request.headers.get('host') != origin.netloc or request.headers.get('origin') not in (None, config.origin):
            raise PermissionError()
        session = inspect(config, request)
        if session.restricted:
            raise PermissionError()
        return ports_for_browser(request, session).observer()

    async def answer(request, read):
        try:
            value = await run_in_threadpool(lambda: read(observer(request)))
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (PermissionError, psycopg.errors.InsufficientPrivilege):
            return error('forbidden', 403)
        except psycopg.errors.InvalidParameterValue:
            return error('invalid_request', 422)
        except Exception as failure:
            if type(failure).__name__ in _DENIED:
                return error('forbidden', 403)
            return error('dependency_unavailable', 503)
        return JSONResponse(value, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @routes.get('/commercial-observations')
    async def records(request: Request, consumer_id: str | None = None):
        if consumer_id is not None and not _HEX.fullmatch(consumer_id):
            return error('invalid_request', 422)
        return await answer(request, lambda o: o.records(consumer_id))

    @routes.get('/commercial-observations/{record_id}')
    async def record(record_id: str, request: Request):
        if not _HEX.fullmatch(record_id):
            return error('invalid_request', 422)
        return await answer(request, lambda o: o.record(record_id))

    @routes.get('/costs')
    async def costs(request: Request):
        return await answer(request, lambda o: o.costs())

    @routes.get('/costs/entries')
    async def entries(request: Request, run_id: str | None = None, cost_kind: str | None = None):
        if (run_id is not None and not _RUN.fullmatch(run_id)) or (cost_kind is not None and cost_kind not in _KINDS):
            return error('invalid_request', 422)
        return await answer(request, lambda o: o.cost_entries(run_id=run_id, cost_kind=cost_kind))

    @routes.get('/metrics/{goal_id}/{goal_version}/{kr_key}')
    async def metric(goal_id: str, goal_version: str, kr_key: str, request: Request):
        if not _SLUG.fullmatch(goal_id) or not re.fullmatch(r'[1-9][0-9]{0,8}', goal_version) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', kr_key):
            return error('invalid_request', 422)
        return await answer(request, lambda o: o.metric(goal_id, int(goal_version), kr_key))
    return routes
