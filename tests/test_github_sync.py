"""NX-034 §3 (scripts/release/sync_github.py) over NX-033's sync: only main (fast-forward) and owner-signed v* tags reach the
remote, every §3.2 gate blocks with a code and leaves the remote unchanged, an unreachable remote is sync_pending with backoff
(AT-053) and catches up afterwards. A local bare repository stands in for GitHub; keys and markers are synthetic."""
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import sync_github as sg  # noqa: E402

ENV = {'GIT_AUTHOR_NAME': 'Synthetic', 'GIT_AUTHOR_EMAIL': 'synthetic@example.invalid', 'GIT_COMMITTER_NAME': 'Synthetic',
       'GIT_COMMITTER_EMAIL': 'synthetic@example.invalid', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
MARKER = 'synthetic-restricted-marker-'
NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


def git(*args, cwd=None, check=True):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=check, env={**os.environ, **ENV})


@pytest.fixture
def lab(tmp_path, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    local, github, work, queue = tmp_path / 'nexloop.git', tmp_path / 'github.git', tmp_path / 'work', tmp_path / 'queue'
    git('init', '-q', '--bare', '-b', 'main', str(local))
    git('init', '-q', '--bare', '-b', 'main', str(github))
    git('init', '-q', '-b', 'main', str(work))
    git('remote', 'add', 'origin', str(local), cwd=work)
    git('remote', 'add', 'github', str(github), cwd=local)
    restricted = tmp_path / 'restricted-identifiers.txt'
    restricted.write_text(MARKER + '[0-9]+\n')

    def commit(message, files=None, passed=True, push=True):
        for name, content in (files or {}).items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(content)
        git('add', '-A', cwd=work)
        git('commit', '-q', '--allow-empty', '-m', message, cwd=work)
        sha = git('rev-parse', 'HEAD', cwd=work).stdout.strip()
        if passed:
            result = queue / 'results' / sha / 'attempt-1'
            result.mkdir(parents=True)
            (result / 'result.json').write_text(json.dumps({'sha': sha, 'status': 'passed', 'attempt': 1}))
        if push:
            git('push', '-q', 'origin', 'HEAD:refs/heads/main', cwd=work)
        return sha

    base = commit('base', {'README.md': 'synthetic\n'})
    git('push', '-q', str(github), f'{base}:refs/heads/main', cwd=work)  # the remote before the first gated sync

    def sync(**kw):
        kw.setdefault('restricted_identifiers', restricted)
        kw.setdefault('now', NOW)
        return sg.sync_github(local, queue, 'github', timeout=10, **kw)

    def remote_main():
        return git('rev-parse', 'main', cwd=github).stdout.strip()

    return dict(local=local, github=github, work=work, queue=queue, commit=commit, base=base, sync=sync, remote_main=remote_main, tmp=tmp_path)


def test_main_is_pushed_after_every_gate_and_the_remote_main_is_recorded(lab):
    first = lab['sync']()
    assert first['status'] == 'sync_blocked' and first['failure'] == 'no_sync_baseline' and lab['remote_main']() == lab['base']
    head = lab['commit']('feature', {'feature.txt': 'x\n'})
    state = lab['sync'](baseline=lab['base'])
    assert state['status'] == 'synced' and state['github_main'] == head == lab['remote_main']() and state['backlog'] == 0 and state['failures'] == 0
    assert git('for-each-ref', '--format=%(refname)', cwd=lab['github']).stdout.split() == ['refs/heads/main']  # nothing else
    assert not git('worktree', 'list', '--porcelain', cwd=lab['local']).stdout.count('worktree ') > 1  # scan worktree removed


@pytest.mark.parametrize('files,passed,code', [({'x.txt': 'host ' + MARKER + '42\n'}, True, 'scan_failed'),
                                               ({'local-only/notes.txt': 'synthetic\n'}, True, 'publication_scan_failed'),
                                               ({'cfg.txt': 'MODEL_API_KEY=' + 'synthetic-invalid-value\n'}, True, 'publication_scan_failed'),
                                               ({'y.txt': 'y\n'}, False, 'ci_result_missing')])
def test_a_failed_gate_blocks_with_a_code_and_leaves_the_remote_unchanged(lab, files, passed, code):
    lab['commit']('bad', files, passed=passed)
    state = lab['sync'](baseline=lab['base'])
    assert state['status'] == 'sync_blocked' and state['failure'] == code and lab['remote_main']() == lab['base']
    text = json.dumps(state)
    assert MARKER not in text and 'synthetic-invalid-value' not in text  # codes only, never scan content


def test_a_missing_restricted_list_fails_closed(lab, tmp_path):
    lab['commit']('ok', {'ok.txt': 'ok\n'})
    state = lab['sync'](baseline=lab['base'], restricted_identifiers=tmp_path / 'missing.txt')
    assert state['failure'] == 'restricted_identifiers_missing' and lab['remote_main']() == lab['base']


def test_non_fast_forward_mirror_and_push_refspecs_are_refused(lab):
    lab['commit']('one', {'a.txt': 'a\n'})
    assert lab['sync'](baseline=lab['base'])['status'] == 'synced'
    # main is rewound to a commit that does not contain the last synced one.
    git('checkout', '-q', '--detach', lab['base'], cwd=lab['work'])
    git('commit', '-q', '--allow-empty', '-m', 'diverged', cwd=lab['work'])
    git('push', '-q', '--force', 'origin', 'HEAD:refs/heads/main', cwd=lab['work'])
    synced = lab['remote_main']()
    assert lab['sync']()['failure'] == 'non_fast_forward' and lab['remote_main']() == synced
    git('config', 'remote.github.mirror', 'true', cwd=lab['local'])
    assert lab['sync']()['failure'] == 'mirror_remote'
    git('config', '--unset', 'remote.github.mirror', cwd=lab['local'])
    git('config', 'remote.github.push', 'refs/heads/*:refs/heads/*', cwd=lab['local'])
    assert lab['sync']()['failure'] == 'push_refspec_configured'


def test_unreachable_remote_is_pending_with_backoff_and_catches_up(lab):
    url = git('config', 'remote.github.url', cwd=lab['local']).stdout.strip()
    git('config', 'remote.github.url', str(lab['tmp'] / 'unreachable' / 'x.git'), cwd=lab['local'])
    lab['commit']('one', {'a.txt': 'a\n'})
    state = lab['sync'](baseline=lab['base'])
    assert state['status'] == 'sync_pending' and state['failure'] == 'network_unreachable' and state['backlog'] == 1
    assert state['retry_after'] == (NOW + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
    assert lab['sync'](now=NOW + timedelta(seconds=30))['skipped'] == 'backoff'
    head = lab['commit']('two', {'b.txt': 'b\n'})  # local work goes on
    later = lab['sync'](now=NOW + timedelta(minutes=2))
    assert later['status'] == 'sync_pending' and later['failures'] == 2 and later['backlog'] == 2 and later['pending_since'] == state['pending_since']
    git('config', 'remote.github.url', url, cwd=lab['local'])
    done = lab['sync'](now=NOW + timedelta(minutes=10))
    assert done['status'] == 'synced' and done['backlog'] == 0 and lab['remote_main']() == head and done['pending_since'] is None


def signing_key(tmp_path):
    key = tmp_path / 'owner-key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'synthetic-owner', '-f', str(key)], check=True)
    signers = tmp_path / 'owner.allowed'
    signers.write_text('synthetic@example.invalid ' + (tmp_path / 'owner-key.pub').read_text())
    return key, signers


def test_only_annotated_owner_signed_tags_on_main_are_pushed(lab, tmp_path):
    key, signers = signing_key(tmp_path)
    head = lab['commit']('release', {'r.txt': 'r\n'})
    work = lab['work']
    git('tag', 'v0.1.1', head, cwd=work)  # lightweight
    git('tag', '-a', '-m', 'unsigned', 'v0.1.2', head, cwd=work)
    git('-c', 'gpg.format=ssh', '-c', f'user.signingkey={key}', 'tag', '-s', '-m', 'signed', 'v0.1.0', head, cwd=work)
    git('checkout', '-q', '--detach', lab['base'], cwd=work)
    git('commit', '-q', '--allow-empty', '-m', 'side', cwd=work)
    git('-c', 'gpg.format=ssh', '-c', f'user.signingkey={key}', 'tag', '-s', '-m', 'side', 'v0.2.0', 'HEAD', cwd=work)
    git('tag', '-a', '-m', 'x', 'release-1', head, cwd=work)
    git('push', '-q', 'origin', 'refs/tags/*:refs/tags/*', cwd=work)
    for tag, code in (('v0.1.1', 'tag_not_annotated'), ('v0.1.2', 'tag_unsigned'), ('v0.2.0', 'tag_not_on_main'), ('release-1', 'tag_name_invalid')):
        state = lab['sync'](baseline=lab['base'], tags=[tag], tag_signers=signers)
        assert state['failure'] == code, tag
        assert lab['remote_main']() == lab['base']  # a refused tag blocks the whole sync
    (tmp_path / 'other').mkdir()
    _, other_signers = signing_key(tmp_path / 'other')
    assert lab['sync'](baseline=lab['base'], tags=['v0.1.0'], tag_signers=other_signers)['failure'] == 'tag_unsigned'  # another signer
    state = lab['sync'](baseline=lab['base'], tags=['v0.1.0'], tag_signers=signers)
    assert state['status'] == 'synced' and state['tags_pushed'] == ['v0.1.0']
    assert git('for-each-ref', '--format=%(refname)', cwd=lab['github']).stdout.split() == ['refs/heads/main', 'refs/tags/v0.1.0']


def test_cli_exit_codes(lab):
    lab['commit']('one', {'a.txt': 'a\n'})
    args = [sys.executable, str(ROOT / 'scripts/release/sync_github.py'), '--repo', str(lab['local']), '--queue', str(lab['queue']), '--remote', 'github',
            '--restricted-identifiers', str(lab['tmp'] / 'restricted-identifiers.txt')]
    blocked = subprocess.run(args, capture_output=True, text=True)
    assert blocked.returncode == 1 and json.loads(blocked.stdout)['failure'] == 'no_sync_baseline'
    synced = subprocess.run(args + ['--baseline', lab['base']], capture_output=True, text=True)
    assert synced.returncode == 0 and json.loads(synced.stdout)['status'] == 'synced'
    bad = subprocess.run(args[:-6] + ['--remote', 'nonexistent', '--restricted-identifiers', 'x'], capture_output=True, text=True)
    assert bad.returncode == 2
