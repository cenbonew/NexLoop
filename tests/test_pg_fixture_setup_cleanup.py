"""Offline fixture setup failures: synthetic temp files and stub processes only.
No PostgreSQL executable, socket, database, or existing temporary root is used.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
import conftest as C
from support import loopback_effects
import nexloop_eios.bootstrap as bootstrap


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'initdb').touch()  # existence check only; never executed
    previous = tmp_path / 'nexloop-pg-previous'
    previous.mkdir()
    (previous / 'keep').write_text('preexisting synthetic sentinel')
    template = tmp_path / 'source-template'
    template.mkdir()
    (template / 'keep').write_text('source template remains')
    state = SimpleNamespace(root=None, created=[], removed=[], commands=[], starts=[],
                            failure=None, cleanup_failure=False, primary=OSError(122, 'synthetic quota failure'),
                            template=template, previous=previous, declared=[])
    real_remove, real_copy = C.shutil.rmtree, C.shutil.copytree

    def mkdtemp(*, prefix):
        root = tmp_path / (prefix + 'owned-' + str(len(state.created)))
        root.mkdir()
        state.created.append(root)
        state.root = root
        return str(root)

    def remove(root, *args, **kw):
        root = Path(root)
        assert root in state.created and root not in (previous, template, tmp_path)
        state.removed.append(root)
        if state.cleanup_failure:
            raise PermissionError('synthetic cleanup refusal')
        return real_remove(root, *args, **kw)

    def run(argv, **kw):
        state.commands.append(argv)
        if Path(argv[0]).name == 'initdb':
            data = Path(argv[argv.index('-D') + 1])
            data.mkdir()
            (data / 'partial').write_text('synthetic partial setup')
            if state.failure == 'initdb':
                raise state.primary
        else:
            assert Path(argv[0]).name == 'pg_ctl' and argv[-1] == 'stop'
        return SimpleNamespace(returncode=0)

    def copy(source, target, **kw):
        assert source == template and kw == {'symlinks': True}
        if state.failure == 'copytree':
            target.mkdir()
            (target / 'partial').write_text('synthetic partial copy')
            raise state.primary
        return real_copy(source, target, **kw)

    def start(_bindir, _data, root):
        state.starts.append(root)
        return 'synthetic-dsn-not-connectable'

    def no_database(*args, **kw):
        raise AssertionError('a real database call is forbidden')

    monkeypatch.setenv('NEXLOOP_TEST_PG_BIN', str(bindir))
    monkeypatch.delenv('NEXLOOP_TEST_PG_AUDIT', raising=False)
    monkeypatch.setattr(C.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setattr(C.tempfile, 'mkdtemp', mkdtemp)
    monkeypatch.setattr(C.shutil, 'rmtree', remove)
    monkeypatch.setattr(C.shutil, 'copytree', copy)
    monkeypatch.setattr(C.subprocess, 'run', run)
    monkeypatch.setattr(C, '_start', start)
    monkeypatch.setattr(C, '_template_directory', C._template_directory)
    monkeypatch.setattr(C, '_TEMPLATE', {})
    monkeypatch.setattr(C.psycopg, 'connect', no_database)
    monkeypatch.setattr(C._compare, 'enabled', lambda: False)
    monkeypatch.setattr(loopback_effects, 'install', lambda dsn: state.declared.append(dsn))
    return state


def request(empty):
    return SimpleNamespace(node=SimpleNamespace(nodeid='synthetic-setup', get_closest_marker=lambda name: object() if empty else None))


def unchanged(state):
    assert (state.previous / 'keep').read_text() == 'preexisting synthetic sentinel'
    assert (state.template / 'keep').read_text() == 'source template remains'


@pytest.mark.parametrize('phase', ['initdb', 'copytree'])
@pytest.mark.parametrize('cleanup_failure', [False, True])
def test_failed_pg_setup_removes_only_its_new_root_and_preserves_original(monkeypatch, sandbox, phase, cleanup_failure):
    s = sandbox
    s.failure, s.cleanup_failure = phase, cleanup_failure
    if phase == 'copytree':
        monkeypatch.setattr(C, '_template_directory', lambda _bin: s.template)
    gen = C.pg.__wrapped__(request(phase == 'initdb'))
    with pytest.raises(OSError) as caught:
        next(gen)
    assert caught.value is s.primary
    assert s.removed == [s.root] and not s.starts
    assert s.root.exists() is cleanup_failure
    if cleanup_failure:
        assert any('PermissionError' in n for n in caught.value.__notes__)
    unchanged(s)


@pytest.mark.parametrize('cleanup_failure', [False, True])
def test_failed_template_initdb_is_not_cached_and_preserves_original(sandbox, cleanup_failure):
    s = sandbox
    s.failure, s.cleanup_failure = 'initdb', cleanup_failure
    with pytest.raises(OSError) as caught:
        C._template_directory(Path('synthetic-bin'))
    assert caught.value is s.primary and C._TEMPLATE == {}
    assert s.removed == [s.root] and not s.starts
    assert s.root.exists() is cleanup_failure
    unchanged(s)


@pytest.mark.parametrize('empty', [False, True])
def test_normal_pg_path_yields_then_stops_and_removes_its_root(monkeypatch, sandbox, empty):
    s = sandbox
    if not empty:
        monkeypatch.setattr(C, '_template_directory', lambda _bin: s.template)
    gen = C.pg.__wrapped__(request(empty))
    assert next(gen) == 'synthetic-dsn-not-connectable'
    assert s.root.exists() and s.removed == []
    gen.close()
    assert s.starts == [s.root] and s.removed == [s.root] and not s.root.exists()
    assert len([a for a in s.commands if Path(a[0]).name == 'pg_ctl']) == 1
    unchanged(s)


def test_normal_template_is_stopped_and_reused_until_existing_session_cleanup(monkeypatch, sandbox):
    s = sandbox
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(C.psycopg, 'connect', lambda *a, **kw: Connection())
    applied = []
    monkeypatch.setattr(bootstrap, 'bootstrap', lambda conn: applied.append(conn))
    first = C._template_directory(Path('synthetic-bin'))
    assert C._template_directory(Path('synthetic-bin')) == first
    assert len(s.created) == len(s.starts) == len(applied) == 1 and not s.removed
    assert C._TEMPLATE == {'data': first, 'root': s.root}
    assert len([a for a in s.commands if Path(a[0]).name == 'pg_ctl']) == 1
    cleanup = C._pg_template_cleanup.__wrapped__()
    next(cleanup)
    with pytest.raises(StopIteration):
        next(cleanup)
    assert s.removed == [s.root]
    unchanged(s)


@pytest.mark.parametrize('template_setup', [False, True])
def test_no_directory_is_removed_when_allocation_itself_fails(monkeypatch, sandbox, template_setup):
    s = sandbox
    def allocation_failed(**kw):
        raise s.primary
    monkeypatch.setattr(C.tempfile, 'mkdtemp', allocation_failed)
    with pytest.raises(OSError) as caught:
        if template_setup:
            C._template_directory(Path('synthetic-bin'))
        else:
            next(C.pg.__wrapped__(request(True)))
    assert caught.value is s.primary and not s.created and not s.removed
    unchanged(s)
