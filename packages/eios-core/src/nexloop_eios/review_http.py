"""NX-046 same-origin review workbench reads (ADR-019 §3.5).

Only an actual Human browser session holding current EXECUTE on the review
Action (ontology.schema.review) sees the queue; anyone else gets 403 and no
queue content. The three decisions are reported as not enabled until NX-044
provides the governed human review Actions: there is no decision endpoint.
"""
import re
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from eios.identity.errors import CredentialInvalid
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.sessions import BrowserSessionService
from nexloop_eios.browser_http import COOKIE

DECISIONS = {'enabled': False, 'reason': 'awaiting_nx044_review_actions', 'actions': ['approve', 'merge_into', 'reject']}
_CANDIDATE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def router(config, *, ports_for_browser):
    """ports_for_browser(request, actual_session) must recheck live authority."""
    if not callable(ports_for_browser):
        raise ValueError('governed browser ports required')
    routes = APIRouter(prefix='/api/v1/review')
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

    async def invoke(request, method, **arguments):
        try:
            value = await run_in_threadpool(lambda: getattr(ports(request), method)(**arguments))
            if value is None:
                return error('not_found', 404)
            return JSONResponse(value, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except PermissionError:
            return error('forbidden', 403)
        except Exception:
            return error('dependency_unavailable', 503)

    @routes.get('/queue')
    async def queue(request: Request, limit: int = 50):
        if not 1 <= limit <= 200:
            return error('invalid_request', 422)
        response = await invoke(request, 'review_queue', limit=limit)
        if response.status_code == 200:
            import json
            items = json.loads(response.body)
            return JSONResponse({'items': [{k: item[k] for k in ('candidate_id', 'kind', 'status', 'revision', 'candidate', 'merge_scores',
                'config_version', 'created_at', 'dependent_claim_count')} for item in items], 'decisions': DECISIONS},
                headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        return response

    @routes.get('/candidates/{candidate_id}')
    async def candidate(candidate_id: str, request: Request):
        if not _CANDIDATE.fullmatch(candidate_id):
            return error('invalid_request', 422)
        response = await invoke(request, 'review_candidate', candidate_id=candidate_id)
        if response.status_code == 200:
            import json
            return JSONResponse({**json.loads(response.body), 'decisions': DECISIONS},
                headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        return response
    return routes
