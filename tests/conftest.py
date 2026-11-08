"""Disposable Homebrew PG; admin never used by business tests."""
import os, pathlib, shutil, socket, subprocess, tempfile
import psycopg
import pytest

DEFAULT_PG_BIN='/opt/homebrew/opt/postgresql@18/bin'

from support import read_object_compare as _compare
if _compare.enabled():
    # ADR-031 equivalence run only: wrap read_object after every catalog application (see support/read_object_compare.py).
    import nexloop_eios.bootstrap as _bootstrap
    _apply = _bootstrap.apply_migrations
    def _apply_and_wrap(connection, *args, **kwargs):
        result = _apply(connection, *args, **kwargs)
        if not connection.execute("select to_regclass('public.zz_read_compare_total')").fetchone()[0]:
            _compare.install(connection)
        return result
    _bootstrap.apply_migrations = _apply_and_wrap

@pytest.fixture(scope='session',autouse=True)
def _quiet_fixture_pools():
    """The connection pools the fixtures hold open retry a connection under parallel load and log a warning from their
    background threads into whatever capture is current (load-rules flake, batch 23 CI seq 99). Only errors of the
    psycopg.pool logger are kept for the session; a service under test that disables that logger itself is unaffected."""
    import logging
    logger=logging.getLogger('psycopg.pool');level=logger.level
    logger.setLevel(logging.ERROR)
    yield
    logger.setLevel(level)


# PG template data directory (CI speed, docs/implementation/ci-speed-analysis.md §3a option 1): each test process initdb's and
# bootstraps (the full catalog, with the loopback declaration installed first, exactly as below) once, stops that cluster and keeps
# its data directory; every test then gets its own copy of it and its own cluster, as isolated as before. Fixtures that call
# bootstrap() find the lineage applied and only check it (apply_migrations is idempotent). A test that must start from an empty
# cluster - it never bootstraps, or it tests bootstrapping itself - is marked pg_empty and gets the old path: its own initdb.
# NEXLOOP_TEST_PG_AUDIT=<file>: append one line per pg test, '<nodeid>\t<1 if the test applied migrations itself, else 0>', to find them.
_TEMPLATE = {}
_APPLIED = [False]


def _audit_hooks():
    import eios.adapters.postgres.migrations as _eios_migrations
    import nexloop_eios.bootstrap as _nexloop_bootstrap
    for module in (_eios_migrations, _nexloop_bootstrap):
        original = module.apply_migrations
        if getattr(original, '_pg_audit', False):
            continue

        def applied(*args, _original=original, **kwargs):
            _APPLIED[0] = True
            return _original(*args, **kwargs)
        applied._pg_audit = True
        module.apply_migrations = applied


_audit_hooks()


def _start(bindir, data, root):
    sock = root / 'socket';sock.mkdir()
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0));port = s.getsockname()[1]
    subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-l', str(root / 'postgres.log'), '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                   check=True, stdout=subprocess.DEVNULL)
    return f'host={sock} port={port} dbname=postgres user=nexloop_bootstrap'


def _template_directory(bindir):
    """This process's bootstrapped data directory (built once): initdb, loopback declaration, bootstrap, clean stop."""
    if 'data' not in _TEMPLATE:
        from support.loopback_effects import install as declare_loopback_effects
        import nexloop_eios.bootstrap as _nexloop_bootstrap
        root = pathlib.Path(tempfile.mkdtemp(prefix='nexloop-pg-template-'))
        data = root / 'data'
        subprocess.run([str(bindir / 'initdb'), '-D', str(data), '-U', 'nexloop_bootstrap', '--auth=trust', '--no-locale', '-E', 'UTF8'], check=True, stdout=subprocess.DEVNULL)
        dsn = _start(bindir, data, root)
        try:
            declare_loopback_effects(dsn)
            with psycopg.connect(dsn, autocommit=True) as connection:
                _nexloop_bootstrap.bootstrap(connection)
        finally:
            subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'], check=True, stdout=subprocess.DEVNULL)
        _TEMPLATE.update(data=data, root=root)
    return _TEMPLATE['data']


@pytest.fixture(scope='session', autouse=True)
def _pg_template_cleanup():
    yield
    root = _TEMPLATE.get('root')
    if root is not None and root.name.startswith('nexloop-pg-template-') and root.parent == pathlib.Path(tempfile.gettempdir()):
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def pg(request):
    bindir=pathlib.Path(os.environ.get('NEXLOOP_TEST_PG_BIN',DEFAULT_PG_BIN))
    if not (bindir/'initdb').exists():
        raise RuntimeError('PostgreSQL required: set NEXLOOP_TEST_PG_BIN (critical test may not skip)')
    empty = request.node.get_closest_marker('pg_empty') is not None
    template = None if empty else _template_directory(bindir)
    root=pathlib.Path(tempfile.mkdtemp(prefix='nexloop-pg-'))
    data=root/'data'
    if empty:
        subprocess.run([str(bindir/'initdb'),'-D',str(data),'-U','nexloop_bootstrap','--auth=trust','--no-locale','-E','UTF8'],check=True,stdout=subprocess.DEVNULL)
    else:
        shutil.copytree(template, data, symlinks=True)
        os.chmod(data, 0o700)
    started=False
    try:
        dsn=_start(bindir,data,root)
        started=True
        if empty:
            # 0220 test harness: loopback channels are declared not external, as trusted configuration does in production.
            from support.loopback_effects import install as declare_loopback_effects
            declare_loopback_effects(dsn)
        _APPLIED[0]=False
        yield dsn
    finally:
        audit=os.environ.get('NEXLOOP_TEST_PG_AUDIT')
        if audit:
            with open(audit,'a') as stream:stream.write(f'{request.node.nodeid}\t{int(_APPLIED[0])}\n')
        if started and _compare.enabled():_compare.record(dsn,request.node.nodeid)
        if started:subprocess.run([str(bindir/'pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
        # Only the exact test-owned mkdtemp directory; no persistent cleanup.
        assert root.name.startswith('nexloop-pg-') and root.parent==pathlib.Path(tempfile.gettempdir())
        shutil.rmtree(root)

@pytest.fixture
def admin(pg):
    with psycopg.connect(pg,autocommit=True) as connection:
        yield connection


def pytest_collection_modifyitems(config,items):
    """AT-015 real browser lane: tests marked real_browser run only when selected with -m real_browser (a separate, opt-in
    lane; deselected rather than skipped, so no green skip appears in the default stages)."""
    if 'real_browser' in (config.getoption('-m') or ''):return
    keep=[i for i in items if i.get_closest_marker('real_browser') is None]
    if len(keep)!=len(items):
        config.hook.pytest_deselected(items=[i for i in items if i.get_closest_marker('real_browser') is not None]);items[:]=keep
