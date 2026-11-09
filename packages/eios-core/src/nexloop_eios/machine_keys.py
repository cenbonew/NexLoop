"""NX-062 M1 (docs/implementation/NX-062-design.md §2.2, L6 C2): machine key format, HMAC digest and the pepper file.

A machine key is ``nxm_<prefix_id>_<secret>``:

* ``prefix_id``: 12 lowercase base32 characters (60 bits), not secret; it is the lookup key of the stored row, shown in the
  workbench and recorded in the audit;
* ``secret``: 43 base64url characters (32 random bytes); it appears once, in the issue response, and is never stored.

The server stores only ``HMAC-SHA256(pepper[v], "nexloop-machine-key-v1:" + prefix_id + ":" + secret)`` together with the
pepper version ``v``. Lookup (C2) is by prefix_id first; the digest is then computed exactly once, with the pepper version of
that row, and compared for equality. Candidate digests over several pepper versions are never tried.

The peppers live in a private file read by the API process only (owner decision Q-B 2026-11-09: never in PostgreSQL, never in
the Runtime). Format, one per line, ``<version> <64 lowercase hex>``; blank and ``#`` lines are ignored; versions are positive
integers, unique; new keys always use the highest version. The file must be a regular file owned by the service user, mode
0600 or stricter (private_configuration.read_private_text). Losing the file invalidates every machine key; it is backed up
with the audit-chain sealing key and covered by the recovery drill.

Nothing here logs, prints or puts key material in an exception message.
"""
import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field

from nexloop_eios.private_configuration import read_private_text

PREFIX = 'nxm_'
DOMAIN = b'nexloop-machine-key-v1:'
_B32 = 'abcdefghijklmnopqrstuvwxyz234567'
PREFIX_ID = re.compile(r'[a-z2-7]{12}')
SECRET = re.compile(r'[A-Za-z0-9_-]{43}')
KEY = re.compile(r'nxm_([a-z2-7]{12})_([A-Za-z0-9_-]{43})')
KEY_LENGTH = len(PREFIX) + 12 + 1 + 43
PEPPER_FILE_MAXIMUM = 16384
_PEPPER_LINE = re.compile(r'([1-9][0-9]{0,8}) ([0-9a-f]{64})')


class MachineKeyInvalid(ValueError):
    """A presented value is not a machine key, or the pepper configuration is unusable. The message never holds key material."""


@dataclass(frozen=True)
class MachineKey:
    prefix_id: str
    secret: str = field(repr=False)

    @property
    def token(self):
        return f'{PREFIX}{self.prefix_id}_{self.secret}'

    def __repr__(self):
        return f'MachineKey(prefix_id={self.prefix_id!r}, secret=<redacted>)'

    __str__ = __repr__


@dataclass(frozen=True)
class Peppers:
    """Pepper versions read from the private file; repr never shows material."""
    _material: dict = field(repr=False)

    @property
    def versions(self):
        return tuple(sorted(self._material))

    @property
    def current(self):
        return max(self._material)

    def material(self, version):
        try:
            return self._material[version]
        except KeyError:
            raise MachineKeyInvalid('pepper version unavailable') from None

    def __repr__(self):
        return f'Peppers(versions={self.versions})'

    __str__ = __repr__


def generate():
    """A new key: random prefix_id and secret from the operating system's CSPRNG."""
    prefix_id = ''.join(secrets.choice(_B32) for _ in range(12))
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode('ascii')
    return MachineKey(prefix_id, secret)


def parse(token):
    """The key of a presented Bearer value; MachineKeyInvalid for anything that is not exactly one machine key."""
    if not isinstance(token, str) or len(token) != KEY_LENGTH:
        raise MachineKeyInvalid('machine key malformed')
    match = KEY.fullmatch(token)
    if match is None:
        raise MachineKeyInvalid('machine key malformed')
    return MachineKey(match.group(1), match.group(2))


def digest(key, peppers, version):
    """HMAC-SHA256 of the key under one pepper version (C2: called once per request, with the stored row's version)."""
    if not isinstance(key, MachineKey) or PREFIX_ID.fullmatch(key.prefix_id) is None or SECRET.fullmatch(key.secret) is None:
        raise MachineKeyInvalid('machine key malformed')
    message = DOMAIN + key.prefix_id.encode('ascii') + b':' + key.secret.encode('ascii')
    return hmac.new(peppers.material(version), message, hashlib.sha256).digest()


def matches(key, peppers, version, stored):
    """Constant-time comparison of the key's digest under ``version`` with a stored digest."""
    if not isinstance(stored, (bytes, bytearray, memoryview)):
        return False
    return hmac.compare_digest(digest(key, peppers, version), bytes(stored))


def parse_peppers(text):
    material = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = _PEPPER_LINE.fullmatch(line)
        if match is None:
            raise MachineKeyInvalid('pepper file malformed')
        version = int(match.group(1))
        if version in material:
            raise MachineKeyInvalid('pepper version duplicated')
        value = bytes.fromhex(match.group(2))
        if value in material.values():
            raise MachineKeyInvalid('pepper material reused')
        material[version] = value
    if not material:
        raise MachineKeyInvalid('pepper file empty')
    return Peppers(material)


def load_peppers(path):
    """Peppers from the private file; any problem (owner, mode, shape) fails closed with MachineKeyInvalid."""
    try:
        text = read_private_text(path, maximum=PEPPER_FILE_MAXIMUM)
    except (OSError, ValueError, UnicodeDecodeError):
        raise MachineKeyInvalid('pepper file unavailable') from None
    return parse_peppers(text)
