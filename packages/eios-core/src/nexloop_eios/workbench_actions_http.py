"""NX-028 slice 2: the owner workbench's write entry (same-origin, real Human browser session).

``POST /api/v1/workbench/actions/{operation}`` turns one form submission into the governed human Action registered for
that operation (0150 capability registry): pause / resume a scope, release a contact restriction (ADR-023), the five
human commitment Actions (NX-026), a manual plan reevaluation and a query of an unknown effect result (D7). The
browser names an operation, never an Action or a tenant: tenant, world and principal come from the inspected session;
the tenant's Action for the capability is looked up in SQL (0151); the governed entry re-checks EXECUTE, the human
subject, the registry and every fence. The Idempotency-Key header is the governed request id (replays are answered from
the terminal outcome). Fixed error codes: unauthenticated 401, forbidden 403, not_found 404 (no Action published for it),
invalid_request 422, not_allowed_in_state 409, conflict 409, unavailable 503 (authority or a dependency cannot be verified).
"""
import asyncio
from datetime import datetime,timedelta
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
from nexloop_eios.browser_http import COOKIE
from nexloop_eios.goal_controls import CAPABILITIES

_HEX=re.compile(r'[0-9a-f]{64}')
_UUID=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_KEY=re.compile(r'[A-Za-z0-9._~-]{16,160}')


def _reason(value):
    if type(value) is not str or not 1<=len(value)<=500:raise ValueError('reason')
    return value


def _utc(value):
    if type(value) is not str:raise ValueError('time')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset()!=timedelta(0):raise ValueError('UTC time required')
    return parsed


def _id(pattern):
    def check(value):
        if type(value) is not str or not pattern.fullmatch(value):raise ValueError('identifier')
        return value
    return check


def _bool(value):
    if type(value) is not bool:raise ValueError('boolean')
    return value


def _scope(value):
    if value not in ('tenant','role','consumer','strategy','action_type'):raise ValueError('scope')
    return value


def _text(value):
    if type(value) is not str or not 1<=len(value)<=255:raise ValueError('text')
    return value


# operation → its fields (each with a validator); every operation is a registered human capability (0150).
OPERATIONS={
    'set_control':{'scope_kind':_scope,'scope_ref':_text,'paused':_bool,'reason':_reason},
    'release_contact_restriction':{'consumer_id':_id(_HEX),'reason':_reason},
    'cancel_commitment':{'commitment_id':_id(_HEX),'reason':_reason},
    'extend_commitment':{'commitment_id':_id(_HEX),'due_at':_utc,'reason':_reason},
    'attest_commitment':{'commitment_id':_id(_HEX),'occurred_at':_utc,'reason':_reason},
    'commitment_condition_met':{'commitment_id':_id(_HEX),'reason':_reason},
    'mark_commitment_communication':{'commitment_id':_id(_HEX),'reason':_reason},
    'request_plan_reevaluation':{'plan_id':_id(_UUID),'reason':_reason},
    'request_effect_query':{'intent_id':_id(_UUID),'reason':_reason},
    # Slice 3 (D3): the duration is optional in the governed Action; the page always sends it (default from policy).
    'take_over_conversation':{'scope_kind':lambda v:v if v in ('conversation','consumer') else (_ for _ in ()).throw(ValueError('scope')),
        'scope_ref':_id(_HEX),'duration_seconds':lambda v:v if type(v) is int and 60<=v<=604800 else (_ for _ in ()).throw(ValueError('duration')),'reason':_reason},
    'hand_back_conversation':{'takeover_id':_id(_UUID),'reason':_reason},
}


class WorkbenchActions:
    """Governed human writes for one inspected browser session (re-authenticated on every call)."""

    def __init__(self,backend,inspected_session):
        self.backend,self.inspected_session=backend,inspected_session

    def run(self,operation,request_id,fields):
        from nexloop_eios.authorization import authority_request_scope
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.goal_controls import GoalGovernedActions
        self.backend.authenticate_browser_reviewer(self.inspected_session)  # open backend + current Human authentication
        with authority_request_scope():
            pool=self.backend._pool;human=authenticate_browser_business(pool,self.inspected_session,world='real')
            with pool.connection() as db:
                action=db.execute('select authz.nexloop_workbench_action_for(%s,%s,%s)',(human.token_digest,human.world,CAPABILITIES[operation])).fetchone()[0]
            if action is None:raise LookupError('Action not published')
            if action.get('granted') is not True:raise PermissionError('no grant on the Action')
            method=getattr(GoalGovernedActions(pool,human,self.backend._signer),operation)
            return method(action_name=action['stable_name'],action_version=action['version'],request_id=request_id,**fields)


def router(config,*,ports_for_browser):
    """ports_for_browser(request, inspected_session) → WorkbenchActions."""
    if not callable(ports_for_browser):raise ValueError('governed browser ports required')
    routes=APIRouter(prefix='/api/v1/workbench/actions')
    origin=urlsplit(config.origin)

    def error(code,status):
        return JSONResponse({'code':code,'message':'请求未完成','trace_id':str(uuid.uuid4()),'retryable':status==503,'details':{}},
            status_code=status,headers={'Cache-Control':'no-store'})

    def bound(request):
        if request.headers.get('host')!=origin.netloc or request.headers.get('origin')!=config.origin:raise PermissionError()
        store=getattr(request.app.state,'browser_store',None)
        if store is None:raise RuntimeError()
        op=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id=str(uuid.uuid4()),trace_id=str(uuid.uuid4()))
        sessions=BrowserSessionService(store,store,application_id=config.application_id,operator=op)
        session=sessions.inspect(request.cookies.get(COOKIE,''))
        sessions.verify_csrf(session,request.headers.get('x-csrf-token',''))
        if session.restricted:raise PermissionError()
        return ports_for_browser(request,session)

    @routes.post('/{operation}')
    async def act(operation:str,request:Request):
        try:
            if operation not in OPERATIONS or request.headers.get('content-type','').split(';')[0]!='application/json':raise ValueError()
            key=request.headers.get('idempotency-key','')
            if not _KEY.fullmatch(key):raise ValueError()
            data=b''
            async with asyncio.timeout(5):
                async for part in request.stream():
                    data+=part
                    if len(data)>8192:raise ValueError()
            def pairs(items):
                result={}
                for name,value in items:
                    if name in result:raise ValueError()
                    result[name]=value
                return result
            body=json.loads(data,object_pairs_hook=pairs)
            spec=OPERATIONS[operation]
            if type(body) is not dict or set(body)!=set(spec):raise ValueError()
            fields={name:check(body[name]) for name,check in spec.items()}
        except TimeoutError:
            return error('request_timeout',408)
        except (ValueError,UnicodeError,TypeError):
            return error('invalid_request',422)
        try:
            result=await run_in_threadpool(lambda:bound(request).run(operation,'workbench-'+key,fields))
            return JSONResponse(json.loads(json.dumps(result,default=str)),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except CredentialInvalid:
            return error('unauthenticated',401)
        except LookupError:
            return error('not_found',404)
        except psycopg.errors.InvalidParameterValue:
            return error('not_allowed_in_state',409)  # the handler refused this transition (state, target, reason)
        except psycopg.errors.SerializationFailure:
            return error('conflict',409)
        except (PermissionError,psycopg.errors.InsufficientPrivilege):
            return error('forbidden',403)
        except Exception as failure:
            if type(failure).__name__ in ('ActionAuthorizationDenied','AuthorizationFactDenied'):return error('forbidden',403)
            # AuthorizationUnavailable and dependencies: cannot be verified now (same label as NX-046), never a silent success.
            return error('unavailable',503)
    return routes
