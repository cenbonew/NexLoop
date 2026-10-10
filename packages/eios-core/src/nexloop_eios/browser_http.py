"""Same-origin Human browser authentication; server-owned realm, private PG."""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import uuid
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse
from eios.identity.local_accounts import LocalAccountService
from eios.identity.evidence import AuthenticationEvidenceAuthority
from eios.identity.sessions import BrowserSessionService
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.errors import CredentialInvalid
from nexloop_eios.browser_rate_limits import PostgresBrowserRateLimiter,client_digest

COOKIE='__Host-nexloop_session'
# NX-028: the owner workbench is its own browser application with its own login realm and cookie (D1).
WORKBENCH_COOKIE='__Host-nexloop_workbench'

@dataclass(frozen=True)
class BrowserConfiguration:
    identity_database_url_file:Path
    rate_key_file:Path
    tenant_id:str
    application_id:str
    origin:str
    def __post_init__(self):
        u=urlsplit(self.origin)
        if u.scheme!='https' or not u.netloc or u.username or u.password or u.path or u.query or u.fragment:raise ValueError('canonical HTTPS origin required')
        if any(type(v) is not str or not v or v!=v.strip() for v in (self.tenant_id,self.application_id)):raise ValueError('server realm required')


def router(config,*,prefix='/api/v1/auth',cookie=COOKIE,store_attribute='browser_store'):
    COOKIE=cookie
    routes=APIRouter(prefix=prefix)
    def response(code,status):return JSONResponse({'code':code},status_code=status,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    def same_origin(request):return request.headers.get('origin')==config.origin and request.headers.get('host')==urlsplit(config.origin).netloc
    def services(request):
        store=getattr(request.app.state,store_attribute,None)
        if store is None:raise RuntimeError()
        op=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id=str(uuid.uuid4()),trace_id=str(uuid.uuid4()))
        return store,op,BrowserSessionService(store,store,application_id=config.application_id,operator=op)
    def view(session):return {'authenticated':True,'tenant_id':session.tenant_id,'principal_id':session.principal_id,'restricted':session.restricted}
    def login_work(request,body):
        try:
            store,op,sessions=services(request)
            key=request.app.state.browser_rate_key
            remote=request.client.host if request.client else ''
            decision=PostgresBrowserRateLimiter(store.pool,identity_operator=True).consume(config.tenant_id,'local_login',
                client_digest(key,canonical_origin=config.origin,client_identifier=remote),operator=op)
            if not decision.allowed:
                result=response('rate_limited',429);result.headers['Retry-After']=str(decision.retry_after_seconds);return result
            if type(body) is not dict or set(body)!={'username','password'} or any(type(body[k]) is not str or not 0<len(body[k])<=1024 for k in body):return response('invalid_request',400)
            passwords=LocalAccountService(store,AuthenticationEvidenceAuthority(store=store),subject_repository=store,
                tenant_id=config.tenant_id,application_id=config.application_id,operator=op,failure_result_verifier=store.verify_failure_result)
            auth=passwords.authenticate(config.tenant_id,body['username'],body['password'])
            now=store.current_time()
            members=[m for m in store.list_memberships(auth.subject_id) if m.tenant_id==config.tenant_id and m.status=='active' and m.valid_from<=now and (m.valid_until is None or now<m.valid_until)]
            if len(members)!=1:raise CredentialInvalid('invalid login')
            issued=sessions.create(config.tenant_id,auth.subject_id,members[0].principal_id,auth.evidence,replaced_session_token=request.cookies.get(COOKIE))
            result=JSONResponse({**view(issued.session),'csrf_token':issued.csrf_token.get_secret_value()},headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
            result.set_cookie(COOKIE,issued.session_token.get_secret_value(),secure=True,httponly=True,samesite='strict',path='/')
            return result
        except CredentialInvalid:return response('unauthenticated',401)
        except (ValueError,UnicodeError):return response('invalid_request',400)
        except Exception:return response('dependency_unavailable',503)
    @routes.post('/login')
    async def login(request:Request):
        if not same_origin(request):return response('forbidden',403)
        try:
            if request.headers.get('content-type','').split(';')[0]!='application/json':return response('invalid_request',400)
            data=b''
            async for part in request.stream():
                data+=part
                if len(data)>8192:return response('invalid_request',400)
            import json
            body=json.loads(data)
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(login_work,request,body)
        except (ValueError,UnicodeError):return response('invalid_request',400)
        except Exception:return response('dependency_unavailable',503)
    @routes.get('/session')
    def session(request:Request):
        if request.headers.get('host')!=urlsplit(config.origin).netloc:return response('forbidden',403)
        try:
            store,op,sessions=services(request)
            return JSONResponse(view(sessions.inspect(request.cookies.get(COOKIE,''))),headers={'Cache-Control':'no-store'})
        except CredentialInvalid:return response('unauthenticated',401)
        except Exception:return response('dependency_unavailable',503)
    @routes.post('/csrf')
    def csrf(request:Request):
        if not same_origin(request):return response('forbidden',403)
        try:
            store,op,sessions=services(request)
            issued=sessions.refresh_csrf(request.cookies.get(COOKIE,''))
            return JSONResponse({**view(issued.session),'csrf_token':issued.csrf_token.get_secret_value()},
                headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except CredentialInvalid:return response('unauthenticated',401)
        except Exception:return response('dependency_unavailable',503)
    @routes.post('/logout')
    def logout(request:Request):
        if not same_origin(request):return response('forbidden',403)
        try:
            store,op,sessions=services(request)
            sessions.revoke(request.cookies.get(COOKIE,''),request.headers.get('x-csrf-token',''))
            result=response('logged_out',200);result.delete_cookie(COOKIE,secure=True,httponly=True,samesite='strict',path='/');return result
        except CredentialInvalid:return response('unauthenticated',401)
        except Exception:return response('dependency_unavailable',503)
    return routes
