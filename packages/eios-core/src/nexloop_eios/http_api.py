"""API process foundation. No unauthenticated business or fake login endpoints."""
from contextlib import ExitStack,asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import argparse
import uuid
import psycopg
from eios.adapters.postgres.database import StorageUnavailable
from nexloop_eios.backend import BackendClosed

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from nexloop_eios.backend import open_backend
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.host_control import HostControlConfiguration, probe_host


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


def create_app(config:ApiConfiguration):
    @asynccontextmanager
    async def lifespan(app):
        with ExitStack() as stack:
            app.state.backend=None
            app.state.browser_store=None
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
                    pool=stack.enter_context(open_browser_identity(read_private_text(config.browser.identity_database_url_file,maximum=16384)))
                    key=bytes.fromhex(read_private_text(config.browser.rate_key_file,maximum=128))
                    if len(key)!=32:raise ValueError()
                    app.state.browser_rate_key=key
                    app.state.browser_store=PostgresBrowserSessionUnitOfWork(pool,tenant_id=config.browser.tenant_id,application_id=config.browser.application_id)
                except Exception:pass
            try:yield
            finally:
                app.state.backend=None
                app.state.browser_store=None
                app.state.browser_rate_key=None
    app=FastAPI(title='NexLoop API',version='0.1.0',lifespan=lifespan,docs_url=None,redoc_url=None)
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
    @app.get('/health/live')
    def live():return {'alive':True}
    @app.get('/health/ready')
    def ready():
        backend=getattr(app.state,'backend',None)
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
            'missing_capabilities':missing},headers={'Cache-Control':'no-store'})
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
        app.mount('/',StaticFiles(directory=str(config.web_root),html=True),name='web')
    return app


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database-url-file',type=Path,required=True);p.add_argument('--signing-key-file',type=Path,required=True)
    p.add_argument('--artifact-root',type=Path,required=True);p.add_argument('--signing-key-id',required=True)
    p.add_argument('--mode',choices=['test'],required=True)
    p.add_argument('--port',type=int,default=8000)
    p.add_argument('--web-root',type=Path)
    # Deployment label only; successful model calls/readiness need actual evidence.
    p.add_argument('--execution-profile',choices=['deterministic-test','real-provider','disabled'])
    p.add_argument('--host-origin');p.add_argument('--host-control-key-file',type=Path);p.add_argument('--host-ca-file',type=Path)
    p.add_argument('--tls-certificate-file',type=Path);p.add_argument('--tls-key-file',type=Path)
    p.add_argument('--identity-database-url-file',type=Path);p.add_argument('--browser-rate-key-file',type=Path)
    p.add_argument('--browser-tenant-id');p.add_argument('--browser-application-id');p.add_argument('--browser-origin')
    a=p.parse_args()
    if not 1024<=a.port<=65535:p.error('unprivileged local port required')
    tls={}
    if a.tls_certificate_file is not None or a.tls_key_file is not None:
        if a.tls_certificate_file is None or a.tls_key_file is None:p.error('both private TLS files required')
        try:
            read_private_text(a.tls_certificate_file,maximum=32768);read_private_text(a.tls_key_file,maximum=32768)
        except Exception:p.error('private service-owned TLS files required')
        tls={'ssl_certfile':str(a.tls_certificate_file),'ssl_keyfile':str(a.tls_key_file)}
    browser=None
    browser_values=(a.identity_database_url_file,a.browser_rate_key_file,a.browser_tenant_id,a.browser_application_id,a.browser_origin)
    if any(v is not None for v in browser_values):
        if any(v is None for v in browser_values):p.error('complete private browser configuration required')
        from nexloop_eios.browser_http import BrowserConfiguration
        try:browser=BrowserConfiguration(*browser_values)
        except ValueError:p.error('valid fixed HTTPS browser realm required')
    host_control=None
    if any(v is not None for v in (a.host_origin,a.host_control_key_file,a.host_ca_file)):
        if any(v is None for v in (a.host_origin,a.host_control_key_file,a.host_ca_file)):p.error('complete Host control configuration required')
        try:host_control=HostControlConfiguration(a.host_origin,a.host_control_key_file,a.host_ca_file)
        except ValueError:p.error('explicit loopback Host origin required')
    import uvicorn
    # Foundation console is localhost-only; no accidental LAN/plaintext login.
    uvicorn.run(create_app(ApiConfiguration(a.database_url_file,a.signing_key_file,a.artifact_root,a.signing_key_id,browser,a.web_root,host_control,execution_profile=a.execution_profile)),
        host='127.0.0.1',port=a.port,access_log=False,log_level='warning',**tls)
    return 0


if __name__=='__main__':raise SystemExit(main())
