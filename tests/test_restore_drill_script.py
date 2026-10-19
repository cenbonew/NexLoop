"""scripts/dev/restore_drill.sh stops the source PostgreSQL however the source side ends (AT-072 second drill, 2026-10-19).

The second drill ran the script as `restore_drill.sh 2>&1 | tee <file>`; after the execution tool's Ctrl-C the PostgreSQL it
had started stayed up with its temp directory. PostgreSQL runs in its own process group (pg_ctl start), so it survives
whatever kills the script's group, and the holder's teardown is the only thing that stops it. These tests run the real script
with a stand-in holder (NEXLOOP_DRILL_HOLDER) and a stand-in pg_ctl: the "postmaster" is a sleep in its own session, recorded
in <cluster>/data/postmaster.pid like PostgreSQL does. No PostgreSQL, no port, no API."""
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/dev/restore_drill.sh'

HOLDER = '''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, tempfile, time, shutil
from pathlib import Path
drill = Path(os.environ['NEXLOOP_DRILL_DIR'])
root = Path(tempfile.mkdtemp(prefix='nexloop-pg-'))
(root / 'data').mkdir()
# Like pg_ctl start: the postmaster is detached (its parent becomes init) and leads its own process group.
pid = int(subprocess.run(['sh', '-c', 'sleep 600 >/dev/null 2>&1 & echo $!'], capture_output=True, text=True, start_new_session=True).stdout)
(root / 'data' / 'postmaster.pid').write_text(f'{pid}\\n')
(drill / 'pg-root').write_text(str(root) + '\\n')
(drill / 'stack.json').write_text(json.dumps({'pg_bin': os.environ['NEXLOOP_TEST_PG_BIN']}))
TEARDOWN = os.environ.get('STANDIN_TEARDOWN') == '1'
def term(*_):
    if TEARDOWN:  # what the fixtures do after uvicorn returns: stop PostgreSQL, remove the cluster directory
        os.kill(pid, signal.SIGTERM)
        while True:
            try: os.kill(pid, 0); time.sleep(0.1)
            except ProcessLookupError: break
        shutil.rmtree(root)
    sys.exit(0)
signal.signal(signal.SIGTERM, term)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
(drill / 'ready').write_text('https://127.0.0.1:0\\n')
while True:
    time.sleep(1)
'''

PG_CTL = '''#!/bin/sh
# stand-in pg_ctl: -D <data> ... stop -> kill the pid in postmaster.pid and wait for it
while [ $# -gt 0 ]; do [ "$1" = -D ] && data=$2; shift; done
pid=$(head -1 "$data/postmaster.pid")
kill "$pid" 2>/dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
rm -f "$data/postmaster.pid"
'''


@pytest.fixture
def standin(tmp_path):
    holder = tmp_path / 'holder.py'
    holder.write_text(HOLDER)
    holder.chmod(0o755)
    pg_bin = tmp_path / 'pgbin'
    pg_bin.mkdir()
    (pg_bin / 'pg_ctl').write_text(PG_CTL)
    (pg_bin / 'pg_ctl').chmod(0o755)
    drill = tmp_path / 'drill'
    env = {**os.environ, 'NEXLOOP_DRILL_HOLDER': str(holder), 'NEXLOOP_TEST_PG_BIN': str(pg_bin), 'NEXLOOP_DRILL_DIR': str(drill),
           'NEXLOOP_DRILL_WATCH_GRACE': '2'}
    started = []

    def start(command, **extra):
        p = subprocess.Popen(['bash', '-c', command], cwd=ROOT, env={**env, **extra}, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started.append(p)
        for _ in range(240):
            if (drill / 'ready').exists() and (drill / 'pg-root').exists():
                break
            time.sleep(0.25)
        root = Path((drill / 'pg-root').read_text().strip())
        return p, root, int((root / 'data' / 'postmaster.pid').read_text())
    yield {'start': start, 'drill': drill, 'env': env}
    for p in started:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # macOS: EPERM for a group left with zombies only
            pass
    # Whatever a failing test left: only the stand-in clusters it recorded.
    pg_root = drill / 'pg-root'
    if pg_root.exists():
        root = Path(pg_root.read_text().strip())
        pid_file = root / 'data' / 'postmaster.pid'
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if root.name.startswith('nexloop-pg-') and root.resolve().parent == Path(tempfile.gettempdir()).resolve():
            subprocess.run(['rm', '-rf', str(root)])


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def gone_within(pid, root, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid) and not root.exists():
            return True
        time.sleep(0.25)
    return False


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGHUP, signal.SIGTERM])
def test_a_signal_to_the_piped_group_stops_the_source_postgres(standin, sig):
    # As the second drill ran it: through `| tee`; the signal goes to the whole foreground group (script, holder, tee).
    p, root, pg = standin['start'](f'{SCRIPT} 2>&1 | tee {standin["drill"].parent}/source.log', STANDIN_TEARDOWN='1')
    os.killpg(p.pid, sig)
    assert gone_within(pg, root, 30), f'source postgres left running after {sig.name}'


def test_a_sigkill_of_the_whole_group_is_caught_by_the_watchdog(standin):
    # Nothing in the group can clean up; the watchdog (own session) stops exactly the recorded cluster after the grace period.
    p, root, pg = standin['start'](f'{SCRIPT} 2>&1 | tee {standin["drill"].parent}/source.log')
    time.sleep(1.5)  # the watchdog is started right after ready
    os.killpg(p.pid, signal.SIGKILL)
    assert gone_within(pg, root, 30)


def test_a_holder_left_behind_by_its_script_is_stopped_with_its_teardown(standin):
    # Third drill: a hang-up through `| tee` could end the script by SIGPIPE before its EXIT trap, leaving the holder (which
    # ignores SIGHUP) serving the API. The watchdog asks it to stop, so its own teardown stops PostgreSQL.
    p, root, pg = standin['start'](str(SCRIPT), STANDIN_TEARDOWN='1')
    time.sleep(1.5)
    holder = int(subprocess.run(['pgrep', '-f', '-g', str(p.pid), 'holder.py'], capture_output=True, text=True).stdout.split()[0])
    script = int(subprocess.run(['pgrep', '-f', '-g', str(p.pid), 'restore_drill.sh'], capture_output=True, text=True).stdout.split()[-1])
    os.kill(script, signal.SIGKILL)
    assert gone_within(pg, root, 30)
    deadline = time.monotonic() + 10
    while alive(holder) and time.monotonic() < deadline:
        time.sleep(0.25)
    assert not alive(holder)


def test_the_watchdog_still_stops_postgres_when_the_drill_directory_was_removed_first(standin):
    p, root, pg = standin['start'](str(SCRIPT))
    time.sleep(1.5)
    subprocess.run(['rm', '-rf', str(standin['drill'])])
    os.killpg(p.pid, signal.SIGKILL)
    assert gone_within(pg, root, 30)


def test_stop_command_stops_only_the_recorded_cluster(standin):
    p, root, pg = standin['start'](str(SCRIPT))
    out = subprocess.run([str(SCRIPT), '--stop'], cwd=ROOT, env=standin['env'], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and 'restore drill: stopped' in out.stdout and '"stopped_now"' in out.stdout
    assert not alive(pg) and not root.exists()
    again = subprocess.run([str(SCRIPT), '--stop'], cwd=ROOT, env=standin['env'], capture_output=True, text=True, timeout=120)
    assert again.returncode == 0 and '"already_gone"' in again.stdout


def test_stop_refuses_a_recorded_path_that_is_not_a_drill_cluster(tmp_path):
    drill = tmp_path / 'drill'
    drill.mkdir()
    victim = tmp_path / 'not-a-cluster'
    (victim / 'data').mkdir(parents=True)
    (drill / 'pg-root').write_text(str(victim))
    out = subprocess.run([str(SCRIPT), '--stop'], cwd=ROOT, env={**os.environ, 'NEXLOOP_DRILL_DIR': str(drill)},
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 1 and 'pg_root_not_a_drill_cluster' in out.stderr + out.stdout
    assert victim.exists()
    assert json.loads(out.stderr.split('(', 1)[1].rsplit(')', 1)[0]) == {'stopped': False, 'refused': 'pg_root_not_a_drill_cluster'}


def test_the_script_header_documents_the_stop_paths():
    head = SCRIPT.read_text().split('set -euo pipefail', 1)[0]
    assert '--stop' in head and 'SIGHUP' in head and 'watchdog' in head
