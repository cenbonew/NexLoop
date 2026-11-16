"""Exercise the opt-in lifecycle in child pytest runs with synthetic files only."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

TESTS = Path(__file__).resolve().parent


def run_suite(tmp_path, source):
    suite = tmp_path / 'suite'
    suite.mkdir()
    (suite / 'test_cases.py').write_text(source)
    base = tmp_path / 'owned-base'
    env = {**os.environ, 'PYTHONPATH': str(TESTS), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'support.passed_tmp_cleanup',
                             '--basetemp', str(base), str(suite)], cwd=suite, env=env, capture_output=True, text=True)
    return result, base


def test_success_reclaims_each_case_after_teardown_but_keeps_unopted_and_neighbors(tmp_path):
    result, base = run_suite(tmp_path, '''
from pathlib import Path
import pytest

@pytest.fixture
def late_reader(tmp_path):
    yield
    assert (tmp_path / 'payload').read_bytes() == b'x'

@pytest.mark.parametrize('i', range(3))
@pytest.mark.usefixtures('reclaim_passed_tmp_path')
def test_copy(tmp_path, late_reader, i):
    assert not list(tmp_path.parent.glob('test_copy*/payload'))
    (tmp_path / 'payload').write_bytes(b'x')
    neighbor = tmp_path.parent / 'unowned'
    neighbor.mkdir(exist_ok=True)
    (neighbor / 'keep').write_text('keep')
    (tmp_path / 'link').symlink_to(neighbor, target_is_directory=True)
    (tmp_path / 'payload').chmod(0o400)
    tmp_path.chmod(0o500)

def test_unopted(tmp_path):
    (tmp_path / 'keep').write_text('unopted')
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(base.glob('test_copy*/payload'))
    assert (base / 'unowned/keep').read_text() == 'keep'
    assert list(base.glob('test_unopted*/keep'))


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown', 'skip'])
def test_failed_or_skipped_phase_keeps_first_failure_artifact(tmp_path, phase):
    result, base = run_suite(tmp_path, f'''
import pytest
@pytest.fixture
def failure(tmp_path, reclaim_passed_tmp_path):
    (tmp_path / 'evidence').write_text('first failure')
    if {phase!r} == 'setup': raise RuntimeError('setup failure')
    yield
    if {phase!r} == 'teardown': raise RuntimeError('teardown failure')

def test_case(tmp_path, failure):
    if {phase!r} == 'call': raise RuntimeError('call failure')
    if {phase!r} == 'skip': pytest.skip('synthetic skip')
''')
    assert result.returncode == (0 if phase == 'skip' else 1), result.stdout + result.stderr
    assert [p.read_text() for p in base.glob('test_case*/evidence') if not p.parent.is_symlink()] == ['first failure']


def test_cleanup_error_is_a_visible_failure_with_residual_evidence(tmp_path):
    result, base = run_suite(tmp_path, '''
import pytest
from support import passed_tmp_cleanup as plugin

@pytest.fixture
def broken_cleanup():
    yield
    def refused(*a, **kw): raise PermissionError('synthetic refusal')
    plugin.make_writable = refused

def test_case(tmp_path, reclaim_passed_tmp_path, broken_cleanup):
    (tmp_path / 'evidence').write_text('retained')
''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'large-copy tmp_path cleanup failed' in result.stdout
    assert list(base.glob('test_case*/evidence'))
