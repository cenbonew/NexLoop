"""The deployment layer of the real dispatch switch (0220): REAL_DISPATCH_ENABLED of the dispatching worker's own process.

Only the exact value 'true' opens it; absent, empty or anything else is closed. The worker passes it to SQL as the
transaction-local setting nexloop.real_dispatch_enabled in the transaction that admits the dispatch, where the tenant and
Action layers are checked against the current switch rows (fail closed)."""
import os

SETTING = 'nexloop.real_dispatch_enabled'


def deployment_open(environ=None):
    return (os.environ if environ is None else environ).get('REAL_DISPATCH_ENABLED') == 'true'


def apply(connection, environ=None):
    """Inside the admitting transaction only (is_local=true)."""
    connection.execute('select set_config(%s,%s,true)', (SETTING, 'true' if deployment_open(environ) else 'false'))
