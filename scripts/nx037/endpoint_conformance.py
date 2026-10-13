#!/usr/bin/env python3
"""NX-037 gap D: a conformance check an enterprise runs against its own delivery endpoint (the NexLoop service effect HTTP
protocol, `nexloop-service-effect-http-v1`, spoken by nexloop_eios.effect_provider.HttpEffectProvider).

    python scripts/nx037/endpoint_conformance.py --origin https://<ENDPOINT_HOST>:<PORT> --credential-file <PRIVATE_FILE> \\
        [--ca-file <CA_FILE>] [--connect-address <IP>] [--timeout 2] [--parameters-file <JSON_FILE>] --out <REPORT_FILE>

Checks (each one line in the report: name, ok, code, elapsed_ms; never a body, header value or credential):
- credential_format: the credential file is private (0600, owned, regular) and is 16–4096 characters of [A-Za-z0-9._~-];
- post_new:        POST /v1/effects answers 200 or 202 with application/json, exactly the fields intent_id, payload_digest, state,
                   provider_reference; echoes the intent id and the payload digest; state accepted or fulfilled; a well-formed
                   provider reference; within the deadline;
- post_replay:     the same POST with the same Idempotency-Key answers 200/202 with the same provider reference (idempotent);
- post_conflict:   the same intent with a different payload answers 409 and the original effect is kept (GET still returns the
                   original payload digest);
- get_found:       GET /v1/effects/<intent> answers 200 with the same provider reference and digest;
- get_not_found:   GET of an intent the endpoint never saw answers 404, state not_found, payload_digest and provider_reference null;
- client_end_to_end: NexLoop's own client (HttpEffectProvider) dispatches and queries a fresh intent successfully. It verifies
                   TLS against the given CA (or the system store) with the origin host name as SNI, refuses redirects and
                   enforces the absolute deadline exactly as the effect worker does.

The check creates real effects on the endpoint (three intents, marked by the parameters below). Run it against the endpoint's
test mode, or give --parameters-file with a payload the enterprise accepts as a no-op. Exit: 0 all checks passed; 1 a check
failed; 2 input error (configuration or credential file). The report holds only codes and numbers; keep it private.
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))

FIELDS = {'intent_id', 'payload_digest', 'state', 'provider_reference'}
REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}')
CREDENTIAL = re.compile(r'[A-Za-z0-9._~-]{16,4096}')
MAX_BODY = 16384


class InputError(ValueError):
    pass


def canonical(value):
    from nexloop_eios.postgres_artifacts import canonical_payload
    return canonical_payload(value)


def digest(parameters):
    return hashlib.sha256(canonical(parameters).encode()).hexdigest()


class Endpoint:
    """Raw protocol calls with the effect worker's transport rules (numeric connect address, SNI = origin host, TLS ≥ 1.2,
    absolute deadline). Returns (status, content_type, parsed_json_or_None, elapsed_ms) or raises with a code."""

    def __init__(self, configuration):
        self.config = configuration
        self.origin = urlsplit(configuration.origin)
        self.address = str(ipaddress.ip_address(configuration.connect_address or self.origin.hostname))
        self.credential = None
        if configuration.credential_file is not None:
            from nexloop_eios.private_configuration import read_private_text
            self.credential = read_private_text(configuration.credential_file, maximum=4096)
        self.ca = None
        if configuration.ca_file is not None:
            from nexloop_eios.private_configuration import read_private_text
            self.ca = read_private_text(configuration.ca_file, maximum=32768)

    def call(self, method, path, body=None, idempotency_key=None):
        started = time.monotonic()
        deadline = started + self.config.timeout
        port = self.origin.port or (80 if self.config.test_loopback_http else 443)
        headers = {'Accept': 'application/json'}
        payload = None
        if body is not None:
            payload = canonical(body).encode()
            headers.update({'Content-Type': 'application/json', 'Content-Length': str(len(payload)), 'Idempotency-Key': idempotency_key})
        if self.credential is not None:
            headers['Authorization'] = 'Bearer ' + self.credential
        if self.config.test_loopback_http:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=self.config.timeout)
        else:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if self.ca is not None:
                context.load_verify_locations(cadata=self.ca)
            connection = http.client.HTTPSConnection(self.origin.hostname, port, timeout=self.config.timeout, context=context)
        address = self.address

        def connect_numeric(unused_address, unused_timeout, source_address=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            return socket.create_connection((address, port), timeout=remaining)
        connection._create_connection = connect_numeric
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_BODY + 1)
            elapsed = (time.monotonic() - started) * 1000
            if time.monotonic() > deadline:
                raise TimeoutError()
            content_type = (response.getheader('Content-Type') or '').split(';')[0]
            value = None
            if len(raw) <= MAX_BODY and content_type == 'application/json':
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = None
            return response.status, content_type, value, round(elapsed, 1), len(raw)
        except ssl.SSLError:
            raise CheckFailed('tls_failed', started) from None
        except (TimeoutError, socket.timeout):
            raise CheckFailed('deadline_exceeded', started) from None
        except (OSError, http.client.HTTPException):
            raise CheckFailed('unreachable', started) from None
        finally:
            connection.close()


class CheckFailed(Exception):
    def __init__(self, code, started):
        super().__init__(code)
        self.code, self.elapsed = code, round((time.monotonic() - started) * 1000, 1)


def _receipt_problem(status, content_type, value, size, *, intent, payload_digest, allowed_status, states):
    if status not in allowed_status:
        return 'unexpected_status'
    if content_type != 'application/json':
        return 'content_type_not_json'
    if size > MAX_BODY:
        return 'response_too_large'
    if type(value) is not dict or set(value) != FIELDS:
        return 'fields_invalid'
    if value['intent_id'] != intent:
        return 'intent_not_echoed'
    if value['state'] not in states:
        return 'state_invalid'
    if value['state'] == 'not_found':
        if value['payload_digest'] is not None or value['provider_reference'] is not None:
            return 'not_found_carries_values'
        return None
    if value['payload_digest'] != payload_digest:
        return 'payload_digest_not_echoed'
    if type(value['provider_reference']) is not str or not REFERENCE.fullmatch(value['provider_reference']):
        return 'provider_reference_invalid'
    return None


def run(configuration, *, parameters=None):
    """Run every check in order; a check that cannot run because an earlier one failed is reported as `skipped`."""
    checks = []

    def record(name, ok, code=None, elapsed=None):
        checks.append({'name': name, 'ok': bool(ok), 'code': code, 'elapsed_ms': elapsed})

    if configuration.credential_file is not None:
        try:
            from nexloop_eios.private_configuration import read_private_text
            credential = read_private_text(configuration.credential_file, maximum=4096)
        except (OSError, ValueError, UnicodeDecodeError):
            raise InputError('credential file must be a private regular file') from None
        if not CREDENTIAL.fullmatch(credential):
            record('credential_format', False, 'credential_charset_or_length')
            return report(configuration, checks)
        record('credential_format', True)
    run_id = str(uuid.uuid4())
    parameters = parameters if parameters is not None else {'nexloop_conformance_check': run_id}
    if type(parameters) is not dict:
        raise InputError('parameters must be a JSON object')
    payload = digest(parameters)
    endpoint = Endpoint(configuration)
    intent = str(uuid.uuid4())
    body = {'intent_id': intent, 'payload_digest': payload, 'parameters': parameters}
    reference = None

    def attempt(name, method, path, request_body, check):
        try:
            status, content_type, value, elapsed, size = endpoint.call(method, path, request_body, intent if request_body else None)
        except CheckFailed as failure:
            record(name, False, failure.code, failure.elapsed)
            return None
        problem = check(status, content_type, value, size)
        if problem is None and elapsed > configuration.timeout * 1000:
            problem = 'deadline_exceeded'
        record(name, problem is None, problem, elapsed)
        return value if problem is None else None

    first = attempt('post_new', 'POST', '/v1/effects', body, lambda s, c, v, n: _receipt_problem(
        s, c, v, n, intent=intent, payload_digest=payload, allowed_status={200, 202}, states={'accepted', 'fulfilled'}))
    if first is not None:
        reference = first['provider_reference']
        attempt('post_replay', 'POST', '/v1/effects', body, lambda s, c, v, n: _receipt_problem(
            s, c, v, n, intent=intent, payload_digest=payload, allowed_status={200, 202}, states={'accepted', 'fulfilled'})
            or (None if v['provider_reference'] == reference else 'replay_not_idempotent'))
        other = dict(parameters, nexloop_conformance_conflict=True)
        conflict = {'intent_id': intent, 'payload_digest': digest(other), 'parameters': other}
        attempt('post_conflict', 'POST', '/v1/effects', conflict, lambda s, c, v, n: None if s == 409 else 'conflict_not_409')
        attempt('get_found', 'GET', '/v1/effects/' + intent, None, lambda s, c, v, n: _receipt_problem(
            s, c, v, n, intent=intent, payload_digest=payload, allowed_status={200}, states={'accepted', 'fulfilled'})
            or (None if v['provider_reference'] == reference else 'original_not_kept'))
    else:
        for name in ('post_replay', 'post_conflict', 'get_found'):
            record(name, False, 'skipped')
    absent = str(uuid.uuid4())
    attempt('get_not_found', 'GET', '/v1/effects/' + absent, None, lambda s, c, v, n: _receipt_problem(
        s, c, v, n, intent=absent, payload_digest=None, allowed_status={404}, states={'not_found'}))
    # NexLoop's own client, exactly as the effect worker uses it.
    from nexloop_eios.effect_provider import HttpEffectProvider
    client = HttpEffectProvider(configuration)
    fresh = str(uuid.uuid4())
    started = time.monotonic()
    try:
        receipt = client.dispatch(intent_id=fresh, payload_digest=payload, parameters=parameters)
        queried = client.query(intent_id=fresh, payload_digest=payload)
        ok = queried.provider_reference == receipt.provider_reference
        record('client_end_to_end', ok, None if ok else 'query_reference_differs', round((time.monotonic() - started) * 1000, 1))
    except Exception as error:
        record('client_end_to_end', False, 'client_' + type(error).__name__, round((time.monotonic() - started) * 1000, 1))
    return report(configuration, checks)


def report(configuration, checks):
    return {'schema': 'nexloop.nx037-endpoint-conformance.v1', 'protocol': 'nexloop-service-effect-http-v1',
            'deadline_seconds': configuration.timeout, 'ok': all(c['ok'] for c in checks) and bool(checks), 'checks': checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description='NX-037: conformance check of an enterprise delivery endpoint')
    parser.add_argument('--origin', required=True)
    parser.add_argument('--credential-file')
    parser.add_argument('--ca-file')
    parser.add_argument('--connect-address')
    parser.add_argument('--timeout', type=float, default=2.0)
    parser.add_argument('--parameters-file')
    parser.add_argument('--test-loopback-http', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    from nexloop_eios.effect_provider import EffectProviderConfiguration
    try:
        out = Path(args.out)
        if out.exists():
            raise InputError('report file exists')
        configuration = EffectProviderConfiguration(args.origin, credential_file=args.credential_file, ca_file=args.ca_file,
                                                    timeout=args.timeout, test_loopback_http=args.test_loopback_http,
                                                    connect_address=args.connect_address)
        parameters = json.loads(Path(args.parameters_file).read_text(encoding='utf-8')) if args.parameters_file else None
        result = run(configuration, parameters=parameters)
    except (InputError, ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error': 'input', 'reason': type(error).__name__}))
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    out.chmod(0o600)
    print(json.dumps({'ok': result['ok'], 'failed': [c['name'] + ':' + str(c['code']) for c in result['checks'] if not c['ok']]}))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
