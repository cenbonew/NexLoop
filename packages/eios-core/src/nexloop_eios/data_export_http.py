"""NX-029 slice 3: same-origin download of a requested Consumer export (GET /api/v1/workbench/data-exports/{export_id}).

Only the actual, unrestricted workbench member session that requested the export receives it, assembled with that
session's own governed reads (data_export.DataExporter, ADR-025 member reads); everyone else gets 401/403/404 and no
content. GET only.
"""
import json
import re
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from eios.identity.errors import CredentialInvalid

_EXPORT = re.compile(r'[A-Za-z0-9._:-]{1,190}')


def _inspect(config, request):
    """The workbench member session (own cookie and session store, as workbench_http)."""
    from eios.identity.ports import TrustedIdentityOperator
    from eios.identity.sessions import BrowserSessionService
    from nexloop_eios.browser_http import WORKBENCH_COOKIE
    store = getattr(request.app.state, 'workbench_store', None)
    if store is None:
        raise RuntimeError()
    op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
    return BrowserSessionService(store, store, application_id=config.application_id, operator=op).inspect(request.cookies.get(WORKBENCH_COOKIE, ''))


class ExportPorts:
    def __init__(self, backend, inspected_session):
        self.backend, self.inspected_session = backend, inspected_session

    def exporter(self):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.data_export import DataExporter
        pool, signer = self.backend._pool, self.backend._signer
        return DataExporter(pool, authenticate_browser_business(pool, self.inspected_session, world='real'), signer)


def router(config, *, ports_for_browser, inspect=_inspect):
    if not callable(ports_for_browser):
        raise ValueError('governed browser ports required')
    routes = APIRouter(prefix='/api/v1/workbench')
    origin = urlsplit(config.origin)

    def error(code, status):
        return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()), 'retryable': status == 503, 'details': {}},
            status_code=status, headers={'Cache-Control': 'no-store'})

    @routes.get('/data-exports/{export_id}')
    async def download(export_id: str, request: Request):
        from nexloop_eios.data_export import ExportUnavailable
        if not _EXPORT.fullmatch(export_id):
            return error('invalid_request', 422)
        if request.headers.get('host') != origin.netloc or request.headers.get('origin') not in (None, config.origin):
            return error('forbidden', 403)
        try:
            session = inspect(config, request)
            if session.restricted:
                return error('forbidden', 403)
            value = await run_in_threadpool(lambda: ports_for_browser(request, session).exporter().export(export_id))
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (ExportUnavailable, PermissionError):
            return error('not_found', 404)
        except Exception:
            return error('dependency_unavailable', 503)
        return Response(json.dumps(value, ensure_ascii=False, separators=(',', ':')), media_type='application/json',
            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                     'Content-Disposition': f'attachment; filename="consumer-export-{export_id}.json"'})
    return routes
