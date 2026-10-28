"""NX-058 slice F: the OIDC protocol checks of nexloop_eios.workbench_oidc without a database - RS256 by the standard library, the ID
token claims (issuer, audience, azp, expiry, issue time, nonce, subject), refused algorithms and tampering, PKCE S256 (RFC 7636 appendix B),
the provider file rules and the token exchange against the in-process IdP (tests/support/mock_oidc_idp.py). Synthetic keys only."""
import base64
import json
import time

import pytest

from nexloop_eios import workbench_oidc as O
from support.mock_oidc_idp import MockIdp, b64, cached_keypair, jws


@pytest.fixture
def idp(tmp_path):
    i = MockIdp()
    secret = tmp_path / 'client-secret'
    secret.write_text(i.client_secret)
    secret.chmod(0o600)
    i.providers = O.parse_providers({'version': 1, 'providers': [i.provider(secret_file=secret)]})
    i.client = O.OidcClient(i.providers, transport=i.transport)
    return i


def verify(i, token, nonce='n-0', now=None):
    p = i.providers['corp']
    return O.verify_id_token(token, provider=p, keys=i.client.keys(p), nonce=nonce, now=time.time() if now is None else now)


def token(i, **claims):
    i.tamper = claims
    return i.id_token('member-sub-1', 'n-0')


def test_pkce_s256_matches_rfc_7636_appendix_b():
    assert O.challenge_for('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk') == 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'
    key = bytes(32)
    v = O.verifier_for(key, 't', 'corp', 'state-a')
    assert len(v) == 43 and v != O.verifier_for(key, 't', 'corp', 'state-b') != O.nonce_for(key, 't', 'corp', 'state-a')


def test_a_valid_token_names_its_subject(idp):
    assert verify(idp, token(idp))['subject'] == 'member-sub-1'
    # aud as a list with the client and azp equal to it
    assert verify(idp, token(idp, aud=[idp.client_id, 'other'], azp=idp.client_id))['subject'] == 'member-sub-1'


class Ago(int):
    """A time claim as an offset from the moment the test runs (not the moment it is collected: a long run would age a fixed time)."""


@pytest.mark.parametrize('claims', [
    {'aud': 'another-client'}, {'aud': ['another-client']}, {'aud': ['nexloop-workbench-test', 'other']},
    {'azp': 'another-client'}, {'iss': 'https://idp.example.test/other'}, {'exp': Ago(-120)},
    {'iat': Ago(-3600), 'exp': Ago(300)}, {'iat': Ago(600)},
    {'nbf': Ago(600)}, {'nonce': 'n-1'}, {'nonce': None}, {'sub': ''}, {'sub': None}, {'exp': None},
    {'exp': 'never'}])
def test_claims_outside_the_rules_are_refused(idp, claims):
    now = int(time.time())
    claims = {k: now + v if isinstance(v, Ago) else v for k, v in claims.items()}
    with pytest.raises(O.OidcFailure) as refused:
        verify(idp, token(idp, **claims), now=now)
    assert refused.value.reason == 'id_token_invalid'


def test_expiry_leeway_is_sixty_seconds(idp):
    now = time.time()
    assert verify(idp, token(idp, exp=int(now) - 30), now=now)['subject'] == 'member-sub-1'
    with pytest.raises(O.OidcFailure):
        verify(idp, token(idp, exp=int(now) - 61), now=now)


def test_tampered_signed_or_unsigned_tokens_are_refused(idp):
    good = token(idp)
    head, body, sig = good.split('.')
    claims = json.loads(base64.urlsafe_b64decode(body + '=='))
    forged = b64(json.dumps({**claims, 'sub': 'someone-else'}).encode())
    cases = [
        f'{head}.{forged}.{sig}',  # payload changed after signing
        f'{head}.{body}.{sig[:-4]}AAAA',  # signature changed
        jws(cached_keypair('attacker'), idp.kid, claims),  # signed by another key with the same kid
        f"{b64(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}.{body}.",  # alg none
        f"{b64(json.dumps({'alg': 'HS256', 'typ': 'JWT', 'kid': idp.kid}).encode())}.{body}.{sig}",  # HS256 with the public key as secret
        f"{b64(json.dumps({'alg': 'RS256', 'kid': idp.kid, 'crit': ['x']}).encode())}.{body}.{sig}",
        good + '.extra', good.replace('.', '..', 1), '',
    ]
    for case in cases:
        with pytest.raises(O.OidcFailure) as refused:
            verify(idp, case)
        assert refused.value.reason == 'id_token_invalid', case[:40]


def test_duplicate_claims_are_refused(idp):
    head = b64(json.dumps({'alg': 'RS256', 'kid': idp.kid}).encode())
    now = int(time.time())
    body = b64(('{"iss":"%s","sub":"a","sub":"b","aud":"%s","exp":%d,"iat":%d,"nonce":"n-0"}' % (idp.issuer, idp.client_id, now + 300, now)).encode())
    from support.mock_oidc_idp import sign_rs256
    with pytest.raises(O.OidcFailure):
        verify(idp, f'{head}.{body}.{b64(sign_rs256(idp.key, (head + "." + body).encode()))}')


def test_short_keys_and_non_rsa_keys_are_never_used():
    n, e, _ = cached_keypair('idp')
    small = (1 << 1023) | 1
    as_jwk = lambda n, e, **x: {'kty': 'RSA', 'n': b64(n.to_bytes((n.bit_length() + 7) // 8, 'big')), 'e': b64(e.to_bytes(3, 'big')), **x}  # noqa: E731
    assert O.rsa_key(as_jwk(n, e)) == (n, e)
    assert O.rsa_key(as_jwk(small, e)) is None
    assert O.rsa_key(as_jwk(n, e, use='enc')) is None and O.rsa_key(as_jwk(n, e, alg='PS256')) is None
    assert O.rsa_key({'kty': 'EC', 'crv': 'P-256'}) is None


def test_the_token_exchange_sends_the_verifier_and_the_secret_and_fails_closed(idp):
    p = idp.providers['corp']
    key = bytes(range(32))
    state = O.new_state()
    verifier = O.verifier_for(key, 't', 'corp', state)
    back = idp.authorize(O.authorization_url(p, redirect_uri='https://app.example.test/cb', state=state, nonce='n-0',
                                             code_challenge=O.challenge_for(verifier)), 'member-sub-1')
    assert back['state'] == state
    id_token = idp.client.exchange(p, code=back['code'], redirect_uri='https://app.example.test/cb', verifier=verifier)
    assert verify(idp, id_token)['subject'] == 'member-sub-1'
    # The code is one-time; a wrong verifier, redirect URI or secret is refused by the IdP; each is token_exchange_failed here.
    with pytest.raises(O.OidcFailure) as refused:
        idp.client.exchange(p, code=back['code'], redirect_uri='https://app.example.test/cb', verifier=verifier)
    assert refused.value.reason == 'token_exchange_failed'
    back = idp.authorize(O.authorization_url(p, redirect_uri='https://app.example.test/cb', state=state, nonce='n-0',
                                             code_challenge=O.challenge_for(verifier)), 'member-sub-1')
    with pytest.raises(O.OidcFailure):
        idp.client.exchange(p, code=back['code'], redirect_uri='https://app.example.test/cb', verifier=O.verifier_for(key, 't', 'corp', 'other'))
    assert 'client_secret' not in idp.token_requests[0] and idp.token_requests[0]['code_verifier'] == verifier


@pytest.mark.parametrize('change', [
    {'issuer': 'http://idp.example.test'}, {'token_endpoint': 'https://' + '.'.join(map(str, (10, 1, 2, 3))) + '/token'}, {'jwks_uri': 'https://[::1]/jwks'},
    {'authorization_endpoint': 'https://user:pw@idp.example.test/a'}, {'jwks_uri': 'https://idp.example.test/jwks#x'},
    {'client_secret_env': 'NEXLOOP_X'}, {'client_secret_file': None}, {'provider_id': 'Corp'}, {'token_endpoint_auth': 'none'},
    {'unknown': 1}])
def test_provider_file_rules(change, tmp_path):
    item = {**MockIdp().provider(secret_file=tmp_path / 's'), **change}
    item = {k: v for k, v in item.items() if v is not None}
    with pytest.raises((O.OidcConfigurationError, TypeError)):
        O.parse_providers({'version': 1, 'providers': [item]})


def test_provider_file_shape_and_fingerprint(tmp_path):
    item = MockIdp().provider(secret_file=tmp_path / 's')
    for document in ({'version': 2, 'providers': [item]}, {'version': 1, 'providers': []}, {'version': 1, 'providers': [item, item]}, [item]):
        with pytest.raises(O.OidcConfigurationError):
            O.parse_providers(document)
    p = O.parse_providers({'version': 1, 'providers': [item]})['corp']
    assert len(p.fingerprint()) == 64 and str(tmp_path) not in json.dumps(p.fingerprint())
    env = O.parse_providers({'version': 1, 'providers': [{**{k: v for k, v in item.items() if k != 'client_secret_file'}, 'client_secret_env': 'NEXLOOP_OIDC_TEST_SECRET'}]})['corp']
    assert env.fingerprint() == p.fingerprint()  # the secret's location is not part of it
