"""Bounded loopback Host control probe; no Run admission or business identity."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request, HTTPSHandler

from nexloop_eios.private_configuration import read_private_text


@dataclass(frozen=True)
class HostControlConfiguration:
    origin: str
    key_file: Path
    ca_file: Path

    def __post_init__(self):
        parsed = urlsplit(self.origin)
        if (parsed.scheme != 'https' or parsed.hostname != '127.0.0.1'
                or parsed.username is not None or parsed.password is not None
                or parsed.port is None or not 1024 <= parsed.port <= 65535
                or self.origin != f'https://127.0.0.1:{parsed.port}'):
            raise ValueError('explicit loopback Host origin required')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def probe_host(config: HostControlConfiguration):
    report = {'reachable': False, 'authenticated': False, 'owner_lock': False, 'product_ready': False}
    try:
        key = read_private_text(config.key_file, maximum=64)
        if not re.fullmatch('[0-9a-f]{64}', key):
            return report
        # No ambient HTTP proxy, redirects, query-string credentials or headers
        # derived from browser requests. Only this fixed readonly route is used.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=read_private_text(config.ca_file, maximum=32768))
        opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
        request = Request(config.origin + '/internal/v1/health/ready',
                          headers={'Authorization': 'Bearer ' + key}, method='GET')
        try:
            response = opener.open(request, timeout=1)
        except HTTPError as error:
            response = error
        with response:
            report['reachable'] = True
            if response.code != 503:
                return report
            raw = response.read(4097)
            if len(raw) > 4096:
                return report
        value = json.loads(raw)
        if (isinstance(value, dict) and value.get('ready') is False
                and value.get('product_ready') is False
                and isinstance(value.get('foundation'), dict)
                and value['foundation'].get('internal_auth') is True
                and value['foundation'].get('owner_lock') is True):
            report.update(authenticated=True, owner_lock=True)
    except Exception:
        pass
    return report


HOST_METRICS = ('available', 'active_runs', 'waiting', 'max_active_runs', 'runs_started', 'admission_timeouts')


def read_host_metrics(config: HostControlConfiguration):
    """NX-030 M09: the Host's concurrency counters (GET /internal/v1/metrics, loopback + control key). Numbers only;
    anything else (extra keys, text, oversize) is rejected as unavailable (None)."""
    try:
        key = read_private_text(config.key_file, maximum=64)
        if not re.fullmatch('[0-9a-f]{64}', key):
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=read_private_text(config.ca_file, maximum=32768))
        opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
        request = Request(config.origin + '/internal/v1/metrics', headers={'Authorization': 'Bearer ' + key}, method='GET')
        with opener.open(request, timeout=1) as response:
            raw = response.read(4097)
            if response.code != 200 or len(raw) > 4096:
                return None
        value = json.loads(raw)
        host = value.get('host') if type(value) is dict and set(value) == {'host'} else None
        if type(host) is not dict or not set(host) <= set(HOST_METRICS) or 'available' not in host:
            return None
        if any(type(v) is not (bool if k == 'available' else int) for k, v in host.items()):
            return None
        return host
    except Exception:
        return None
