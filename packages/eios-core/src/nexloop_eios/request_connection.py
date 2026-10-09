"""NX-049 means 4a: one pooled connection per backend request; transaction boundaries unchanged.

``RequestConnectionPool`` wraps the backend pool. Inside ``request_connection_scope`` (opened
at the Backend request entry), a checkout that finds the request's own connection idle -- no
transaction open on it -- reuses that connection instead of taking another one from the pool.
Every caller still opens and ends its own transaction exactly where it did before: a block
that leaves a transaction open is committed at its end (rolled back on error), as the pool's
own ``connection()`` does. A checkout made while the request connection has a transaction
open (nested work, e.g. proofs built inside the effect-tool transaction) uses the request's
second connection, again only while that one is idle; nesting depth is unchanged (at most two
per request, see LifecycleLock), and a third level takes a separate pooled connection as
before. Outside any scope the facade is the plain pool.

The application-role check (assembly.verify_application_role) runs once per request
connection: the result is kept on the scope, never on the pooled connection, so the next
request (or a connection outside the scope) is checked again.
"""
from contextlib import contextmanager
import contextvars

from psycopg.pq import TransactionStatus

_SCOPE = contextvars.ContextVar('nexloop_request_connection', default=None)


SCOPED_CONNECTIONS = 2  # the request connection and one for nested work (LifecycleLock budget)


class _Scope:
    __slots__ = ('pool', 'connections', 'roles')

    def __init__(self, pool):
        self.pool, self.connections, self.roles = pool, [], {}


class RequestConnectionPool:
    """Pool facade; attribute access other than connection() goes to the wrapped pool."""

    def __init__(self, pool):
        self._pool = pool

    def __getattr__(self, name):
        return getattr(self._pool, name)

    @contextmanager
    def connection(self, timeout=None):
        scope = _SCOPE.get()
        if scope is None or scope.pool is not self._pool:
            with self._pool.connection(timeout=timeout) as connection:
                yield connection
            return
        for held in [c for c in scope.connections if c.closed]:
            scope.connections.remove(held);scope.roles.pop(id(held), None)
            self._pool.putconn(held)  # a broken connection is discarded by the pool
        connection = next((c for c in scope.connections if c.info.transaction_status == TransactionStatus.IDLE), None)
        if connection is None and len(scope.connections) < SCOPED_CONNECTIONS:
            connection = self._pool.getconn(timeout=timeout)
            scope.connections.append(connection)
        if connection is None:
            # Deeper nesting than the request budget: a separate pooled connection, as before.
            with self._pool.connection(timeout=timeout) as other:
                yield other
            return
        try:
            yield connection
        except BaseException:
            if not connection.closed and connection.info.transaction_status != TransactionStatus.IDLE:
                connection.rollback()
            raise
        if not connection.closed and connection.info.transaction_status != TransactionStatus.IDLE:
            connection.commit()


@contextmanager
def request_connection_scope(pool):
    """One request connection for the duration of a backend request (re-entrant)."""
    if not isinstance(pool, RequestConnectionPool) or _SCOPE.get() is not None:
        yield
        return
    scope = _Scope(pool._pool)
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)
        for connection in reversed(scope.connections):
            try:
                if not connection.closed and connection.info.transaction_status != TransactionStatus.IDLE:
                    connection.rollback()
            finally:
                pool._pool.putconn(connection)


def verified_role(connection):
    """The application role already verified for this request connection, else None."""
    scope = _SCOPE.get()
    if scope is None or not any(c is connection for c in scope.connections):return None
    return scope.roles.get(id(connection))


def remember_role(connection, role):
    scope = _SCOPE.get()
    if scope is not None and any(c is connection for c in scope.connections):
        scope.roles[id(connection)] = role
