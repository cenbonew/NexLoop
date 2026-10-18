"""scripts/deploy/tos_prefix_probe.py (NX-035 repo2 §1.3): SigV4 against the published AWS example, the checks a prefix-scoped
sub-user must pass, and that keys stay out of argv and output."""
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import tos_prefix_probe as P  # noqa: E402

# The AWS Signature Version 4 example of the S3 documentation ("GET Object" with a Range header): its published example key pair
# and expected signature (an independent reference, not a credential).
AWS_EXAMPLE_ACCESS = 'AKIAIOSFODNN7EXAMPLE'
AWS_EXAMPLE_SECRET = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'


def test_sigv4_matches_the_published_s3_example():
    headers = P.sign('GET', 'examplebucket.s3.amazonaws.com', '/test.txt', [], {'Range': 'bytes=0-9'}, P.EMPTY_SHA256,
                     access_key=AWS_EXAMPLE_ACCESS, secret_key=AWS_EXAMPLE_SECRET, region='us-east-1', now=datetime(2013, 5, 24, tzinfo=UTC))
    assert headers['authorization'] == ('AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, '
                                        'SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, '
                                        'Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41')


class Fake:
    """A prefix-scoped TOS: 200/204 inside the allowed prefixes, 403 elsewhere; records hosts and paths, never answers bodies."""

    def __init__(self, allowed, path_style_status=200):
        self.allowed, self.path_style_status, self.requests = allowed, path_style_status, []

    def request(self, method, host, path, query, headers, body=b''):
        self.requests.append((method, host, path, dict(query), headers))
        if not host.startswith('synthetic-bucket.'):
            return self.path_style_status
        target = dict(query).get('prefix', '') if method == 'GET' else path.lstrip('/')
        inside = any(target.startswith(p) for p in self.allowed) and target != ''
        return (200 if method != 'DELETE' else 204) if inside else 403


def test_a_correctly_scoped_sub_user_passes_and_a_wide_one_fails(capsys):
    fake = Fake(P.PREFIXES['data'])
    stdin = io.StringIO('synthetic-access-id\nsynthetic-secret-value\n')
    code = P.main(['--bucket', 'synthetic-bucket', '--role', 'data', '--credentials-stdin', '--write'], stdin=stdin, transport=fake)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out['ok'] and [c['check'] for c in out['checks']] == [
        'list_own:nexloop/pgbackrest/', 'list_own:nexloop/restic-journal/', 'list_bucket_root', 'list_other:nexloop/restic-files/',
        'list_path_style:nexloop/pgbackrest/', 'put_own:nexloop/pgbackrest/', 'delete_own:nexloop/pgbackrest/', 'put_other:nexloop/restic-files/']
    text = json.dumps(out)
    assert 'synthetic-secret-value' not in text and 'synthetic-access-id' not in text and '.nexloop-probe-' not in text
    assert all(h['authorization'].startswith('AWS4-HMAC-SHA256 Credential=synthetic-access-id/') for *_, h in fake.requests)
    assert fake.requests[0][1] == 'synthetic-bucket.tos-s3-cn-shanghai.volces.com' and fake.requests[4][1] == 'tos-s3-cn-shanghai.volces.com'
    # A sub-user allowed on the whole bucket: the root listing and the other role's prefix answer 200, exit 1.
    wide = Fake(('nexloop/', ''))
    wide.request = lambda method, host, path, query, headers, body=b'': 200
    assert P.main(['--bucket', 'synthetic-bucket', '--role', 'app', '--credentials-stdin'], stdin=io.StringIO('a\nb\n'), transport=wide) == 1
    failed = [c['check'] for c in json.loads(capsys.readouterr().out)['checks'] if not c['ok']]
    assert failed == ['list_bucket_root', 'list_other:nexloop/pgbackrest/', 'list_other:nexloop/restic-journal/']


def test_keys_come_from_the_keychain_by_item_name_never_from_argv(capsys):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return type('Done', (), {'returncode': 0, 'stdout': 'value-of-' + argv[3].rsplit('/', 1)[1] + '\n'})()
    assert P.main(['--bucket', 'synthetic-bucket', '--role', 'app'], transport=Fake(P.PREFIXES['app']), runner=runner) == 0
    assert calls == [['security', 'find-generic-password', '-s', 'nexloop/backup/tos-app-access-key', '-w'],
                     ['security', 'find-generic-password', '-s', 'nexloop/backup/tos-app-secret-key', '-w']]
    assert 'value-of-' not in capsys.readouterr().out
    missing = lambda argv, **kw: type('Done', (), {'returncode': 44, 'stdout': ''})()  # noqa: E731
    assert P.main(['--bucket', 'synthetic-bucket', '--role', 'data'], transport=Fake(()), runner=missing) == 2
    assert json.loads(capsys.readouterr().out)['detail'] == 'keychain item unreadable: nexloop/backup/tos-data-access-key'
    for argv in (['--bucket', 'Bad_Bucket', '--role', 'data'], ['--bucket', 'synthetic-bucket', '--role', 'data', '--region', 'x'],
                 ['--bucket', 'synthetic-bucket', '--role', 'data', '--access-key-item', 'a b']):
        assert P.main(argv, transport=Fake(()), runner=runner) == 2
        capsys.readouterr()


def _server(answer):
    """A local HTTP server over a raw socket: answer(method, path, headers) -> bytes to send, or None to stay silent."""
    import socket
    import threading
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    seen = []

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            head = data.split(b'\r\n\r\n', 1)[0].decode()
            method, path = head.split(' ')[:2]
            headers = {k.lower(): v.strip() for k, _, v in (line.partition(':') for line in head.split('\r\n')[1:])}
            seen.append((method, path, headers))
            reply = answer(method, path, headers)
            if reply is not None:
                conn.sendall(reply)
            threading.Thread(target=lambda c=conn: (threading.Event().wait(5), c.close()), daemon=True).start()  # kept open
    threading.Thread(target=serve, daemon=True).start()
    return listener, seen


def test_a_put_answered_without_a_length_on_a_kept_alive_connection_returns_its_status_at_once():
    """2026-10-17 stage: --write ended as a bare TimeoutError. A 200 without Content-Length (connection kept open) was read to EOF;
    now only the status and headers are awaited. The PUT carries Content-Length 0, no Expect, the signed empty-payload hash."""
    import http.client
    import time
    listener, seen = _server(lambda method, path, headers: b'HTTP/1.1 200 OK\r\nx-tos-request-id: synthetic\r\n\r\n')
    port = listener.getsockname()[1]
    transport = P.HttpsTransport(timeout=3, connection_factory=lambda host, timeout: http.client.HTTPConnection('127.0.0.1', port, timeout=timeout))
    probe = P.Probe(transport, bucket='synthetic-bucket', region='cn-shanghai', endpoint='tos-s3-cn-shanghai.volces.com', access_key='a', secret_key='b')
    started = time.monotonic()
    assert probe.call('PUT', 'nexloop/pgbackrest/.nexloop-probe-x') == 200
    assert time.monotonic() - started < 2
    method, path, headers = seen[0]
    assert (method, path, headers['content-length'], headers['x-amz-content-sha256']) == ('PUT', '/nexloop/pgbackrest/.nexloop-probe-x', '0', P.EMPTY_SHA256)
    assert 'expect' not in headers and 'content-length' in headers['authorization']
    listener.close()


def test_a_request_without_an_answer_is_its_own_failed_check_with_its_stage_and_the_rest_still_run(capsys):
    import http.client
    listener, _ = _server(lambda method, path, headers: None if method == 'PUT' else b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
    port = listener.getsockname()[1]
    transport = P.HttpsTransport(timeout=1, connection_factory=lambda host, timeout: http.client.HTTPConnection('127.0.0.1', port, timeout=timeout))
    code = P.main(['--bucket', 'synthetic-bucket', '--role', 'app', '--credentials-stdin', '--write'], stdin=io.StringIO('a\nb\n'), transport=transport)
    out = json.loads(capsys.readouterr().out)
    by = {c['check']: c for c in out['checks']}
    assert code == 1 and not out['ok'] and len(out["checks"]) == 8  # every check reported, none lost to the timeout
    assert by['put_own:nexloop/restic-files/'] == {'check': 'put_own:nexloop/restic-files/', 'expected': 200, 'status': None,
                                                   'stage': 'response_headers', 'error': 'TimeoutError', 'ok': False}
    assert by['delete_own:nexloop/restic-files/']['status'] == 403 and by['list_bucket_root'] == {'check': 'list_bucket_root', 'expected': 403, 'status': 403, 'ok': True}
    assert by['put_other:nexloop/pgbackrest/']['stage'] == 'response_headers'
    listener.close()


def test_cleanup_deletes_only_leftover_probe_objects_and_prints_counts(capsys):
    # Activation run sheet (2026-10-18): probe objects left under nexloop/restic-files/ by earlier --write runs.
    leftover = [f'nexloop/restic-files/.nexloop-probe-{c * 32}' for c in 'ab']
    listed = leftover + ['nexloop/restic-files/.nexloop-probe-not-hex', 'nexloop/restic-files/data/keep-me']
    deleted = []

    class Store(Fake):
        def fetch(self, method, host, path, query, headers):
            assert method == 'GET' and dict(query)['prefix'] == 'nexloop/restic-files/.nexloop-probe-' and host.startswith('synthetic-bucket.')
            return 200, ''.join(f'<Contents><Key>{k}</Key></Contents>' for k in listed).encode()

        def request(self, method, host, path, query, headers, body=b''):
            assert method == 'DELETE'
            deleted.append(path.lstrip('/'))
            return 204
    code = P.main(['--bucket', 'synthetic-bucket', '--role', 'app', '--credentials-stdin', '--cleanup-probes'], stdin=io.StringIO('a\nb\n'), transport=Store(()))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and deleted == leftover
    assert out['cleanup'] == [{'prefix': 'nexloop/restic-files/', 'found': 2, 'deleted': 2, 'failed': 0, 'list_status': 200}]
    assert '.nexloop-probe-' + 'a' * 32 not in json.dumps(out)
    assert P.main(['--bucket', 'synthetic-bucket', '--role', 'app', '--write', '--cleanup-probes', '--credentials-stdin'], stdin=io.StringIO('a\nb\n')) == 2
