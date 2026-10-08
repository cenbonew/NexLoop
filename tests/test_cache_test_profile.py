import socket
import ssl
import threading
import time
import pytest
from nexloop_eios.cache_test_profile import _command, _context


def test_resp_bulk_handles_fragmented_tls_style_reads():
    client,server=socket.socketpair();client.settimeout(2);server.settimeout(2)
    errors=[]
    def respond():
        try:
            assert server.recv(1024).startswith(b'*2\r\n')
            server.sendall(b'$5\r\nh');time.sleep(.03);server.sendall(b'ello\r\n')
        except Exception as e:errors.append(e)
        finally:server.close()
    thread=threading.Thread(target=respond);thread.start()
    try:
        with client:assert _command(client,'GET','nexloop:test:cache:x')==(b'$',b'hello')
    finally:thread.join(timeout=3)
    assert not thread.is_alive() and not errors


def test_explicit_cache_tls_context_has_no_ambient_session_key_log(monkeypatch,tmp_path):
    output=tmp_path/'session-key-log';monkeypatch.setenv('SSLKEYLOGFILE',str(output))
    context=_context()
    assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
    assert context.keylog_filename is None and not output.exists()
