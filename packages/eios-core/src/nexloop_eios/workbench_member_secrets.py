"""NX-058 (docs/design/account-self-service.md §1, §2, §4): one-time workbench secrets - reset codes now, invitation tokens with
slice A. A secret is drawn here, shown once to the owner who issued it, and stored only as HMAC(identity_token_key, purpose|tenant|
secret): the key is private configuration (a 0600 file of 64 hex characters), separate from the rate-limit key, and never reaches the
database, a log or a response. Lookups compare the HMAC for equality. A source digest (the request's client address under the same key,
its own purpose) lets an audit say whether issuer and completer came from the same place without storing the address."""
import hashlib
import hmac
import re
import secrets

RESET = 'nexloop-reset/1'
INVITE = 'nexloop-invite/1'
SOURCE = 'nexloop-source/1'
_CODE = re.compile(r'[A-Za-z0-9_-]{22}')  # 128 bits, base64url without padding


class SecretsUnavailable(RuntimeError):
    pass


def load_key(path):
    from nexloop_eios.private_configuration import read_private_text
    if path is None:
        raise SecretsUnavailable('identity token key not configured')
    value = read_private_text(path, maximum=128).strip()
    if not re.fullmatch(r'[0-9a-f]{64}', value):
        raise SecretsUnavailable('identity token key: 64 hex characters')
    return bytes.fromhex(value)


def new_code():
    return secrets.token_urlsafe(16)


def valid_code(value):
    return type(value) is str and _CODE.fullmatch(value) is not None


def digest(key, purpose, tenant, secret):
    return hmac.new(key, f'{purpose}|{tenant}|{secret}'.encode(), hashlib.sha256).hexdigest()


def source_digest(key, tenant, client_address):
    return digest(key, SOURCE, tenant, client_address)
