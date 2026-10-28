"""Test outputs that the code under test requires below the repository's ignored .ci-results/ (prepare_project and
the community build context refuse any other place). A passing test's output is removed at teardown; a failing
one is kept for inspection, at most KEEP of them per prefix and none older than MAX_AGE (pruned at the next use)."""
import contextlib
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / '.ci-results'
KEEP = 3
MAX_AGE = 24 * 3600


def prune(prefix, *, keep=KEEP, max_age=MAX_AGE, now=None):
    """Kept failure outputs beyond the newest `keep`, or older than `max_age`; an output in use by a parallel
    worker is younger than any test, so the age bound alone never takes one, and the count bound skips the last hour."""
    now = time.time() if now is None else now
    found = sorted((p for p in RESULTS.glob(prefix + '*') if p.is_dir() and not p.is_symlink()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for index, path in enumerate(found):
        age = now - path.stat().st_mtime
        if age > max_age or (index >= keep and age > 3600):
            shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def results_output(request, prefix):
    """A new, not yet created path RESULTS/<prefix><random>; removed afterwards unless this test failed."""
    RESULTS.mkdir(exist_ok=True)
    prune(prefix)
    output = RESULTS / (prefix + secrets.token_hex(12))
    log = os.environ.get('NEXLOOP_TEST_RESULTS_LOG')
    if log:  # lets a test name exactly the outputs a child run made, whatever parallel workers do meanwhile
        with open(log, 'a') as written:
            written.write(str(output) + '\n')
    failed = request.session.testsfailed
    try:
        yield output
    finally:
        # The call phase is reported before teardown, so a failure of this test has been counted by now.
        if request.session.testsfailed == failed:
            shutil.rmtree(output, ignore_errors=True)


PREFIXES = ('launcher-test-', 'community-contract-')

if __name__ == '__main__':
    # scripts/ci/check runs this at the end, so kept outputs are bounded even when no test uses the fixture again.
    for prefix in sys.argv[1:] or PREFIXES:
        prune(prefix)
