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


WINDOW_MINUTES = 10


class WindowNotClosed(RuntimeError):
    """The configurator could not be set NOLOGIN again (even over a fresh bootstrap connection): its password still expires by itself
    at the window's VALID UNTIL (at most WINDOW_MINUTES), and the next window heals it before opening."""


def _close(admin_or_none, bootstrap):
    """NOLOGIN, no password, no expiry; over the window's connection, else once over a fresh bootstrap connection."""
    import psycopg
    statement = f"alter role {CONFIGURATOR} nologin password null valid until 'infinity'"
    try:
        if admin_or_none is None or admin_or_none.closed:
            raise psycopg.OperationalError('window connection gone')
        admin_or_none.execute(statement)
        return
    except psycopg.Error:
        pass
    try:
        with psycopg.connect(bootstrap, autocommit=True, connect_timeout=5) as fresh:
            fresh.execute(statement)
    except psycopg.Error:
        raise WindowNotClosed('configurator window not closed: its password expires by itself within the window time') from None


@contextlib.contextmanager
def _sigterm_raises():
    """SIGTERM (docker stop, a killed compose run, systemd) becomes an exception inside the window, so its finally closes it."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextlib.contextmanager
def configurator_window(bootstrap_dsn_file, *, refused=ValueError):
    """Yields the path of a private configurator DSN file; `refused` is the exception raised for a DSN that is not the
    nexloop_bootstrap DSN over the local socket, or for a window already open.

    Hardening (L6 sim-credentials-window review 1-1): the password is valid for WINDOW_MINUTES only (VALID UNTIL, server time), so a
    window a killed process never closed lapses by itself; SIGTERM closes the window like any exception; the close comes first
    (NOLOGIN, no password, no expiry - over a fresh bootstrap connection when the window's own is gone) and the temporary DSN file
    goes after. Before opening: a configurator still able to log in with a lapsed VALID UNTIL is a stale window - closed first (self
    healing); one still valid is another window (or one left behind minutes ago) - refused, never taken over; so is one able to log
    in with no VALID UNTIL at all (no window ever opens one that way: someone opened it on purpose)."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from nexloop_eios.trusted_configuration import read_private_text
    bootstrap = read_private_text(bootstrap_dsn_file, maximum=16384).strip()
    params = conninfo_to_dict(bootstrap)
    if params.get('user') != 'nexloop_bootstrap' or not str(params.get('host', '')).startswith('/'):
        raise refused('--bootstrap-dsn-file: the nexloop_bootstrap DSN over the local socket (host=<socket directory>)')
    password = secrets.token_hex(24)
    with _sigterm_raises(), psycopg.connect(bootstrap, autocommit=True, connect_timeout=5) as admin:
        login, lapsed = admin.execute(f"""select rolcanlogin, coalesce(rolvaliduntil<=clock_timestamp(),false) from pg_roles
            where rolname='{CONFIGURATOR}'""").fetchone()
        if login and not lapsed:
            raise refused('a configurator window is open (or was left open minutes ago): retry after its VALID UNTIL')
        if login:
            _close(admin, bootstrap)  # a stale window whose password has lapsed: heal it before opening
        until = admin.execute(f"select (clock_timestamp()+interval '{WINDOW_MINUTES} minutes')::text").fetchone()[0]
        scratch = Path(tempfile.mkdtemp(prefix='nexloop-configurator-'))
        dsn = scratch / 'configurator-dsn'
        try:
            admin.execute(sql.SQL('alter role {} login password {} valid until {}').format(
                sql.Identifier(CONFIGURATOR), sql.Literal(scram_verifier(password)), sql.Literal(until)))
            write_private(dsn, make_conninfo(bootstrap, user=CONFIGURATOR, password=password))
            yield dsn
        finally:
            try:
                _close(admin, bootstrap)  # first: the window closes whatever happens to the files
            finally:
                dsn.unlink(missing_ok=True)
                for leftover in scratch.iterdir():
                    leftover.unlink(missing_ok=True)
                scratch.rmdir()
