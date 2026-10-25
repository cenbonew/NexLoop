"""The service's own stderr in an in-process CLI test: the lines the service printed, without the structured records of the
connection pool's logger (psycopg.pool). Those come from pools the test fixtures hold open (their background threads retry a
connection under parallel load and log a warning into whatever the current capture is); they are not the service's output."""
import json


def _pool_record(line):
    if not line.startswith('{'):
        return False
    try:
        return json.loads(line).get('logger') == 'psycopg.pool'
    except ValueError:
        return False


def service_stderr(err):
    return ''.join(line + '\n' for line in err.splitlines() if not _pool_record(line))
