"""API process foundation. No unauthenticated business or fake login endpoints."""
from contextlib import ExitStack,asynccontextmanager
from dataclasses import dataclass,field
from pathlib import Path
import argparse
import os
import threading
import time
import uuid
import psycopg
from eios.adapters.postgres.database import StorageUnavailable
from nexloop_eios.backend import BackendClosed

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from nexloop_eios.backend import open_backend
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.postgres_artifacts import AuthoritySigner
from nexloop_eios.host_control import HostControlConfiguration, probe_host


# The API's deployment modes (--mode); deploy/authorization/api-routes.v1.json lists routes per mode (NX-062 C8).
MODES=('test','stage')


@dataclass(frozen=True)
class ApiConfiguration:
    database_url_file:Path
    signing_key_file:Path
    artifact_root:Path
    signing_key_id:str
    browser:object|None=None
    web_root:Path|None=None
    host_control:HostControlConfiguration|None=None
    execution_profile:str|None=None
    conversation_stream_seconds:int=180
    identity_pool_max:int=4
    cache_wakeup:object|None=None
    # NX-028: the owner workbench's own browser application (same identity store, realm and origin; own login and cookie).
    workbench:object|None=None
    # EIOS gap E4: a loopback port for this process's per-route request metrics (eios.api.metrics); None = no export (default).
    api_metrics_port:int|None=None
    # 'test' (the foundation console and container tests; readiness stays 503 with diagnostics) or 'stage' (deployment:
    # /health/ready is the real readiness of nexloop_eios.deployment_readiness, fixed reason codes only). Required and keyword-only
    # (NX-062 C8, L6 S-4): what is mounted depends on it (the test-world Artifact read exists in 'test' only), so no caller gets a
    # mode by default.
    mode:str=field(kw_only=True)
    def __post_init__(self):
        if self.mode not in MODES:raise ValueError('mode: test or stage')


def serve_route_metrics(registry,port):
    """E4: GET /internal/api-metrics on 127.0.0.1 only, this process's per-route counters and histograms (no tenant data, no IDs)."""
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from nexloop_eios.bootstrap import catalog
    if type(port) is not int or not 1024<=port<=65535:raise ValueError('unprivileged loopback port required')
    revision=catalog()[-1].version  # the catalog this code expects (the database is verified separately at readiness)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def do_GET(self):
            if self.path!='/internal/api-metrics' or self.client_address[0]!='127.0.0.1':
                self.send_response(404);self.end_headers();return
            body=registry.render(storage_mode='postgresql',revision=revision).encode()
            self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def create_app(config:ApiConfiguration):
    from eios.api.metrics import ApiMetricsRegistry
    from nexloop_eios.observability import API,flush_api,start_sampler
    routes_metrics=ApiMetricsRegistry()

    @asynccontextmanager
    async def lifespan(app):
        with ExitStack() as stack:
            app.state.backend=None
            app.state.browser_store=None
            app.state.workbench_store=None
            try:
                dsn=read_private_text(config.database_url_file,maximum=16384)
                app.state.backend=stack.enter_context(open_backend(database_url=dsn,
                    artifact_root=config.artifact_root,signing_key_file=config.signing_key_file,
                    signing_key_id=config.signing_key_id))
            except Exception:pass
            if config.browser is not None:
                try:
                    from nexloop_eios.browser_identity import open_browser_identity
                    from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
                    pool=stack.enter_context(open_browser_identity(read_private_text(config.browser.identity_database_url_file,maximum=16384),pool_max=config.identity_pool_max))
                    key=bytes.fromhex(read_private_text(config.browser.rate_key_file,maximum=128))
                    if len(key)!=32:raise ValueError()
                    app.state.browser_rate_key=key
                    app.state.browser_store=PostgresBrowserSessionUnitOfWork(pool,tenant_id=config.browser.tenant_id,application_id=config.browser.application_id)
                    if config.workbench is not None:
                        app.state.workbench_store=PostgresBrowserSessionUnitOfWork(pool,tenant_id=config.workbench.tenant_id,application_id=config.workbench.application_id)
                except Exception:pass
            # E4: one 'api' process sample per request class a minute (best effort, never breaks a request); per-route detail on
            # an optional loopback port.
            stop=threading.Event();stack.callback(stop.set)
            if app.state.backend is not None:
                backend=app.state.backend
                start_sampler(stop,60,lambda:flush_api(backend._pool))
            if config.api_metrics_port is not None:
                server=serve_route_metrics(routes_metrics,config.api_metrics_port)
                threading.Thread(target=server.serve_forever,name='nexloop-api-route-metrics',daemon=True).start()
                stack.callback(server.server_close);stack.callback(server.shutdown)
            try:yield
            finally:
                app.state.backend=None
                app.state.browser_store=None
                app.state.workbench_store=None
                app.state.browser_rate_key=None
    # NX-062 C8 F-1: no runtime OpenAPI document (the contracts are published from packages/contracts; app.openapi() still works).
    app=FastAPI(title='NexLoop API',version='0.1.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.route_metrics=routes_metrics

    @app.middleware('http')
    async def request_metrics(request:Request,call_next):
        # E4: every request, by its route template (never the raw path: bounded labels), method and status. A streaming response
        # counts until its headers are sent; an exception counts as 500 and is re-raised.
        started,status=time.perf_counter(),500
        try:
            response=await call_next(request);status=response.status_code
            return response
        finally:
            elapsed=time.perf_counter()-started
            route=getattr(request.scope.get('route'),'path',None)
            template=route if route else ('/' if route=='' else 'unmatched')
            routes_metrics.observe(method=request.method,route=template,status=status,duration_seconds=elapsed)
            API.observe(request.method,status,elapsed*1000)
    if config.browser is not None:
        from nexloop_eios.browser_http import router
        app.include_router(router(config.browser))
        from nexloop_eios.web_chat_http import router as conversation_router
        def browser_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            return backend.authenticate_browser(inspected_session)
        app.include_router(conversation_router(config.browser,
            ports_for_browser=browser_ports, execution_profile=config.execution_profile,
            stream_seconds=config.conversation_stream_seconds))
        from nexloop_eios.review_http import router as review_router
        from nexloop_eios.review_actions import workbench_ports
        def review_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            # NX-044 governed human review decisions; backend.py itself is unchanged.
            return workbench_ports(backend, inspected_session)
        app.include_router(review_router(config.browser, ports_for_browser=review_ports))
        from nexloop_eios.context_audit_http import ContextAuditPorts, router as context_audit_router
        def context_audit_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            # NX-023 human-only Manifest read (0094); backend.py itself is unchanged.
            return ContextAuditPorts(backend, inspected_session)
        app.include_router(context_audit_router(config.browser, ports_for_browser=context_audit_ports))
        from nexloop_eios.conversation_takeover_http import router as takeover_state_router
        def takeover_backend(request):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            return backend
        # NX-028 D4: the customer's "a person is handling this" status (0152); backend.py itself is unchanged.
        app.include_router(takeover_state_router(config.browser, backend_for=takeover_backend))
        from nexloop_eios.commercial_observe_http import ObservePorts, router as observe_router
        def observe_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            # NX-027 human-only reads of records, costs and key results (0134).
            return ObservePorts(backend, inspected_session)
        app.include_router(observe_router(config.browser, ports_for_browser=observe_ports))
    if config.browser is not None and config.workbench is not None:
        # NX-028 slice 1: workbench login realm (own cookie) and governed human reads.
        from nexloop_eios.browser_http import WORKBENCH_COOKIE, router as auth_router
        from nexloop_eios.workbench_http import router as workbench_router
        app.include_router(auth_router(config.workbench, prefix='/api/v1/workbench/auth', cookie=WORKBENCH_COOKIE, store_attribute='workbench_store',
                                    member_resets=True))
        def workbench_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            return backend.authenticate_workbench(inspected_session)
        app.include_router(workbench_router(config.workbench, ports_for_workbench=workbench_ports))
        from nexloop_eios.data_export_http import ExportPorts, router as export_router
        def export_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            # NX-029 slice 3: the requester's own export, assembled with the requester's member reads (0141, ADR-025).
            return ExportPorts(backend, inspected_session)
        app.include_router(export_router(config.workbench, ports_for_browser=export_ports))
        from nexloop_eios.workbench_actions_http import WorkbenchActions, router as workbench_actions_router
        def workbench_action_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            # NX-028 slice 2: governed human writes (0150 registry) for the workbench login realm only.
            return WorkbenchActions(backend, inspected_session)
        app.include_router(workbench_actions_router(config.workbench, ports_for_browser=workbench_action_ports))
        from nexloop_eios.review_actions import workbench_ports as review_workbench_ports
        from nexloop_eios.review_http import router as workbench_review_router
        def workbench_review_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            # Reviewer decisions in the workbench realm: the same governed human review Action (NX-044) as the browser
            # route, decided by the member's current ontology.schema.review grant (reviewer role only).
            return review_workbench_ports(backend, inspected_session)
        app.include_router(workbench_review_router(config.workbench, ports_for_browser=workbench_review_ports,
            prefix='/api/v1/workbench/review-decisions', cookie=WORKBENCH_COOKIE, store_attribute='workbench_store'))
        from nexloop_eios.schema_publication_http import SchemaPublicationPorts, router as schema_publication_router
        def schema_publication_ports(request, inspected_session):
            backend = getattr(request.app.state, 'backend', None)
            if backend is None:
                raise BackendClosed('backend is unavailable')
            # NX-040 F3/F4: the existing SchemaPublicationPort for the member's Human workbench session (ontology.schema.review).
            return SchemaPublicationPorts(backend, inspected_session)
        app.include_router(schema_publication_router(config.workbench, ports_for_browser=schema_publication_ports, cookie=WORKBENCH_COOKIE))
    # NX-027 signed commercial webhook: the connector signature authenticates (no session).
    from nexloop_eios.commercial_http import router as commercial_router
    app.include_router(commercial_router())
    @app.get('/health/live')
    def live():return {'alive':True}
    @app.get('/health/ready')
    def ready():
        backend=getattr(app.state,'backend',None)
        if config.mode=='stage':
            from nexloop_eios.deployment_readiness import dispatch_switch
            status,body=503,{'ready':False,'mode':'not_ready','reasons':['database_unavailable'],'dispatch_switch':dispatch_switch(os.environ),
                'real_dispatch':{'tenants_open':None,'actions_open':None}}
            try:
                if backend is not None:status,body=backend.deployment_readiness(os.environ)
            except Exception:pass
            return JSONResponse(status_code=status,content=body)
        foundation={'foundation_ready':False,'checks':{}}
        try:
            if backend is not None:foundation=backend.foundation_readiness()
        except Exception:pass
        browser_available=False
        browser_store=getattr(app.state,'browser_store',None)
        try:
            if browser_store is not None:
                # Actual restricted read rechecks PostgreSQL/identity role and port.
                browser_store.get_session('nexloop-readiness-probe')
                browser_available=True
        except Exception:pass
        missing=['host_dispatch']
        if not browser_available:missing.insert(0,'browser_session')
        # Host diagnostics do not prove Run dispatch or recovery readiness.
        return JSONResponse(status_code=503,content={'ready':False,'product_ready':False,
            'foundation':foundation,'browser_session_available':browser_available,
            'host':probe_host(config.host_control) if config.host_control else None,
            'missing_capabilities':missing,
            'cache':config.cache_wakeup.health(probe=True) if config.cache_wakeup is not None else {'configured':False,'available':False,'degraded':False,'authoritative':False}},headers={'Cache-Control':'no-store'})
    # S1 HTTP foundation: one Artifact by a Bearer service token of the fixed test world (the community container's smoke check).
    # NX-062 C8 F-2 (dispatcher 2026-10-26): mounted in --mode test only, never on a stage or any other deployment mode.
    if config.mode=='test':
        @app.get('/api/v1/artifacts/{artifact_id}')
        def artifact(artifact_id:str,request:Request):
            def error(code,status):
                return JSONResponse(status_code=status,content={'code':code,'message':'Resource unavailable',
                    'trace_id':str(uuid.uuid4()),'retryable':status==503,'details':{}},headers={'Cache-Control':'no-store'})
            header=request.headers.get('authorization','')
            if not header.startswith('Bearer ') or len(header)>2048:return error('unauthenticated',401)
            backend=getattr(app.state,'backend',None)
            if backend is None:return error('dependency_unavailable',503)
            try:service=backend.authenticate(header[7:],world='test')
            except (StorageUnavailable,BackendClosed,psycopg.OperationalError,psycopg.InterfaceError):return error('dependency_unavailable',503)
            except Exception:return error('unauthenticated',401)
            try:payload=service.read_artifact(artifact_id)
            except (StorageUnavailable,BackendClosed,psycopg.OperationalError,psycopg.InterfaceError):return error('dependency_unavailable',503)
            except Exception:return error('resource_unavailable',404)
            return Response(payload,media_type='application/octet-stream',headers={'Cache-Control':'no-store',
                'X-Content-Type-Options':'nosniff','Content-Disposition':'attachment'})
    @app.exception_handler(404)
    async def unknown(request,exc):
        return JSONResponse(status_code=404,content={'code':'not_found','message':'Resource unavailable',
            'trace_id':str(uuid.uuid4()),'retryable':False,'details':{}},headers={'Cache-Control':'no-store'})
    if config.web_root is not None:
        from fastapi.staticfiles import StaticFiles
        if config.web_root.is_symlink() or not config.web_root.is_dir():raise ValueError('built frontend directory required')
        # NX-028: workbench pages live in the URL (/workbench/<page>[/<id>]); the built single page serves them.
        from fastapi.responses import FileResponse
        index=config.web_root/'index.html'
        @app.get('/workbench',include_in_schema=False)
        @app.get('/workbench/{rest:path}',include_in_schema=False)
        def workbench_page(rest:str=''):
            if not index.is_file():return JSONResponse(status_code=404,content={'code':'not_found','message':'Resource unavailable','trace_id':str(uuid.uuid4()),'retryable':False,'details':{}})
            return FileResponse(index,headers={'Cache-Control':'no-store'})
        app.mount('/',StaticFiles(directory=str(config.web_root),html=True),name='web')
    return app


# Test-only switches a deployment must never carry (NX-032): the deterministic test model profile and the test harness's
# environment. Secrets reach a stage API as private files only, never as environment values.
STAGE_FORBIDDEN_ENVIRONMENT=('NEXLOOP_DATABASE_URL','MODEL_API_KEY','EMBEDDING_API_KEY')
STAGE_FORBIDDEN_PREFIXES=('NEXLOOP_TEST_',)


def stage_refusals(a,environ):
    """Why a --mode stage start is refused (fixed messages; empty when the configuration is complete)."""
    refusals=[]
    if a.signing_key_id is None:refusals.append('--signing-key-id required')
    if a.tls_certificate_file is None or a.tls_key_file is None:refusals.append('loopback TLS files required')
    if any(v is None for v in (a.host_origin,a.host_control_key_file,a.host_ca_file)):refusals.append('Host control configuration required')
    if a.execution_profile not in ('real-provider','disabled'):refusals.append('--execution-profile real-provider or disabled required')
    if environ.get('MODEL_PROVIDER','').strip().lower()=='test':refusals.append('test model provider refused')
    for key in environ:
        if key in STAGE_FORBIDDEN_ENVIRONMENT or key.startswith(STAGE_FORBIDDEN_PREFIXES):refusals.append('test or secret environment refused: '+key)
    try:read_private_text(a.database_url_file,maximum=16384)
    except Exception:refusals.append('private file required: --database-url-file')
    # The signing key is raw bytes (any 32, binary or ASCII): checked with the backend's own read, never decoded as text.
    try:AuthoritySigner.from_file(a.signing_key_file)
    except Exception:refusals.append('private file required: --signing-key-file')
    return refusals


def parse(argv=None,environ=None):
    """Arguments → (ApiConfiguration, port, uvicorn TLS options). Refusals exit through argparse (status 2)."""
    environ=os.environ if environ is None else environ
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database-url-file',type=Path,required=True);p.add_argument('--signing-key-file',type=Path,required=True)
    p.add_argument('--artifact-root',type=Path,required=True);p.add_argument('--signing-key-id')
    p.add_argument('--mode',choices=list(MODES),required=True)
    p.add_argument('--port',type=int,default=8000)
    p.add_argument('--web-root',type=Path)
    p.add_argument('--cache-configuration-file',type=Path)
    # Deployment label only; successful model calls/readiness need actual evidence.
    p.add_argument('--execution-profile',choices=['deterministic-test','real-provider','disabled'])
    p.add_argument('--host-origin');p.add_argument('--host-control-key-file',type=Path);p.add_argument('--host-ca-file',type=Path)
    p.add_argument('--tls-certificate-file',type=Path);p.add_argument('--tls-key-file',type=Path)
    p.add_argument('--identity-database-url-file',type=Path);p.add_argument('--browser-rate-key-file',type=Path)
    p.add_argument('--identity-token-key-file',type=Path,help='NX-058: private key of one-time workbench secrets (reset codes, invitations)')
    p.add_argument('--browser-tenant-id');p.add_argument('--browser-application-id');p.add_argument('--browser-origin')
    p.add_argument('--workbench-application-id')
    # L6 C11: the gateway peers (address or CIDR, repeatable) whose X-NexLoop-Client-IP the login rate limit trusts; none = the socket peer.
    p.add_argument('--trusted-proxy-peer',action='append',default=[])
    # NX-032: the browser identity pool of this process (connection budget entry api-browser-identity, 4 per process).
    p.add_argument('--identity-pool-max',type=int,default=4)
    # EIOS gap E4: the loopback port of this process's per-route request metrics (off by default; one port per API process).
    p.add_argument('--api-metrics-port',type=int)
    a=p.parse_args(argv)
    if a.mode=='test' and a.signing_key_id is None:p.error('--signing-key-id required')
    if a.mode=='stage':
        refusals=stage_refusals(a,environ)
        if refusals:p.error('stage configuration refused: '+'; '.join(refusals))
    if not 1024<=a.port<=65535:p.error('unprivileged local port required')
    if not 1<=a.identity_pool_max<=8:p.error('identity pool size 1..8')
    if a.api_metrics_port is not None and (not 1024<=a.api_metrics_port<=65535 or a.api_metrics_port==a.port):p.error('separate unprivileged metrics port required')
    tls={}
    if a.tls_certificate_file is not None or a.tls_key_file is not None:
        if a.tls_certificate_file is None or a.tls_key_file is None:p.error('both private TLS files required')
        try:
            read_private_text(a.tls_certificate_file,maximum=32768);read_private_text(a.tls_key_file,maximum=32768)
        except Exception:p.error('private service-owned TLS files required')
        tls={'ssl_certfile':str(a.tls_certificate_file),'ssl_keyfile':str(a.tls_key_file)}
    browser=None
    if a.trusted_proxy_peer and not (a.identity_database_url_file or a.browser_origin):p.error('trusted proxy peers need the browser realm')
    browser_values=(a.identity_database_url_file,a.browser_rate_key_file,a.browser_tenant_id,a.browser_application_id,a.browser_origin)
    if any(v is not None for v in browser_values):
        if any(v is None for v in browser_values):p.error('complete private browser configuration required')
        from nexloop_eios.browser_http import BrowserConfiguration
        try:browser=BrowserConfiguration(*browser_values,trusted_proxy_peers=tuple(a.trusted_proxy_peer))
        except ValueError:p.error('valid fixed HTTPS browser realm required')
    workbench=None
    if a.workbench_application_id is not None:
        if browser is None or a.workbench_application_id==a.browser_application_id:p.error('workbench needs the browser realm and its own application')
        from nexloop_eios.browser_http import BrowserConfiguration
        workbench=BrowserConfiguration(a.identity_database_url_file,a.browser_rate_key_file,a.browser_tenant_id,a.workbench_application_id,a.browser_origin,
            trusted_proxy_peers=tuple(a.trusted_proxy_peer),identity_token_key_file=a.identity_token_key_file)
    host_control=None
    if any(v is not None for v in (a.host_origin,a.host_control_key_file,a.host_ca_file)):
        if any(v is None for v in (a.host_origin,a.host_control_key_file,a.host_ca_file)):p.error('complete Host control configuration required')
        try:host_control=HostControlConfiguration(a.host_origin,a.host_control_key_file,a.host_ca_file)
        except ValueError:p.error('explicit loopback Host origin required')
    from nexloop_eios.valkey_wakeup import ValkeyWakeup
    cache=None
    if a.cache_configuration_file is not None:
        try:cache=ValkeyWakeup.from_file(a.cache_configuration_file)
        except Exception:p.error('private cache configuration unavailable')
    return ApiConfiguration(a.database_url_file,a.signing_key_file,a.artifact_root,a.signing_key_id,browser,a.web_root,host_control,
        execution_profile=a.execution_profile,cache_wakeup=cache,workbench=workbench,mode=a.mode,identity_pool_max=a.identity_pool_max,
        api_metrics_port=a.api_metrics_port),a.port,tls


def main():
    # NX-030 / AT-049: every log record leaves this process as one allowlisted structured line (IDs, codes, durations only).
    from nexloop_eios.structured_log import configure as _structured_logging
    _structured_logging('api')
    config,port,tls=parse()
    import uvicorn
    # Foundation console is localhost-only; no accidental LAN/plaintext login.
    uvicorn.run(create_app(config),
        host='127.0.0.1',port=port,access_log=False,log_level='warning',log_config=__import__('nexloop_eios.structured_log',fromlist=['x']).uvicorn_log_config('api'),**tls)
    return 0


if __name__=='__main__':raise SystemExit(main())
