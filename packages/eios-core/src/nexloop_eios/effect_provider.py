"""Trusted fixed-origin service provider transport; not an Action authorization port.

Only a governed worker may call this adapter after durable dispatch admission.
Transport uncertainty never authorizes retry; reconcile queries the same intent.
No endpoint or credential is accepted from Run/tool parameters.
"""
from dataclasses import dataclass, field
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text


class EffectProviderUnknown(RuntimeError):
    def __init__(self):
        super().__init__('effect_provider_result_unknown')


class EffectProviderQueryUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('effect_provider_query_unavailable')


@dataclass(frozen=True)
class EffectProviderConfiguration:
    origin: str
    credential_file: str | None = field(default=None, repr=False)
    ca_file: str | None = field(default=None, repr=False)
    timeout: float = 2.0
    test_loopback_http: bool = False
    connect_address: str | None = None

    def __post_init__(self):
        try:
            parsed = urlsplit(self.origin)
            port = parsed.port
            if (parsed.username is not None or parsed.password is not None
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                or not parsed.hostname or port is not None and not 1 <= port <= 65535
                or type(self.test_loopback_http) is not bool
                or type(self.timeout) not in {int, float} or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 30):
                raise ValueError()
            if self.test_loopback_http:
                if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or port is None:
                    raise ValueError()
                if self.credential_file is not None:
                    raise ValueError()
            elif parsed.scheme != 'https' or self.credential_file is None:
                raise ValueError()
            address = ipaddress.ip_address(self.connect_address or parsed.hostname)
            if self.test_loopback_http and str(address) != '127.0.0.1':
                raise ValueError()
        except Exception:
            raise ValueError('effect_provider_configuration_invalid') from None


@dataclass(frozen=True)
class EffectProviderReceipt:
    intent_id: str
    payload_digest: str | None
    state: str
    provider_reference: str | None


def _intent(value):
    if type(value) is not str:
        raise ValueError('effect_intent_invalid')
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except Exception:
        raise ValueError('effect_intent_invalid') from None
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('effect_payload_digest_invalid')
    return value


class HttpEffectProvider:
    """HTTPS, no proxy/redirect, bounded framing and absolute response deadline.

    This specific service protocol advertises idempotent dispatch and query by
    intent. Its query 'not_found' is only provider evidence, never permission to
    resend; the durable governed worker must decide safe retry independently.
    """
    def __init__(self, configuration):
        if type(configuration) is not EffectProviderConfiguration:
            raise ValueError('effect_provider_configuration_invalid')
        self._configuration = configuration
        self._profile_digest = None
        self._ca_material = None

    @property
    def configuration(self):
        return self._configuration

    @property
    def profile_digest(self):
        # Credentials rotate independently. Endpoint, numeric routing and trust
        # material identify the provider queried for an already durable attempt.
        # Snapshot CA bytes once so the actual request uses exactly this profile.
        if self._profile_digest is None:
            config=self.configuration;origin=urlsplit(config.origin)
            self._ca_material=(None if config.ca_file is None else read_private_text(config.ca_file,maximum=32768))
            identity={'protocol':'nexloop-service-effect-http-v1','origin':f'{origin.scheme}://{origin.hostname}:{origin.port or 443}',
                'connect_address':str(ipaddress.ip_address(config.connect_address or origin.hostname)),
                'trust_digest':'system' if self._ca_material is None else hashlib.sha256(self._ca_material.encode()).hexdigest(),
                'test_loopback_http':config.test_loopback_http}
            self._profile_digest=hashlib.sha256(canonical_payload(identity).encode()).hexdigest()
        return self._profile_digest

    def dispatch(self, *, intent_id, payload_digest, parameters):
        _intent(intent_id); _digest(payload_digest)
        if type(parameters) is not dict:
            raise ValueError('effect_parameters_invalid')
        encoded = canonical_payload(parameters).encode()
        if len(encoded) > 131072 or hashlib.sha256(encoded).hexdigest() != payload_digest:
            raise ValueError('effect_payload_digest_invalid')
        # Invalid local payload is rejected before opening a provider connection.
        body = canonical_payload({'intent_id': intent_id, 'payload_digest': payload_digest,
                                  'parameters': parameters}).encode()
        return self._request('POST', '/v1/effects', body, intent_id, payload_digest)

    def query(self, *, intent_id, payload_digest):
        _intent(intent_id); _digest(payload_digest)
        return self._request('GET', '/v1/effects/' + intent_id, None, intent_id, payload_digest)

    def _request(self, method, path, body, intent_id, payload_digest):
        connection = None
        transport_socket = None
        timer = None
        deadline = time.monotonic() + self.configuration.timeout
        error = EffectProviderUnknown if method == 'POST' else EffectProviderQueryUnavailable
        try:
            self.profile_digest
            origin = urlsplit(self.configuration.origin)
            headers = {'Accept': 'application/json'}
            if body is not None:
                headers.update({'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                                'Idempotency-Key': intent_id})
            if self.configuration.credential_file is not None:
                credential = read_private_text(self.configuration.credential_file, maximum=4096)
                if re.fullmatch('[A-Za-z0-9._~-]{16,4096}', credential) is None:
                    raise ValueError()
                headers['Authorization'] = 'Bearer ' + credential
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError()
            if self.configuration.test_loopback_http:
                connection = http.client.HTTPConnection('127.0.0.1', origin.port, timeout=remaining)
            else:
                context = ssl.create_default_context()
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                if self.configuration.ca_file is not None:
                    context.load_verify_locations(cadata=self._ca_material)
                connection = http.client.HTTPSConnection(origin.hostname, origin.port or 443,
                                                         timeout=remaining, context=context)
            # Trusted numeric connect address bypasses DNS entirely. For HTTPS,
            # http.client still verifies the original origin hostname through SNI.
            # Timer owns TCP connect, TLS handshake and HTTP headers/body.
            address = ipaddress.ip_address(self.configuration.connect_address or origin.hostname)
            def connect_numeric(unused_address, unused_timeout, source_address=None):
                nonlocal transport_socket
                remaining = deadline - time.monotonic()
                if remaining <= 0 or source_address is not None:
                    raise ValueError()
                transport_socket = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                                                 socket.SOCK_STREAM)
                transport_socket.settimeout(remaining)
                transport_socket.connect((str(address), origin.port or 443))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError()
                transport_socket.settimeout(remaining)
                return transport_socket
            connection._create_connection = connect_numeric
            def expire():
                if transport_socket is not None:
                    try:
                        transport_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError()
            connection.timeout = remaining
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            connection.connect()
            # Capture the TLS socket after wrapping, retained even if the
            # response Connection:close makes http.client detach it later.
            transport_socket = connection.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError()
            transport_socket.settimeout(remaining)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status not in ({200, 202} if method == 'POST' else {200, 404}) or response.getheader('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError()
            headers = response.getheaders()
            names = [name.lower() for name, _ in headers]
            length = response.getheader('Content-Length')
            encoding = response.getheader('Transfer-Encoding')
            if ('content-encoding' in names or names.count('content-length') > 1
                or names.count('transfer-encoding') > 1
                or encoding is not None and (encoding.lower() != 'chunked' or length is not None)
                or length is not None and (not length.isdecimal() or not 1 <= int(length) <= 16384)
                or length is None and encoding is None):
                raise ValueError()
            raw = response.read(16385)
            if (len(raw) > 16384 or time.monotonic() >= deadline
                or length is not None and len(raw) != int(length)):
                raise ValueError()
            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            value = json.loads(raw, object_pairs_hook=unique_pairs)
            if (type(value) is not dict
                or set(value) != {'intent_id', 'payload_digest', 'state', 'provider_reference'}
                or value['intent_id'] != intent_id
                or value['state'] not in {'accepted', 'fulfilled', 'not_found'}
                or method == 'POST' and value['state'] == 'not_found'):
                raise ValueError()
            reference = value['provider_reference']
            if value['state'] == 'not_found':
                if method != 'GET' or response.status != 404 or reference is not None or value['payload_digest'] is not None:
                    raise ValueError()
            elif response.status == 404 or value['payload_digest'] != payload_digest or type(reference) is not str or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', reference) is None:
                raise ValueError()
            return EffectProviderReceipt(intent_id, value['payload_digest'], value['state'], reference)
        except Exception:
            # Provider error text/body/credentials never reach caller or logs.
            raise error() from None
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()
            if transport_socket is not None:
                transport_socket.close()
