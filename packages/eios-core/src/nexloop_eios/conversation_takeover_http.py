"""NX-028 D4: the customer sees whether a person is handling the conversation ("人工客服处理中"), nothing more.

``GET /api/v1/conversations/{conversation_id}/handling`` for the conversation's own browser Human only (SQL checks the
owner, 0152): ``{"conversation_id", "handled_by": "agent" | "human"}``. Who took it over, why and until when stay internal.
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
from nexloop_eios.browser_http import COOKIE


def router(config,*,backend_for):
    routes=APIRouter(prefix='/api/v1/conversations')
    origin=urlsplit(config.origin)

    def error(code,status):
        return JSONResponse({'code':code,'message':'请求未完成','trace_id':str(uuid.uuid4()),'retryable':status==503,'details':{}},
            status_code=status,headers={'Cache-Control':'no-store'})

    def state(request,conversation_id):
        from nexloop_eios.authorization import authority_request_scope
        from nexloop_eios.browser_authorization import authenticate_browser_business
        if request.headers.get('host')!=origin.netloc or request.headers.get('origin') not in (None,config.origin):raise PermissionError()
        store=getattr(request.app.state,'browser_store',None)
        if store is None:raise RuntimeError()
        op=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id=str(uuid.uuid4()),trace_id=str(uuid.uuid4()))
        session=BrowserSessionService(store,store,application_id=config.application_id,operator=op).inspect(request.cookies.get(COOKIE,''))
        backend=backend_for(request)
        backend.authenticate_browser(session)  # open backend + current Human authentication
        with authority_request_scope():
            human=authenticate_browser_business(backend._pool,session,world='real')
            with backend._pool.connection() as db:
                return db.execute('select authz.nexloop_conversation_takeover_state(%s,%s,%s)',(human.token_digest,human.world,conversation_id)).fetchone()[0]

    @routes.get('/{conversation_id}/handling')
    async def handling(conversation_id:str,request:Request):
        if not re.fullmatch(r'[0-9a-f]{64}',conversation_id):return error('invalid_request',422)
        try:
            value=await run_in_threadpool(lambda:state(request,conversation_id))
            return JSONResponse(value,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except CredentialInvalid:return error('unauthenticated',401)
        except (PermissionError,psycopg.errors.InsufficientPrivilege):return error('forbidden',403)
        except Exception:return error('dependency_unavailable',503)
    return routes
