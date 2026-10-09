"""Same-origin human Manifest read (owner decision: humans only; nexloop.context.audit:1).

Only an actual, unrestricted Human browser session holding current EXECUTE on the audit
Action receives a Manifest; SQL (0094) refuses services, Agents and Run credentials before
any grant is evaluated. Everyone else gets 403 and no content. Read-only: GET only.
"""
from dataclasses import dataclass
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
from nexloop_eios.browser_http import COOKIE

_RUN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_DENIED = ('ContextDenied', 'ActionAuthorizationDenied', 'AuthorizationUnavailable', 'AuthorizationFactDenied')


@dataclass(frozen=True)
class ContextAuditPorts:
    """Wiring for http_api; the reader runs on the human's own authenticated session."""
    backend: object
    inspected_session: object

    def manifest(self, *, run_id, call_sequence):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.context_engine.audit import ContextAuditReader
        pool, signer = self.backend._pool, self.backend._signer
        human = authenticate_browser_business(pool, self.inspected_session, world='real')
        return ContextAuditReader(pool, human, signer).manifest(run_id, call_sequence)


def router(config, *, ports_for_browser):
    """ports_for_browser(request, actual_session) must recheck live authority."""
    if not callable(ports_for_browser):
        raise ValueError('governed browser ports required')
    routes = APIRouter(prefix='/api/v1/context')
    origin = urlsplit(config.origin)

    def error(code, status):
        return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()),
            'retryable': status == 503, 'details': {}}, status_code=status, headers={'Cache-Control': 'no-store'})

    def ports(request):
        if request.headers.get('host') != origin.netloc or request.headers.get('origin') not in (None, config.origin):
            raise PermissionError()
        store = getattr(request.app.state, 'browser_store', None)
        if store is None:
            raise RuntimeError()
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        session = BrowserSessionService(store, store, application_id=config.application_id, operator=op).inspect(request.cookies.get(COOKIE, ''))
        if session.restricted:
            raise PermissionError()
        return ports_for_browser(request, session)

    @routes.get('/manifests/{run_id}/{call_sequence}')
    async def manifest(run_id: str, call_sequence: str, request: Request):
        if not _RUN.fullmatch(run_id) or not re.fullmatch(r'[1-9][0-9]{0,4}', call_sequence):
            return error('invalid_request', 422)
        try:
            value = await run_in_threadpool(lambda: ports(request).manifest(run_id=run_id, call_sequence=int(call_sequence)))
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (PermissionError, psycopg.errors.InsufficientPrivilege):
            return error('forbidden', 403)
        except Exception as failure:
            if type(failure).__name__ in _DENIED:
                return error('forbidden', 403)
            return error('dependency_unavailable', 503)
        if value is None:
            return error('not_found', 404)
        return JSONResponse(value, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    return routes
