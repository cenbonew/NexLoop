"""NX-036 §2 (scripts/check_licenses.py): license inventory over synthetic lock files and a synthetic policy.

Licenses are never guessed: missing or ambiguous metadata, a package installed on no checked platform, an unlisted license,
and a weak-copyleft license without its own package entry all fail; THIRD_PARTY_NOTICES.md drift fails. Nothing here reads
the network; the real repository is used only for the vendored-source and policy-file checks."""
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_licenses as cl  # noqa: E402

UV_LOCK = '''version = 1

[[package]]
name = "nexloop-eios-core"
version = "0.1.0"
source = { editable = "packages/eios-core" }
dependencies = [{ name = "liba" }]

[[package]]
name = "liba"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.example/liba-1.0-py3-none-any.whl", hash = "sha256:aaaa" }]

[[package]]
name = "devb"
version = "2.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.example/devb-2.0-py3-none-any.whl", hash = "sha256:bbbb" }]
'''

PNPM_LOCK = '''lockfileVersion: '9.0'
importers:
  apps/web:
    dependencies:
      left:
        specifier: ^1.0.0
        version: 1.0.0
    devDependencies:
      tool:
        specifier: ^2.0.0
        version: 2.0.0
packages:
  left@1.0.0:
    resolution: {integrity: sha512-LEFT}
  tool@2.0.0:
    resolution: {integrity: sha512-TOOL}
snapshots:
  left@1.0.0: {}
  tool@2.0.0: {}
'''

LICENSE_TEXT = b'MIT License\n\nCopyright (c) synthetic\n'
BSD_TEXT = b'Redistribution and use in source and binary forms ... Neither the name ... (synthetic three-clause text)\n'


@pytest.fixture
def repo(tmp_path):
    (tmp_path / 'uv.lock').write_text(UV_LOCK)
    (tmp_path / 'pnpm-lock.yaml').write_text(PNPM_LOCK)
    (tmp_path / 'LICENSE').write_bytes(LICENSE_TEXT)
    (tmp_path / 'NOTICE').write_text('NexLoop synthetic notice\n')
    (tmp_path / 'versions.lock.json').write_text(json.dumps({'upstream_sources': {}}))
    policy = cl.load_policy(ROOT / 'deploy/compliance/license-policy.v1.json')
    policy.update(clarifications=[], conditional=[], off_platform=[], vendored={}, assets=[],
                  project_license={'file': 'LICENSE', 'spdx': 'MIT', 'sha256': hashlib.sha256(LICENSE_TEXT).hexdigest(), 'notice_copies': []})
    installed = {('python', 'liba', '1.0'): 'MIT', ('python', 'devb', '2.0'): 'Apache-2.0',
                 ('node', 'left', '1.0.0'): 'MIT', ('node', 'tool', '2.0.0'): 'ISC'}
    files = {}

    def lookup(ecosystem):
        def find(name, version):
            key = (ecosystem, name, version)
            if key not in installed:
                return None
            return {'raw': installed[key], 'read': lambda relative: files.get((key, relative))}
        return find

    def run(write=False):
        rows, problems, notices = cl.check(tmp_path, policy, python_lookup=lookup('python'), node_lookup=lookup('node'), tracked=tracked)
        if write:
            (tmp_path / 'THIRD_PARTY_NOTICES.md').write_text(notices)
            rows, problems, notices = cl.check(tmp_path, policy, python_lookup=lookup('python'), node_lookup=lookup('node'), tracked=tracked)
        return {(r['ecosystem'], r['name']): r for r in rows}, problems, notices
    tracked = ['LICENSE', 'NOTICE', 'uv.lock']
    return {'root': tmp_path, 'policy': policy, 'installed': installed, 'files': files, 'run': run, 'tracked': tracked}


def test_a_compliant_inventory_passes_and_scopes_follow_the_dependency_closure(repo):
    rows, problems, notices = repo['run'](write=True)
    assert problems == []
    assert rows[('python', 'liba')]['scope'] == 'runtime' and rows[('python', 'devb')]['scope'] == 'dev'
    assert rows[('node', 'left')]['scope'] == 'runtime' and rows[('node', 'tool')]['scope'] == 'dev'
    assert ('python', 'nexloop-eios-core') not in rows  # the project's own package is not third-party
    assert '| liba | 1.0 | MIT |' in notices and '| tool | 2.0.0 | ISC |' in notices


def test_notices_drift_fails(repo):
    repo['run'](write=True)
    target = repo['root'] / 'THIRD_PARTY_NOTICES.md'
    target.write_text(target.read_text() + 'hand edit\n')
    assert any('THIRD_PARTY_NOTICES.md' in p for p in repo['run']()[1])
    target.unlink()
    assert any('THIRD_PARTY_NOTICES.md' in p for p in repo['run']()[1])


@pytest.mark.parametrize('raw', [None, ['BSD License'], 'BSD License', 'UNKNOWN', ['MIT License', 'Some Custom License']])
def test_missing_or_ambiguous_metadata_is_never_guessed(repo, raw):
    repo['installed'][('python', 'liba', '1.0')] = raw
    rows, problems, _ = repo['run']()
    assert rows[('python', 'liba')]['license'] is None or rows[('python', 'liba')]['category'] == 'denied'
    assert any('liba@1.0' in p for p in problems)


def test_a_clarification_needs_the_hash_of_the_license_file_it_was_read_from(repo):
    repo['installed'][('python', 'liba', '1.0')] = ['BSD License']
    path = 'liba-1.0.dist-info/licenses/LICENSE'
    repo['policy']['clarifications'] = [{'ecosystem': 'python', 'name': 'liba', 'version_pattern': r'1\.0', 'license': 'BSD-3-Clause',
                                          'license_file': path, 'license_file_sha256': hashlib.sha256(BSD_TEXT).hexdigest(), 'basis': 'synthetic'}]
    rows, problems, _ = repo['run']()
    assert any('clarified license file missing or changed' in p for p in problems)  # file absent
    repo['files'][(('python', 'liba', '1.0'), path)] = BSD_TEXT
    rows, problems, _ = repo['run'](write=True)
    assert problems == [] and rows[('python', 'liba')]['license'] == 'BSD-3-Clause' and rows[('python', 'liba')]['source'] == 'clarification'
    repo['files'][(('python', 'liba', '1.0'), path)] = BSD_TEXT + b'changed'
    assert any('clarified license file missing or changed' in p for p in repo['run']()[1])


def test_a_package_installed_on_no_checked_platform_needs_a_record_bound_to_the_locked_artifact(repo):
    del repo['installed'][('node', 'tool', '2.0.0')]
    with pytest.raises(cl.Incomplete):
        repo['run']()
    record = {'ecosystem': 'node', 'name': 'tool', 'version': '2.0.0', 'artifact': 'sha512-OTHER', 'license': 'MIT', 'license_basis': 'package.json license field',
              'license_file': 'LICENSE', 'license_file_sha256': 'c' * 64, 'recorded': '2026-10-11'}
    repo['policy']['off_platform'] = [record]
    assert any('another artifact' in p for p in repo['run']()[1])
    record['artifact'] = 'sha512-TOOL'
    rows, problems, _ = repo['run'](write=True)
    assert problems == [] and rows[('node', 'tool')]['source'] == 'off_platform_record'
    record.pop('license_file_sha256')
    assert any('exactly one kind of license text evidence' in p for p in repo['run']()[1])
    record['license_text_from'] = {'ecosystem': 'node', 'name': 'left', 'version': '1.0.0', 'file': 'LICENSE', 'sha256': hashlib.sha256(LICENSE_TEXT).hexdigest()}
    assert any('referenced license text missing or changed' in p for p in repo['run']()[1])
    repo['files'][(('node', 'left', '1.0.0'), 'LICENSE')] = LICENSE_TEXT
    assert repo['run'](write=True)[1] == []
    record.pop('license_text_from')
    record['license_text_absent'] = 'synthetic: the artifact ships no license text'
    rows, problems, notices = repo['run'](write=True)
    assert problems == [] and 'tool 2.0.0 (MIT): synthetic' in notices
    record.pop('license_basis')
    assert any('record incomplete' in p for p in repo['run']()[1])


def test_weak_copyleft_needs_its_own_package_entry_for_that_version_and_scope(repo):
    repo['installed'][('python', 'liba', '1.0')] = 'LGPL-3.0-only'
    assert any('weak-copyleft license without a package entry' in p for p in repo['run']()[1])
    entry = {'ecosystem': 'python', 'name': 'liba', 'version_pattern': r'1\..*', 'scopes': ['dev'], 'use': 'synthetic separate library', 'review_by': '2099-01-01'}
    repo['policy']['conditional'] = [entry]
    assert any('does not cover this version or scope' in p for p in repo['run']()[1])  # liba is runtime
    entry['scopes'] = ['runtime']
    entry['review_by'] = '2020-01-01'
    assert any('review date passed' in p for p in repo['run']()[1])
    entry['review_by'] = '2099-01-01'
    rows, problems, notices = repo['run'](write=True)
    assert problems == [] and 'liba 1.0 (LGPL-3.0-only, runtime): synthetic separate library' in notices
    other = copy.deepcopy(entry)
    other['name'] = 'devb'
    repo['installed'][('python', 'devb', '2.0')] = 'MPL-2.0'
    assert any('devb@2.0' in p for p in repo['run']()[1])  # an entry for one package never covers another


@pytest.mark.parametrize('expression,category', [('MIT', 'allowed'), ('GPL-3.0-only', 'denied'), ('Some-Unlisted-1.0', 'denied'),
                                                 ('MIT OR GPL-3.0-only', 'allowed'), ('MIT AND GPL-3.0-only', 'denied'),
                                                 ('(MIT OR Apache-2.0) AND LGPL-3.0-only', 'conditional'), ('AGPL-3.0-only', 'denied')])
def test_expressions_take_the_best_alternative_and_the_worst_conjunct(expression, category):
    policy = cl.load_policy(ROOT / 'deploy/compliance/license-policy.v1.json')
    assert cl.classify(cl.parse(expression), policy) == category


@pytest.mark.parametrize('expression', ['', 'MIT OR', '(MIT', 'MIT AND AND Apache-2.0', 'MIT ) OR (ISC'])
def test_malformed_expressions_are_rejected(expression):
    with pytest.raises(ValueError):
        cl.parse(expression)


def test_repository_assets_must_be_listed_with_an_owner(repo):
    repo['tracked'].append('docs/design/new-picture.png')
    assert any('docs/design/new-picture.png: not listed' in p for p in repo['run']()[1])
    repo['policy']['assets'] = [{'path': 'docs/design/new-picture.png', 'owner': 'third-party', 'license': 'GPL-3.0-only'}]
    assert any('license denied' in p for p in repo['run']()[1])
    repo['policy']['assets'] = [{'path': 'docs/design/new-picture.png', 'owner': 'project'}]
    assert not any('asset' in p for p in repo['run']()[1])


def test_project_license_change_fails(repo):
    (repo['root'] / 'LICENSE').write_bytes(b'Proprietary\n')
    assert any('project LICENSE missing or changed' in p for p in repo['run']()[1])


def test_the_repository_vendored_sources_match_their_provenance_and_notice():
    policy = cl.load_policy(ROOT / 'deploy/compliance/license-policy.v1.json')
    assert cl.vendored_problems(ROOT, policy) == []
    assert cl.asset_problems(ROOT, policy) == []
    assert cl.off_platform_problems(ROOT, policy, node_lookup=lambda name, version: {'read': lambda relative: None}) != []  # lookups are verified


def test_vendored_file_tampering_is_detected(tmp_path):
    policy = cl.load_policy(ROOT / 'deploy/compliance/license-policy.v1.json')
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(tmp_path / 'src.tar'), 'HEAD', 'NOTICE', 'LICENSE', 'licenses', 'versions.lock.json',
                    'vendor/pi', 'packages/eios-core'], cwd=ROOT, check=True)
    subprocess.run(['tar', '-xf', str(tmp_path / 'src.tar'), '-C', str(tmp_path)], check=True)
    assert cl.vendored_problems(tmp_path, policy) == []
    provenance = json.loads((tmp_path / 'vendor/pi/PROVENANCE.json').read_text())
    target = tmp_path / provenance['files'][0]['path']
    target.write_bytes(target.read_bytes() + b'\n')
    (tmp_path / 'packages/eios-core/NOTICE').write_text('changed\n')
    problems = cl.vendored_problems(tmp_path, policy)
    assert any('differs from its provenance record' in p for p in problems) and any('differs from NOTICE' in p for p in problems)


def test_the_policy_shape_is_strict(tmp_path):
    policy = json.loads((ROOT / 'deploy/compliance/license-policy.v1.json').read_text())
    for broken in ({**policy, 'extra': 1}, {**policy, 'allowed': policy['allowed'] + ['MPL-2.0']}):
        (tmp_path / 'p.json').write_text(json.dumps(broken))
        with pytest.raises(cl.InputError):
            cl.load_policy(tmp_path / 'p.json')


def test_off_platform_records_in_the_policy_are_complete_and_bound_to_the_lock():
    import tomllib
    import yaml
    policy = cl.load_policy(ROOT / 'deploy/compliance/license-policy.v1.json')
    uv = {(p['name'], p['version']): cl._python_artifact(p) for p in tomllib.loads((ROOT / 'uv.lock').read_text())['package']}
    pnpm = {key.rpartition('@')[0::2]: value['resolution']['integrity'] for key, value in yaml.safe_load((ROOT / 'pnpm-lock.yaml').read_text())['packages'].items()}
    for record in policy['off_platform']:
        artifacts = uv if record['ecosystem'] == 'python' else pnpm
        assert artifacts[(record['name'], record['version'])] == record['artifact'], record['name']
        assert record['license'] and record['license_basis'] and sum(k in record for k in ('license_file_sha256', 'license_text_from', 'license_text_absent')) == 1


def test_assets_are_checked_in_a_git_archive_extraction_without_git(tmp_path):
    """The release preflight runs the license check in a `git archive` extraction of the commit (no .git; release.py
    _clean_checkout). The asset check reads that tree's files instead of `git ls-files`, ignoring what the dependency phase
    installed, and still finds an unlisted asset."""
    import tarfile
    archive = tmp_path / 'source.tar'
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), 'HEAD'], cwd=ROOT, check=True)
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(checkout, filter='data')
    assert not (checkout / '.git').exists()
    policy = cl.load_policy(checkout / 'deploy/compliance/license-policy.v1.json')
    for installed in ('.venv/lib/site-packages/pkg/icon.png', 'node_modules/pkg/logo.svg', 'apps/web/node_modules/x/y.woff2'):
        path = checkout / installed
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    assert cl.asset_problems(checkout, policy) == []
    (checkout / 'apps/web/unlisted-banner.png').write_bytes(b'x')
    assert cl.asset_problems(checkout, policy) == ['asset apps/web/unlisted-banner.png: not listed (record its owner or third-party license)']
