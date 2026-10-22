"""NX-040 F3/F4 Schema publication in the workbench login realm (dispatcher 2026-10-21, S6 closure: the publication had no operable
entry - only the backend's SchemaPublicationPort, reachable with a browser signer).

The routes call the existing SchemaPublicationPort and nothing else: no second judgement, no direct SQL. The port runs for the member's
actual Human workbench session and SQL decides on every call: the Action ontology.schema.review (the F3 role list: reviewer and owner;
the operator holds it not, so 403), the caller's own tenant (another tenant's proposal is unknown, 404), and for a publication the
candidate's release permit from an accepted nx040-gates-1 report (F4: G9-G13; permit_missing / permit_used / permit_expired /
permit_mismatch otherwise), the F1 verdict, the successor Actions and the permission proof, all re-derived in SQL.

    GET  /api/v1/workbench/schema-proposals/{proposal_id}/basis      the publication basis (proposal, Parent version, Actions, counts)
    GET  /api/v1/workbench/schema-proposals/{proposal_id}/check      what a publication would be judged: the F1 report and the next version
                                                                     (built by the port's own build(); nothing written), and the
                                                                     published Definitions it may affect (A1, schema_impact)
    POST /api/v1/workbench/schema-proposals/{proposal_id}/publish    {rationale, batch?} + Idempotency-Key, CSRF
    POST /api/v1/workbench/schema-proposals/{proposal_id}/hide       {rationale} (the rollback: the next version, the property hidden)
    POST /api/v1/workbench/schema-publications/{publication_id}/migrate-batch   the next 10 000 objects of a batched publication
"""
from __future__ import annotations

import asyncio
import json
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

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_KEY = re.compile(r'[A-Za-z0-9._~-]{16,160}')
NO_STORE = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


class SchemaPublicationPorts:
    """One inspected workbench session: re-authenticated on every call, then the existing SchemaPublicationPort."""

    def __init__(self, backend, inspected_session):
        self.backend, self.inspected_session = backend, inspected_session

    def _port(self):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.schema_publication import SchemaPublicationPort
        self.backend.authenticate_browser_reviewer(self.inspected_session)  # backend open, current Human authentication
        human = authenticate_browser_business(self.backend._pool, self.inspected_session, world='real')
        return SchemaPublicationPort(self.backend._pool, human, self.backend._signer)

    def _run(self, work):
        from nexloop_eios.authorization import authority_request_scope
        with authority_request_scope():
            return work(self._port())

    def basis(self, proposal_id):
        return self._run(lambda p: p.basis(proposal_id))

    def check(self, proposal_id):
        from nexloop_eios.schema_impact import impact
        from nexloop_eios.schema_publication import build

        def work(p):
            basis = p.basis(proposal_id)
            if basis is None:
                return None
            publication, report = build(basis, 'publish')
            return {'proposal_id': proposal_id, 'operation': basis.get('operation'), 'type_name': basis.get('type_name'),
                    'parent_version': (basis.get('schema') or {}).get('version'), 'next_version': publication['schema'].get('version'),
                    'successor_actions': len(publication['actions']), 'compatibility': report, 'impact': impact(p.dependents(proposal_id))}
        return self._run(work)

    def publish(self, proposal_id, *, rationale, idempotency_key, batch=False):
        return self._run(lambda p: p.publish(proposal_id, rationale=rationale, idempotency_key=idempotency_key, batch=batch))

    def hide(self, proposal_id, *, rationale, idempotency_key):
        return self._run(lambda p: p.hide(proposal_id, rationale=rationale, idempotency_key=idempotency_key))

    def migrate_batch(self, publication_id):
        return self._run(lambda p: p.migrate_batch(publication_id))


def router(config, *, ports_for_browser, cookie, store_attribute='workbench_store', prefix='/api/v1/workbench'):
    if not callable(ports_for_browser):
        raise ValueError('governed browser ports required')
    routes = APIRouter(prefix=prefix)
    origin = urlsplit(config.origin)

    def error(code, status):
        return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()), 'retryable': status == 503, 'details': {}},
                            status_code=status, headers={'Cache-Control': 'no-store'})

    def ports(request, *, write):
        if request.headers.get('host') != origin.netloc or request.headers.get('origin') not in ((config.origin,) if write else (None, config.origin)):
            raise PermissionError()
        store = getattr(request.app.state, store_attribute, None)
        if store is None:
            raise RuntimeError()
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        sessions = BrowserSessionService(store, store, application_id=config.application_id, operator=op)
        session = sessions.inspect(request.cookies.get(cookie, ''))
        if write:
            sessions.verify_csrf(session, request.headers.get('x-csrf-token', ''))
        if session.restricted:
            raise PermissionError()
        return ports_for_browser(request, session)

    async def invoke(request, method, *, write=False, **arguments):
        try:
            value = await run_in_threadpool(lambda: getattr(ports(request, write=write), method)(**arguments))
            if value is None:
                return error('not_found', 404)
            return JSONResponse(json.loads(json.dumps(value, default=str)), headers=NO_STORE)
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (PermissionError, psycopg.errors.InsufficientPrivilege):
            return error('forbidden', 403)
        except LookupError:
            return error('not_found', 404)
        except psycopg.errors.SerializationFailure:
            return error('conflict', 409)
        except (ValueError, psycopg.errors.InvalidParameterValue):
            return error('invalid_request', 422)
        except Exception as failure:
            if type(failure).__name__ in ('ActionAuthorizationDenied', 'AuthorizationFactDenied'):
                return error('forbidden', 403)
            return error('unavailable', 503)

    async def body_of(request, allowed, required):
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise ValueError()
        data = b''
        async with asyncio.timeout(5):
            async for part in request.stream():
                data += part
                if len(data) > 4096:
                    raise ValueError()

        def pairs(items):
            out = {}
            for k, v in items:
                if k in out:
                    raise ValueError()
                out[k] = v
            return out
        body = json.loads(data, object_pairs_hook=pairs)
        if type(body) is not dict or not set(required) <= set(body) <= set(allowed):
            raise ValueError()
        if type(body.get('rationale')) is not str or not 1 <= len(body['rationale'].strip()) <= 2000:
            raise ValueError()
        if 'batch' in body and type(body['batch']) is not bool:
            raise ValueError()
        return body

    @routes.get('/schema-proposals/{proposal_id}/basis')
    async def basis(proposal_id: str, request: Request):
        if not _UUID.fullmatch(proposal_id):
            return error('invalid_request', 422)
        return await invoke(request, 'basis', proposal_id=proposal_id)

    @routes.get('/schema-proposals/{proposal_id}/check')
    async def check(proposal_id: str, request: Request):
        if not _UUID.fullmatch(proposal_id):
            return error('invalid_request', 422)
        return await invoke(request, 'check', proposal_id=proposal_id)

    async def write(request, proposal_id, method, allowed):
        try:
            key = request.headers.get('idempotency-key', '')
            if not _UUID.fullmatch(proposal_id) or not _KEY.fullmatch(key):
                raise ValueError()
            body = await body_of(request, allowed, ('rationale',))
        except TimeoutError:
            return error('request_timeout', 408)
        except (ValueError, UnicodeError):
            return error('invalid_request', 422)
        return await invoke(request, method, write=True, proposal_id=proposal_id, idempotency_key=key, **body)

    @routes.post('/schema-proposals/{proposal_id}/publish')
    async def publish(proposal_id: str, request: Request):
        return await write(request, proposal_id, 'publish', ('rationale', 'batch'))

    @routes.post('/schema-proposals/{proposal_id}/hide')
    async def hide(proposal_id: str, request: Request):
        return await write(request, proposal_id, 'hide', ('rationale',))

    @routes.post('/schema-publications/{publication_id}/migrate-batch')
    async def migrate_batch(publication_id: str, request: Request):
        if not _UUID.fullmatch(publication_id):
            return error('invalid_request', 422)
        return await invoke(request, 'migrate_batch', write=True, publication_id=publication_id)
    return routes
