"""Known risk v0.1 §4 (run-data-pg-upgrade-20261019 §4.2): after a PostgreSQL restart the pools kept broken connections and each
cost one request a 503 (a worker its next tick). Every pool built by create_pool (API, identity, workers) now checks a connection
when it is taken out. Simulated restart: every server process of the role is terminated, as a restart ends them."""
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from eios.adapters.postgres.database import create_pool
from eios.persistence.settings import StorageSettings
from nexloop_eios.bootstrap import bootstrap


def restart_sessions(admin, role):
    """What a PostgreSQL restart does to the pooled connections of `role`: every one of them is gone."""
    ended = admin.execute('select count(*) from (select pg_terminate_backend(pid) from pg_stat_activity where usename=%s) t', (role,)).fetchone()[0]
    return ended


def test_the_first_request_after_a_restart_gets_a_live_connection(admin, pg):
    bootstrap(admin)
    pool = create_pool(StorageSettings(database_url=make_conninfo(pg, user='nexloop_api'), pool_min_size=2, pool_max_size=2), open_pool=True)
    try:
        for _ in range(2):
            with pool.connection() as c:
                assert c.execute('select 1').fetchone() == (1,)
        assert restart_sessions(admin, 'nexloop_api') >= 1
        # Every pooled connection is broken now; each request still gets a live one (no OperationalError, no 503).
        for _ in range(4):
            with pool.connection() as c:
                assert c.execute('select 1').fetchone() == (1,)
        assert pool.get_stats().get('connections_lost', 0) >= 1  # the broken ones were found at checkout and replaced
    finally:
        pool.close()


def test_without_the_check_the_first_request_after_a_restart_fails(admin, pg):
    """The behaviour the check removes (the pool as before): a broken pooled connection reaches the caller."""
    from psycopg_pool import ConnectionPool
    bootstrap(admin)
    pool = ConnectionPool(make_conninfo(pg, user='nexloop_api'), min_size=1, max_size=1, open=True)
    try:
        with pool.connection() as c:
            c.execute('select 1')
        restart_sessions(admin, 'nexloop_api')
        with pytest.raises(psycopg.OperationalError):
            with pool.connection() as c:
                c.execute('select 1')
    finally:
        pool.close()


def test_a_worker_tick_after_a_restart_authenticates_and_reads(admin, pg):
    """The worker path: open_core (create_pool), the service authentication every tick does, a work feed read; after the
    restart the next tick neither fails nor needs a retry."""
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    from nexloop_eios.assembly import open_core
    from nexloop_eios.authorization import authenticate_service
    from multi_authority_fixture import seed_multi_authority
    from support.tenants import TENANT_A
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    with open_core(make_conninfo(pg, user='nexloop_domain_worker')) as pool:
        _, token = seed_multi_authority(admin, pool, [('eios:action:NexLoop.feed.recall-instance:1', ResourceType.ACTION, Operation.EXECUTE)],
                                        identity_suffix='-restart')
        authenticate_service(pool, token, world='real')
        assert restart_sessions(admin, 'nexloop_domain_worker') >= 1
        for _ in range(3):  # the next ticks: every pooled connection was broken, none reaches the worker
            assert authenticate_service(pool, token, world='real').authentication.tenant_id
