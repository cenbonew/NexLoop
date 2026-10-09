"""NX-046 same-origin review workbench reads (ADR-019 §3.5).

Only an actual Human browser session holding current EXECUTE on the review
Action (ontology.schema.review) sees the queue; anyone else gets 403 and no
queue content. Decisions (NX-044) are enabled only when the wired ports provide
the governed human review Action; otherwise they are reported as not enabled.
"""
import asyncio
import json
import re
import uuid

import psycopg
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from eios.identity.errors import CredentialInvalid
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.sessions import BrowserSessionService
from nexloop_eios.browser_http import COOKIE

DECISIONS = {'enabled': False, 'reason': 'awaiting_nx044_review_actions', 'actions': ['approve', 'merge_into', 'reject']}
ENABLED = {'enabled': True, 'reason': 'governed_human_review_action', 'actions': ['approve', 'merge_into', 'reject']}
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

    def ports(request, *, write=False):
        if request.headers.get('host') != origin.netloc or request.headers.get('origin') not in ((config.origin,) if write else (None, config.origin)):
            raise PermissionError()
        store = getattr(request.app.state, 'browser_store', None)
        if store is None:
            raise RuntimeError()
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        sessions = BrowserSessionService(store, store, application_id=config.application_id, operator=op)
        session = sessions.inspect(request.cookies.get(COOKIE, ''))
        if write:
            sessions.verify_csrf(session, request.headers.get('x-csrf-token', ''))
        if session.restricted:
            raise PermissionError()
        return ports_for_browser(request, session)

    async def invoke(request, method, *, write=False, **arguments):
        try:
            def work():
                bound = ports(request, write=write)
                if not callable(getattr(bound, method, None)):
                    raise NotImplementedError()
                return getattr(bound, method)(**arguments), ENABLED if callable(getattr(bound, 'decide', None)) else DECISIONS
            value, decisions = await run_in_threadpool(work)
            if value is None:
                return error('not_found', 404)
            response = JSONResponse(value, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
            response.decisions = decisions
            return response
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (PermissionError, psycopg.errors.InsufficientPrivilege):
            return error('forbidden', 403)
        except NotImplementedError:
            return error('decisions_not_enabled', 501)
        except psycopg.errors.SerializationFailure:
            return error('candidate_changed', 409)
        except LookupError:
            return error('not_found', 404)
        except (ValueError, psycopg.errors.InvalidParameterValue):
            return error('invalid_request', 422)
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
                'config_version', 'created_at', 'dependent_claim_count')} for item in items], 'decisions': response.decisions},
                headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        return response

    @routes.get('/candidates/{candidate_id}')
    async def candidate(candidate_id: str, request: Request):
        if not _CANDIDATE.fullmatch(candidate_id):
            return error('invalid_request', 422)
        response = await invoke(request, 'review_candidate', candidate_id=candidate_id)
        if response.status_code == 200:
            import json
            return JSONResponse({**json.loads(response.body), 'decisions': response.decisions},
                headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        return response

    @routes.get('/awaiting')
    async def awaiting(request: Request):
        # Decided items whose dependent Claims still wait (reflow pending or grants missing).
        response = await invoke(request, 'review_awaiting')
        if response.status_code == 200:
            return JSONResponse({'items': json.loads(response.body)}, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        return response

    @routes.post('/candidates/{candidate_id}/decisions')
    async def decide(candidate_id: str, request: Request):
        try:
            if not _CANDIDATE.fullmatch(candidate_id) or request.headers.get('content-type', '').split(';')[0] != 'application/json':
                raise ValueError()
            key = request.headers.get('idempotency-key', '')
            if not re.fullmatch(r'[A-Za-z0-9._~-]{16,160}', key):
                raise ValueError()
            data = b''
            async with asyncio.timeout(5):
                async for part in request.stream():
                    data += part
                    if len(data) > 8192:
                        raise ValueError()
            def pairs(items):
                result = {}
                for name, value in items:
                    if name in result:
                        raise ValueError()
                    result[name] = value
                return result
            body = json.loads(data, object_pairs_hook=pairs)
            allowed = {'decision', 'expected_revision', 'rationale', 'merge_target_ref'}
            if (type(body) is not dict or not {'decision', 'expected_revision', 'rationale'} <= set(body) <= allowed
                    or body['decision'] not in ('approve', 'merge_into', 'reject') or type(body['expected_revision']) is not int
                    or type(body['rationale']) is not str or ('merge_target_ref' in body and type(body['merge_target_ref']) is not str)):
                raise ValueError()
        except TimeoutError:
            return error('request_timeout', 408)
        except (ValueError, UnicodeError):
            return error('invalid_request', 422)
        return await invoke(request, 'decide', write=True, candidate_id=candidate_id, decision=body['decision'],
            expected_revision=body['expected_revision'], rationale=body['rationale'], idempotency_key=key,
            merge_target_ref=body.get('merge_target_ref'))
    return routes
