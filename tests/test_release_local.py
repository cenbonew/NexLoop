"""NX-034 local part (scripts/release/release.py): manifest, ssh-keygen -Y signature, AT-055 verification, SBOM and scan with
pinned syft/grype (missing tools are never a pass), AT-057 rollback protection. Temporary directories and freshly generated
keys only; nothing is pushed, tagged, published or sent anywhere."""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
import release  # noqa: E402

SCRIPT = ROOT / 'scripts/release/release.py'
DIGEST_API = 'sha256:' + 'a' * 64
DIGEST_HOST = 'sha256:' + 'b' * 64


def head_sha():
    return subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()


def oci_archive(path, digest):
    """A minimal OCI image layout archive whose index.json declares ``digest``."""
    with tarfile.open(path, 'w') as tar:
        for name, body in (('oci-layout', b'{"imageLayoutVersion":"1.0.0"}'),
                           ('index.json', json.dumps({'schemaVersion': 2, 'manifests': [{'digest': digest, 'mediaType': 'application/vnd.oci.image.manifest.v1+json'}]}).encode())):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


@pytest.fixture
def inputs(tmp_path):
    sha = head_sha()
    images = {'nexloop-api': {'archive': str(oci_archive(tmp_path / 'api.tar', DIGEST_API)), 'digest': DIGEST_API, 'base': 'python-build-base'},
              'nexloop-host': {'archive': str(oci_archive(tmp_path / 'host.tar', DIGEST_HOST)), 'digest': DIGEST_HOST}}
    (tmp_path / 'images.json').write_text(json.dumps(images))
    ci = {'sha': sha, 'status': 'passed', 'policy_version': 1, 'deadline_gate': 'passed', 'counts': {'passed': 10, 'failed': 0, 'skipped': 0}}
    (tmp_path / 'ci.json').write_text(json.dumps(ci))
    return dict(sha=sha, images=tmp_path / 'images.json', ci=tmp_path / 'ci.json', tmp=tmp_path)


def make(i, name='release', **kw):
    return release.build_manifest(sha=i['sha'], images=i['images'], ci_result=i['ci'], out=i['tmp'] / name, **kw)


def keypair(tmp, name):
    key = tmp / name
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'synthetic-test-key', '-f', str(key)], check=True)
    allowed = tmp / (name + '.allowed')
    allowed.write_text(f'{release.SIGNER_IDENTITY} namespaces="{release.NAMESPACE}" {(tmp / (name + ".pub")).read_text().strip()}\n')
    return key, allowed


def cli(*args, env=None):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, env=env)
    return result.returncode, json.loads(result.stdout)


def test_manifest_records_the_tested_commit_and_its_inputs(inputs):
    m = make(inputs)
    catalog = json.loads((ROOT / 'packages/eios-core/src/eios/migrations/catalog.json').read_text())
    assert m['commit'] == inputs['sha'] and m['release_id'].endswith('-' + inputs['sha'][:12] + '-1')
    assert m['catalog']['head'] == catalog['migrations'][-1]['version'] and m['catalog']['count'] == len(catalog['migrations'])
    assert m['runtime_store_format'] == 1 and set(m['locks']) == {'versions.lock.json', 'uv.lock', 'pnpm-lock.yaml'}
    assert [i['name'] for i in m['images']] == ['nexloop-api', 'nexloop-host'] and m['images'][0]['base_digest'].startswith('sha256:')
    assert m['images'][0]['archive_sha256'] == release.sha256_file(inputs['tmp'] / 'release' / m['images'][0]['archive'])
    assert m['tests'] == {'passed': 10, 'failed': 0, 'skipped': 0} and m['deadline_gate'] == 'passed' and len(m['source_archive_sha256']) == 64
    text = (inputs['tmp'] / 'release/manifest.json').read_text()
    assert str(Path.home()) not in text and str(inputs['tmp']) not in text  # hashes, digests, versions only; no host paths
    with pytest.raises(release.InputError, match='immutable'):
        make(inputs)


def test_manifest_refuses_an_untested_commit_or_a_mismatched_archive(inputs):
    ci = json.loads(inputs['ci'].read_text())
    for change in ({'status': 'failed'}, {'sha': '0' * 40}):
        inputs['ci'].write_text(json.dumps({**ci, **change}))
        with pytest.raises(release.Refused, match='CI result'):
            make(inputs, name='r-' + next(iter(change)))
    inputs['ci'].write_text(json.dumps(ci))
    images = json.loads(inputs['images'].read_text())
    images['nexloop-api']['digest'] = 'sha256:' + 'c' * 64  # the archive declares another digest
    inputs['images'].write_text(json.dumps(images))
    with pytest.raises(release.Refused, match='does not contain the declared digest'):
        make(inputs, name='r-digest')


def test_signed_manifest_verifies_and_every_deploy_mismatch_is_refused(inputs):
    make(inputs)
    rel = inputs['tmp'] / 'release'
    key, allowed = keypair(inputs['tmp'], 'release-key')
    images = ['--deploy-image', f'nexloop-api={DIGEST_API}', '--deploy-image', f'nexloop-host={DIGEST_HOST}']
    code, out = cli('verify', '--release-dir', rel, '--allowed-signers', allowed, '--deploy-sha', inputs['sha'], *images)
    assert code == 1 and 'not signed' in out['refused']
    assert cli('sign', '--release-dir', rel, '--key', key)[0] == 0
    code, out = cli('verify', '--release-dir', rel, '--allowed-signers', allowed, '--deploy-sha', inputs['sha'], *images)
    assert code == 0 and out['verified'] is True
    # AT-055: a different commit, a different image digest, a missing image, an unknown image.
    code, out = cli('verify', '--release-dir', rel, '--allowed-signers', allowed, '--deploy-sha', '1' * 40, *images)
    assert code == 1 and 'deploy_sha_differs_from_tested_commit' in out['refused']
    code, out = cli('verify', '--release-dir', rel, '--allowed-signers', allowed, '--deploy-sha', inputs['sha'],
                    '--deploy-image', f'nexloop-api={DIGEST_HOST}', '--deploy-image', f'nexloop-host={DIGEST_HOST}')
    assert code == 1 and 'image_digest_differs:nexloop-api' in out['refused']
    code, out = cli('verify', '--release-dir', rel, '--allowed-signers', allowed, '--deploy-sha', inputs['sha'], '--deploy-image', f'nexloop-api={DIGEST_API}')
    assert code == 1 and 'image_not_deployed_from_manifest:nexloop-host' in out['refused']
    code, out = cli('verify', '--release-dir', rel, '--allowed-signers', allowed, '--deploy-sha', inputs['sha'], *images, '--deploy-image', f'other={DIGEST_API}')
    assert code == 1 and 'image_not_in_manifest:other' in out['refused']
    # A replaced archive, an edited manifest and another signer are refused.
    archive = rel / 'images/nexloop-host.tar'
    original = archive.read_bytes()
    archive.write_bytes(original + b'x')
    assert 'archive_changed:nexloop-host' in cli('verify', '--release-dir', rel, '--allowed-signers', allowed, '--deploy-sha', inputs['sha'], *images)[1]['refused']
    archive.write_bytes(original)
    _, other = keypair(inputs['tmp'], 'other-key')
    assert 'signature invalid' in cli('verify', '--release-dir', rel, '--allowed-signers', other, '--deploy-sha', inputs['sha'], *images)[1]['refused']
    manifest = rel / 'manifest.json'
    manifest.write_text(manifest.read_text().replace('"passed"', '"Passed"', 1))
    assert 'signature invalid' in cli('verify', '--release-dir', rel, '--allowed-signers', allowed, '--deploy-sha', inputs['sha'], *images)[1]['refused']


def fake_tool(bin_dir, name, version, body):
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\nif [ "$1" = version ]; then echo "Application: {name}"; echo "Version: {version}"; exit 0; fi\n{body}\n')
    path.chmod(0o755)


def test_sbom_and_scan_need_the_pinned_tools_and_never_pass_without_them(inputs):
    make(inputs)
    rel = inputs['tmp'] / 'release'
    bare = {**os.environ, 'PATH': '/usr/bin:/bin'}
    code, out = cli('sbom', '--release-dir', rel, env=bare)
    assert code == 3 and 'syft not installed' in out['not_installed']
    pinned = release.pinned_tools()
    bin_dir = inputs['tmp'] / 'bin'
    bin_dir.mkdir()
    # Stand-ins with the pinned versions: syft writes an SPDX file, grype reports one critical (fixable) and one low finding.
    fake_tool(bin_dir, 'syft', pinned['syft']['version'], 'for a in "$@"; do case "$a" in spdx-json=*) echo \'{"spdxVersion":"SPDX-2.3"}\' > "${a#spdx-json=}";; esac; done')
    report = json.dumps({'matches': [{'vulnerability': {'severity': 'Critical', 'fix': {'state': 'fixed'}, 'description': 'SECRET-TEXT'}},
                                     {'vulnerability': {'severity': 'Low'}}], 'descriptor': {'db': {'built': '2026-10-10T00:00:00Z'}}})
    fake_tool(bin_dir, 'grype', pinned['grype']['version'], f"echo '{report}'")
    env = {**os.environ, 'PATH': f'{bin_dir}:/usr/bin:/bin'}
    code, out = cli('sbom', '--release-dir', rel, env=env)
    assert code == 0, out
    manifest = json.loads((rel / 'manifest.json').read_text())
    assert manifest['scan']['images']['nexloop-api']['counts']['Critical'] == 1 and manifest['scan']['images']['nexloop-api']['critical_with_fix'] == 1
    assert manifest['images'][0]['sbom_sha256'] and manifest['scan']['tools'] == {'syft': pinned['syft']['version'], 'grype': pinned['grype']['version']}
    assert 'SECRET-TEXT' not in (rel / 'manifest.json').read_text()  # counts only
    fake_tool(bin_dir, 'grype', '0.0.1', 'exit 0')
    code, out = cli('sbom', '--release-dir', rel, env=env)
    assert code == 1 and 'not the pinned' in out['refused']


def migrations_dir(tmp, label, marked):
    d = tmp / label
    d.mkdir()
    rows = []
    for version, name in (('0001', 'base'), ('0002', 'add_column'), ('0003', 'drop_column')):
        body = (release.EXPAND_ONLY + '\n' if version in marked else '-- synthetic\n') + 'select 1;\n'
        (d / f'{version}_{name}.sql').write_text(body)
        rows.append({'version': version, 'name': f'{version}_{name}.sql', 'sha256': '0' * 64})
    (d / 'catalog.json').write_text(json.dumps({'lineage': 'synthetic', 'migrations': rows}))
    return d


def manifest_file(tmp, name, head, fmt, release_id):
    path = tmp / name
    path.write_text(json.dumps({'release_id': release_id, 'catalog': {'head': head, 'expand_only_since': head}, 'runtime_store_format': fmt}))
    return path


def test_rollback_is_refused_unless_the_target_runs_on_the_current_schema_and_runtime(tmp_path):
    current = manifest_file(tmp_path, 'current.json', '0003', 2, 'r-current')
    # Same schema and runtime: allowed.
    same = manifest_file(tmp_path, 'same.json', '0003', 2, 'r-same')
    plain = migrations_dir(tmp_path, 'plain', set())
    assert release.rollback_check(current, same, migrations=plain)['rollback_allowed']
    # Older head with an unmarked migration above it: refused with a forward-fix hint (AT-057).
    older = manifest_file(tmp_path, 'older.json', '0002', 2, 'r-older')
    with pytest.raises(release.Refused) as refused:
        release.rollback_check(current, older, migrations=plain)
    detail = json.loads(str(refused.value))
    assert detail['reasons'] == ['schema_not_backward_compatible:0003'] and 'fix forward' in detail['hints'][0]
    # Only expand-only migrations above the target: allowed.
    marked = migrations_dir(tmp_path, 'marked', {'0003'})
    assert release.rollback_check(current, older, migrations=marked)['target_head'] == '0002'
    # A runtime store format downgrade, a newer target, a database ahead of the checkout: refused.
    oldfmt = manifest_file(tmp_path, 'oldfmt.json', '0003', 1, 'r-oldfmt')
    with pytest.raises(release.Refused, match='runtime_store_format_downgrade:2->1'):
        release.rollback_check(current, oldfmt, migrations=marked)
    newer = manifest_file(tmp_path, 'newer.json', '0004', 2, 'r-newer')
    with pytest.raises(release.Refused, match='target_is_newer_schema'):
        release.rollback_check(current, newer, migrations=marked)
    with pytest.raises(release.Refused, match='migrations_between_unknown'):
        release.rollback_check(current, same, database_head='0009', migrations=marked)
    # The CLI reports the refusal with exit 1.
    code, out = cli('rollback-check', '--current', current, '--target', oldfmt)
    assert code == 1 and 'runtime_store_format_downgrade' in out['refused']
