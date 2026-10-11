#!/usr/bin/env python3
"""NX-032 §3.3 (AT-058): isolation probes from one location, the same script for the simulation (scripts/deploy/netns_sim) and for
the real two-host deployment (slice 5, run by the owner from the management machine, DATA_HOST and APP_HOST).

Standard library only, so it runs wherever python3 is. It only opens connections and speaks just enough protocol to classify the
answer; it sends no business data and changes nothing:
  tcp     connect only → reachable | unreachable
  pg      PostgreSQL SSLRequest, TLS with the given CA/name/client certificate, startup and SCRAM-SHA-256 when a password file is
          given → authenticated | server_refused | auth_failed | client_refused | unreachable
  valkey  Valkey over TLS (or plaintext to the TLS port), optional AUTH, PING → pong | acl_refused | server_refused |
          client_refused | unreachable
The private spec names targets (host, port) and the probes with their expected class. The report carries only the location code,
probe and target codes, the expected and actual class and a reason code — never an address, a path or a secret.

    probe_isolation.py --spec <private spec.json> [--out <report.json>]
Exit 0 every probe as expected, 1 otherwise, 2 spec error.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import struct
import sys
from datetime import datetime, timezone

SCHEMA = 'nexloop-isolation-report/1'
TIMEOUT = 3.0


# Connection ----------------------------------------------------------------------------------------------------------------
def connect(host, port, timeout=TIMEOUT):
    try:
        return socket.create_connection((host, int(port)), timeout=timeout), None
    except socket.gaierror:
        return None, 'unreachable', 'dns'
    except ConnectionRefusedError:
        return None, 'unreachable', 'refused'
    except socket.timeout:
        return None, 'unreachable', 'timeout'
    except OSError as error:
        return None, 'unreachable', {101: 'network_unreachable', 113: 'no_route', 65: 'no_route', 51: 'network_unreachable'}.get(error.errno, 'os_error')


def _open(target):
    out = connect(target['host'], target['port'])
    return (out[0], None) if out[0] else (None, (out[1], out[2]))


def _tls_context(probe):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(probe['ca'])
    if probe.get('cert'):
        ctx.load_cert_chain(probe['cert'], probe['key'])
    return ctx


def _wrap(sock, probe, server_name):
    try:
        return _tls_context(probe).wrap_socket(sock, server_hostname=server_name), None
    except ssl.SSLCertVerificationError:
        return None, ('client_refused', 'certificate_not_verified')
    except (ssl.SSLError, ConnectionResetError, BrokenPipeError) as error:
        return None, ('server_refused', 'tls_handshake_refused')
    except OSError:
        return None, ('server_refused', 'connection_closed')


# PostgreSQL ---------------------------------------------------------------------------------------------------------------
def _pg_read(sock):
    head = b''
    while len(head) < 5:
        chunk = sock.recv(5 - len(head))
        if not chunk:
            raise ConnectionError()
        head += chunk
    kind, length = head[:1], struct.unpack('!I', head[1:])[0]
    body = b''
    while len(body) < length - 4:
        chunk = sock.recv(length - 4 - len(body))
        if not chunk:
            raise ConnectionError()
        body += chunk
    return kind, body


def _pg_error(body):
    fields = {f[:1]: f[1:].decode(errors='replace') for f in body.split(b'\0') if f}
    return fields.get(b'C', 'unknown')


def _pg_refusal(code):
    return ('server_refused' if code == '28000' else 'auth_failed'), 'sqlstate_' + code


def _pg_send(sock, kind, payload):
    sock.sendall(kind + struct.pack('!I', len(payload) + 4) + payload)


def _scram(sock, user, password):
    nonce = base64.b64encode(os.urandom(18)).decode()
    first_bare = f'n=,r={nonce}'
    first = 'n,,' + first_bare
    _pg_send(sock, b'p', b'SCRAM-SHA-256\0' + struct.pack('!I', len(first)) + first.encode())
    kind, body = _pg_read(sock)
    if kind == b'E':
        return _pg_refusal(_pg_error(body))
    server_first = body[4:].decode()
    attrs = dict(item.split('=', 1) for item in server_first.split(','))
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(attrs['s']), int(attrs['i']))
    client_key = hmac.new(salted, b'Client Key', 'sha256').digest()
    final_without_proof = f'c=biws,r={attrs["r"]}'
    auth_message = f'{first_bare},{server_first},{final_without_proof}'.encode()
    signature = hmac.new(hashlib.sha256(client_key).digest(), auth_message, 'sha256').digest()
    proof = base64.b64encode(bytes(a ^ b for a, b in zip(client_key, signature))).decode()
    _pg_send(sock, b'p', f'{final_without_proof},p={proof}'.encode())
    while True:
        kind, body = _pg_read(sock)
        if kind == b'E':
            # clientcert=verify-full checks the certificate's CN against the role after the password: a certificate of another
            # role is refused here, with 28P01 like a wrong password (a spec pairs it with a positive control of the same password).
            return _pg_refusal(_pg_error(body))
        if kind == b'R' and struct.unpack('!I', body[:4])[0] == 0:
            return 'authenticated', 'scram_sha_256'


def probe_pg(probe, target):
    sock, failed = _open(target)
    if failed:
        return failed
    try:
        sock.settimeout(TIMEOUT)
        if probe.get('sslmode') != 'disable':
            sock.sendall(struct.pack('!II', 8, 80877103))
            if sock.recv(1) != b'S':
                return 'server_refused', 'tls_not_offered'
            sock, failed = _wrap(sock, probe, probe.get('server_name') or target['host'])
            if failed:
                return failed
        user, database = probe['user'], probe.get('database', 'postgres')
        _pg_send(sock, b'', struct.pack('!I', 196608) + f'user\0{user}\0database\0{database}\0\0'.encode())
        try:
            kind, body = _pg_read(sock)
        except (ConnectionError, ssl.SSLError, OSError):
            # PostgreSQL refuses a client certificate it cannot verify at the TLS layer when the alert arrives here.
            return 'server_refused', 'connection_closed'
        if kind == b'E':
            return _pg_refusal(_pg_error(body))
        if kind != b'R':
            return 'server_refused', 'unexpected_message'
        if struct.unpack('!I', body[:4])[0] != 10:
            return 'server_refused', 'not_scram'
        if not probe.get('password_file'):
            return 'reached_authentication', 'scram_requested'
        with open(probe['password_file']) as stream:
            password = stream.read().strip()
        return _scram(sock, user, password)
    except (ConnectionError, OSError):
        return 'server_refused', 'connection_closed'
    finally:
        try:
            sock.close()
        except Exception:
            pass


# Valkey -------------------------------------------------------------------------------------------------------------------
def _resp(sock, *args):
    sock.sendall(b''.join([f'*{len(args)}\r\n'.encode()] + [f'${len(a.encode())}\r\n{a}\r\n'.encode() for a in args]))
    line = b''
    while not line.endswith(b'\r\n'):
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError()
        line += chunk
    return line.decode(errors='replace').strip()


def probe_valkey(probe, target):
    sock, failed = _open(target)
    if failed:
        return failed
    try:
        sock.settimeout(TIMEOUT)
        if probe.get('plaintext'):
            try:
                reply = _resp(sock, 'PING')
            except (ConnectionError, OSError):
                return 'server_refused', 'plaintext_closed'
            return ('pong', 'plaintext_answered') if reply == '+PONG' else ('server_refused', 'plaintext_not_answered')
        sock, failed = _wrap(sock, probe, probe.get('server_name') or target['host'])
        if failed:
            return failed
        try:
            if probe.get('user'):
                with open(probe['password_file']) as stream:
                    reply = _resp(sock, 'AUTH', probe['user'], stream.read().strip())
                if not reply.startswith('+OK'):
                    return 'acl_refused', reply.split()[0].lstrip('-').lower()
            reply = _resp(sock, 'PING')
        except (ConnectionError, ssl.SSLError, OSError):
            return 'server_refused', 'connection_closed'
        if reply == '+PONG':
            return 'pong', 'ping'
        return 'acl_refused', reply.split()[0].lstrip('-').lower()
    finally:
        try:
            sock.close()
        except Exception:
            pass


def probe_tcp(probe, target):
    out = connect(target['host'], target['port'])
    if out[0]:
        out[0].close()
        return 'reachable', 'connected'
    return out[1], out[2]


KINDS = {'tcp': probe_tcp, 'pg': probe_pg, 'valkey': probe_valkey}


def run(spec):
    if type(spec) is not dict or type(spec.get('probes')) is not list or type(spec.get('targets')) is not dict or not spec.get('location'):
        raise ValueError('spec invalid')
    results = []
    for probe in spec['probes']:
        actual, reason = KINDS[probe['kind']](probe, spec['targets'][probe['target']])
        results.append({'probe': probe['code'], 'target': probe['target'], 'kind': probe['kind'], 'expected': probe['expect'], 'actual': actual,
                        'reason': reason, 'passed': actual == probe['expect']})
    return {'schema': SCHEMA, 'location': spec['location'], 'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'passed': all(r['passed'] for r in results), 'results': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--spec', required=True)
    parser.add_argument('--out')
    a = parser.parse_args(argv)
    try:
        with open(a.spec) as stream:
            report = run(json.load(stream))
    except (ValueError, KeyError, OSError) as error:
        print(json.dumps({'schema': SCHEMA, 'passed': False, 'error': type(error).__name__}))
        return 2
    text = json.dumps(report, sort_keys=True)
    if a.out:
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text + '\n')
    print(text)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
