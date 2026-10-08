"""Disposable Homebrew PG; admin never used by business tests."""
import os, pathlib, shutil, socket, subprocess, tempfile
import psycopg
import pytest

@pytest.fixture
def pg():
    bindir=pathlib.Path(os.environ.get('NEXLOOP_TEST_PG_BIN','/opt/homebrew/opt/postgresql@18/bin'))
    if not (bindir/'initdb').exists():
        raise RuntimeError('PostgreSQL required: set NEXLOOP_TEST_PG_BIN (critical test may not skip)')
    root=pathlib.Path(tempfile.mkdtemp(prefix='nexloop-pg-'))
    data=root/'data'; sock=root/'socket';sock.mkdir()
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    subprocess.run([str(bindir/'initdb'),'-D',str(data),'-U','nexloop_bootstrap','--auth=trust','--no-locale','-E','UTF8'],check=True,stdout=subprocess.DEVNULL)
    started=False
    try:
        subprocess.run([str(bindir/'pg_ctl'),'-D',str(data),'-l',str(root/'postgres.log'),'-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,stdout=subprocess.DEVNULL)
        started=True
        yield f'host={sock} port={port} dbname=postgres user=nexloop_bootstrap'
    finally:
        if started:subprocess.run([str(bindir/'pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
        # Only the exact test-owned mkdtemp directory; no persistent cleanup.
        assert root.name.startswith('nexloop-pg-') and root.parent==pathlib.Path(tempfile.gettempdir())
        shutil.rmtree(root)

@pytest.fixture
def admin(pg):
    with psycopg.connect(pg,autocommit=True) as connection:
        yield connection
