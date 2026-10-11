"""NX-036 §5: the whole-history audit on synthetic repositories (tmp git init, synthetic restricted list). Covers hits deleted
from history, hits brought in by a merge, commit message hits, the binary rule shared with check_publication, the D9 exit
codes, and a report that never repeats what matched."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/release'))
sys.path.insert(0, str(ROOT / 'scripts'))
import history_audit as H  # noqa: E402
from check_publication import violation  # noqa: E402

ALIAS = 'synthhost' + 'zeta'
USER_PATH = '/Users/' + 'synthuser'
FINGERPRINT = 'SHA256:' + 'Q' * 20
NET = '10\\.77\\.' + '0\\.9'
HARDWARE = 'Synth' + 'Board-X9'
KEY_HEADER = '-----BEGIN ' + 'PRIVATE KEY-----'
SECRET = 'sk-' + 'A1b2C3d4E5f6G7h8I9j0K1'
PRIVATE_ADDRESS = '192.168.' + '77.5'


def sh(repo, *args, **kw):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True, **kw).stdout


def commit(repo, files, message, *, delete=()):
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        sh(repo, 'add', '--', name)
    for name in delete:
        sh(repo, 'rm', '-q', '--', name)
    sh(repo, 'commit', '-q', '--allow-empty', '-m', message)
    return sh(repo, 'rev-parse', 'HEAD').strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    sh(repo, 'init', '-q', '-b', 'main')
    sh(repo, 'config', 'user.email', 'synthetic@example.invalid')
    sh(repo, 'config', 'user.name', 'Synthetic')
    listing = tmp_path / 'restricted.txt'
    listing.write_text(f'# synthetic list\n{ALIAS}\n{USER_PATH}\n{FINGERPRINT}\n{NET}\n# [hardware]\n{HARDWARE}\n')
    monkeypatch.setenv('NEXLOOP_RESTRICTED_IDENTIFIERS_FILE', str(listing))
    monkeypatch.chdir(repo)
    return repo


def by(report, category, path):
    return [f for f in report['findings'] if f['category'] == category and f['path'] == path]


def test_clean_history_exits_zero(repo):
    commit(repo, {'README.md': 'hello\n'}, 'initial')
    report = H.audit()
    assert report['findings'] == [] and H.exit_code(report) == 0 and H.main([]) == 0


def test_a_deleted_hit_is_still_found_and_marked_not_in_the_tree(repo):
    first = commit(repo, {'notes.md': f'deploy to {ALIAS}\n', 'keep.md': f'cd {USER_PATH}/x\n'}, 'add notes')
    commit(repo, {'other.md': 'x\n'}, 'more')
    removed = commit(repo, {'notes.md': 'deploy to the app host\n'}, 'clean notes')
    report = H.audit()
    [alias] = by(report, 'host_alias', 'notes.md')
    assert alias['first_commit'] == first == alias['last_commit'] and alias['in_current_tree'] is False and alias['rule'] == 'identifier:1'
    [path] = by(report, 'local_path', 'keep.md')
    assert path['in_current_tree'] is True
    assert removed not in alias['commits'] and H.exit_code(report) == 1  # accepted categories only (D9)


def test_a_hit_brought_in_by_a_merge_and_a_commit_message_hit(repo):
    commit(repo, {'a.md': 'a\n'}, 'base')
    sh(repo, 'checkout', '-q', '-b', 'side')
    side = commit(repo, {'side.md': f'board {HARDWARE}\n'}, f'side work on {ALIAS}')
    sh(repo, 'checkout', '-q', 'main')
    commit(repo, {'b.md': 'b\n'}, 'main work')
    sh(repo, 'merge', '-q', '--no-ff', '-m', 'merge side', 'side')
    merge = sh(repo, 'rev-parse', 'HEAD').strip()
    report = H.audit()
    [hardware] = by(report, 'hardware', 'side.md')
    assert hardware['commits'] == [side, merge]  # the merge introduces it against its first parent
    [message] = by(report, 'host_alias', H.MESSAGE)
    assert message['commits'] == [side] and message['in_current_tree'] is False


def test_rotation_categories_exit_two_and_the_report_never_repeats_the_match(repo, tmp_path):
    commit(repo, {'deploy/key.pem': KEY_HEADER + '\nabc\n', 'conf.env.example': f'X={SECRET}\n', 'net.md': f'gw {PRIVATE_ADDRESS}\n',
                  'fp.md': f'host key {FINGERPRINT[:7]}{"Q" * 20}\n'}, 'oops')
    commit(repo, {'deploy/key.pem': 'removed\n'}, 'remove key')
    report = H.audit()
    assert by(report, 'private_key', 'deploy/key.pem')[0]['in_current_tree'] is False
    assert {f['rule'] for f in by(report, 'credential', 'conf.env.example')} == {'credential_value'}
    assert {f['rule'] for f in by(report, 'restricted_network_identifier', 'net.md')} == {'publication:private_address'}
    assert by(report, 'restricted_network_identifier', 'fp.md')
    assert report['rotate_required'] == ['credential', 'private_key', 'restricted_network_identifier'] and H.exit_code(report) == 2
    out_json, out_md = tmp_path / 'r.json', tmp_path / 'r.md'
    assert H.main(['--json', str(out_json), '--markdown', str(out_md)]) == 2
    text = out_json.read_text() + out_md.read_text()
    for secret in (SECRET, PRIVATE_ADDRESS, KEY_HEADER, FINGERPRINT, ALIAS, USER_PATH, 'abc'):
        assert secret not in text
    assert H.main(['--json', str(out_json)]) == 3  # never overwrites a report
    assert out_json.stat().st_mode & 0o077 == 0


def test_binary_and_large_blobs_follow_check_publication(repo):
    binary = b'\0\1' + ALIAS.encode() + SECRET.encode() + PRIVATE_ADDRESS.encode()
    large = ('filler line\n' * 200000 + f'{ALIAS}\n').encode()  # ~2.4 MB of text is still scanned, as check_publication does
    commit(repo, {'blob.bin': binary, 'big.txt': large, 'local-only/x.bin': b'\0'}, 'binary and large')
    assert violation('blob.bin', binary) is None and violation('big.txt', large) is None
    report = H.audit()
    assert not [f for f in report['findings'] if f['path'] == 'blob.bin']
    assert by(report, 'host_alias', 'big.txt')
    assert [(f['category'], f['rule']) for f in report['findings'] if f['path'] == 'local-only/x.bin'] == [('other', 'publication:private_path')]


def test_input_errors_exit_three(repo, monkeypatch, tmp_path):
    commit(repo, {'a.md': 'a\n'}, 'base')
    assert H.main(['no-such-ref']) == 3
    monkeypatch.setenv('NEXLOOP_RESTRICTED_IDENTIFIERS_FILE', str(tmp_path / 'missing.txt'))
    assert H.main([]) == 3
    empty = tmp_path / 'empty.txt'
    empty.write_text('# nothing\n\n')
    monkeypatch.setenv('NEXLOOP_RESTRICTED_IDENTIFIERS_FILE', str(empty))
    assert H.main([]) == 3


def test_all_refs_includes_unmerged_branches_and_the_credential_rule_is_prepush_scans(repo):
    commit(repo, {'a.md': 'a\n'}, 'base')
    sh(repo, 'checkout', '-q', '-b', 'topic')
    commit(repo, {'t.md': f'{ALIAS}\n'}, 'topic')
    sh(repo, 'checkout', '-q', 'main')
    assert not H.audit('main')['findings'] and by(H.audit('main', all_refs=True), 'host_alias', 't.md')
    expression = H.credential_expression().pattern.decode()
    assert expression in (ROOT / 'scripts/ci/prepush-scan.sh').read_text()
    assert json.dumps(H.audit('main', all_refs=True))
