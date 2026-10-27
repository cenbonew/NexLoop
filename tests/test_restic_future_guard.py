"""scripts/deploy/restic_future_guard.py (L6 F1 of the rest-server review): no forget/prune while a snapshot claims a future time, and a
restore's journal snapshot chosen as the newest not in the future instead of restic's `latest`. Synthetic snapshot lists only."""
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import restic_future_guard as G  # noqa: E402

NOW = datetime(2026, 10, 27, 3, 30, tzinfo=UTC)
REAL = [{'id': '1' * 64, 'short_id': '11111111', 'time': '2026-10-27T11:25:00.123456789+08:00', 'tags': ['journal']},
        {'id': '2' * 64, 'short_id': '22222222', 'time': '2026-10-26T03:30:00Z', 'tags': ['journal']},
        {'id': '3' * 64, 'short_id': '33333333', 'time': '2026-10-27T03:00:00Z', 'tags': ['files_artifact']}]


def test_restic_times_with_nanoseconds_and_offsets_are_read():
    assert G.parse_time('2026-10-27T11:25:00.123456789+08:00') == datetime(2026, 10, 27, 3, 25, 0, 123456, tzinfo=UTC)
    assert G.parse_time('2026-10-27T03:25:00Z') == datetime(2026, 10, 27, 3, 25, tzinfo=UTC)
    for bad in ('yesterday', '2026-10-27 03:25:00', '2026-10-27T03:25:00', None, 1700000000, '2026-13-40T00:00:00Z'):
        assert G.parse_time(bad) is None, bad


@pytest.mark.parametrize('extra, ok, ahead, unreadable', [
    ([], True, [], 0),
    ([{'short_id': 'ffffffff', 'time': '2030-01-01T00:00:00Z'}], False, ['ffffffff'], 0),           # the F1 attack
    ([{'short_id': 'eeeeeeee', 'time': '2026-10-27T04:31:00Z'}], False, ['eeeeeeee'], 0),           # 61 minutes ahead
    ([{'short_id': 'dddddddd', 'time': '2026-10-27T04:29:00Z'}], True, [], 0),                      # within the clock margin
    ([{'short_id': 'cccccccc', 'time': 'not a time'}], False, [], 1),                                # unreadable: never passed
    (['not a snapshot'], False, [], 1),
])
def test_a_snapshot_more_than_the_margin_ahead_or_unreadable_fails_the_check(extra, ok, ahead, unreadable):
    assert G.check(REAL + extra, NOW, 3600) == {'ok': ok, 'snapshots': len(REAL) + len(extra), 'future': ahead, 'unreadable': unreadable}
    assert G.check({'not': 'a list'}, NOW, 3600)['ok'] is False


def test_the_restore_selection_is_the_newest_tagged_snapshot_not_in_the_future():
    assert G.latest(REAL, 'journal', NOW) == '1' * 64
    assert G.latest(REAL + [{'id': '9' * 64, 'time': '2026-10-27T03:40:00Z', 'tags': ['journal']}], 'journal', NOW) == '1' * 64
    assert G.latest(REAL, 'files_runtime', NOW) is None


class Run:
    def __init__(self, code=0, stdout=None):
        self.code, self.stdout, self.calls = code, json.dumps(REAL) if stdout is None else stdout, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.code, self.stdout, '')


def main(argv, run):
    out = io.StringIO()
    with redirect_stdout(out):
        code = G.main(argv, run=run, clock=lambda: NOW)
    return code, json.loads(out.getvalue())


def test_the_command_line_lists_without_a_lock_and_exits_one_on_anything_but_a_clean_list():
    run = Run()
    code, doc = main(['--', '/usr/local/bin/restic', '--no-cache', '--repository-file', '/f', '--password-file', '/p'], run)
    assert code == 0 and doc == {'ok': True, 'snapshots': 3, 'future': [], 'unreadable': 0}
    assert run.calls == [['/usr/local/bin/restic', '--no-cache', '--repository-file', '/f', '--password-file', '/p', 'snapshots', '--json', '--no-lock']]
    future = json.dumps(REAL + [{'id': 'f' * 64, 'short_id': 'ffffffff', 'time': '2030-01-01T00:00:00Z', 'tags': ['journal']}])
    assert main(['--', 'restic'], Run(stdout=future)) == (1, {'ok': False, 'snapshots': 4, 'future': ['ffffffff'], 'unreadable': 0})
    assert main(['--', 'restic'], Run(code=12)) == (1, {'ok': False, 'error': 'restic snapshots exit 12'})
    assert main(['--', 'restic'], Run(stdout='garbage')) == (1, {'ok': False, 'error': 'restic snapshots: not JSON'})
    assert main(['--max-ahead-seconds', '-1', '--', 'restic'], Run())[0] == 2 and main([], Run())[0] == 2
    code, doc = main(['--select-latest', 'journal', '--', 'restic'], Run())
    assert code == 0 and doc['selected'] == '1' * 64
    code, doc = main(['--select-latest', 'journal', '--', 'restic'], Run(stdout=future))
    assert code == 1 and 'selected' not in doc  # a future snapshot exists: nothing is selected at all
    code, doc = main(['--select-latest', 'files_runtime', '--', 'restic'], Run())
    assert code == 1 and doc['selected'] is None


class Locks:
    """`restic list locks --no-lock` answering the lock ids it is given, one list per call (the last repeated)."""
    def __init__(self, *answers, code=0):
        self.answers, self.code, self.calls, self.slept = list(answers), code, [], []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return subprocess.CompletedProcess(argv, self.code, ''.join(f'{i}\n' for i in answer), '')


def wait(argv, run):
    clock = iter(range(0, 10 ** 6, 10))  # each sleep is 10 seconds
    out = io.StringIO()
    with redirect_stdout(out):
        code = G.main(argv, run=run, sleep=run.slept.append, monotonic=lambda: next(clock))
    return code, json.loads(out.getvalue())


def test_wait_unlocked_waits_for_no_lock_and_fails_after_its_time():
    """L6 F2: the files repository's maintenance stops the rest-server between two file backup rounds, not in one: it waits until no
    lock is held, and only lists locks (no snapshot check, no lock of its own)."""
    run = Locks(['a' * 64], ['b' * 64], [])
    code, doc = wait(['--wait-unlocked', '600', '--', 'restic', '-r', '/r'], run)
    assert (code, doc) == (0, {'ok': True, 'locks': 0}) and run.slept == [10, 10]
    assert run.calls == [['restic', '-r', '/r', 'list', 'locks', '--no-lock']] * 3
    run = Locks(['a' * 64])
    code, doc = wait(['--wait-unlocked', '30', '--', 'restic'], run)
    assert code == 1 and doc == {'ok': False, 'locks': 1, 'error': 'locks still held after 30 seconds'} and len(run.calls) == 3
    assert wait(['--wait-unlocked', '30', '--', 'restic'], Locks([], code=1)) == (1, {'ok': False, 'error': 'restic list locks exit 1'})
    for bad in (['--wait-unlocked', '0'], ['--wait-unlocked', '3601'], ['--wait-unlocked', '30', '--select-latest', 'journal']):
        assert wait([*bad, '--', 'restic'], Locks(['a' * 64]))[0] == 2, bad
