#!/usr/bin/env python3
"""NX-034 §3: GitHub synchronization with the §3.2 gates in front of NX-033's `nexloop_ci.py sync` (AT-053).

Only `main` (fast-forward) and owner-signed `v*` tags that are named explicitly are pushed, always with explicit refspecs:
never --force, --mirror, --all, --tags or a deletion, and a remote configured as a mirror or with its own push refspecs is
refused. Before anything is pushed, all gates run on the outgoing range <last synced>..main:

  1. fast-forward: the last synced main is an ancestor of main;
  2. every first-parent commit in the range has a passed CI result in the queue (the merge controller already guarantees it);
  3. scripts/check_publication.py --tree main: no private path, private address, private key or credential in the tree;
  4. scripts/ci/prepush-scan.sh main <last synced> in a temporary worktree of main, with the private restricted-identifier
     list (a missing list fails closed);
  5. each tag: vMAJOR.MINOR.PATCH (optionally -rc.N), annotated, points into main, and its SSH signature verifies against the owner's allowed
     signers file (`git verify-tag`).

A failed gate is `sync_blocked` with a code only (never scan content) and nothing is pushed; it is not retried until a
maintainer fixes the cause. An unreachable remote is `sync_pending` (nexloop_ci.sync), retried with exponential backoff (at most
hourly); local CI, merges and deployments do not wait for it. After a push, the remote main is read back and recorded.

    sync_github.py --repo R --queue Q --remote NAME --restricted-identifiers F [--tag vX.Y.Z --tag-signers F]...
                   [--baseline SHA] [--timeout S] [--now ISO]

The scripts used for the gates are this trusted checkout's, not the ones in the outgoing commits. Prints one JSON object;
exit 0 synced or skipped (backoff), 1 blocked or pending, 2 configuration error.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import nexloop_ci as ci  # noqa: E402

_TAG = ci._VERSION_TAG  # vMAJOR.MINOR.PATCH, optionally -rc.N (owner 2026-10-20: the same rule as the LAN pre-receive)
_SHA = re.compile(r'[0-9a-f]{40}')
_REMOTE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


class Blocked(Exception):
    """A §3.2 gate failed; the message is a code."""


def _git(repo, *args, env=None):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, env=env)


def _ancestor(repo, older, newer):
    return _git(repo, 'merge-base', '--is-ancestor', older, newer).returncode == 0


def _remote_config(repo, remote):
    if not _REMOTE.fullmatch(remote or ''):
        raise ci.ConfigError('remote name')
    if _git(repo, 'config', '--get', f'remote.{remote}.url').returncode != 0:
        raise ci.ConfigError('remote not configured')
    if _git(repo, 'config', '--bool', '--get', f'remote.{remote}.mirror').stdout.strip() == 'true':
        raise Blocked('mirror_remote')
    if _git(repo, 'config', '--get-all', f'remote.{remote}.push').stdout.strip():
        raise Blocked('push_refspec_configured')


def _ci_results(repo, queue, base, main):
    commits = _git(repo, 'rev-list', '--first-parent', f'{base}..{main}').stdout.split()
    for sha in commits:
        result = ci.latest_result(queue, sha)
        if not result or result.get('status') != 'passed':
            raise Blocked('ci_result_missing')
    return len(commits)


def _publication(repo, main):
    if subprocess.run([sys.executable, str(ROOT / 'scripts/check_publication.py'), '--tree', main], cwd=repo, capture_output=True).returncode != 0:
        raise Blocked('publication_scan_failed')


def _prepush(repo, main, base, restricted):
    if not Path(restricted).is_file():
        raise Blocked('restricted_identifiers_missing')
    work = Path(tempfile.mkdtemp(prefix='nexloop-sync-scan-'))
    tree = work / 'tree'
    try:
        if _git(repo, 'worktree', 'add', '--detach', '-q', str(tree), main).returncode != 0:
            raise Blocked('scan_failed')
        env = {**ci._stage_env({}), 'NEXLOOP_RESTRICTED_IDENTIFIERS_FILE': str(restricted)}
        scanned = subprocess.run(['bash', str(ROOT / 'scripts/ci/prepush-scan.sh'), main, base], cwd=tree, capture_output=True, text=True, env=env)
        if scanned.returncode != 0:
            raise Blocked('scan_failed')
    finally:
        _git(repo, 'worktree', 'remove', '--force', str(tree))
        _git(repo, 'worktree', 'prune')
        shutil.rmtree(work, ignore_errors=True)


def _tag_problem(repo, tag, main, signers):
    if not _TAG.fullmatch(tag or ''):
        return 'tag_name_invalid'
    if _git(repo, 'cat-file', '-t', f'refs/tags/{tag}').stdout.strip() != 'tag':
        return 'tag_not_annotated'
    target = _git(repo, 'rev-parse', f'refs/tags/{tag}^{{commit}}').stdout.strip()
    if not _SHA.fullmatch(target) or not _ancestor(repo, target, main):
        return 'tag_not_on_main'
    if not signers or not Path(signers).is_file():
        return 'tag_unsigned'
    verified = _git(repo, '-c', 'gpg.format=ssh', '-c', f'gpg.ssh.allowedSignersFile={signers}', 'verify-tag', f'refs/tags/{tag}')
    return None if verified.returncode == 0 else 'tag_unsigned'


def _state_path(queue, remote):
    return ci.queue_dirs(queue) / 'sync' / f'{remote}.json'


def _block(queue, remote, state, code, now):
    state.update(status='sync_blocked', failure=code, blocked_at=now.isoformat().replace('+00:00', 'Z'))
    state.pop('retry_after', None)
    ci.write_atomic(_state_path(queue, remote), state)
    return state


def sync_github(repo, queue, remote, *, restricted_identifiers, tags=(), tag_signers=None, baseline=None, timeout=30, now=None):
    now = now or datetime.now(UTC)
    path = _state_path(queue, remote)
    state = json.loads(path.read_text()) if path.exists() else {'remote': remote, 'last_synced': None}
    retry_after = state.get('retry_after')
    if state.get('status') == 'sync_pending' and retry_after and datetime.fromisoformat(retry_after.replace('Z', '+00:00')) > now:
        return {**state, 'skipped': 'backoff'}
    main = ci.git(repo, 'rev-parse', 'refs/heads/main')
    try:
        _remote_config(repo, remote)
        base = state.get('last_synced') or baseline
        if not base or not _SHA.fullmatch(base) or _git(repo, 'cat-file', '-e', base + '^{commit}').returncode != 0:
            raise Blocked('no_sync_baseline')
        if not _ancestor(repo, base, main):
            raise Blocked('non_fast_forward')
        _ci_results(repo, queue, base, main)
        _publication(repo, main)
        _prepush(repo, main, base, restricted_identifiers)
        for tag in tags:
            problem = _tag_problem(repo, tag, main, tag_signers)
            if problem:
                raise Blocked(problem)
    except Blocked as error:
        return _block(queue, remote, state, str(error), now)
    if not state.get('last_synced'):
        state['last_synced'] = base  # the baseline the maintainer confirmed: the remote main before the first gated sync
        ci.write_atomic(path, state)
    state = ci.sync(repo, queue, remote, timeout=timeout)  # NX-033: main only, fast-forward, pending when unreachable
    stamp = now.isoformat().replace('+00:00', 'Z')
    if state['status'] != 'synced':
        failures = state.get('failures', 1)
        state.update(failure='network_unreachable', pending_since=state.get('pending_since') or stamp,
                     retry_after=(now + timedelta(minutes=min(2 ** (failures - 1), 60))).isoformat().replace('+00:00', 'Z'))
        ci.write_atomic(path, state)
        return state
    env = {**ci._stage_env({}), 'GIT_ALLOW_PROTOCOL': 'file:ssh:https'}
    remote_main = subprocess.run(['git', '-C', str(repo), 'ls-remote', remote, 'refs/heads/main'], capture_output=True, text=True,
                                 timeout=timeout, env=env).stdout.split()
    if not remote_main or remote_main[0] != main:
        return _block(queue, remote, state, 'post_push_mismatch', now)
    pushed_tags = []
    for tag in tags:
        pushed = subprocess.run(['git', '-C', str(repo), 'push', '--porcelain', remote, f'refs/tags/{tag}:refs/tags/{tag}'],
                                capture_output=True, text=True, timeout=timeout, env=env)
        if pushed.returncode != 0:
            return _block(queue, remote, state, 'tag_push_refused', now)
        pushed_tags.append(tag)
    state.update(github_main=remote_main[0], failures=0, failure=None, pending_since=None, retry_after=None, tags_pushed=pushed_tags)
    ci.write_atomic(path, state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--queue', required=True, type=Path)
    parser.add_argument('--remote', required=True)
    parser.add_argument('--restricted-identifiers', required=True, type=Path)
    parser.add_argument('--tag', action='append', default=[])
    parser.add_argument('--tag-signers', type=Path)
    parser.add_argument('--baseline')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--now')
    args = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now.replace('Z', '+00:00')) if args.now else None
        state = sync_github(args.repo, args.queue, args.remote, restricted_identifiers=args.restricted_identifiers, tags=args.tag,
                            tag_signers=args.tag_signers, baseline=args.baseline, timeout=args.timeout, now=now)
    except (ci.ConfigError, ValueError, OSError) as error:
        print(json.dumps({'error': type(error).__name__, 'detail': str(error) if isinstance(error, ci.ConfigError) else None}))
        return 2
    print(json.dumps(state, sort_keys=True))
    return 0 if state.get('status') == 'synced' or state.get('skipped') else 1


if __name__ == '__main__':
    sys.exit(main())
