"""NX-058 slice F (docs/design/account-self-service.md §5; owner 2026-10-28): workbench single sign-on by OIDC authorization code with
PKCE (S256), state and nonce. Off unless a provider file is configured; only the providers it lists are accepted.

* Providers: a private file (0600, service-owned; nexloop_eios.private_configuration) of {version: 1, providers: [...]}. Each names its
  issuer, client_id, the three endpoints (HTTPS only, no userinfo or fragment, no private, loopback or link-local address literal) and
  where its client secret is: client_secret_file (another private file) or client_secret_env (an environment variable name). The secret
  is read at each token exchange and never logged, stored or returned.
* Browser binding: the start draws a 256-bit state (sent to the IdP) and a 256-bit binding value (an HttpOnly cookie of this browser).
  The database keeps their HMACs only, for 5 minutes, used once. The nonce and the PKCE verifier are HMACs of the state under the private
  identity token key, so nothing secret is stored and the state alone (it is in URLs) derives neither.
* ID token: RS256 only (alg none, HS* and every other algorithm refused), checked here with the standard library alone: PKCS#1 v1.5 by
  comparing the whole encoded message, never by parsing it (no Bleichenbacher-style forgery), keys of 2048 bits or more from the
  provider's JWKS (HTTPS, size-capped, cached 5 minutes, refetched at most once a minute for an unknown kid). iss must equal the
  configured issuer, aud must contain the client_id (azp must equal it when present or when aud has several values), exp/nbf/iat within
  a 60-second leeway and iat at most 10 minutes old, nonce equal by constant-time comparison, sub a non-empty string.
* Every failure is an OidcFailure with a fixed reason (the audit's, never the browser's): the caller answers one way for all of them.

The upstream eios/identity/oidc.py (frozen release commit) needs joserfc, which NexLoop does not depend on; this module follows its
rules (PKCE S256, one-time state, nonce, JWKS allowlist, fail-closed HTTPS) without copying it.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

STATE = 'nexloop-oidc-state/1'
BINDING = 'nexloop-oidc-binding/1'
NONCE = 'nexloop-oidc-nonce/1'
PKCE = 'nexloop-oidc-pkce/1'
CALLBACK_PATH = '/api/v1/workbench/auth/oidc/callback'
REASONS = ('state_invalid', 'provider_error', 'token_exchange_failed', 'id_token_invalid', 'not_linked', 'member_unavailable')
LEEWAY = 60
MAX_TOKEN_AGE = 600
_ID = re.compile(r'[a-z][a-z0-9_-]{0,31}')
_B64 = re.compile(r'[A-Za-z0-9_-]*')
_DIGEST_INFO = bytes.fromhex('3031300d060960864801650304020105000420')  # DER prefix of a SHA-256 DigestInfo (RFC 8017 §9.2)


class OidcFailure(Exception):
    def __init__(self, reason):
        if reason not in REASONS:
            raise ValueError('unknown OIDC failure reason')
        super().__init__(reason)
        self.reason = reason


class OidcConfigurationError(ValueError):
    pass


def _https(value, name):
    if type(value) is not str or len(value) > 2048:
        raise OidcConfigurationError(f'{name}: HTTPS URL required')
    u = urlsplit(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.fragment:
        raise OidcConfigurationError(f'{name}: HTTPS URL without userinfo or fragment required')
    try:
        address = ipaddress.ip_address(u.hostname)
    except ValueError:
        return value
    if address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved:
        raise OidcConfigurationError(f'{name}: no private, loopback or link-local address')
    return value


@dataclass(frozen=True)
class Provider:
    provider_id: str
    issuer: str
    client_id: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    client_secret_file: str | None = None
    client_secret_env: str | None = None
    token_endpoint_auth: str = 'client_secret_basic'
    display_name: str = ''

    def __post_init__(self):
        if type(self.provider_id) is not str or not _ID.fullmatch(self.provider_id):
            raise OidcConfigurationError('provider_id: [a-z][a-z0-9_-]{0,31}')
        if type(self.client_id) is not str or not 0 < len(self.client_id) <= 255 or self.client_id != self.client_id.strip():
            raise OidcConfigurationError('client_id required')
        for name in ('issuer', 'authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            _https(getattr(self, name), name)
        if self.token_endpoint_auth not in ('client_secret_basic', 'client_secret_post'):
            raise OidcConfigurationError('token_endpoint_auth: client_secret_basic or client_secret_post')
        if (self.client_secret_file is None) == (self.client_secret_env is None):
            raise OidcConfigurationError('exactly one of client_secret_file and client_secret_env')
        if self.client_secret_env is not None and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', str(self.client_secret_env)):
            raise OidcConfigurationError('client_secret_env: an environment variable name')
        if type(self.display_name) is not str or len(self.display_name) > 80:
            raise OidcConfigurationError('display_name: at most 80 characters')

    def fingerprint(self):
        """sha256 of the provider's public configuration (no secret, no secret location): recorded with each SSO login."""
        public = {k: getattr(self, k) for k in ('provider_id', 'issuer', 'client_id', 'authorization_endpoint', 'token_endpoint', 'jwks_uri', 'token_endpoint_auth')}
        return hashlib.sha256(json.dumps(public, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def client_secret(self):
        if self.client_secret_file is not None:
            from nexloop_eios.private_configuration import read_private_text
            return read_private_text(self.client_secret_file, maximum=4096)
        value = os.environ.get(self.client_secret_env, '')
        if not value or len(value) > 4096:
            raise OidcConfigurationError('client secret environment variable empty')
        return value


FIELDS = {'provider_id', 'issuer', 'client_id', 'authorization_endpoint', 'token_endpoint', 'jwks_uri', 'client_secret_file', 'client_secret_env',
          'token_endpoint_auth', 'display_name'}


def parse_providers(document):
    if type(document) is not dict or set(document) != {'version', 'providers'} or document['version'] != 1 or type(document['providers']) is not list:
        raise OidcConfigurationError('provider file: {version: 1, providers: [...]}')
    providers = {}
    for item in document['providers']:
        if type(item) is not dict or not set(item) <= FIELDS:
            raise OidcConfigurationError('provider: unknown field')
        provider = Provider(**item)
        if provider.provider_id in providers:
            raise OidcConfigurationError('provider_id repeated')
        providers[provider.provider_id] = provider
    if not 0 < len(providers) <= 8:
        raise OidcConfigurationError('provider file: 1 to 8 providers')
    return providers


def load_providers(path):
    from nexloop_eios.private_configuration import read_private_text
    return parse_providers(json.loads(read_private_text(path, maximum=65536)))


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text):
    if type(text) is not str or not _B64.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError('base64url')
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _derived(key, purpose, tenant, provider_id, state):
    return _b64(hmac.new(key, f'{purpose}|{tenant}|{provider_id}|{state}'.encode(), hashlib.sha256).digest())


def nonce_for(key, tenant, provider_id, state):
    return _derived(key, NONCE, tenant, provider_id, state)


def verifier_for(key, tenant, provider_id, state):
    return _derived(key, PKCE, tenant, provider_id, state)  # 43 characters of [A-Za-z0-9_-] (RFC 7636 §4.1)


def challenge_for(verifier):
    return _b64(hashlib.sha256(verifier.encode('ascii')).digest())


def authorization_url(provider, *, redirect_uri, state, nonce, code_challenge):
    query = urlencode({'response_type': 'code', 'client_id': provider.client_id, 'redirect_uri': redirect_uri, 'scope': 'openid',
                       'state': state, 'nonce': nonce, 'code_challenge': code_challenge, 'code_challenge_method': 'S256'})
    return provider.authorization_endpoint + ('&' if urlsplit(provider.authorization_endpoint).query else '?') + query


def new_state():
    return secrets.token_urlsafe(32)


def valid_state(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{43}', value) is not None


def _strict_json(data):
    def pairs(items):
        out = {}
        for name, value in items:
            if name in out:
                raise ValueError('duplicate member')
            out[name] = value
        return out
    value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
    if type(value) is not dict:
        raise ValueError('object required')
    return value


def rsa_key(jwk):
    """(n, e) of an RSA signing JWK, or None for a key this verifier never uses."""
    if type(jwk) is not dict or jwk.get('kty') != 'RSA' or jwk.get('use', 'sig') != 'sig' or jwk.get('alg', 'RS256') != 'RS256':
        return None
    if 'key_ops' in jwk and (type(jwk['key_ops']) is not list or 'verify' not in jwk['key_ops']):
        return None
    n, e = int.from_bytes(_unb64(jwk['n']), 'big'), int.from_bytes(_unb64(jwk['e']), 'big')
    if n.bit_length() < 2048 or n.bit_length() > 8192 or e < 3 or e % 2 == 0 or e.bit_length() > 32:
        return None
    return n, e


def rs256_valid(key, signing_input, signature):
    n, e = key
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, 'big')
    if s >= n:
        return False
    encoded = pow(s, e, n).to_bytes(k, 'big')
    digest_info = _DIGEST_INFO + hashlib.sha256(signing_input).digest()
    expected = b'\x00\x01' + b'\xff' * (k - len(digest_info) - 3) + b'\x00' + digest_info
    return hmac.compare_digest(encoded, expected)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def verify_id_token(token, *, provider, keys, nonce, now):
    """keys(kid) -> list of (n, e) candidates for that kid (refetching once for an unknown kid). Returns {subject, issued_at, expires_at}."""
    try:
        if type(token) is not str or len(token) > 16384 or token.count('.') != 2:
            raise ValueError('compact JWS')
        head, body, sig = token.split('.')
        header = _strict_json(_unb64(head))
        if header.get('alg') != 'RS256' or 'crit' in header or header.get('typ', 'JWT') not in ('JWT', 'jwt') or ('kid' in header and type(header['kid']) is not str):
            raise ValueError('header')
        signature = _unb64(sig)
        signing_input = (head + '.' + body).encode('ascii')
        if not any(rs256_valid(k, signing_input, signature) for k in keys(header.get('kid'))):
            raise ValueError('signature')
        claims = _strict_json(_unb64(body))
    except OidcFailure:
        raise
    except Exception:
        raise OidcFailure('id_token_invalid') from None
    aud = claims.get('aud')
    audiences = [aud] if type(aud) is str else aud if type(aud) is list and all(type(a) is str for a in aud) else []
    exp, iat, nbf = claims.get('exp'), claims.get('iat'), claims.get('nbf')
    if (claims.get('iss') != provider.issuer or provider.client_id not in audiences
            or (('azp' in claims or len(audiences) > 1) and claims.get('azp') != provider.client_id)
            or not _number(exp) or now >= exp + LEEWAY
            or not _number(iat) or iat > now + LEEWAY or now - iat > MAX_TOKEN_AGE
            or (nbf is not None and (not _number(nbf) or nbf > now + LEEWAY))
            or type(claims.get('nonce')) is not str or not hmac.compare_digest(claims['nonce'].encode(), nonce.encode())
            or type(claims.get('sub')) is not str or not 0 < len(claims['sub']) <= 255 or claims['sub'] != claims['sub'].strip()):
        raise OidcFailure('id_token_invalid')
    return {'subject': claims['sub'], 'issued_at': iat, 'expires_at': exp}


class OidcClient:
    """The HTTP side: the JWKS cache and the token exchange. `transport` (an httpx transport) is for the in-process test IdP only."""

    def __init__(self, providers, *, transport=None, clock=time.time):
        self.providers, self.transport, self.clock = providers, transport, clock
        self._lock = threading.Lock()
        self._jwks = {}  # provider_id -> (fetched_at, {kid or None: [(n, e)]})

    def _http(self):
        import httpx
        return httpx.Client(transport=self.transport, timeout=10.0, follow_redirects=False, trust_env=False)

    @staticmethod
    def _json(response, limit):
        if response.status_code != 200 or len(response.content) > limit:
            raise ValueError('response')
        return _strict_json(response.content)

    def _fetch_jwks(self, provider):
        with self._http() as http:
            document = self._json(http.get(provider.jwks_uri, headers={'Accept': 'application/json'}), 65536)
        if type(document.get('keys')) is not list:
            raise ValueError('jwks')
        found = {}
        for jwk in document['keys'][:32]:
            try:
                key = rsa_key(jwk)
            except Exception:
                key = None
            if key is not None:
                found.setdefault(jwk.get('kid') if type(jwk.get('kid')) is str else None, []).append(key)
        return found

    def keys(self, provider):
        def lookup(kid):
            with self._lock:
                fetched_at, found = self._jwks.get(provider.provider_id, (None, {}))
                now = self.clock()
                stale = fetched_at is None or now - fetched_at > 300
                unknown = (kid not in found if kid is not None else not found) and (fetched_at is None or now - fetched_at > 60)
                if stale or unknown:
                    try:
                        found = self._fetch_jwks(provider)
                    except Exception:
                        raise OidcFailure('id_token_invalid') from None
                    self._jwks[provider.provider_id] = (now, found)
            if kid is None:
                return [k for ks in found.values() for k in ks]
            return found.get(kid, [])
        return lookup

    def exchange(self, provider, *, code, redirect_uri, verifier):
        try:
            secret = provider.client_secret()
            form = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri, 'code_verifier': verifier}
            headers = {'Accept': 'application/json'}
            if provider.token_endpoint_auth == 'client_secret_basic':
                from urllib.parse import quote
                pair = f'{quote(provider.client_id, safe="")}:{quote(secret, safe="")}'.encode()
                headers['Authorization'] = 'Basic ' + base64.b64encode(pair).decode('ascii')
            else:
                form.update(client_id=provider.client_id, client_secret=secret)
            with self._http() as http:
                document = self._json(http.post(provider.token_endpoint, data=form, headers=headers), 65536)
            if type(document.get('id_token')) is not str or str(document.get('token_type', 'Bearer')).lower() != 'bearer':
                raise ValueError('token response')
            return document['id_token']
        except Exception:
            raise OidcFailure('token_exchange_failed') from None
