"""NX-036 §4 and §5: CI isolation (AT-054 static half), DCO sign-off (D1), the public files and their
configured values (D2, D3, D4, D10; owner decision 2026-10-11), and the publication scan over a whole tree and a source archive.
Temporary repositories and synthetic markers only."""
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_ci_isolation as ci  # noqa: E402
import check_public_files as pf  # noqa: E402
import check_signoff as so  # noqa: E402

SAFE = '''name: test
on: [push, pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
'''


def test_the_repository_has_no_unsafe_workflow():
    assert ci.check(ROOT)[1] == []


@pytest.mark.parametrize('text,problem', [
    (SAFE.replace('[push, pull_request]', '[pull_request_target]'), 'pull_request_target'),
    (SAFE.replace('on: [push, pull_request]', 'on:\n  pull_request_target:\n    types: [opened]'), 'pull_request_target'),
    (SAFE.replace('echo ok', 'echo ${{ secrets.TOKEN }}'), 'references secrets'),
    (SAFE + '  call:\n    uses: ./.github/workflows/x.yml\n    secrets: inherit\n', 'references secrets'),
    (SAFE.replace('contents: read', 'contents: write'), 'top-level permissions'),
    (SAFE.replace('permissions:\n  contents: read\n', ''), 'top-level permissions'),
    (SAFE.replace('permissions:\n  contents: read', 'permissions: write-all'), 'top-level permissions'),
    (SAFE.replace('    runs-on', '    permissions:\n      packages: write\n    runs-on'), 'job test permissions'),
    ('on: [push\n', 'not valid YAML')])
def test_unsafe_workflows_are_refused(tmp_path, text, problem):
    workflows = tmp_path / '.github/workflows'
    workflows.mkdir(parents=True)
    (workflows / 'ci.yml').write_text(SAFE)
    assert ci.check(tmp_path) == (1, [])
    (workflows / 'ci.yml').write_text(text)
    count, problems = ci.check(tmp_path)
    assert count == 1 and any(problem in p for p in problems), problems


def test_read_all_and_empty_permissions_are_read_only(tmp_path):
    workflows = tmp_path / '.github/workflows'
    workflows.mkdir(parents=True)
    for value in ('read-all', '{}'):
        (workflows / 'ci.yaml').write_text(SAFE.replace('permissions:\n  contents: read', 'permissions: ' + value))
        assert ci.check(tmp_path)[1] == []


def git(root, *args, env=None):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=True, env=env).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'probe'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Synthetic Author')
    git(root, 'config', 'user.email', 'author@example.invalid')
    git(root, 'config', 'commit.gpgsign', 'false')
    (root / 'a.txt').write_text('a\n')
    git(root, 'add', 'a.txt')
    git(root, 'commit', '-q', '-m', 'base')
    return root


def commit(root, name, message, *extra):
    (root / name).write_text(name + '\n')
    git(root, 'add', name)
    git(root, 'commit', '-q', '-m', message, *extra)
    return git(root, 'rev-parse', 'HEAD')


def test_every_non_merge_commit_needs_a_sign_off_matching_its_author(repository):
    base = git(repository, 'rev-parse', 'HEAD')
    commit(repository, 'b.txt', 'signed', '-s')
    assert so.check(base, 'HEAD', repository) == (1, [])
    unsigned = commit(repository, 'c.txt', 'unsigned')
    other = commit(repository, 'd.txt', 'signed by someone else\n\nSigned-off-by: Someone Else <else@example.invalid>')
    count, problems = so.check(base, 'HEAD', repository)
    assert count == 3 and sorted(problems) == sorted(sha[:12] + ': no Signed-off-by matching the author' for sha in (unsigned, other))


def test_merge_commits_are_not_required_to_carry_a_sign_off(repository):
    base = git(repository, 'rev-parse', 'HEAD')
    git(repository, 'checkout', '-q', '-b', 'side')
    commit(repository, 'side.txt', 'side', '-s')
    git(repository, 'checkout', '-q', 'main')
    commit(repository, 'main.txt', 'main', '-s')
    git(repository, 'merge', '-q', '--no-ff', '--no-edit', 'side')
    assert so.check(base, 'HEAD', repository) == (2, [])


def test_signoff_refuses_option_like_refs(repository):
    with pytest.raises(ValueError):
        list(so.commits('--all', 'HEAD', repository))


@pytest.fixture
def public_root(tmp_path):
    for name in ('LICENSE', 'NOTICE', 'README.md', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md', 'CODE_OF_CONDUCT.md',
                 'deploy/compliance/public-release.v1.json'):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def test_ci_mode_passes_on_the_repository_and_public_mode_fails_until_the_owner_fills_the_contact():
    assert pf.check(ROOT) == []
    problems = pf.check(ROOT, public=True)
    assert 'security_contact: not set (no formal response channel; D2)' in problems
    assert 'SECURITY.md: security contact placeholder not filled in' in problems


def test_public_mode_passes_once_every_condition_holds(public_root):
    config_path = public_root / 'deploy/compliance/public-release.v1.json'
    config = json.loads(config_path.read_text())
    config['security_contact'] = 'GitHub private vulnerability reporting (synthetic)'
    config_path.write_text(json.dumps(config))
    for name in ('SECURITY.md', 'CODE_OF_CONDUCT.md'):
        path = public_root / name
        path.write_text(path.read_text().replace('<SECURITY_CONTACT>', 'synthetic channel'))
    (public_root / 'README.md').write_text('# NexLoop\n\n[中文](README.zh-CN.md)\n')
    (public_root / 'README.zh-CN.md').write_text('# NexLoop\n\n[English](README.md)\n')
    for name in ('GOVERNANCE.md', 'docs/release/public-release-checklist.md'):
        (public_root / name).parent.mkdir(parents=True, exist_ok=True)
        (public_root / name).write_text('synthetic\n')
    assert pf.check(public_root, public=True) == []
    (public_root / 'README.zh-CN.md').write_text('# NexLoop\n')
    assert pf.check(public_root, public=True) == ['README.zh-CN.md: does not link to README.md']


def test_documents_must_state_the_configured_values(public_root):
    config_path = public_root / 'deploy/compliance/public-release.v1.json'
    config = json.loads(config_path.read_text())
    config['response_targets_days']['fix_high_days'] = 45
    config_path.write_text(json.dumps(config))
    assert pf.check(public_root) == ['SECURITY.md: does not state response_targets_days (45 天)']
    (public_root / 'CONTRIBUTING.md').write_text('no sign-off described\n')
    (public_root / 'LICENSE').write_text('Apache License\n')
    problems = pf.check(public_root)
    assert 'CONTRIBUTING.md: does not describe the DCO sign-off' in problems and 'LICENSE: not the MIT license' in problems
    (public_root / 'CODE_OF_CONDUCT.md').unlink()
    assert 'CODE_OF_CONDUCT.md: missing' in pf.check(public_root)


PUBLICATION = ROOT / 'scripts/check_publication.py'


def scan(root, *args):
    return subprocess.run([sys.executable, str(PUBLICATION), *map(str, args)], cwd=root, capture_output=True, text=True)


def test_tree_and_archive_modes_scan_every_tracked_file_not_only_the_diff(repository, tmp_path):
    (repository / 'local-only').mkdir()
    (repository / 'local-only/notes.txt').write_text('synthetic\n')
    (repository / 'old.cfg').write_text('host=192.168.' + '213.97\n')
    (repository / 'link').symlink_to('a.txt')
    git(repository, 'add', '-f', 'local-only/notes.txt', 'old.cfg', 'link')
    git(repository, 'commit', '-q', '-m', 'old')
    commit(repository, 'new.txt', 'clean change')  # the latest diff alone is clean
    tree = scan(repository, '--tree', 'HEAD')
    assert tree.returncode == 1
    assert 'local-only/notes.txt (tree): private path' in tree.stdout and 'old.cfg (tree): private address' in tree.stdout
    assert 'link (tree): symbolic link' in tree.stdout and '213.97' not in tree.stdout
    archive = tmp_path / 'src.tar'
    git(repository, 'archive', '--format=tar', '-o', str(archive), 'HEAD')
    result = scan(repository, '--archive', archive)
    assert result.returncode == 1 and 'old.cfg (archive): private address' in result.stdout and 'local-only/notes.txt (archive)' in result.stdout
    clean = tmp_path / 'clean.tar'
    with tarfile.open(clean, 'w') as tar:
        tar.add(repository / 'a.txt', arcname='a.txt')
    assert scan(repository, '--archive', clean).returncode == 0
    assert scan(repository, '--tree').returncode == 2
