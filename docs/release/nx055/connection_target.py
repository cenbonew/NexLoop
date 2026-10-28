"""NX-055 E1 §1.4a item 2: which database grant_drift.py check reached. Run exactly as §1.3 runs the check (same image, same
$CHECK_COMMON $WINDOW, same /run/bootstrap-dsn); reads only, in a read-only transaction. Prints one JSON line, no DSN. The DSN file
is read as grant_drift.py reads it (read_private_text: owner-only, bounded); on any error only the exception's class name is printed,
never its text (it can carry a host or user name), and the exit code is 2. An optional argument names another DSN file (local runs)."""
import json
import sys

import psycopg
from nexloop_eios.private_configuration import read_private_text

QUERY = """select pg_catalog.current_database(), pg_catalog.inet_server_port(), count(*)
from pg_catalog.pg_roles where oid<>10 and rolname !~ '^pg_'"""


def main(argv):
    dsn_file = argv[0] if argv else '/run/bootstrap-dsn'
    with psycopg.connect(read_private_text(dsn_file, maximum=4096).strip(), connect_timeout=5) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute('show transaction_read_only')
            read_only = cursor.fetchone()[0]
            cursor.execute(QUERY)
            database, port, roles = cursor.fetchone()
        connection.rollback()
    return {'current_database': database, 'inet_server_port': port, 'roles': roles, 'transaction_read_only': read_only}


if __name__ == '__main__':
    try:
        result = main(sys.argv[1:])
    except Exception as error:
        print(f'connection target: {type(error).__name__}', file=sys.stderr)
        sys.exit(2)
    print(json.dumps(result))
