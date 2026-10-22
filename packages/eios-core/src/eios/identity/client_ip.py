"""Explicit client-IP trust boundaries for the Identity BFF (vendored from NEX-EIOS src/eios/api/identity_client_ip.py, see PROVENANCE.json)."""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Protocol

from starlette.requests import Request


# NexLoop: the gateway (deploy/stage/config/Caddyfile.template) overwrites this header with {remote_host}.
IDENTITY_CLIENT_IP_HEADER = "X-NexLoop-Client-IP"
_HEADER_BYTES = IDENTITY_CLIENT_IP_HEADER.lower().encode("ascii")
_FAILURE = "identity client peer is unavailable"


class IdentityClientIpResolver(Protocol):
    def resolve(self, request: Request) -> str: ...


class DirectIdentityClientIpResolver:
    """Trust only the ASGI socket peer; every forwarding header is ignored."""

    def resolve(self, request: Request) -> str:
        return _socket_peer(request)


class TrustedProxyIdentityClientIpResolver:
    """Trust one private header only from an explicitly allowlisted proxy peer."""

    def __init__(self, trusted_proxy_peers: Iterable[str]) -> None:
        try:
            values = tuple(trusted_proxy_peers)
            if not 0 < len(values) <= 64:
                raise ValueError
            networks = tuple(_strict_network(value) for value in values)
            if len(set(networks)) != len(networks):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("trusted proxy peers are invalid") from None
        self._networks = networks

    def resolve(self, request: Request) -> str:
        peer = _address(_socket_peer(request))
        if not any(peer in network for network in self._networks):
            raise RuntimeError(_FAILURE)
        values = tuple(
            value
            for name, value in request.scope.get("headers", ())
            if name.lower() == _HEADER_BYTES
        )
        if len(values) != 1:
            raise RuntimeError(_FAILURE)
        try:
            raw = values[0].decode("ascii")
            client = _address(raw)
        except (AttributeError, UnicodeError, ValueError):
            raise RuntimeError(_FAILURE) from None
        canonical = str(client)
        if raw != canonical:
            raise RuntimeError(_FAILURE)
        return canonical


def _socket_peer(request: Request) -> str:
    client = request.client
    if client is None or type(client.host) is not str:
        raise RuntimeError(_FAILURE)
    try:
        return str(_address(client.host))
    except ValueError:
        raise RuntimeError(_FAILURE) from None


def _address(value: str) -> IPv4Address | IPv6Address:
    if type(value) is not str or not 0 < len(value) <= 64 or value != value.strip():
        raise ValueError
    return ip_address(value)


def _strict_network(value: str):
    if type(value) is not str or not 0 < len(value) <= 128 or value != value.strip():
        raise ValueError
    if "/" not in value:
        address = _address(value)
        value = f"{address}/{address.max_prefixlen}"
    return ip_network(value, strict=True)


__all__ = [
    "DirectIdentityClientIpResolver",
    "IDENTITY_CLIENT_IP_HEADER",
    "IdentityClientIpResolver",
    "TrustedProxyIdentityClientIpResolver",
]
