"""NX-058 slice F: an in-process OIDC IdP for tests only - self-signed JWKS (RSA keys made here with the standard library), an
authorization step the test drives, and a token endpoint behind an httpx.MockTransport (nothing leaves the process). The token endpoint
checks what a real IdP checks: the client's secret, the one-time code, the redirect URI and the PKCE S256 verifier. Every key, secret and
subject is synthetic and drawn per run."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx

ISSUER = 'https://idp.example.test/realm'
CLIENT_ID = 'nexloop-workbench-test'
_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


def _probable_prime(bits):
    while True:
        n = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if any(n % p == 0 for p in _SMALL_PRIMES):
            continue
        d, r = n - 1, 0
        while d % 2 == 0:
            d, r = d // 2, r + 1
        for _ in range(40):
            x = pow(secrets.randbelow(n - 3) + 2, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                break
        else:
            return n


def rsa_keypair(bits=2048, e=65537):
    while True:
        p, q = _probable_prime(bits // 2), _probable_prime(bits // 2)
        n, phi = p * q, (p - 1) * (q - 1)
        if p != q and n.bit_length() == bits and phi % e:
            return n, e, pow(e, -1, phi)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _int_b64(value):
    return b64(value.to_bytes((value.bit_length() + 7) // 8, 'big'))


_DIGEST_INFO = bytes.fromhex('3031300d060960864801650304020105000420')


def sign_rs256(key, signing_input):
    n, _, d = key
    k = (n.bit_length() + 7) // 8
    t = _DIGEST_INFO + hashlib.sha256(signing_input).digest()
    em = b'\x00\x01' + b'\xff' * (k - len(t) - 3) + b'\x00' + t
    return pow(int.from_bytes(em, 'big'), d, n).to_bytes(k, 'big')


def jws(key, kid, claims, header=None):
    head = b64(json.dumps(header or {'alg': 'RS256', 'typ': 'JWT', 'kid': kid}).encode())
    body = b64(json.dumps(claims).encode())
    return f'{head}.{body}.{b64(sign_rs256(key, (head + "." + body).encode()))}'


_KEYS = {}


def cached_keypair(name):
    """One 2048-bit key per name per test process (key generation takes a moment)."""
    if name not in _KEYS:
        _KEYS[name] = rsa_keypair()
    return _KEYS[name]


class MockIdp:
    def __init__(self, *, issuer=ISSUER, client_id=CLIENT_ID):
        self.issuer, self.client_id = issuer, client_id
        self.client_secret = secrets.token_urlsafe(24)
        self.key, self.kid = cached_keypair('idp'), 'kid-' + secrets.token_hex(4)
        self.codes = {}
        self.token_requests = []
        self.tamper = {}  # claim overrides / 'header' / 'sign_with' for the next ID token

    @property
    def base(self):
        return self.issuer

    def provider(self, provider_id='corp', secret_file=None):
        return {'provider_id': provider_id, 'issuer': self.issuer, 'client_id': self.client_id,
                'authorization_endpoint': self.base + '/authorize', 'token_endpoint': self.base + '/token', 'jwks_uri': self.base + '/jwks',
                'client_secret_file': str(secret_file), 'display_name': 'Test IdP'}

    def jwks(self):
        n, e, _ = self.key
        return {'keys': [{'kty': 'RSA', 'use': 'sig', 'alg': 'RS256', 'kid': self.kid, 'n': _int_b64(n), 'e': _int_b64(e)}]}

    def authorize(self, location, subject):
        """The user signs in at the IdP as `subject`; returns the query of the redirect back (code and state)."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert location.startswith(self.base + '/authorize?')
        assert q['response_type'] == 'code' and q['client_id'] == self.client_id and q['code_challenge_method'] == 'S256' and q['scope'] == 'openid'
        code = secrets.token_urlsafe(24)
        self.codes[code] = {'subject': subject, 'nonce': q['nonce'], 'challenge': q['code_challenge'], 'redirect_uri': q['redirect_uri']}
        return {'code': code, 'state': q['state']}

    def id_token(self, subject, nonce):
        now = int(time.time())
        claims = {'iss': self.issuer, 'sub': subject, 'aud': self.client_id, 'exp': now + 300, 'iat': now, 'nonce': nonce}
        claims.update({k: v for k, v in self.tamper.items() if k not in ('header', 'sign_with')})
        claims = {k: v for k, v in claims.items() if v is not None}
        key = self.tamper.get('sign_with', self.key)
        return jws(key, self.kid, claims, self.tamper.get('header'))

    def handle(self, request):
        path = urlsplit(str(request.url)).path
        if request.method == 'GET' and str(request.url) == self.base + '/jwks':
            return httpx.Response(200, json=self.jwks())
        if request.method == 'POST' and str(request.url) == self.base + '/token':
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            basic = 'Basic ' + base64.b64encode(f'{self.client_id}:{self.client_secret}'.encode()).decode()
            grant = self.codes.pop(form.get('code', ''), None)
            if (request.headers.get('authorization') != basic or form.get('grant_type') != 'authorization_code' or grant is None
                    or form.get('redirect_uri') != grant['redirect_uri']
                    or b64(hashlib.sha256(form.get('code_verifier', '').encode()).digest()) != grant['challenge']):
                return httpx.Response(400, json={'error': 'invalid_grant'})
            return httpx.Response(200, json={'access_token': secrets.token_urlsafe(16), 'token_type': 'Bearer', 'expires_in': 300,
                                             'id_token': self.id_token(grant['subject'], grant['nonce'])})
        return httpx.Response(404, json={'error': 'not_found', 'path': path})

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)
