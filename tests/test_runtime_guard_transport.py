"""Actual HTTPS transport tests; fake workers prove transport only.

The final case separately uses the real restricted PG activation authority.
All transport keys and error sentinels are disposable synthetic material.
"""
import json
import secrets
import socket
import threading
import time

import httpx
import pytest

from test_agent_host import files, trusted_tls
from test_runtime_host_admission import guard_server
from test_runtime_authority import authority
from test_runtime_activation import synthetic_credentials, activation

PATH = '/internal/v1/runtime/authorize'
SENTINEL = 'synthetic-runtime-guard-error-secret-sentinel'
BODY = {'activation_ref': 'activation_transport_test_only',
        'command': {'run_id': 'synthetic-transport-run'}, 'operation': 'model'}


def test_connection_limit_applies_before_tls_and_releases_slots(tmp_path):
    from nexloop_eios.runtime_control import create_runtime_guard_server
    _,key=files(tmp_path);worker=TransportOnlyWorker()
    server=create_runtime_guard_server(worker,port=0,key_file=key,
        certificate_file=tmp_path/'host-cert.pem',tls_key_file=tmp_path/'host-key.pem')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    sockets=[]
    try:
        # Actual idle TCP peers never send a TLS ClientHello. Their connection
        # threads must consume the bound before any HTTP POST permission slot.
        for _ in range(8):sockets.append(socket.create_connection(('127.0.0.1',server.server_port),timeout=1))
        deadline=time.monotonic()+2
        while server.connection_slots._value!=0:
            assert time.monotonic()<deadline;time.sleep(.01)
        with socket.create_connection(('127.0.0.1',server.server_port),timeout=1) as extra:
            extra.settimeout(.5);assert extra.recv(1)==b''
        assert worker.calls==0
        for connection in sockets:connection.close()
        sockets.clear();deadline=time.monotonic()+2
        while server.connection_slots._value!=8:
            assert time.monotonic()<deadline;time.sleep(.01)
        with client(key) as http:
            response=http.post(f'https://127.0.0.1:{server.server_port}{PATH}',
                headers={'Authorization':'Bearer '+key.read_text()},json=BODY)
            assert response.status_code==200 and worker.calls==1
    finally:
        for connection in sockets:connection.close()
        server.shutdown();server.server_close();thread.join(3)
        assert not thread.is_alive()


class TransportOnlyWorker:
    """Deliberately not an EIOS authority implementation."""
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def authorize_runtime_activation(self, **parameters):
        self.calls += 1
        if self.fail:
            raise RuntimeError(SENTINEL)
        return {'authorized': True, 'run_id': parameters['command']['run_id'], 'ever_execution_authorized':True,
                '_run_digest': SENTINEL, 'token': SENTINEL, 'diagnostics': SENTINEL}


def client(key):
    return httpx.Client(verify=trusted_tls(key), trust_env=False, timeout=3)


def raw_request(key, port, headers, content=b'{}', path=PATH):
    request = f'POST {path} HTTP/1.1\r\n'.encode()
    request += b''.join(f'{name}: {value}\r\n'.encode() for name, value in headers)
    request += b'Connection: close\r\n\r\n' + content
    with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
        with trusted_tls(key).wrap_socket(connection, server_hostname='127.0.0.1') as tls:
            tls.sendall(request)
            response = b''
            while True:
                chunk = tls.recv(16384)
                if not chunk:
                    break
                response += chunk
                assert len(response) < 32768
    head, body = response.split(b'\r\n\r\n', 1)
    return int(head.split(b' ', 2)[1]), json.loads(body)


def test_transport_key_rotation_and_public_response_allowlist(tmp_path, capsys):
    _, key = files(tmp_path)
    worker = TransportOnlyWorker()
    with guard_server(worker, tmp_path, key) as port, client(key) as http:
        url = f'https://127.0.0.1:{port}{PATH}'
        old = key.read_text()
        assert http.post(url, headers={'Authorization': 'Bearer ' + secrets.token_hex(32)}, json=BODY).status_code == 401
        response = http.post(url, headers={'Authorization': 'Bearer ' + old}, json=BODY)
        assert response.status_code == 200
        assert response.json() == {'authorized': True, 'run_id': BODY['command']['run_id'], 'ever_execution_authorized':True}
        rotated = key.parent / 'transport-rotated-key'
        rotated.write_text(secrets.token_hex(32))
        rotated.chmod(0o600)
        rotated.replace(key)
        assert http.post(url, headers={'Authorization': 'Bearer ' + old}, json=BODY).status_code == 401
        assert http.post(url, headers={'Authorization': 'Bearer ' + key.read_text()}, json=BODY).status_code == 200
        key.chmod(0o644)
        assert http.post(url, headers={'Authorization': 'Bearer ' + key.read_text()}, json=BODY).status_code == 503
        key.chmod(0o600)
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert worker.calls == 2


@pytest.mark.parametrize('change', ['origin', 'fetch-site', 'host', 'duplicate-auth', 'path'])
def test_transport_rejects_browser_identity_and_ambiguous_auth(tmp_path, change):
    _, key = files(tmp_path)
    worker = TransportOnlyWorker()
    with guard_server(worker, tmp_path, key) as port:
        content = json.dumps(BODY).encode()
        headers = [('Host', f'127.0.0.1:{port}'), ('Authorization', 'Bearer ' + key.read_text()),
                   ('Content-Type', 'application/json'), ('Content-Length', str(len(content)))]
        path = PATH
        if change == 'origin':
            headers.append(('Origin', 'https://untrusted.invalid'))
        elif change == 'fetch-site':
            headers.append(('Sec-Fetch-Site', 'same-origin'))
        elif change == 'host':
            headers[0] = ('Host', 'untrusted.invalid')
        elif change == 'duplicate-auth':
            headers.append(('Authorization', 'Bearer ' + key.read_text()))
        else:
            path += '?redirect=/public'
        status, result = raw_request(key, port, headers, content, path)
        assert status == (404 if change == 'path' else 401)
        assert result == {'authorized': False}
        assert worker.calls == 0


@pytest.mark.parametrize('change', ['oversized', 'zero', 'duplicate-length', 'chunked', 'encoding', 'content-type', 'non-json', 'non-object', 'extra-field'])
def test_transport_rejects_invalid_framing_and_body_without_worker_call(tmp_path, change):
    _, key = files(tmp_path)
    worker = TransportOnlyWorker()
    with guard_server(worker, tmp_path, key) as port:
        content = json.dumps(BODY).encode()
        headers = [('Host', f'127.0.0.1:{port}'), ('Authorization', 'Bearer ' + key.read_text()),
                   ('Content-Type', 'application/json'), ('Content-Length', str(len(content)))]
        expected = 400
        if change in ('oversized', 'zero'):
            headers[-1] = ('Content-Length', '262145' if change == 'oversized' else '0')
            expected = 413
        elif change == 'duplicate-length':
            headers.append(('Content-Length', str(len(content))))
        elif change == 'chunked':
            headers.append(('Transfer-Encoding', 'chunked'))
        elif change == 'encoding':
            headers.append(('Content-Encoding', 'gzip'))
        elif change == 'content-type':
            headers[2] = ('Content-Type', 'text/plain')
        elif change == 'non-json':
            content = b'not-json'
            headers[-1] = ('Content-Length', str(len(content)))
            expected = 400  # Invalid JSON is not a transient authority failure.
        elif change == 'non-object':
            content = b'[]'
            headers[-1] = ('Content-Length', str(len(content)))
        else:
            content = json.dumps({**BODY, 'raw_token': SENTINEL}).encode()
            headers[-1] = ('Content-Length', str(len(content)))
        status, result = raw_request(key, port, headers, content)
        assert status == expected
        assert result == {'authorized': False}
        assert worker.calls == 0


def test_transport_worker_error_does_not_leak_to_response_or_logs(tmp_path, capsys):
    _, key = files(tmp_path)
    worker = TransportOnlyWorker(fail=True)
    with guard_server(worker, tmp_path, key) as port, client(key) as http:
        response = http.post(f'https://127.0.0.1:{port}{PATH}',
            headers={'Authorization': 'Bearer ' + key.read_text()}, json=BODY)
        assert response.status_code == 503
        assert response.json() == {'authorized': False}
        assert SENTINEL not in response.text
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert worker.calls == 1


def test_actual_pg_activation_guard_rechecks_current_authority(authority, synthetic_credentials, admin, tmp_path):
    result = activation(authority, synthetic_credentials)
    _, command, worker, issued, invocation = authority
    _, key = files(tmp_path)
    body = {'activation_ref': result['activation_ref'], 'command': command, 'operation': 'model'}
    with guard_server(worker, tmp_path, key) as port, client(key) as http:
        url = f'https://127.0.0.1:{port}{PATH}'
        headers = {'Authorization': 'Bearer ' + key.read_text()}
        response = http.post(url, headers=headers, json=body)
        assert response.status_code == 200
        assert response.json() == {'authorized': True, 'run_id': issued.run_id, 'ever_execution_authorized':True}
        assert issued.token not in response.text
        admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{status}','\"revoked\"') where fact_kind='agent_release' and entity_key=%s", ([invocation.release_id],))
        denied = http.post(url, headers=headers, json=body)
        assert denied.status_code == 503
        assert denied.json() == {'authorized': False}
        assert issued.token not in denied.text
