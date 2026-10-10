"""Dedicated loopback HTTPS runtime guard. It exposes no credentials or facts.

A transport key authenticates this local Host, not a business principal. The
provided independent Worker service must resolve current activation authority
through EIOS on every request; arbitrary Run IDs are never authority.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import re
import socket
import ssl
import threading
import time
from nexloop_eios.service_offerings import CatalogScopeDenied
from nexloop_eios.private_configuration import read_private_text


def _inherited_listener(listener, port):
    """NX-049 multi-process prototype: a loopback listener created by the parent Worker."""
    if (not isinstance(listener, socket.socket) or listener.family != socket.AF_INET
            or listener.type != socket.SOCK_STREAM or listener.getsockname() != ('127.0.0.1', port)
            or not 1024 <= port <= 65535):
        raise ValueError('inherited loopback listener required')
    try:listening=listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    except (AttributeError, OSError):listening=1  # not queryable on this platform (e.g. macOS)
    if not listening:raise ValueError('inherited loopback listener required')
    return listener


def create_runtime_guard_server(worker, *, port, key_file, certificate_file, tls_key_file, listen_socket=None):
    if type(port) is not int or not (port == 0 or 1024 <= port <= 65535):
        raise ValueError('unprivileged loopback port required')
    if listen_socket is not None:_inherited_listener(listen_socket,port)
    key_file=Path(key_file)
    def key():
        value=read_private_text(key_file,maximum=64)
        if not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('guard key unavailable')
        return value
    key()
    read_private_text(Path(certificate_file),maximum=32768)
    read_private_text(Path(tls_key_file),maximum=32768)
    slots=threading.BoundedSemaphore(8)
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def setup(self):
            super().setup();self.connection.settimeout(3);self.connection.do_handshake()
        def send(self,status,value):
            body=json.dumps(value,separators=(',',':')).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
            self.send_header('Connection','close');self.end_headers();self.wfile.write(body);self.close_connection=True
        def do_POST(self):
            if not slots.acquire(blocking=False):self.send(503,{'authorized':False});return
            try:
                expected=f'127.0.0.1:{self.server.server_port}'
                header=self.headers.get('Authorization','')
                if (self.client_address[0]!='127.0.0.1' or self.headers.get('Host')!=expected
                    or self.headers.get('Origin') is not None or self.headers.get('Sec-Fetch-Site') is not None
                    or len(self.headers.get_all('Authorization',[]))!=1
                    or not hmac.compare_digest(header,'Bearer '+key())):
                    self.send(401,{'authorized':False});return
                if self.path not in ('/internal/v1/runtime/authorize','/internal/v1/runtime/effects/submit','/internal/v1/runtime/effects/find',
                                     '/internal/v1/runtime/outcomes/record'):self.send(404,{'authorized':False});return
                if (self.headers.get('Content-Type')!='application/json'
                    or self.headers.get('Transfer-Encoding') is not None
                    or self.headers.get('Content-Encoding') is not None
                    or len(self.headers.get_all('Content-Length',[]))!=1):
                    self.send(400,{'authorized':False});return
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:self.send(400,{'authorized':False});return
                if not 1<=size<=262144:self.send(413,{'authorized':False});return
                try:body=json.loads(self.rfile.read(size))
                except (json.JSONDecodeError,UnicodeDecodeError):self.send(400,{'authorized':False});return
                if self.path=='/internal/v1/runtime/outcomes/record':
                    # NX-024 run-outcome of a plan reevaluation Run (contract run-outcome 1.0).
                    if (type(body) is not dict or set(body)!={'activation_ref','command','outcome'} or type(body['command']) is not dict
                        or type(body['activation_ref']) is not str or type(body['outcome']) is not dict):
                        self.send(400,{'code':'invalid_run_outcome'});return
                    import psycopg
                    try:result=worker.record_plan_outcome(activation_ref=body['activation_ref'],command=body['command'],outcome=body['outcome'])
                    except (ValueError,psycopg.errors.InvalidParameterValue):self.send(400,{'code':'invalid_run_outcome'});return
                    except psycopg.errors.SerializationFailure:self.send(409,{'code':'plan_version_not_current'});return
                    except Exception:self.send(403,{'code':'run_outcome_unavailable'});return
                    if type(result) is not dict or result.get('recorded') is not True:self.send(503,{'code':'run_outcome_unavailable'});return
                    self.send(200,{'run_id':body['command'].get('run_id'),'recorded':True,'replay':result['replay'] is True,'kind':result['kind'],
                        'plan_version':result['version'],'new_version':result.get('new_version')});return
                if self.path.startswith('/internal/v1/runtime/effects/'):
                    operation=self.path.rsplit('/',1)[1]
                    field='parameters' if operation=='submit' else 'intent_id'
                    if (type(body) is not dict or set(body) not in ({'activation_ref','command',field}, {'activation_ref','command',field,'request_scope'} if operation=='submit' else {'activation_ref','command',field})
                        or type(body['command']) is not dict or type(body['activation_ref']) is not str
                        or (operation=='submit' and type(body[field]) is not dict)
                        or (operation=='find' and type(body[field]) is not str)):
                        self.send(400,{'code':'invalid_effect_request'});return
                    from nexloop_eios.effect_intents import EffectIntentConflict,EffectIntentUnavailable
                    try:
                        result=worker.runtime_effect_tool(activation_ref=body['activation_ref'],command=body['command'],
                            tool_operation=operation,**{field:body[field]},**({'request_scope':body['request_scope']} if 'request_scope' in body else {}))
                    except CatalogScopeDenied as denial:
                        self.send(403,{'code':'outside_catalog_terms','scope':denial.scope});return
                    except EffectIntentConflict:
                        self.send(409,{'code':'intent_payload_conflict'});return
                    except EffectIntentUnavailable as unavailable:
                        # Same public code; transient contention (rolled back, deadline not
                        # passed) is 503/retryable, every other fail-closed outcome stays 403.
                        retryable=(unavailable.diagnosis or {}).get('retryable') is True
                        self.send(503 if retryable else 403,{'code':'effect_intent_unavailable'});return
                    expected_keys={'intent_id','receipt_id','state','payload_digest','provider_payload_digest','scope','business_action_success'}
                    if (type(result) is not dict or set(result)!={'run_id','receipt'}
                        or result['run_id']!=body['command'].get('run_id') or type(result['receipt']) is not dict
                        or set(result['receipt'])!=expected_keys or result['receipt']['scope']!='effect_intent'
                        or result['receipt']['business_action_success'] is not False):
                        self.send(503,{'code':'effect_intent_unavailable'});return
                    self.send(200,result);return
                if (type(body) is not dict or set(body) not in ({'activation_ref','command','operation'},{'activation_ref','command','operation','input'},{'activation_ref','command','operation','request_snapshot'},{'activation_ref','command','operation','model_result'})
                    or type(body['command']) is not dict or type(body['activation_ref']) is not str
                    or type(body['operation']) is not str or ('input' in body and type(body['input']) is not str)
                    or ('request_snapshot' in body and (body['operation']!='model' or type(body['request_snapshot']) is not dict))
                    or ('model_result' in body and (body['operation']!='model' or type(body['model_result']) is not dict))):
                    self.send(400,{'authorized':False});return
                result=worker.authorize_runtime_activation(activation_ref=body['activation_ref'],command=body['command'],operation=body['operation'],**({'input':body['input']} if 'input' in body else {}),
                    **({'request_snapshot':body['request_snapshot']} if 'request_snapshot' in body else {}),**({'model_result':body['model_result']} if 'model_result' in body else {}))
                if result.get('authorized') is not True or result.get('run_id')!=body['command'].get('run_id') or type(result.get('ever_execution_authorized')) is not bool:
                    self.send(403,{'authorized':False});return
                # Never serialize the backend object or a raw EIOS identity row.
                projected={'authorized':True,'run_id':result['run_id'],'ever_execution_authorized':result['ever_execution_authorized']}
                if 'context_artifact' in result:
                    import re
                    artifact=result['context_artifact']
                    if (type(artifact) is not dict or set(artifact)!={'artifact_ref','sha256','command_binding_digest'}
                        or any(type(v) is not str for v in artifact.values())
                        or re.fullmatch('artifact:[a-f0-9]{32}',artifact['artifact_ref']) is None
                        or any(re.fullmatch('[a-f0-9]{64}',artifact[key]) is None for key in ('sha256','command_binding_digest'))):
                        self.send(503,{'authorized':False});return
                    projected['context_artifact']=artifact
                self.send(200,projected)
            except Exception:
                self.send(503,{'authorized':False})
            finally:slots.release()
        def do_GET(self):self.send(404,{'authorized':False})
    class GuardServer(ThreadingHTTPServer):
        daemon_threads=True
        # Bound before spawning threads and before TLS/header reads, so idle
        # peers cannot consume an unbounded number of blocked request threads.
        def __init__(self,*args,**kwargs):
            self.connection_slots=threading.BoundedSemaphore(8)
            super().__init__(*args,**kwargs)
        def process_request(self,request,client_address):
            if not self.connection_slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:super().process_request(request,client_address)
            except BaseException:
                self.connection_slots.release()
                self.shutdown_request(request)
                raise
        def process_request_thread(self,request,client_address):
            try:super().process_request_thread(request,client_address)
            finally:self.connection_slots.release()
        def handle_error(self,request,client_address):pass  # No credential-bearing traceback.
        def drain(self,timeout):
            """After shutdown(): wait until every accepted connection has been answered."""
            deadline=time.monotonic()+timeout;held=0
            try:
                for _ in range(8):
                    if not self.connection_slots.acquire(timeout=max(0,deadline-time.monotonic())):return False
                    held+=1
                return True
            finally:
                for _ in range(held):self.connection_slots.release()
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate_file,tls_key_file)
    if listen_socket is None:
        server=GuardServer(('127.0.0.1',port),Handler)
    else:
        # Shared with sibling guard processes; this process never binds or listens itself.
        # Non-blocking: every sibling wakes on a pending connection, the losers' accept()
        # fails with EAGAIN (ignored by socketserver) instead of blocking serve_forever.
        listen_socket.setblocking(False)
        server=GuardServer(('127.0.0.1',port),Handler,bind_and_activate=False)
        server.socket.close();server.socket=listen_socket
        server.server_address=listen_socket.getsockname();server.server_name,server.server_port='127.0.0.1',port
    try:server.socket=context.wrap_socket(server.socket,server_side=True,do_handshake_on_connect=False)
    except Exception:
        server.server_close()
        raise
    return server
