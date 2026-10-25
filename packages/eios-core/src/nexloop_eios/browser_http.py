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
    # L6 C11 (2026-10-22): the gateway peers whose X-NexLoop-Client-IP header is trusted (eios.identity.client_ip); empty = the
    # socket peer only, every forwarding header ignored.
    trusted_proxy_peers:tuple=()
    # NX-058: the private key of one-time workbench secrets (reset codes, invitations); None = those endpoints are unavailable.
    identity_token_key_file:Path|None=None
    def __post_init__(self):
        u=urlsplit(self.origin)
        if u.scheme!='https' or not u.netloc or u.username or u.password or u.path or u.query or u.fragment:raise ValueError('canonical HTTPS origin required')
        if any(type(v) is not str or not v or v!=v.strip() for v in (self.tenant_id,self.application_id)):raise ValueError('server realm required')
        if type(self.trusted_proxy_peers) is not tuple:raise ValueError('trusted proxy peers must be a tuple')
        client_ip_resolver(self)  # refuses an invalid peer list at start


def client_ip_resolver(config):
    """Behind trusted proxies: their X-NexLoop-Client-IP only, strictly (eios.identity.client_ip; anything else fails the request).
    Without them: the socket peer as before (request.client.host; it cannot be forged), every forwarding header ignored."""
    if config.trusted_proxy_peers:
        from eios.identity.client_ip import TrustedProxyIdentityClientIpResolver
        return TrustedProxyIdentityClientIpResolver(config.trusted_proxy_peers)
    class SocketPeer:
        def resolve(self,request):return request.client.host if request.client and request.client.host else 'unknown'
    return SocketPeer()


def login_username_key(username):
    """The username dimension of the login rate limit (L6 C11): NFKC, trimmed, case-folded, so case and width variants of one
    account share one bucket. Only an HMAC digest of it is stored (browser_rate_limits.client_digest)."""
    import hashlib,unicodedata
    return 'user:'+hashlib.sha256(unicodedata.normalize('NFKC',username).strip().casefold().encode()).hexdigest()


def router(config,*,prefix='/api/v1/auth',cookie=COOKIE,store_attribute='browser_store',member_resets=False):
    """member_resets (the workbench realm only, NX-058 slice B): POST /password/reset completes an owner-issued reset code, the
    session view carries the 7-day reset notice and GET /reset-history the member's own 90 days."""
    COOKIE=cookie
    routes=APIRouter(prefix=prefix)
    resolver=client_ip_resolver(config)
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
            # L6 C11: two dimensions, each with its own bucket of the local_login policy (domain-separated digests): the client
            # address (behind the gateway, its X-NexLoop-Client-IP from a trusted peer only; never a client-supplied header) and
            # the account name. Every attempt counts in both (success does not reset either, as before); either one over the limit
            # is 429. The address bucket is taken first, also for a malformed body; the name bucket once the body is valid.
            limiter=PostgresBrowserRateLimiter(store.pool,identity_operator=True)
            def consume(identifier):
                decision=limiter.consume(config.tenant_id,'local_login',client_digest(key,canonical_origin=config.origin,client_identifier=identifier),operator=op)
                if decision.allowed:return None
                result=response('rate_limited',429);result.headers['Retry-After']=str(decision.retry_after_seconds);return result
            refused=consume('ip:'+resolver.resolve(request))
            if refused is not None:return refused
            if type(body) is not dict or set(body)!={'username','password'} or any(type(body[k]) is not str or not 0<len(body[k])<=1024 for k in body):return response('invalid_request',400)
            refused=consume(login_username_key(body['username']))
            if refused is not None:return refused
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
    def notices(store,session,days):
        from nexloop_eios.browser_identity import verify_identity_role
        with store.pool.connection() as c,c.transaction():
            verify_identity_role(c)
            return c.execute('select control.nexloop_workbench_reset_notices(%s,%s,%s)',(config.tenant_id,session.principal_id,days)).fetchone()[0]
    @routes.get('/session')
    def session(request:Request):
        if request.headers.get('host')!=urlsplit(config.origin).netloc:return response('forbidden',403)
        try:
            store,op,sessions=services(request)
            current=sessions.inspect(request.cookies.get(COOKIE,''))
            # NX-058 V3: every login within 7 days of a completed owner-issued reset shows it; nothing can dismiss it. A failed read of
            # the notices does not fail the session view (review S-4): the field is left out and the failure logged by type only.
            extra={}
            if member_resets:
                try:extra={'reset_notices':notices(store,current,7)}
                except Exception as failure:
                    import logging
                    logging.getLogger('nexloop.workbench').warning('reset notices unavailable %s',{'reason':type(failure).__name__})
            return JSONResponse({**view(current),**extra},headers={'Cache-Control':'no-store'})
        except CredentialInvalid:return response('unauthenticated',401)
        except Exception:return response('dependency_unavailable',503)
    if member_resets:
        @routes.get('/reset-history')
        def reset_history(request:Request):
            if request.headers.get('host')!=urlsplit(config.origin).netloc:return response('forbidden',403)
            try:
                store,op,sessions=services(request)
                current=sessions.inspect(request.cookies.get(COOKIE,''))
                return JSONResponse({'resets':notices(store,current,90)},headers={'Cache-Control':'no-store'})
            except CredentialInvalid:return response('unauthenticated',401)
            except Exception:return response('dependency_unavailable',503)
        def reset_work(request,body):
            # NX-058 §2 (I3, R4): no session; the tenant is the server's (config.tenant_id), never the body's. The body is the code
            # and the new password only (no username: nothing to enumerate, no per-name bucket to exhaust). One answer for every
            # refusal; the source bucket of password_reset_complete only; a code's own failures void it at five; the login lockout
            # is never touched.
            from argon2 import PasswordHasher
            from nexloop_eios import workbench_member_secrets as W
            from nexloop_eios.browser_identity import verify_identity_role
            from nexloop_eios.identity_password_reset import password_problem
            invalid=lambda:response('reset_invalid',400)  # noqa: E731
            try:key=W.load_key(config.identity_token_key_file)
            except Exception:return response('dependency_unavailable',503)
            store,op,sessions=services(request)
            address=resolver.resolve(request)
            limiter=PostgresBrowserRateLimiter(store.pool,identity_operator=True)
            decision=limiter.consume(config.tenant_id,'password_reset_complete',client_digest(request.app.state.browser_rate_key,
                canonical_origin=config.origin,client_identifier='ip:'+address),operator=op)
            if not decision.allowed:
                result=response('rate_limited',429);result.headers['Retry-After']=str(decision.retry_after_seconds);return result
            if type(body) is not dict or set(body)!={'code','password'} or not W.valid_code(body['code']) or type(body['password']) is not str:
                PasswordHasher().hash('nexloop-timing-equaliser');return invalid()
            code=W.digest(key,W.RESET,config.tenant_id,body['code'])
            with store.pool.connection() as c:
                with c.transaction():
                    verify_identity_role(c)
                    found=c.execute('select control.nexloop_workbench_reset_lookup(%s,%s)',(config.tenant_id,code)).fetchone()[0]
                if found is None:
                    PasswordHasher().hash('nexloop-timing-equaliser');return invalid()
                if password_problem(body['password'],found['password_hash'],found['password_history'] or []) is not None:
                    with c.transaction():
                        c.execute('select control.nexloop_workbench_reset_failed(%s,%s)',(config.tenant_id,code))
                    return invalid()
                encoded=PasswordHasher().hash(body['password'])
                try:
                    with c.transaction():
                        c.execute('select control.nexloop_complete_workbench_reset(%s,%s,%s,%s,%s,%s)',(config.tenant_id,code,found['revision'],encoded,
                            W.source_digest(key,config.tenant_id,address),op.trace_id))
                except Exception as error:
                    if getattr(error,'sqlstate',None)=='22023':return invalid()
                    raise
            return response('password_reset',200)
        def accept_work(request,body):
            # NX-058 slice A §1 (R4, I3): no session; the tenant is the server's. The body is the token and the password only. One answer
            # for every refusal; the source bucket of invitation_accept; a token's own failures void it at five. The Human is built here
            # (the identity directory's models, as the initial identity does) and written by SQL with the configuration version and the
            # role template's grants, in one transaction.
            import uuid as _uuid
            from argon2 import PasswordHasher
            from psycopg.types.json import Jsonb
            from eios.identity.models import EncodedPasswordHash,LocalAccount,MembershipKind,Subject,SubjectKind,TenantMembership
            from nexloop_eios import workbench_member_secrets as W
            from nexloop_eios.browser_identity import verify_identity_role
            from nexloop_eios.identity_password_reset import password_problem
            invalid=lambda:response('invitation_invalid',400)  # noqa: E731
            try:key=W.load_key(config.identity_token_key_file)
            except Exception:return response('dependency_unavailable',503)
            store,op,sessions=services(request)
            address=resolver.resolve(request)
            limiter=PostgresBrowserRateLimiter(store.pool,identity_operator=True)
            decision=limiter.consume(config.tenant_id,'invitation_accept',client_digest(request.app.state.browser_rate_key,
                canonical_origin=config.origin,client_identifier='ip:'+address),operator=op)
            if not decision.allowed:
                result=response('rate_limited',429);result.headers['Retry-After']=str(decision.retry_after_seconds);return result
            if type(body) is not dict or set(body)!={'token','password'} or not W.valid_token(body['token']) or type(body['password']) is not str:
                PasswordHasher().hash('nexloop-timing-equaliser');return invalid()
            token=W.digest(key,W.INVITE,config.tenant_id,body['token'])
            with store.pool.connection() as c:
                with c.transaction():
                    verify_identity_role(c)
                    found=c.execute('select control.nexloop_workbench_invitation_lookup(%s,%s)',(config.tenant_id,token)).fetchone()[0]
                if found is None:
                    PasswordHasher().hash('nexloop-timing-equaliser');return invalid()
                if password_problem(body['password'],None,[]) is not None:
                    with c.transaction():
                        c.execute('select control.nexloop_workbench_invitation_failed(%s,%s)',(config.tenant_id,token))
                    return invalid()
                now=store.current_time();ident=_uuid.uuid4().hex
                subject=Subject(subject_id=f'workbench-subject-{ident}',kind=SubjectKind.HUMAN,status='active',created_at=now,updated_at=now,revision=1)
                membership=TenantMembership(tenant_id=config.tenant_id,principal_id=f'workbench-{ident}',subject_id=subject.subject_id,kind=MembershipKind.HOME,
                    status='active',valid_from=now,valid_until=None,revision=1)
                encoded=PasswordHasher().hash(body['password'])
                account=LocalAccount(local_account_id=f'workbench-account-{ident}',tenant_id=config.tenant_id,subject_id=subject.subject_id,
                    username=found['username'],verified_email=found['username']+'@workbench.invalid',password_hash=EncodedPasswordHash(encoded),status='active',failed_attempts=0,lockout_level=0,locked_until=None,
                    must_change_password=False,session_epoch=1,created_at=now,updated_at=now,revision=1).model_dump(mode='json')
                account.pop('password_hash');account.pop('password_history',None)
                try:
                    with c.transaction():
                        c.execute('select control.nexloop_accept_workbench_invitation_v3(%s,%s,%s,%s,%s,%s,%s,%s)',(config.tenant_id,token,
                            Jsonb(subject.model_dump(mode='json')),Jsonb(membership.model_dump(mode='json')),Jsonb(account),encoded,
                            W.source_digest(key,config.tenant_id,address),op.trace_id))
                except Exception as error:
                    if getattr(error,'sqlstate',None)=='22023':return invalid()
                    raise
            return response('invitation_accepted',200)
        @routes.post('/invitation/accept')
        async def invitation_accept(request:Request):
            if not same_origin(request):return response('forbidden',403)
            try:
                if request.headers.get('content-type','').split(';')[0]!='application/json':return response('invitation_invalid',400)
                data=b''
                async for part in request.stream():
                    data+=part
                    if len(data)>4096:return response('invitation_invalid',400)
                import json
                body=json.loads(data)
                from starlette.concurrency import run_in_threadpool
                return await run_in_threadpool(accept_work,request,body)
            except (ValueError,UnicodeError):return response('invitation_invalid',400)
            except Exception:return response('dependency_unavailable',503)
        @routes.post('/password/reset')
        async def password_reset(request:Request):
            if not same_origin(request):return response('forbidden',403)
            try:
                if request.headers.get('content-type','').split(';')[0]!='application/json':return response('reset_invalid',400)
                data=b''
                async for part in request.stream():
                    data+=part
                    if len(data)>4096:return response('reset_invalid',400)
                import json
                body=json.loads(data)
                from starlette.concurrency import run_in_threadpool
                return await run_in_threadpool(reset_work,request,body)
            except (ValueError,UnicodeError):return response('reset_invalid',400)
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
