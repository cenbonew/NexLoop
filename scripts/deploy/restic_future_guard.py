#!/usr/bin/env python3
"""NX-035 (L6 F1 of the rest-server review, 2026-10-27): no forget/prune while a snapshot claims a time in the future.

restic's --keep-within and --keep-monthly count from the newest snapshot of each group, not from now. A writer that can add snapshots
(append-only lets it) can add one stamped years ahead (`backup --time 2030-01-01 --host nexloop --tag journal`): the group's real
snapshots then fall outside the window and the next forget --prune - run by the maintainer with full rights - deletes them for it.
So every forget is preceded by this guard: it lists the repository's snapshots (`snapshots --json --no-lock`) and fails (exit 1) when
any snapshot is later than now plus --max-ahead-seconds, naming their short ids; the maintenance unit stops there and
timer_scheduled_failure alerts. An unreadable answer or time also fails: the guard never passes what it could not check.

    restic_future_guard.py [--max-ahead-seconds N] [--select-latest TAG] -- RESTIC [ARGS...]
    restic_future_guard.py --wait-unlocked SECONDS -- RESTIC [ARGS...]

RESTIC ARGS is the restic command line up to the subcommand (binary, --no-cache, -o ..., --repository-file/-r, --password-file); the
environment is passed on as it is (the unit's). --select-latest TAG (restores, drills): prints the full id of the newest snapshot with
that tag that is not in the future - refusing, as above, while any future snapshot exists - instead of trusting restic's `latest`.
--wait-unlocked SECONDS (L6 F2, the files repository's maintenance on DATA_HOST, before the rest-server is stopped): no snapshot check;
waits until `list locks --no-lock` lists no lock (every 10 seconds), exit 0, or fails after SECONDS (exit 1): the rest-server is then
stopped between two file backup rounds instead of in the middle of one.
Self-contained (stdlib only): DATA_HOST runs it from /opt/nexloop/restic-maint/, APP_HOST's repo2 maintenance imports it. One JSON
line on stdout; never restic's own output or a secret.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

_TIME = re.compile(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})')


def parse_time(text):
    """restic's snapshot time (RFC 3339 with nanoseconds and an offset) as an aware datetime; None when it is not one."""
    m = _TIME.fullmatch(text) if type(text) is str else None
    if not m:
        return None
    fraction = (m.group(2) or '.0')[:7]
    try:
        return datetime.fromisoformat(m.group(1) + fraction + ('+00:00' if m.group(3) == 'Z' else m.group(3)))
    except ValueError:
        return None


def future(snapshots, now, max_ahead_seconds):
    """(future short ids, unreadable count) of a `restic snapshots --json` list."""
    limit, ahead, unreadable = now + timedelta(seconds=max_ahead_seconds), [], 0
    for s in snapshots:
        t = parse_time(s.get('time')) if type(s) is dict else None
        if t is None:
            unreadable += 1
        elif t > limit:
            ahead.append(str(s.get('short_id') or s.get('id') or '?')[:8])
    return ahead, unreadable


def check(snapshots, now, max_ahead_seconds):
    """{'ok', 'snapshots', 'future', 'unreadable'} for a parsed list; ok only with no future and no unreadable snapshot."""
    if type(snapshots) is not list:
        return {'ok': False, 'error': 'not a snapshot list'}
    ahead, unreadable = future(snapshots, now, max_ahead_seconds)
    return {'ok': not ahead and not unreadable, 'snapshots': len(snapshots), 'future': ahead, 'unreadable': unreadable}


def latest(snapshots, tag, now):
    """The full id of the newest snapshot with `tag` that is not later than now; None when there is none."""
    best = None
    for s in snapshots:
        t = parse_time(s.get('time'))
        if t is not None and t <= now and tag in (s.get('tags') or []) and (best is None or t > best[0]):
            best = (t, s.get('id'))
    return best[1] if best and type(best[1]) is str and re.fullmatch(r'[0-9a-f]{64}', best[1]) else None


def list_snapshots(restic_argv, run=subprocess.run, environment=None):
    done = run([*restic_argv, 'snapshots', '--json', '--no-lock'], capture_output=True, text=True, timeout=600, env=environment)
    if done.returncode != 0:
        return None, f'restic snapshots exit {done.returncode}'
    try:
        return json.loads(done.stdout or 'null'), None
    except ValueError:
        return None, 'restic snapshots: not JSON'


def wait_unlocked(restic_argv, seconds, run=subprocess.run, sleep=time.sleep, monotonic=time.monotonic):
    """(ok, result): no lock in the repository within `seconds`; a failing `list locks` fails at once."""
    deadline = monotonic() + seconds
    while True:
        done = run([*restic_argv, 'list', 'locks', '--no-lock'], capture_output=True, text=True, timeout=600)
        if done.returncode != 0:
            return False, {'ok': False, 'error': f'restic list locks exit {done.returncode}'}
        locks = len((done.stdout or '').split())
        if not locks:
            return True, {'ok': True, 'locks': 0}
        if monotonic() >= deadline:
            return False, {'ok': False, 'locks': locks, 'error': f'locks still held after {seconds} seconds'}
        sleep(10)


def main(argv=None, run=subprocess.run, clock=lambda: datetime.now(UTC), sleep=time.sleep, monotonic=time.monotonic):
    p = argparse.ArgumentParser(description='refuse forget/prune while a restic snapshot is dated in the future')
    p.add_argument('--max-ahead-seconds', type=int, default=3600)
    p.add_argument('--select-latest', metavar='TAG')
    p.add_argument('--wait-unlocked', type=int, metavar='SECONDS')
    p.add_argument('restic', nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    restic = a.restic[1:] if a.restic[:1] == ['--'] else a.restic
    if not restic or not 0 <= a.max_ahead_seconds <= 86400 or (a.wait_unlocked is not None and (not 0 < a.wait_unlocked <= 3600 or a.select_latest)):
        print(json.dumps({'ok': False, 'error': 'usage: [--max-ahead-seconds 0-86400] [--select-latest TAG] | --wait-unlocked 1-3600 -- RESTIC [ARGS...]'}))
        return 2
    if a.wait_unlocked is not None:
        try:
            ok, result = wait_unlocked(restic, a.wait_unlocked, run, sleep, monotonic)
        except (OSError, subprocess.SubprocessError) as e:
            ok, result = False, {'ok': False, 'error': type(e).__name__}
        print(json.dumps(result, sort_keys=True))
        return 0 if ok else 1
    try:
        snapshots, error = list_snapshots(restic, run)
    except (OSError, subprocess.SubprocessError) as e:
        snapshots, error = None, type(e).__name__
    if error:
        print(json.dumps({'ok': False, 'error': error}))
        return 1
    now = clock()
    result = check(snapshots, now, a.max_ahead_seconds)
    if result['ok'] and a.select_latest:
        result['selected'] = latest(snapshots, a.select_latest, now)
        result['ok'] = result['selected'] is not None
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
