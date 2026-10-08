"""Actual production HTTP client against independent synthetic fault service."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
import time
import uuid

import pytest
from nexloop_eios.effect_provider import (EffectProviderConfiguration,HttpEffectProvider,
    EffectProviderUnknown,EffectProviderQueryUnavailable)
from support.effect_provider import effect_provider,payload_digest


def arguments():
    parameters={'operation':'synthetic fulfillment'}
    return {'intent_id':str(uuid.uuid4()),'parameters':parameters,'payload_digest':payload_digest(parameters)}


def client(provider,timeout=2):
    return HttpEffectProvider(EffectProviderConfiguration(provider.origin,timeout=timeout,test_loopback_http=True))


def test_real_transport_dispatch_query_and_payload_conflict_preserve_original(tmp_path):
    values=arguments()
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        transport=client(provider);accepted=transport.dispatch(**values)
        assert accepted.state=='accepted' and accepted.provider_reference
        assert transport.query(intent_id=values['intent_id'],payload_digest=values['payload_digest'])==accepted
        changed={**values,'parameters':{'operation':'different'}};changed['payload_digest']=payload_digest(changed['parameters'])
        with pytest.raises(EffectProviderUnknown):transport.dispatch(**changed)
        assert transport.query(intent_id=values['intent_id'],payload_digest=values['payload_digest'])==accepted
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==1 and snapshot['requests'][2][2]==409


@pytest.mark.parametrize('invalid',['digest','parameters','intent'])
def test_invalid_local_payload_performs_zero_network_io(tmp_path,invalid):
    values=arguments()
    if invalid=='digest':values['payload_digest']='0'*64
    elif invalid=='parameters':values['parameters']=[]
    else:values['intent_id']='not-a-valid-uuid'
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        with pytest.raises(ValueError):client(provider).dispatch(**values)
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==0 and snapshot['requests']==[]


def test_post_commit_timeout_queries_original_intent_without_redispatch(tmp_path):
    values=arguments()
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        provider.control('pause');transport=client(provider,timeout=.2);started=time.monotonic()
        with pytest.raises(EffectProviderUnknown,match='^effect_provider_result_unknown$'):transport.dispatch(**values)
        assert time.monotonic()-started<1
        assert provider.wait('accepted_committed')['intent_id']==values['intent_id']
        receipt=transport.query(intent_id=values['intent_id'],payload_digest=values['payload_digest'])
        assert receipt.state=='accepted' and receipt.state!='fulfilled'
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==1 and snapshot['requests']==[('POST',values['intent_id'],202),('GET',values['intent_id'],200)]
        provider.control('release')


def test_query_unavailable_never_becomes_fulfilled(tmp_path):
    values=arguments()
    with effect_provider(tmp_path/'provider.sqlite') as provider:transport=client(provider)
    with pytest.raises(EffectProviderQueryUnavailable,match='^effect_provider_query_unavailable$'):transport.query(intent_id=values['intent_id'],payload_digest=values['payload_digest'])


@contextmanager
def bad_server(mode,sentinel):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            requests.append(self.path);self.rfile.read(int(self.headers['Content-Length']))
            raw=json.dumps({'unexpected':sentinel}).encode()
            self.send_response(307 if mode=='redirect' else 200)
            if mode=='redirect':self.send_header('Location','http://127.0.0.1:1/'+sentinel)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port),requests
    finally:server.shutdown();server.server_close();thread.join(5)


@pytest.mark.parametrize('mode',['redirect','malformed'])
def test_bad_receipt_or_redirect_has_fixed_sanitized_error(mode,capsys):
    sentinel='synthetic-provider-error-'+uuid.uuid4().hex
    with bad_server(mode,sentinel) as (origin,requests):
        transport=HttpEffectProvider(EffectProviderConfiguration(origin,test_loopback_http=True))
        with pytest.raises(EffectProviderUnknown) as failure:transport.dispatch(**arguments())
        assert str(failure.value)=='effect_provider_result_unknown' and sentinel not in str(failure.value)
        assert failure.value.__suppress_context__ and requests==['/v1/effects']
    output=capsys.readouterr();assert sentinel not in output.out+output.err


def test_query_not_found_has_no_digest_or_reference_and_zero_post(tmp_path):
    values=arguments()
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        receipt=client(provider).query(intent_id=values['intent_id'],payload_digest=values['payload_digest'])
        assert receipt.intent_id==values['intent_id'] and receipt.state=='not_found'
        assert receipt.payload_digest is None and receipt.provider_reference is None
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==0 and snapshot['requests']==[('GET',values['intent_id'],404)]


@pytest.mark.parametrize('origin,address',[
    ('https://synthetic.invalid',None),('https://synthetic.invalid','synthetic.invalid'),
    ('https://127.0.0.1','not-a-numeric-address'),
])
def test_https_configuration_rejects_unbounded_dns(origin,address):
    with pytest.raises(ValueError,match='^effect_provider_configuration_invalid$'):
        EffectProviderConfiguration(origin,credential_file='synthetic-private-path',connect_address=address)


def test_https_configuration_accepts_explicit_numeric_target():
    config=EffectProviderConfiguration('https://synthetic.invalid',credential_file='synthetic-private-path',connect_address='127.0.0.1')
    assert config.connect_address=='127.0.0.1'


def test_real_https_tls_fixed_numeric_origin_and_private_credential(tmp_path,capsys):
    import ssl
    from test_agent_host import files
    _,key=files(tmp_path);values=arguments();requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            assert self.headers.get('Authorization')=='Bearer '+key.read_text()
            assert self.headers['Idempotency-Key']==values['intent_id']
            received=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert received==values
            requests.append(self.path)
            content=json.dumps({'intent_id':values['intent_id'],'payload_digest':values['payload_digest'],
                'state':'accepted','provider_reference':'synthetic:tls-receipt'}).encode()
            self.send_response(202);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(tmp_path/'host-cert.pem',tmp_path/'host-key.pem')
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        config=EffectProviderConfiguration('https://127.0.0.1:'+str(server.server_port),credential_file=str(key),ca_file=str(tmp_path/'host-cert.pem'))
        receipt=HttpEffectProvider(config).dispatch(**values)
        assert receipt.state=='accepted' and receipt.provider_reference=='synthetic:tls-receipt'
        assert requests==['/v1/effects']
    finally:server.shutdown();server.server_close();thread.join(5)
    output=capsys.readouterr();assert key.read_text() not in output.out+output.err


@pytest.mark.parametrize('operation,error',[('dispatch',EffectProviderUnknown),('query',EffectProviderQueryUnavailable)])
def test_numeric_https_silent_tls_handshake_obeys_absolute_deadline(tmp_path,operation,error):
    import socket
    from test_agent_host import files
    _,key=files(tmp_path);values=arguments();accepted=threading.Event();stop=threading.Event()
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(.1)
    def silent():
        while not stop.is_set():
            try:connection,_=listener.accept()
            except TimeoutError:continue
            except OSError:return
            with connection:
                accepted.set()
                # A real established TCP connection consumes ClientHello but
                # sends no TLS bytes. DNS and HTTP cannot explain this timeout.
                connection.settimeout(.5)
                try:connection.recv(4096)
                except OSError:pass
                stop.wait(5)
            return
    thread=threading.Thread(target=silent,daemon=True);thread.start()
    try:
        configuration=EffectProviderConfiguration('https://127.0.0.1:'+str(listener.getsockname()[1]),
            credential_file=str(key),ca_file=str(tmp_path/'host-cert.pem'),timeout=.25)
        transport=HttpEffectProvider(configuration);started=time.monotonic()
        with pytest.raises(error) as failure:
            if operation=='dispatch':transport.dispatch(**values)
            else:transport.query(intent_id=values['intent_id'],payload_digest=values['payload_digest'])
        elapsed=time.monotonic()-started
        assert accepted.is_set() and elapsed<1.5
        assert str(failure.value)==('effect_provider_result_unknown' if operation=='dispatch' else 'effect_provider_query_unavailable')
        assert failure.value.__suppress_context__ and key.read_text() not in str(failure.value)
    finally:stop.set();listener.close();thread.join(5);assert not thread.is_alive()


@contextmanager
def trickle_https(tmp_path,values,mode):
    import ssl
    from test_agent_host import files
    _,key=files(tmp_path);stop=threading.Event();connected=threading.Event();sent=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def response(self):
            connected.set()
            content=json.dumps({'intent_id':values['intent_id'],'payload_digest':values['payload_digest'],
                'state':'accepted','provider_reference':'synthetic:trickle-receipt'}).encode()
            headers=('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(content))+'\r\nConnection: close\r\n\r\n').encode()
            self.close_connection=True
            pieces=headers if mode=='headers' else content
            try:
                if mode=='body':self.connection.sendall(headers)
                # Each byte arrives inside the ordinary socket timeout, while
                # the full response greatly exceeds the absolute request limit.
                for value in pieces:
                    if stop.is_set():break
                    self.connection.sendall(bytes([value]));sent.append(value)
                    if stop.wait(.04):break
            except OSError:pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']));self.response()
        def do_GET(self):self.response()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(tmp_path/'host-cert.pem',tmp_path/'host-key.pem')
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield EffectProviderConfiguration('https://127.0.0.1:'+str(server.server_port),credential_file=str(key),ca_file=str(tmp_path/'host-cert.pem'),timeout=.3),connected,sent
    finally:stop.set();server.shutdown();server.server_close();thread.join(5);assert not thread.is_alive()


@pytest.mark.parametrize('mode',['headers','body'])
@pytest.mark.parametrize('operation,error',[('dispatch',EffectProviderUnknown),('query',EffectProviderQueryUnavailable)])
def test_tls_trickle_close_response_cannot_extend_absolute_deadline(tmp_path,mode,operation,error,capsys):
    values=arguments()
    with trickle_https(tmp_path,values,mode) as (configuration,connected,sent):
        transport=HttpEffectProvider(configuration);started=time.monotonic()
        with pytest.raises(error) as failure:
            if operation=='dispatch':transport.dispatch(**values)
            else:transport.query(intent_id=values['intent_id'],payload_digest=values['payload_digest'])
        elapsed=time.monotonic()-started
        assert connected.is_set() and 2<=len(sent)<30 and elapsed<1.5
        assert str(failure.value)==('effect_provider_result_unknown' if operation=='dispatch' else 'effect_provider_query_unavailable')
        assert failure.value.__suppress_context__
    output=capsys.readouterr();assert (tmp_path/'internal-key').read_text() not in output.out+output.err+str(failure.value)
