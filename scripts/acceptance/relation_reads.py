"""The connection of the NX-054 stage read tools (relation_types_precheck.py, relation_derive_timing.py): the existing read-only
metrics login (nexloop_metrics executes functions only and writes nothing - L6 review R-2: the server's read-only identity, not a
client setting), refused unless the session is that role, with a statement and lock timeout on every query."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
from nexloop_eios.private_configuration import read_private_text  # noqa: E402

ROLE = 'nexloop_metrics'


class Refused(ValueError):
    pass


def connect_metrics(path, *, statement_timeout='60s'):
    import psycopg
    try:
        dsn = read_private_text(path, maximum=8192).strip()
    except Exception:  # noqa: BLE001 - the file is named, never its content
        raise Refused('--dsn-file: unreadable or not a 0600 file of the caller') from None
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    return checked(conn, statement_timeout=statement_timeout)


def checked(conn, *, statement_timeout='60s'):
    if conn.execute('select current_user,session_user').fetchone() != (ROLE, ROLE):
        conn.close()
        raise Refused(f'--dsn-file: the read-only {ROLE} login only')
    conn.execute(f"set statement_timeout='{statement_timeout}'")
    conn.execute("set lock_timeout='3s'")
    return conn
