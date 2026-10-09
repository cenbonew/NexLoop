"""Bounded fail-closed assembly; no Memory fallback or administrative DSN."""
from contextlib import contextmanager
from psycopg_pool import ConnectionPool
from eios.adapters.postgres.database import create_pool, StorageUnavailable
from eios.persistence.settings import StorageSettings
from nexloop_eios.bootstrap import verify

APPLICATION_ROLES = frozenset({
    'nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler',
})


def verify_application_role(connection):
    """Reject SET ROLE disguises and inherited paths to elevated authority.

    NX-049 4a: on a backend request's own connection the check runs once per request.
    """
    from nexloop_eios.request_connection import remember_role, verified_role
    cached = verified_role(connection)
    if cached is not None:
        return cached
    role = connection.execute('''
        select current_user, session_user, rolsuper, rolbypassrls, rolcreaterole,
               rolcreatedb, rolreplication,
               exists (
                   select 1 from pg_roles elevated
                   where elevated.rolname <> current_user
                     and (elevated.rolsuper or elevated.rolbypassrls
                          or elevated.rolcreaterole or elevated.rolcreatedb
                          or elevated.rolreplication or elevated.rolname = 'nexloop_owner')
                     and pg_has_role(session_user, elevated.oid, 'MEMBER')
               )
        from pg_roles where rolname=current_user
    ''').fetchone()
    if (not role or role[0] not in APPLICATION_ROLES or role[0] != role[1]
            or any(role[2:])):
        raise StorageUnavailable('application database role is not restricted')
    remember_role(connection, role[0])
    return role[0]


@contextmanager
def open_core(database_url: str):
    pool: ConnectionPool = create_pool(StorageSettings(database_url=database_url), open_pool=True)
    try:
        pool.wait(timeout=10)
        with pool.connection() as connection:
            verify_application_role(connection)
            verify(connection)
        yield pool
    finally:
        pool.close()
