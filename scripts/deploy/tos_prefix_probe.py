#!/usr/bin/env python3
"""NX-035 repo2 on TOS: check one backup sub-user's prefix scope without anyone seeing its keys (run sheet run-repo2-tos §1.3).

The owner runs it on the management Mac. The access key pair is read from the macOS Keychain by this process (`security
find-generic-password -s <item> -w`; or two lines on stdin with --credentials-stdin), never from argv, and is never printed.
Requests go to the TOS S3-compatible endpoint (AWS Signature Version 4, service s3) over TLS:

  list_own:<prefix>     ListObjectsV2 under each prefix of the role          expect 200
  list_bucket_root      ListObjectsV2 with an empty prefix                    expect 403 (the policy names prefixes only)
  list_other:<prefix>   ListObjectsV2 under the other role's prefix           expect 403
  list_path_style       the first own prefix with path-style addressing      reported only (restic's default for non-AWS)
  with --write:
  put_own / delete_own  a 0-byte probe object nexloop/.../.nexloop-probe-<uuid> expect 200 / 204
  put_other             the same under the other role's prefix              expect 403

    tos_prefix_probe.py --bucket <TOS_BUCKET> --role data|app [--region cn-shanghai] [--write]
        [--access-key-item nexloop/backup/tos-data-access-key --secret-key-item nexloop/backup/tos-data-secret-key]

Output: one JSON object of check names, prefixes, expected and actual HTTP status codes (no keys, no object names, no bodies). A
request that does not complete is its own failed check with the stage it stopped at (connect, send, response_headers) and the
error type; the following checks still run. Exit 0 every check as expected; 1 a check differs or failed; 2 input refused. Standard library only.
"""
import argparse
import hashlib
import hmac
import http.client
import json
import re
import ssl
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

PREFIXES = {'data': ('nexloop/pgbackrest/', 'nexloop/restic-journal/'), 'app': ('nexloop/restic-files/',)}
ITEMS = {'data': ('nexloop/backup/tos-data-access-key', 'nexloop/backup/tos-data-secret-key'),
         'app': ('nexloop/backup/tos-app-access-key', 'nexloop/backup/tos-app-secret-key')}
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
_BUCKET = re.compile(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]')
_REGION = re.compile(r'[a-z]{2}-[a-z]+(-[a-z0-9]+)*')
_ITEM = re.compile(r'[A-Za-z0-9._/-]{1,128}')


class Refused(ValueError):
    pass


def _hmac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def sign(method, host, path, query, headers, payload_hash, *, access_key, secret_key, region, service='s3', now=None):
    """AWS Signature Version 4 header signing. query: list of (name, value); headers: dict (host and x-amz-* are added)."""
    now = now or datetime.now(UTC)
    amz_date, day = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    headers = {**{k.lower(): v.strip() for k, v in headers.items()}, 'host': host, 'x-amz-date': amz_date, 'x-amz-content-sha256': payload_hash}
    canonical_query = '&'.join(f'{quote(k, safe="-_.~")}={quote(v, safe="-_.~")}' for k, v in sorted(query))
    signed = ';'.join(sorted(headers))
    canonical = '\n'.join([method, quote(path, safe='/-_.~'), canonical_query, ''.join(f'{k}:{headers[k]}\n' for k in sorted(headers)), signed, payload_hash])
    scope = f'{day}/{region}/{service}/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(('AWS4' + secret_key).encode(), day), region), service), 'aws4_request')
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers['authorization'] = f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}'
    return headers


class ProbeFailed(Exception):
    """A request that did not complete: the stage it stopped at (connect, send, response_headers) and the error type."""

    def __init__(self, stage, error):
        super().__init__(stage)
        self.stage, self.error = stage, error


class HttpsTransport:
    """One TLS connection per request. Only the status line and headers are waited for: a body is read only when the response
    declares a small Content-Length (an answer without a length on a kept-alive connection would otherwise be read until the
    timeout, which is how the first write probes of 2026-10-17 ended as a bare TimeoutError). The body is never printed."""

    def __init__(self, timeout=15, connection_factory=None):
        self.timeout, self.context = timeout, ssl.create_default_context()
        # Tests pass a plain-HTTP factory to a local server; the probe itself always speaks TLS to port 443.
        self._connection = connection_factory or (lambda host, timeout: http.client.HTTPSConnection(host, 443, timeout=timeout, context=self.context))

    def request(self, method, host, path, query, headers, body=b''):
        target = path + ('?' + '&'.join(f'{quote(k, safe="-_.~")}={quote(v, safe="-_.~")}' for k, v in query) if query else '')
        connection = self._connection(host, self.timeout)
        stage = 'connect'
        try:
            connection.connect()
            stage = 'send'
            # body as bytes (b'' for the probe object): http.client sends exactly the signed Content-Length, no Expect header.
            connection.request(method, target, body=body, headers=headers)
            stage = 'response_headers'
            response = connection.getresponse()
            if response.length is not None and response.length <= 65536:
                try:
                    response.read()
                except (OSError, http.client.HTTPException):
                    pass  # the status is what counts
            return response.status
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise ProbeFailed(stage, type(error).__name__) from None
        finally:
            connection.close()


class Probe:
    def __init__(self, transport, *, bucket, region, endpoint, access_key, secret_key):
        self.transport, self.bucket, self.region, self.endpoint = transport, bucket, region, endpoint
        self._access, self._secret = access_key, secret_key

    def call(self, method, key='', query=(), *, path_style=False, body=b''):
        host = self.endpoint if path_style else f'{self.bucket}.{self.endpoint}'
        path = (f'/{self.bucket}/' if path_style else '/') + key
        payload = hashlib.sha256(body).hexdigest()
        headers = sign(method, host, path, list(query), {'content-length': str(len(body))} if method == 'PUT' else {}, payload,
                       access_key=self._access, secret_key=self._secret, region=self.region)
        return self.transport.request(method, host, path, list(query), headers, body)

    def list(self, prefix, **kw):
        return self.call('GET', '', [('list-type', '2'), ('max-keys', '1'), ('prefix', prefix)], **kw)


def run(probe, role, *, write=False):
    own, other = PREFIXES[role], PREFIXES['app' if role == 'data' else 'data']
    checks = []

    def check(name, expected, request):
        """One check; a request that does not complete is recorded with its stage and error, and the next checks still run."""
        try:
            status = request()
        except ProbeFailed as failed:
            checks.append({'check': name, 'expected': expected, 'status': None, 'stage': failed.stage, 'error': failed.error, 'ok': False})
            return
        checks.append({'check': name, 'expected': expected, 'status': status, 'ok': expected is None or status == expected})
    for prefix in own:
        check(f'list_own:{prefix}', 200, lambda prefix=prefix: probe.list(prefix))
    check('list_bucket_root', 403, lambda: probe.list(''))
    for prefix in other:
        check(f'list_other:{prefix}', 403, lambda prefix=prefix: probe.list(prefix))
    check(f'list_path_style:{own[0]}', None, lambda: probe.list(own[0], path_style=True))
    if write:
        name = f'.nexloop-probe-{uuid.uuid4().hex}'
        check(f'put_own:{own[0]}', 200, lambda: probe.call('PUT', own[0] + name))
        check(f'delete_own:{own[0]}', 204, lambda: probe.call('DELETE', own[0] + name))  # also after a failed put: harmless
        check(f'put_other:{other[0]}', 403, lambda: probe.call('PUT', other[0] + name))
    return checks


def keychain(item, runner=subprocess.run):
    done = runner(['security', 'find-generic-password', '-s', item, '-w'], capture_output=True, text=True, timeout=30)
    value = done.stdout.strip() if done.returncode == 0 else ''
    if not value:
        raise Refused(f'keychain item unreadable: {item}')
    return value


def main(argv=None, *, stdin=None, transport=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--role', choices=('data', 'app'), required=True)
    parser.add_argument('--region', default='cn-shanghai')
    parser.add_argument('--endpoint', help='default tos-s3-<region>.volces.com')
    parser.add_argument('--access-key-item')
    parser.add_argument('--secret-key-item')
    parser.add_argument('--credentials-stdin', action='store_true', help='two lines on stdin: access key id, secret access key')
    parser.add_argument('--write', action='store_true', help='also put and delete one 0-byte probe object under the own prefix')
    parser.add_argument('--timeout', type=float, default=15.0, help='seconds per request (connect, send and response headers)')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True))
        return code
    try:
        if not _BUCKET.fullmatch(args.bucket) or not _REGION.fullmatch(args.region):
            raise Refused('bucket or region malformed')
        endpoint = args.endpoint or f'tos-s3-{args.region}.volces.com'
        if not re.fullmatch(r'[a-z0-9.-]{4,253}', endpoint):
            raise Refused('endpoint malformed')
        if args.credentials_stdin:
            lines = (stdin or sys.stdin).read(4096).splitlines()
            if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
                raise Refused('two lines expected on stdin')
            access, secret = lines[0].strip(), lines[1].strip()
        else:
            items = (args.access_key_item or ITEMS[args.role][0], args.secret_key_item or ITEMS[args.role][1])
            if not all(_ITEM.fullmatch(i) for i in items):
                raise Refused('keychain item name malformed')
            access, secret = keychain(items[0], runner), keychain(items[1], runner)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    if not 1 <= args.timeout <= 120:
        return emit({'ok': False, 'detail': 'timeout out of range (1-120 s)'}, 2)
    probe = Probe(transport or HttpsTransport(args.timeout), bucket=args.bucket, region=args.region, endpoint=endpoint, access_key=access, secret_key=secret)
    checks = run(probe, args.role, write=args.write)  # every request failure is a check of its own (stage, error)
    ok = all(c['ok'] for c in checks)
    return emit({'ok': ok, 'role': args.role, 'region': args.region, 'checks': checks}, 0 if ok else 1)


if __name__ == '__main__':
    sys.exit(main())
