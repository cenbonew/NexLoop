"""NX-055 E1 §1.4a item 2: which database grant_drift.py check reached. Run exactly as §1.3 runs the check (same image, same
$CHECK_COMMON $WINDOW, same /run/bootstrap-dsn); reads only, in a read-only transaction. Prints one JSON line, no DSN."""
import json
import pathlib

import psycopg

QUERY = """select pg_catalog.current_database(), pg_catalog.inet_server_port(), count(*)
from pg_catalog.pg_roles where oid<>10 and rolname !~ '^pg_'"""

with psycopg.connect(pathlib.Path('/run/bootstrap-dsn').read_text().strip()) as connection:
    connection.read_only = True
    with connection.cursor() as cursor:
        cursor.execute('show transaction_read_only')
        read_only = cursor.fetchone()[0]
        cursor.execute(QUERY)
        database, port, roles = cursor.fetchone()
    connection.rollback()
print(json.dumps({'current_database': database, 'inet_server_port': port, 'roles': roles, 'transaction_read_only': read_only}))
