"""NX-034 part 2 (scripts/release/release.py build / load-check / preflight, scripts/release/archive.py): images built from the
released commit and saved into the release directory under nexloop/<name>@<digest>, the stage preflight recorded in the signed
manifest and checked by verify, and the local archive (seal, index, D3 retention). A stand-in docker records every call and
writes OCI archives; no Docker daemon is started, nothing is pushed or loaded into a real image store."""
import io
import json
import os
import subprocess
import sys
import tarfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive  # noqa: E402
import release  # noqa: E402
from test_release_compliance import Runner, finding, policy, spdx, waiver  # noqa: E402
from test_release_local import head_sha, keypair, passed_preflight  # noqa: E402
from support.writable_tree import make_writable  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_tree_writable_after(tmp_path):
    """Teardown: the release trees this test made read-only become removable again (support.writable_tree)."""
    yield
    make_writable(tmp_path)


CONTAINERD = '[["driver-type","io.containerd.snapshotter.v1"]]'


def digest_of(name):
    return 'sha256:' + (name.encode().hex() * 64)[:64]


class Docker:
    """Stand-in for the docker CLI with the containerd image store: `save` writes an OCI layout whose index.json names the
    tag with the annotations docker save records; `image inspect` answers the manifest digest; `load` remembers it."""

    def __init__(self, info=CONTAINERD, digests=None, inspect=None, fail=(), daemon='linux/x86_64', image_platform=('linux', 'amd64'), attest=False):
        self.info, self.digests, self.inspect, self.fail, self.calls, self.store = info, digests or {}, inspect or {}, set(fail), [], {}
        self.daemon, self.image_platform, self.attest = daemon, image_platform, attest

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        if args[0] in self.fail:
            return 1, ''
        if args[0] == 'info':
            return 0, (self.daemon if '{{.OSType}}/{{.Architecture}}' in args else self.info)
        if args[0] in ('build', 'tag'):
            tag = args[args.index('--tag') + 1] if args[0] == 'build' else args[2]
            self.store[tag] = self.digests.get(tag.split(':')[0], digest_of(tag.split(':')[0]))
            return 0, ''
        if args[0] == 'save':
            path, tag = Path(args[2]), args[3]
            index = {'schemaVersion': 2, 'manifests': [{'mediaType': 'application/vnd.oci.image.index.v1+json', 'digest': self.store[tag],
                                                         'annotations': {'io.containerd.image.name': 'docker.io/' + tag,
                                                                         'org.opencontainers.image.ref.name': tag.rsplit(':', 1)[1]}}]}
            # The saved digest names an OCI index of the platform image (and, with attestations on, a buildx attestation manifest).
            nested = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': [
                {'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'digest': 'sha256:' + '1' * 64,
                 'platform': {'os': self.image_platform[0], 'architecture': self.image_platform[1]}}]
                + ([{'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'digest': 'sha256:' + '2' * 64, 'platform': {'os': 'unknown', 'architecture': 'unknown'},
                     'annotations': {'vnd.docker.reference.type': 'attestation-manifest'}}] if self.attest else [])}
            with tarfile.open(path, 'w') as tar:
                for name, body in (('oci-layout', b'{"imageLayoutVersion":"1.0.0"}'), ('index.json', json.dumps(index).encode()),
                                   ('blobs/sha256/' + self.store[tag].split(':')[1], json.dumps(nested).encode())):
                    info = tarfile.TarInfo(name)
                    info.size = len(body)
                    tar.addfile(info, io.BytesIO(body))
            return 0, ''
        if args[0] == 'load':
            with tarfile.open(args[-1]) as tar:
                index = json.load(tar.extractfile('index.json'))
            for m in index['manifests']:
                name = m['annotations']['io.containerd.image.name'].removeprefix('docker.io/').rsplit(':', 1)[0]
                self.store[f"{name}@{m['digest']}"] = m['digest']
            return 0, ''
        if args[:2] == ['image', 'inspect']:
            reference = args[-1]
            platform = (' ' + '/'.join(self.image_platform)) if '{{.Os}}' in args[3] else ''
            if reference in self.inspect:
                return 0, self.inspect[reference] + platform + '\n'
            if reference.startswith('docker.io/') and '@sha256:' in reference and platform:
                return 0, reference.split('@')[1] + platform + '\n'
            if reference.startswith('docker.io/') and '@sha256:' in reference:  # pinned upstream present by digest
                return 0, reference.split('@')[1] + '\n'
            return (0, self.store[reference] + '\n') if reference in self.store else (1, '')
        raise AssertionError(args)


def preparer(context, target):
    target.mkdir(parents=True)
    (target / 'Dockerfile').write_text('FROM scratch\n')
    return target


@pytest.fixture
def ci_result(tmp_path):
    path = tmp_path / 'result.json'
    path.write_text(json.dumps({'sha': head_sha(), 'status': 'passed', 'policy_version': 1, 'deadline_gate': 'passed', 'counts': {'passed': 1}}))
    return path


def build(tmp_path, ci_result, docker, sequence, **kw):
    kw.setdefault('only', ['agent-host', 'backend'])
    return release.build(head_sha(), ci_result, tmp_path / 'releases-root', sequence=sequence, root=ROOT, docker=docker, preparer=preparer,
                         now=datetime(2026, 10, 11, tzinfo=UTC), checkout_check=lambda root, sha: None, **kw)


def test_build_saves_each_image_under_the_reference_the_deploy_dry_run_renders(tmp_path, ci_result):
    docker = Docker()
    result = build(tmp_path, ci_result, docker, 101)
    rid = f'20261011-{head_sha()[:12]}-101'
    directory = tmp_path / 'releases-root' / 'releases' / rid
    assert result['release_id'] == rid and Path(result['release_dir']) == directory
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['manifest_version'] == 2 and manifest['preflight'] is None
    assert {i['name']: i['reference'] for i in manifest['images']} == {'agent-host': 'nexloop/agent-host@' + digest_of('nexloop/agent-host'),
                                                                     'backend': 'nexloop/backend@' + digest_of('nexloop/backend')}
    for image in manifest['images']:
        assert release.saved_image_digest(directory / image['archive'], f"nexloop/{image['name']}:{rid}") == image['digest']
    builds = [c for c in docker.calls if c[0] == 'build']
    lock = json.loads((ROOT / 'versions.lock.json').read_text())['images']
    backend = next(c for c in builds if f'nexloop/backend:{rid}' in c)
    assert f"PYTHON_IMAGE=docker.io/library/python@{lock['python-build-base']['digest']}" in backend and '--pull=false' in backend
    assert f'org.opencontainers.image.revision={head_sha()}' in backend
    assert manifest['images'][0]['base_digest'] == lock['node-host-base']['digest']  # agent-host: the Node runtime stage
    assert not (ROOT / '.ci-results/release-build' / rid).exists()  # staging removed
    assert str(tmp_path) not in (directory / 'manifest.json').read_text()


def test_build_refuses_without_a_passed_result_the_containerd_store_or_a_consistent_digest(tmp_path, ci_result, monkeypatch):
    failed = tmp_path / 'failed.json'
    failed.write_text(json.dumps({'sha': head_sha(), 'status': 'failed'}))
    with pytest.raises(release.Refused, match='CI result'):
        build(tmp_path, failed, Docker(), 102)
    with pytest.raises(release.InputError, match='containerd image store'):
        build(tmp_path, ci_result, Docker(info='[["Backing Filesystem","extfs"]]'), 103)
    with pytest.raises(release.Refused, match='local image id differs'):
        build(tmp_path, ci_result, Docker(inspect={f'nexloop/backend:20261011-{head_sha()[:12]}-104': 'sha256:' + 'f' * 64}), 104, only=['backend'])
    with pytest.raises(release.Refused, match='image build failed'):
        build(tmp_path, ci_result, Docker(fail={'build'}), 105, only=['backend'])
    # An unpinned gateway is refused; simulated, since versions.lock.json pins it since the first release (NX-034).
    lock = json.loads((ROOT / 'versions.lock.json').read_text())
    lock['images']['gateway'] = {'digest': '', 'status': 'unresolved_not_deployable'}
    real = Path.read_text
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', lambda self, *a, **k: json.dumps(lock) if self == ROOT / 'versions.lock.json' else real(self, *a, **k))
        with pytest.raises(release.Refused, match='gateway: not pinned'):
            build(tmp_path, ci_result, Docker(), 106, only=['gateway'])
    with pytest.raises(release.InputError, match='unknown image'):
        build(tmp_path, ci_result, Docker(), 107, only=['other'])
    build(tmp_path, ci_result, Docker(), 108, only=['backend'])
    with pytest.raises(release.InputError, match='immutable'):
        build(tmp_path, ci_result, Docker(), 108, only=['backend'])
    assert not list((ROOT / '.ci-results/release-build').glob(f'*-{head_sha()[:12]}-10[2-8]')) if (ROOT / '.ci-results/release-build').exists() else True


def test_an_upstream_image_is_retagged_by_digest_never_pulled(tmp_path, ci_result, monkeypatch):
    # No release image is 'upstream' since the gateway is rebuilt (NX-036); the kind stays supported: a synthetic entry.
    lock = json.loads((ROOT / 'versions.lock.json').read_text())
    lock['images']['proxy'] = {'reference': 'docker.io/library/caddy:2.10.0-alpine', 'digest': 'sha256:' + 'c' * 64}
    config = json.loads((ROOT / 'deploy/release/images.v1.json').read_text())
    config['images']['proxy'] = {'kind': 'upstream', 'lock': 'proxy'}
    real = Path.read_text
    replaced = {ROOT / 'versions.lock.json': lock, ROOT / 'deploy/release/images.v1.json': config}
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **k: json.dumps(replaced[self]) if self in replaced else real(self, *a, **k))
    docker = Docker()
    build(tmp_path, ci_result, docker, 109, only=['proxy'])
    assert ['tag', 'docker.io/library/caddy@sha256:' + 'c' * 64, f'nexloop/proxy:20261011-{head_sha()[:12]}-109'] in docker.calls
    assert not any(c[0] == 'pull' for c in docker.calls)


def test_saved_digest_needs_exactly_one_manifest_for_the_tag(tmp_path):
    def oci(manifests):
        path = tmp_path / f'{len(manifests)}-{os.urandom(4).hex()}.tar'
        body = json.dumps({'schemaVersion': 2, 'manifests': manifests}).encode()
        with tarfile.open(path, 'w') as tar:
            info = tarfile.TarInfo('index.json')
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        return path
    one = {'digest': 'sha256:' + 'a' * 64, 'annotations': {'org.opencontainers.image.ref.name': 'r1'}}
    assert release.saved_image_digest(oci([one]), 'nexloop/backend:r1') == 'sha256:' + 'a' * 64
    two = {'digest': 'sha256:' + 'b' * 64, 'annotations': {'io.containerd.image.name': 'docker.io/nexloop/backend:r1'}}
    with pytest.raises(release.Refused):
        release.saved_image_digest(oci([one, two]), 'nexloop/backend:r1')
    with pytest.raises(release.Refused):
        release.saved_image_digest(oci([]), 'nexloop/backend:r1')
    with pytest.raises(release.Refused):
        release.saved_image_digest(tmp_path / 'missing.tar', 'nexloop/backend:r1')


def test_the_checkout_must_be_the_released_commit_without_local_changes(tmp_path):
    repo = tmp_path / 'checkout'
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 's', 'GIT_AUTHOR_EMAIL': 's@example.invalid',
           'GIT_COMMITTER_NAME': 's', 'GIT_COMMITTER_EMAIL': 's@example.invalid'}
    subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env)
    (repo / 'a').write_text('a\n')
    subprocess.run(['git', 'add', 'a'], cwd=repo, check=True, env=env)
    subprocess.run(['git', 'commit', '-q', '-m', 'a'], cwd=repo, check=True, env=env)
    sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    release.require_checkout_at(repo, sha)
    with pytest.raises(release.Refused, match='not at the released commit'):
        release.require_checkout_at(repo, '1' * 40)
    (repo / 'untracked').write_text('x\n')
    with pytest.raises(release.Refused, match='modified or untracked'):
        release.require_checkout_at(repo, sha)


def test_load_check_resolves_every_image_by_its_manifest_digest(tmp_path, ci_result):
    result = build(tmp_path, ci_result, Docker(), 110)
    directory = Path(result['release_dir'])
    assert release.load_check(directory, docker=Docker())['loaded'] == result['images']
    wrong = Docker(inspect={result['images']['backend']: 'sha256:' + 'e' * 64})
    with pytest.raises(release.Refused, match='loaded_digest_differs:backend'):
        release.load_check(directory, docker=wrong)
    with pytest.raises(release.InputError, match='containerd'):
        release.load_check(directory, docker=Docker(info='[]'))
    archive_path = next((directory / 'images').glob('backend.tar'))
    archive_path.write_bytes(archive_path.read_bytes() + b'x')
    with pytest.raises(release.Refused, match='archive_changed:backend'):
        release.load_check(directory, docker=Docker())


def PINNED():  # noqa: N802 - a stand-in for a lock whose release images are all pinned by digest
    return []


def sbom_ok():
    return spdx('pkg:pypi/liba@1.0'), [finding(severity='Critical')], None


def test_stage_preflight_blocks_on_licenses_and_tools_but_only_lists_sbom_differences(tmp_path):
    ok = release.stage_preflight('HEAD', tmp_path / 'a', runner=Runner(), sbom_maker=sbom_ok, pinned_checker=PINNED)
    assert ok['ok'] is True and list(ok['steps']) == list(release.STAGE_PREFLIGHT_STEPS) and ok['category'] == 'stage'
    assert ok['steps']['vulnerabilities']['detail']['listed'] == ['CVE-2026-0001']  # stage: listed, not blocking (D5)
    listed = release.stage_preflight('HEAD', tmp_path / 'b', runner=Runner(), sbom_maker=lambda: (spdx('pkg:pypi/liba@1.0', 'pkg:npm/x@1.0.0'), [], None),
                                     pinned_checker=PINNED)
    assert listed['ok'] is True and listed['steps']['source_sbom']['detail']['listed_count'] == 1
    assert release.stage_preflight('HEAD', tmp_path / 'c', runner=Runner(['check_licenses.py']), sbom_maker=sbom_ok)['steps']['licenses']['ok'] is False

    def missing():
        raise release.NotInstalled('syft not installed')
    no_tool = release.stage_preflight('HEAD', tmp_path / 'd', runner=Runner(), sbom_maker=missing)
    assert no_tool['ok'] is False and no_tool['steps']['source_sbom']['ok'] is False and no_tool['steps']['vulnerabilities']['ok'] is False


def test_stage_preflight_fails_on_an_expired_waiver(tmp_path, monkeypatch):
    expired = policy(waivers=[waiver(created='2025-12-01', expires='2026-01-01')])
    monkeypatch.setattr(release, 'load_vulnerability_policy', lambda root=None: expired)
    report = release.stage_preflight('HEAD', tmp_path / 'a', runner=Runner(), sbom_maker=sbom_ok)
    assert report['ok'] is False and any('expired' in p for p in report['steps']['vulnerabilities']['detail']['problems'])


@pytest.fixture
def built(tmp_path, ci_result, request):
    # A distinct sequence per test: the staging directory below the checkout is shared by parallel workers.
    sequence = 1000 + zlib.crc32(request.node.nodeid.encode()) % 900000
    result = build(tmp_path, ci_result, Docker(), sequence)
    key, allowed = keypair(tmp_path, 'release-key')
    return {'dir': Path(result['release_dir']), 'id': result['release_id'], 'key': key, 'allowed': allowed, 'root': tmp_path / 'releases-root',
            'images': result['images']}


def deploy_images(built):
    return {name: reference.split('@')[1] for name, reference in built['images'].items()}


def test_the_preflight_is_signed_with_the_manifest_and_verify_checks_it(built):
    report = release.run_preflight(built['dir'], 'stage', root=ROOT, runner=Runner(), sbom_maker=sbom_ok, pinned_checker=PINNED)
    assert report['ok'] is True and not (ROOT / '.ci-results/release-preflight' / f"{built['id']}-stage").exists()
    manifest = json.loads((built['dir'] / 'manifest.json').read_text())
    assert manifest['preflight']['category'] == 'stage' and manifest['preflight']['ok'] is True and manifest['preflight']['steps']['licenses'] is True
    assert sorted(p.name for p in (built['dir'] / 'preflight').iterdir()) == ['licenses.json', 'preflight.json']
    with pytest.raises(release.InputError, match='once'):
        release.run_preflight(built['dir'], 'stage', root=ROOT, runner=Runner(), sbom_maker=sbom_ok, pinned_checker=PINNED)
    release.sign(built['dir'], built['key'])
    with pytest.raises(release.InputError, match='signed'):
        release.attach_preflight(built['dir'], passed_preflight(head_sha()))
    assert release.verify(built['dir'], allowed_signers=built['allowed'], deploy_sha=head_sha(), deploy_images=deploy_images(built))['verified']
    report_path = built['dir'] / 'preflight/preflight.json'
    original = report_path.read_text()
    report_path.write_text(original.replace('"ok": true', '"ok": false', 1))
    with pytest.raises(release.Refused, match='preflight_report_changed'):
        release.verify(built['dir'], allowed_signers=built['allowed'], deploy_sha=head_sha(), deploy_images=deploy_images(built))
    report_path.write_text(original)


def test_verify_refuses_a_failed_missing_or_foreign_preflight(built, tmp_path):
    failed = passed_preflight(head_sha())
    failed['ok'] = False
    failed['steps']['licenses']['ok'] = False
    release.attach_preflight(built['dir'], failed)
    release.sign(built['dir'], built['key'])
    with pytest.raises(release.Refused, match='preflight_not_passed'):
        release.verify(built['dir'], allowed_signers=built['allowed'], deploy_sha=head_sha(), deploy_images=deploy_images(built))
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'manifest.json').write_text((built['dir'] / 'manifest.json').read_text())
    with pytest.raises(release.InputError, match='another commit'):
        manifest = json.loads((other / 'manifest.json').read_text())
        manifest['preflight'] = None
        (other / 'manifest.json').write_text(json.dumps(manifest))
        release.attach_preflight(other, {**passed_preflight(head_sha()), 'commit': '1' * 40})
    assert release.preflight_failures(other, {**manifest, 'preflight': None}) == ['preflight_missing']
    manifest['manifest_version'] = 1
    (other / 'manifest.json').write_text(json.dumps(manifest))
    (other / 'manifest.json.sig').write_bytes((built['dir'] / 'manifest.json.sig').read_bytes())
    with pytest.raises(release.Refused):
        release.verify(other, allowed_signers=built['allowed'], deploy_sha=head_sha(), deploy_images=deploy_images(built))


def test_seal_makes_a_complete_signed_release_read_only_and_indexes_it(built):
    with pytest.raises(release.Refused, match='not signed'):
        archive.seal(built['root'], built['id'], built['allowed'])
    release.attach_preflight(built['dir'], passed_preflight(head_sha()))
    release.sign(built['dir'], built['key'])
    (built['dir'] / 'notes.txt').write_text('x\n')
    with pytest.raises(release.Refused, match='unexpected_file:notes.txt'):
        archive.seal(built['root'], built['id'], built['allowed'])
    (built['dir'] / 'notes.txt').unlink()
    with pytest.raises(release.Refused, match='not_sealed'):
        archive.check(built['root'], built['id'], built['allowed'])
    assert archive.seal(built['root'], built['id'], built['allowed']) == {'sealed': built['id'], 'index': 1}
    assert all(not os.access(p, os.W_OK) for p in [built['dir'], *built['dir'].rglob('*')])
    with pytest.raises(release.InputError, match='already sealed'):
        archive.seal(built['root'], built['id'], built['allowed'])
    assert archive.check(built['root'], built['id'], built['allowed'])['images'] == built['images']
    index = json.loads((built['root'] / 'index.json').read_text())
    assert index['releases'][0]['release_id'] == built['id'] and index['releases'][0]['sealed'] is True and index['releases'][0]['preflight'] == {'category': 'stage', 'ok': True}
    with pytest.raises(release.InputError):
        archive.seal(built['root'], '../escape', built['allowed'])


def fake_release(root, n, built_at):
    """A sealed release directory as seal leaves it (no signature check needed for retention)."""
    rid = f'20261011-{n:012x}-1'
    directory = root / 'releases' / rid
    (directory / 'images').mkdir(parents=True)
    (directory / 'images/backend.tar').write_bytes(b'image')
    (directory / 'manifest.json').write_text(json.dumps({'release_id': rid, 'commit': f'{n:040x}', 'built_at': built_at,
                                                        'images': [{'name': 'backend', 'archive': 'images/backend.tar', 'reference': 'nexloop/backend@sha256:' + 'a' * 64}],
                                                        'preflight': {'category': 'stage', 'ok': True}}))
    (directory / 'manifest.json.sig').write_text('sig')
    archive._read_only(directory)
    return rid


def test_retention_keeps_recent_recently_deployed_current_and_previous_known_good(tmp_path):
    root = tmp_path / 'archive'
    now = datetime(2026, 10, 11, tzinfo=UTC)
    ids = [fake_release(root, n, (now - timedelta(days=100 - n)).isoformat()) for n in range(1, 11)]  # ids[-1] newest
    archive.record_deployment(root, ids[0], known_good=True, at=now - timedelta(days=200))
    archive.record_deployment(root, ids[1], known_good=True, at=now - timedelta(days=95))
    archive.record_deployment(root, ids[2], known_good=False, at=now - timedelta(days=30))
    archive.record_deployment(root, ids[3], known_good=True, at=now - timedelta(days=20))
    (root / 'current').symlink_to(f'releases/{ids[3]}')
    plan = archive.prune(root, now=now)
    assert plan['dry_run'] is True and plan['removed'] == [] and all((root / 'releases' / r / 'images/backend.tar').exists() for r in ids)
    assert plan['keep'] == {**{r: ['recent'] for r in ids[5:]}, ids[2]: ['deployed_recently'], ids[3]: ['current', 'current_link', 'deployed_recently'],
                            ids[1]: ['previous_known_good']}
    assert plan['remove_images'] == [ids[0], ids[4]]
    applied = archive.prune(root, apply=True, now=now)
    assert applied['removed'] == [ids[0], ids[4]]
    for rid in ids:
        directory = root / 'releases' / rid
        assert (directory / 'manifest.json').exists() and (directory / 'manifest.json.sig').exists()  # never removed
        assert (directory / 'images/backend.tar').exists() == (rid not in (ids[0], ids[4]))
        assert not os.access(directory / 'images', os.W_OK)
    index = json.loads((root / 'index.json').read_text())
    assert {e['release_id']: e['images_present'] for e in index['releases']}[ids[0]] is False and index['current'] == ids[3]
    unprotected = {'keep_last': 0, 'deployed_within_days': 0, 'keep_current_and_previous_known_good': False}
    assert ids[3] not in archive.prune(root, apply=True, now=now, policy=unprotected)['removed']  # current is never pruned


def test_a_deployment_is_recorded_only_for_a_sealed_release(tmp_path, built):
    with pytest.raises(release.Refused, match='sealed'):
        archive.record_deployment(built['root'], built['id'])


def test_cli_contracts(tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / 'scripts/release/archive.py'), 'prune', '--root', str(tmp_path)], capture_output=True, text=True)
    assert out.returncode == 0 and json.loads(out.stdout)['dry_run'] is True
    out = subprocess.run([sys.executable, str(ROOT / 'scripts/release/release.py'), 'load-check', '--release-dir', str(tmp_path / 'none')],
                         capture_output=True, text=True)
    assert out.returncode == 2


def test_stage_preflight_refuses_an_image_not_pinned_by_digest(tmp_path):
    """The released commit's lock must pin every release image (the gateway is not yet: back-filled on the CI host with
    `release.py pin-upstream`); the preflight fails on it instead of the build or the deploy."""
    unpinned = release.pinned_image_problems(ROOT)
    lock = json.loads((ROOT / 'versions.lock.json').read_text())['images']
    if not lock['gateway'].get('digest'):
        assert unpinned == ['gateway: not pinned by digest (unresolved_not_deployable)']
    report = release.stage_preflight('HEAD', tmp_path / 'a', runner=Runner(), sbom_maker=sbom_ok)
    assert report['ok'] is (not unpinned) and report['steps']['images_pinned'] == {'ok': not unpinned, 'detail': {'unpinned': unpinned}}
    repo = tmp_path / 'repo'
    (repo / 'deploy/release').mkdir(parents=True)
    (repo / 'deploy/release/images.v1.json').write_text((ROOT / 'deploy/release/images.v1.json').read_text())
    pinned = {**lock, 'gateway': {'reference': 'docker.io/library/caddy:2', 'digest': 'sha256:' + 'c' * 64, 'platform': 'linux/amd64', 'status': 'pinned'}}
    (repo / 'versions.lock.json').write_text(json.dumps({'images': pinned}))
    assert release.pinned_image_problems(repo) == []
    (repo / 'versions.lock.json').write_text(json.dumps({'images': {**pinned, 'node-host-base': {'reference': 'x', 'digest': ''}}}))
    assert release.pinned_image_problems(repo) == ['node-host-base: not pinned by digest (missing)']


def test_release_builds_only_on_linux_amd64_and_never_with_attestations(tmp_path, ci_result):
    """A development machine (arm64) builds images no stage host runs: refused before any build. A saved image of another
    platform, or with a buildx attestation manifest, is refused too; builds pass --platform linux/amd64 --provenance=false
    --sbom=false (NX-034 local dry run)."""
    arm = Docker(daemon='linux/aarch64')
    with pytest.raises(release.Refused, match='only on the CI host'):
        build(tmp_path, ci_result, arm, 201)
    assert not any(call[0] == 'build' for call in arm.calls)
    with pytest.raises(release.Refused, match='exactly one linux/amd64 image without attestations'):
        build(tmp_path, ci_result, Docker(attest=True), 202)
    with pytest.raises(release.Refused, match='exactly one linux/amd64 image'):
        build(tmp_path, ci_result, Docker(image_platform=('linux', 'arm64')), 203)
    docker = Docker()
    build(tmp_path, ci_result, docker, 204)
    for call in (c for c in docker.calls if c[0] == 'build'):
        assert call[1:6] == ['--pull=false', '--platform', 'linux/amd64', '--provenance=false', '--sbom=false']


def test_pin_upstream_backfills_an_upstream_digest_only_for_a_local_linux_amd64_image(tmp_path):
    # No release image is 'upstream' since the gateway is rebuilt (NX-036, test_gateway_image); a synthetic 'proxy' entry.
    repo = tmp_path / 'repo'
    (repo / 'deploy/release').mkdir(parents=True)
    config = json.loads((ROOT / 'deploy/release/images.v1.json').read_text())
    config['images']['proxy'] = {'kind': 'upstream', 'lock': 'proxy'}
    (repo / 'deploy/release/images.v1.json').write_text(json.dumps(config))
    original = json.loads((ROOT / 'versions.lock.json').read_text())
    original['images']['proxy'] = {'reference': 'docker.io/library/caddy:2.9', 'digest': 'sha256:' + 'd' * 64, 'platform': 'linux/amd64', 'status': 'pinned'}
    (repo / 'versions.lock.json').write_text(json.dumps(original, indent=2, ensure_ascii=False) + '\n')
    digest = 'sha256:' + 'c' * 64
    with pytest.raises(release.InputError, match='upstream image'):
        release.pin_upstream('gateway', 'docker.io/library/caddy:2.10', digest, root=repo, docker=Docker())
    with pytest.raises(release.InputError, match='digest'):
        release.pin_upstream('proxy', 'docker.io/library/caddy:2.10', 'latest', root=repo, docker=Docker())
    with pytest.raises(release.Refused, match='only on the CI host'):
        release.pin_upstream('proxy', 'docker.io/library/caddy:2.10', digest, root=repo, docker=Docker(daemon='linux/aarch64'))
    with pytest.raises(release.Refused, match='not present locally'):
        release.pin_upstream('proxy', 'mirror.invalid/caddy:2.10', digest, root=repo, docker=Docker())
    with pytest.raises(release.Refused, match='platform'):
        release.pin_upstream('proxy', 'docker.io/library/caddy:2.10', digest, root=repo, docker=Docker(image_platform=('linux', 'arm64')))
    assert json.loads((repo / 'versions.lock.json').read_text()) == original  # nothing written on refusal
    out = release.pin_upstream('proxy', 'docker.io/library/caddy:2.10', digest, root=repo, docker=Docker(),
                               now=datetime(2026, 10, 12, tzinfo=UTC))
    assert out == {'lock': 'proxy', 'reference': 'docker.io/library/caddy@' + digest, 'platform': 'linux/amd64'}
    written = json.loads((repo / 'versions.lock.json').read_text())
    assert written['images']['proxy'] == {'reference': 'docker.io/library/caddy:2.10', 'digest': digest, 'platform': 'linux/amd64',
                                            'status': 'pinned', 'pinned_at': '2026-10-12'}
    assert {k: v for k, v in written['images'].items() if k != 'proxy'} == {k: v for k, v in original['images'].items() if k != 'proxy'}
    assert release.pinned_image_problems(repo) == [] and release.pinned_reference(written, 'proxy') == 'docker.io/library/caddy@' + digest
