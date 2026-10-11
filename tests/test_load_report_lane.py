"""NX-031 lane script (scripts/load/run_on_lane.sh) in a disposable repository with fake template builder and matrix runner.

The real template builder and matrix runner are L4's and run only on the CI lane; here they are replaced by fakes that write
the results contract, so the script's own duties are checked: preflight refusals (§5.1 safety, window, clean checkout), the
CI lock held for the whole run, report generation, the failure summary, and stopping everything the run started.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_RUNNER = textwrap.dedent('''\
    import json, os, subprocess, sys
    from pathlib import Path
    sys.path.insert(0, os.environ['FAKE_TESTS_DIR'])
    import test_load_report as t
    args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    out, lane = Path(args['--out']), Path(args['--lane-root'])
    assert Path(args['--template'], 'template.json').exists() and os.environ['REAL_DISPATCH_ENABLED'] == 'false'
    assert os.environ['NEXLOOP_TEST_GUARD_WORKERS'] == '4' and os.environ['LOAD_CI_COMMAND'] == 'scripts/ci/check'
    # The CI lock is held by the lane for the whole run: a second taker is refused.
    import fcntl
    with open(os.environ['NEXLOOP_CI_LOCK'], 'a') as other:
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sys.exit('CI lock was not held')
        except BlockingIOError:
            pass
    # Leftovers a crashed runner could leave: a cluster pid file and a process naming the run directory.
    (lane / 'cell-C4' / 'pgdata').mkdir(parents=True)
    (lane / 'cell-C4' / 'pgdata' / 'postmaster.pid').write_text('1')
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', str(lane / 'marker')], start_new_session=True)
    print('runner: synthetic cells')
    if os.environ.get('FAKE_RUNNER_FAIL'):
        print('runner: cell C4 service_start_failed'); sys.exit(3)
    (out / 'cells').mkdir()
    (out / 'run.json').write_text(json.dumps({'run_id': 'r', 'seed': 31, 'consumers': 1000, 'manifest_digest': 'a' * 64, 'commit': 'b' * 40,
        'catalog_digest': 'c' * 64, 'catalog_head': '0133', 'matrix_version': 1, 'template': {'key': 'd' * 64, 'build_seconds': 5},
        'started_at': '2026-10-11T22:00:00Z', 'finished_at': '2026-10-12T02:00:00Z'}))
    for name in t.report.CELLS:
        t.make_cell(out / 'cells', name)
''')


def fake_bin(tmp):
    b = tmp / 'bin'
    b.mkdir()
    (b / 'uv').write_text(f'#!/bin/sh\n[ "$1 $2 $3" = "run --frozen python" ] || exit 9\nshift 3\nexec {sys.executable} "$@"\n')
    pg = tmp / 'pgbin'
    pg.mkdir()
    (pg / 'initdb').write_text('#!/bin/sh\nexit 0\n')
    (pg / 'pg_ctl').write_text(f'#!/bin/sh\necho "$@" >> {tmp}/pg_ctl.calls\nexit 0\n')
    if not shutil.which('flock'):
        # flock(1) stand-in for macOS: the lock belongs to the inherited open file description, exactly as with flock(1).
        (b / 'flock').write_text(textwrap.dedent(f'''\
            #!{sys.executable}
            import fcntl, sys, time
            wait, fd = float(sys.argv[2]), int(sys.argv[3])
            end = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); sys.exit(0)
                except BlockingIOError:
                    if time.monotonic() >= end: sys.exit(1)
                    time.sleep(0.1)
            '''))
    for f in list(b.iterdir()) + list(pg.iterdir()):
        f.chmod(0o755)
    return b, pg


@pytest.fixture
def lane(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'scripts/load').mkdir(parents=True)
    (repo / 'deploy/load').mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/load/run_on_lane.sh', repo / 'scripts/load/run_on_lane.sh')
    shutil.copy(ROOT / 'scripts/load/report.py', repo / 'scripts/load/report.py')
    (repo / 'scripts/load/build_template.sh').write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = --out ] && out=$2; shift; done\n'
                                                        'mkdir -p "$out" && echo \'{"seed":31}\' > "$out/template.json"\n')
    (repo / 'scripts/load/matrix.py').write_text(FAKE_RUNNER)
    (repo / 'deploy/load/matrix.v1.json').write_text('{"version": 1}\n')
    (repo / '.gitignore').write_text('.ci-results/\n')
    for f in ('run_on_lane.sh', 'build_template.sh'):
        (repo / 'scripts/load' / f).chmod(0o755)
    git = lambda *a: subprocess.run(['git', *a], cwd=repo, check=True, capture_output=True)
    git('init', '-q', '-b', 'main')
    git('-c', 'user.email=test@example.invalid', '-c', 'user.name=lane', 'add', '.')
    git('-c', 'user.email=test@example.invalid', '-c', 'user.name=lane', 'commit', '-q', '-m', 'lane fixture')
    b, pg = fake_bin(tmp_path)
    env = {'PATH': f'{b}:/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(tmp_path), 'NEXLOOP_CI_LOCK': str(tmp_path / 'ci.lock'),
           'NEXLOOP_TEST_PG_BIN': str(pg), 'LOAD_MIN_FREE_GIB': '0', 'LOAD_LOCK_WAIT': '1', 'FAKE_TESTS_DIR': str(ROOT / 'tests')}
    def run(*args, **extra):
        return subprocess.run(['bash', str(repo / 'scripts/load/run_on_lane.sh'), *args], cwd=repo, env={**env, **extra}, capture_output=True, text=True, timeout=120)
    return dict(repo=repo, run=run, tmp=tmp_path)


def only_run_dir(repo):
    runs = [p for p in (repo / '.ci-results/load').iterdir() if p.is_dir()]
    assert len(runs) == 1
    return runs[0]


def no_leftovers(run_dir):
    for _ in range(50):
        if subprocess.run(['pgrep', '-f', str(run_dir)], capture_output=True).returncode == 1:
            return True
        time.sleep(0.2)
    return False


def test_full_run_writes_the_report_and_stops_what_it_started(lane):
    result = lane['run']('--outside-window')
    assert result.returncode == 0, result.stdout + result.stderr
    run_dir = only_run_dir(lane['repo'])
    summary = json.loads((run_dir / 'lane-summary.json').read_text())
    assert summary['status'] == 'completed' and summary['step'] == 'done' and summary['exit_code'] == 0
    assert set(summary['seconds']) == {'template', 'matrix', 'report'} and summary['cells'].count(',') == 7
    assert json.loads((run_dir / 'report.json').read_text())['at060']['result'] == 'passed' and (run_dir / 'report.md').exists()
    assert not (run_dir / 'failure-summary.md').exists() and oct(run_dir.stat().st_mode)[-3:] == '700'
    assert no_leftovers(run_dir) and 'cell-C4/pgdata' in (lane['tmp'] / 'pg_ctl.calls').read_text()


def test_failed_step_keeps_a_summary_and_cleans_up(lane):
    result = lane['run']('--outside-window', FAKE_RUNNER_FAIL='1')
    assert result.returncode == 1
    run_dir = only_run_dir(lane['repo'])
    summary = json.loads((run_dir / 'lane-summary.json').read_text())
    assert summary['status'] == 'failed' and summary['step'] == 'matrix' and summary['exit_code'] == 3 and set(summary['seconds']) == {'template', 'matrix'}
    failure = (run_dir / 'failure-summary.md').read_text()
    assert 'step: matrix, exit code: 3' in failure and 'service_start_failed' in failure
    assert not (run_dir / 'report.json').exists() and no_leftovers(run_dir)


@pytest.mark.parametrize('extra,reason', [({'REAL_DISPATCH_ENABLED': 'true'}, 'REAL_DISPATCH_ENABLED'), ({'MODEL_API_KEY': 'x'}, 'MODEL_API_KEY'),
                                          ({'PGHOST': 'db'}, 'PGHOST'), ({'NEXLOOP_CI_LOCK': ''}, 'NEXLOOP_CI_LOCK')])
def test_preflight_refuses_unsafe_environment(lane, extra, reason):
    result = lane['run']('--outside-window', **extra)
    results = lane['repo'] / '.ci-results/load'
    assert result.returncode == 2 and reason in result.stderr
    assert not results.exists() or not any(results.iterdir())


def test_preflight_refuses_outside_the_window_and_a_dirty_checkout(lane):
    now = datetime.now()
    later = (now + timedelta(hours=2)).strftime('%H:00'), (now + timedelta(hours=3)).strftime('%H:00')
    result = lane['run'](LOAD_WINDOW=f'{later[0]}-{later[1]}')
    assert result.returncode == 2 and 'outside the night window' in result.stderr
    (lane['repo'] / 'scripts/load/report.py').write_text('# changed\n')
    result = lane['run']('--outside-window')
    assert result.returncode == 2 and 'local changes' in result.stderr


def test_busy_ci_lock_is_not_bypassed(lane):
    with open(lane['tmp'] / 'ci.lock', 'a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = lane['run']('--outside-window')
    assert result.returncode == 75 and 'CI lock busy' in result.stderr
    assert not (lane['repo'] / '.ci-results/load').exists() or not any((lane['repo'] / '.ci-results/load').iterdir())
