"""The configurator login window over the bootstrap socket (NX-032-stage-secrets.md §3b), shared by stage_secrets.py register
and scripts/nx037/real_tenant_init.py configure.

The stage has no configurator login. The window logs the configurator in as itself (trusted_configuration refuses SET ROLE
and elevated sessions): a one-time password reaches PostgreSQL as a SCRAM verifier only, the configurator DSN lives in a private
temporary file for the duration of the window, and the role is NOLOGIN without a password again when the window closes,
whatever happened."""
import base64
import contextlib
import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path

CONFIGURATOR = 'nexloop_configurator'


def scram_verifier(password, *, iterations=4096, salt=None):
    """The PostgreSQL SCRAM-SHA-256 verifier (RFC 5803 form) of `password`: what ALTER ROLE ... PASSWORD stores as is."""
    salt = salt or secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    client_key = hmac.new(salted, b'Client Key', 'sha256').digest()
    server_key = hmac.new(salted, b'Server Key', 'sha256').digest()
    def b64(value):
        return base64.b64encode(value).decode()
    return f'SCRAM-SHA-256${iterations}:{b64(salt)}${b64(hashlib.sha256(client_key).digest())}:{b64(server_key)}'


def write_private(path, data, mode=0o600):
    """A new file (never overwritten) in a 0700 directory, mode 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data if isinstance(data, bytes) else data.encode())
    os.chmod(path, mode)


@contextlib.contextmanager
def configurator_window(bootstrap_dsn_file, *, refused=ValueError):
    """Yields the path of a private configurator DSN file; `refused` is the exception raised for a DSN that is not the
    nexloop_bootstrap DSN over the local socket."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from nexloop_eios.trusted_configuration import read_private_text
    bootstrap = read_private_text(bootstrap_dsn_file, maximum=16384).strip()
    params = conninfo_to_dict(bootstrap)
    if params.get('user') != 'nexloop_bootstrap' or not str(params.get('host', '')).startswith('/'):
        raise refused('--bootstrap-dsn-file: the nexloop_bootstrap DSN over the local socket (host=<socket directory>)')
    password = secrets.token_hex(24)
    scratch = Path(tempfile.mkdtemp(prefix='nexloop-configurator-'))
    dsn = scratch / 'configurator-dsn'
    with psycopg.connect(bootstrap, autocommit=True, connect_timeout=5) as admin:
        admin.execute(f"alter role {CONFIGURATOR} login password '{scram_verifier(password)}'")
        try:
            write_private(dsn, make_conninfo(bootstrap, user=CONFIGURATOR, password=password))
            yield dsn
        finally:
            dsn.unlink(missing_ok=True)
            scratch.rmdir()
            admin.execute(f'alter role {CONFIGURATOR} nologin password null')
