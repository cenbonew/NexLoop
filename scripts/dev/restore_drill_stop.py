#!/usr/bin/env python3
"""Development machine only: stop the source PostgreSQL of the AT-072 local restore drill (scripts/dev/restore_drill.sh).

    restore_drill_stop.py stop  --drill-dir .dev-drill [--pg-bin DIR]
    restore_drill_stop.py watch --drill-dir .dev-drill --pg-bin DIR --pid PID [--pid PID ...] [--grace SECONDS]

The holder records the cluster it started in <drill-dir>/pg-root (a mkdtemp directory nexloop-pg-* directly under the temp
directory). `stop` stops exactly that cluster (pg_ctl stop -m fast on its data directory, when its postmaster is alive) and
removes exactly that directory; any other recorded path is refused and left alone. `watch` detaches into its own session (so
a signal to the drill's process group, a closed terminal or a SIGKILL of the script does not reach it), first waits until the holder has recorded its cluster (it is started with the holder), then until any of
the given processes is gone, gives the holder's own teardown --grace seconds, then runs `stop`. Prints one JSON line.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _alive(pid):
    """A running process; a zombie (exited, not yet reaped by its parent) counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith('Z')


def recorded_root(drill_dir):
    """The recorded cluster directory, or None when it is absent or not a drill cluster under the temp directory."""
    try:
        root = Path((Path(drill_dir) / 'pg-root').read_text().strip())
    except FileNotFoundError:
        return None
    temp = Path(tempfile.gettempdir()).resolve()
    if not root.name.startswith('nexloop-pg-') or root.resolve().parent != temp or root.is_symlink():
        raise ValueError('recorded pg-root is not a drill cluster under the temp directory')
    return root


def postmaster_pid(root):
    try:
        return int((root / 'data' / 'postmaster.pid').read_text().split('\n', 1)[0])
    except (FileNotFoundError, ValueError):
        return None


def stop(drill_dir, pg_bin=None, root=None):
    if root is None:
        try:
            root = recorded_root(drill_dir)
        except ValueError:
            return {'stopped': False, 'refused': 'pg_root_not_a_drill_cluster'}
    if root is None or not root.exists():
        return {'stopped': True, 'state': 'already_gone'}
    if pg_bin is None:
        try:
            pg_bin = json.loads((Path(drill_dir) / 'stack.json').read_text())['pg_bin']
        except (FileNotFoundError, KeyError, ValueError):
            pg_bin = os.environ.get('NEXLOOP_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@18/bin')
    pid = postmaster_pid(root)
    state = 'not_running'
    if pid is not None and _alive(pid):
        subprocess.run([str(Path(pg_bin) / 'pg_ctl'), '-D', str(root / 'data'), '-m', 'fast', '-w', 'stop'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if _alive(pid):
            return {'stopped': False, 'refused': 'postmaster_still_running'}
        state = 'stopped_now'
    shutil.rmtree(root)
    return {'stopped': True, 'state': state}


def watch(drill_dir, pg_bin, pids, grace, holder=None):
    # Started together with the holder, before PostgreSQL exists: wait for the holder to record its cluster, then remember it
    # (the drill directory may be removed before the source side is stopped).
    root = None
    while root is None:
        try:
            root = recorded_root(drill_dir)
        except ValueError:
            return {'stopped': False, 'refused': 'pg_root_not_a_drill_cluster'}
        if root is None:
            if not all(_alive(p) for p in pids):
                return {'stopped': True, 'state': 'no_cluster_recorded'}
            time.sleep(0.5)
    while all(_alive(p) for p in pids):
        time.sleep(0.5)
    if holder is not None and _alive(holder):
        # The script is gone but the holder (API) is not: ask it to stop, so its own teardown stops PostgreSQL.
        os.kill(holder, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not root.exists() and (holder is None or not _alive(holder)):
            return {'stopped': True, 'state': 'teardown_done'}
        time.sleep(1)
    out = stop(drill_dir, pg_bin, root)
    if holder is not None and _alive(holder):
        os.kill(holder, signal.SIGKILL)
        out['holder'] = 'killed'
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('stop')
    s.add_argument('--drill-dir', type=Path, required=True)
    s.add_argument('--pg-bin')
    w = sub.add_parser('watch')
    w.add_argument('--drill-dir', type=Path, required=True)
    w.add_argument('--pg-bin', required=True)
    w.add_argument('--pid', type=int, action='append', required=True)
    w.add_argument('--holder', type=int, help='the holder process: sent SIGTERM once the script is gone, SIGKILL after the grace period')
    w.add_argument('--grace', type=float, default=45)
    w.add_argument('--foreground', action='store_true', help='do not detach (tests)')
    a = parser.parse_args(argv)
    if a.command == 'watch':
        # Before anything else: a hang-up or Ctrl-C to the drill's process group must not reach the watchdog.
        for handled in (signal.SIGHUP, signal.SIGINT):
            signal.signal(handled, signal.SIG_IGN)
    if a.command == 'stop':
        out = stop(a.drill_dir, a.pg_bin)
        print(json.dumps(out))
        return 0 if out['stopped'] else 1
    if not a.foreground:
        if os.fork():
            return 0
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
    out = watch(a.drill_dir, a.pg_bin, a.pid, a.grace, a.holder)
    print(json.dumps(out), flush=True)
    return 0 if out['stopped'] else 1


if __name__ == '__main__':
    sys.exit(main())
