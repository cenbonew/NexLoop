"""NX-035 test support: a disposable PostgreSQL 18 cluster with WAL archiving into a test-owned directory.

`archived_pg` replaces the conftest `pg` fixture in the modules that import it (pytest picks the module-level fixture named
`pg`), so the usual `admin` and business fixtures run on a cluster that can be backed up and restored to a point in time.
Everything lives in one mkdtemp directory removed at the end; no other database or directory is touched."""
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile

import psycopg
import pytest

# The same PostgreSQL the conftest cluster uses: NEXLOOP_TEST_PG_BIN, else the conftest default.
import conftest
PG_BIN = pathlib.Path(os.environ.get('NEXLOOP_TEST_PG_BIN') or conftest.DEFAULT_PG_BIN)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def cluster():
    if not (PG_BIN / 'initdb').exists() or not (PG_BIN / 'pg_basebackup').exists() or not (PG_BIN / 'pg_verifybackup').exists():
        raise RuntimeError('PostgreSQL 18 with pg_basebackup/pg_verifybackup required: set NEXLOOP_TEST_PG_BIN (critical test may not skip)')
    root = pathlib.Path(tempfile.mkdtemp(prefix='nexloop-pg-'))
    data, sock, archive = root / 'data', root / 'socket', root / 'archive'
    sock.mkdir()
    archive.mkdir()
    port = free_port()
    subprocess.run([str(PG_BIN / 'initdb'), '-D', str(data), '-U', 'nexloop_bootstrap', '--auth=trust', '--no-locale', '-E', 'UTF8'],
                   check=True, stdout=subprocess.DEVNULL)
    with open(data / 'postgresql.conf', 'a') as conf:
        conf.write(f"wal_level = replica\narchive_mode = on\narchive_command = 'test ! -f \"{archive}/%f\" && cp \"%p\" \"{archive}/%f\"'\n"
                   "max_wal_senders = 4\n")
    started = False
    try:
        subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(data), '-l', str(root / 'postgres.log'), '-o', f"-k {sock} -p {port} -c listen_addresses=''",
                        '-w', 'start'], check=True, stdout=subprocess.DEVNULL)
        started = True
        yield {'root': root, 'data': data, 'socket': sock, 'archive': archive, 'port': port, 'bin': PG_BIN,
               'dsn': f'host={sock} port={port} dbname=postgres user=nexloop_bootstrap',
               'as_role': lambda role: f'host={sock} port={port} dbname=postgres user={role}'}
    finally:
        if started:
            subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'], check=True, stdout=subprocess.DEVNULL)
        # Only the exact test-owned mkdtemp directory; no persistent cleanup.
        assert root.name.startswith('nexloop-pg-') and root.parent == pathlib.Path(tempfile.gettempdir())
        shutil.rmtree(root)


@pytest.fixture
def pg(cluster):
    return cluster['dsn']


def switch_wal(admin, cluster, timeout=30):
    """Close the current WAL segment and wait until the archiver has it (the archive is the PITR source)."""
    name = admin.execute('select pg_walfile_name(pg_current_wal_lsn())').fetchone()[0]
    admin.execute('select pg_switch_wal()')
    import time
    deadline = time.monotonic() + timeout
    while not (cluster['archive'] / name).exists():
        assert time.monotonic() < deadline, 'WAL segment not archived'
        time.sleep(0.1)
    return name


def as_role(cluster, role):
    return psycopg.connect(cluster['as_role'](role), autocommit=True)
