"""Opt-in cleanup of a test's own large tmp_path after all phases pass.

Failed/skipped setup, call, or teardown retains its evidence. No session-wide
sweep, shared template, external path, or production cleanup is involved.
"""
import shutil

import pytest
from support.writable_tree import make_writable

_STATE = pytest.StashKey()


@pytest.fixture
def reclaim_passed_tmp_path(tmp_path, tmp_path_factory, request):
    base = tmp_path_factory.getbasetemp()
    if tmp_path.is_symlink() or tmp_path.parent != base:
        raise RuntimeError('large-copy cleanup requires this test worker\'s own tmp_path')
    request.node.stash[_STATE] = (tmp_path, {})


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    state = item.stash.get(_STATE, None)
    if state is None:
        return
    path, outcomes = state
    outcomes[report.when] = report.outcome
    if report.when != 'teardown' or outcomes != {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'}:
        return
    try:
        if path.is_symlink():
            raise RuntimeError('refusing a replaced tmp_path symlink')
        make_writable(path)  # sealed releases deliberately contain read-only files
        shutil.rmtree(path)
    except Exception as error:
        report.outcome = 'failed'
        report.longrepr = 'large-copy tmp_path cleanup failed: ' + repr(error)
