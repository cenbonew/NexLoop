"""NX-036 follow-up (CVE-2025-13836): the retention keeper's HostPurger reads the Host's loopback reply with an upper bound,
like every other http.client read in the package. A reply longer than the bound is a failure (the queue keeps the item),
and a Content-Length the server never fills is not waited on beyond the bound."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import ssl
import subprocess
import threading
import time
import uuid

import pytest

from nexloop_eios.retention import HostPurger


@contextmanager
def loopback(tmp_path,status,body,*,content_length=None,hold=0.0):
    certificate,server_key=tmp_path/'host-cert.pem',tmp_path/'host-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(server_key),'-out',str(certificate),'-days','1',
        '-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path,self.headers.get('Authorization')));self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body) if content_length is None else content_length));self.end_headers()
            self.wfile.write(body);self.wfile.flush()
            if hold:time.sleep(hold)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(str(certificate),str(server_key))
    server.socket=tls.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    key=tmp_path/'host-key';key.write_text('a'*64);key.chmod(0o600)
    try:
        yield HostPurger(port=server.server_port,key_file=key,ca_file=certificate),seen
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


def reply(outcome,pad=0):
    return json.dumps({'outcome':outcome,'pad':'x'*pad}).encode() if pad else json.dumps({'outcome':outcome}).encode()


def test_a_reply_within_the_bound_is_read(tmp_path):
    body=reply('purged',pad=HostPurger.RESPONSE_LIMIT-len(reply('purged',pad=1))+1)
    assert len(body)==HostPurger.RESPONSE_LIMIT
    with loopback(tmp_path,200,body) as (purger,seen):
        assert purger(str(uuid.uuid4()))=='purged'
        assert seen==[('/internal/v1/runs/purge','Bearer '+'a'*64)]


@pytest.mark.parametrize('size',[HostPurger.RESPONSE_LIMIT+1,1<<20])
def test_a_reply_longer_than_the_bound_is_a_failure_even_when_it_says_purged(tmp_path,size):
    body=reply('purged',pad=size)
    with loopback(tmp_path,200,body) as (purger,_):
        assert purger(str(uuid.uuid4()))=='failed'


def test_an_unfilled_content_length_is_not_waited_on_beyond_the_bound(tmp_path):
    # The server announces 1 GiB, sends just over the bound and then keeps the connection open: the bounded read
    # returns after RESPONSE_LIMIT+1 bytes instead of buffering or waiting for the rest (the connection timeout is 10 s).
    body=b'{"outcome":"purged","pad":"'+b'x'*HostPurger.RESPONSE_LIMIT
    with loopback(tmp_path,200,body,content_length=1<<30,hold=8) as (purger,_):
        started=time.monotonic()
        assert purger(str(uuid.uuid4()))=='failed'
        assert time.monotonic()-started<5


def test_a_non_200_reply_is_a_failure_whatever_its_body(tmp_path):
    with loopback(tmp_path,500,reply('purged')) as (purger,_):
        assert purger(str(uuid.uuid4()))=='failed'
