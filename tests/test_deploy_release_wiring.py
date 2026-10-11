"""NX-034 part 2 on the deploy side (deploy.py with release.py and archive.py): load-check after docker load (the image must
resolve by its manifest digest), promote (previous current → known_good, the new release → current, each link switched
atomically, put back on a failure), rollback-check against known_good and the current schema, and retention never pruning the
current or the known_good release. Synthetic sealed releases in temporary release roots, the stand-in docker of
test_release_build_archive; no host is contacted and no real image is loaded."""
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'scripts/release'))
import archive  # noqa: E402
import deploy  # noqa: E402
import release  # noqa: E402
from test_deploy_dryrun import stage  # noqa: E402,F401
from test_release_build_archive import Docker, preparer  # noqa: E402
from test_release_local import head_sha, keypair, passed_preflight  # noqa: E402

BASE = datetime(2026, 10, 11, tzinfo=UTC)


@pytest.fixture
def root(tmp_path, request):
    ci = tmp_path / 'result.json'
    ci.write_text(json.dumps({'sha': head_sha(), 'status': 'passed', 'policy_version': 1, 'deadline_gate': 'passed', 'counts': {'passed': 1}}))
    key, allowed = keypair(tmp_path, 'release-key')
    release_root = tmp_path / 'release-root'
    made = []
    # A distinct sequence range per test: the build staging directory below the checkout is shared by parallel workers.
    import zlib
    offset = 1000 + zlib.crc32(request.node.nodeid.encode()) % 90000 * 10

    def make(sequence):
        """A sealed release built at BASE + sequence hours (as build, preflight, sign and seal leave it)."""
        out = release.build(head_sha(), ci, release_root, sequence=offset + sequence, root=ROOT, docker=Docker(), preparer=preparer,
                            now=BASE + timedelta(hours=sequence), checkout_check=lambda r, s: None, only=['backend'])
        directory = Path(out['release_dir'])
        release.attach_preflight(directory, passed_preflight(head_sha()))
        release.sign(directory, key)
        archive.seal(release_root, out['release_id'], allowed)
        made.append(out['release_id'])
        return out['release_id']
    yield {'root': release_root, 'allowed': allowed, 'make': make}
    for rid in made:  # sealed directories are read-only
        for path in [release_root / 'releases' / rid, *(release_root / 'releases' / rid).rglob('*')]:
            if path.is_dir():
                path.chmod(0o755)


def links(root):
    return archive.current(root), archive.known_good(root)


def test_dry_run_load_check_refuses_an_image_that_does_not_resolve_to_its_manifest_digest(root, stage, tmp_path):
    rid = root['make'](11)
    directory = root['root'] / 'releases' / rid
    manifest = json.loads((directory / 'manifest.json').read_text())
    reference = manifest['images'][0]['reference']
    args = dict(host_role='app', stage_env=stage, release_dir=directory, allowed_signers=root['allowed'], trusted_sha=head_sha(),
                image_vars={'BACKEND_IMAGE': 'backend'}, load_check=True)
    ok = deploy.dry_run(out=tmp_path / 'plan-ok', docker=Docker(), **args)
    assert ok['checks']['images_loaded_by_digest'] is True and ok['details']['loaded'] == {'backend': reference}
    wrong = deploy.dry_run(out=tmp_path / 'plan-wrong', docker=Docker(inspect={reference: 'sha256:' + 'e' * 64}), **args)
    assert wrong['ready'] is False and wrong['checks']['images_loaded_by_digest'] is False and wrong['details']['load_refused'] == ['loaded_digest_differs:backend']
    plain = deploy.dry_run(out=tmp_path / 'plan-plain', docker=Docker(), **{**args, 'load_check': False})
    assert 'images_loaded_by_digest' not in plain['checks']  # the plain dry run never calls docker
    step = next(s for s in plain['steps'] if s['step'] == 'load_images')
    assert step['command'].startswith('deploy.py load-check --release-dir')


def test_promote_switches_current_and_known_good_atomically_and_puts_them_back_on_failure(root):
    first, second, third = root['make'](1), root['make'](2), root['make'](3)
    assert deploy.promote(root['root'], first, root['allowed'])['known_good'] is None
    assert links(root['root']) == (first, None)
    out = deploy.promote(root['root'], second, root['allowed'])
    assert out['current'] == second and out['known_good'] == first and links(root['root']) == (second, first)
    assert os.readlink(root['root'] / 'current') == f'releases/{second}'  # relative, inside the release root
    for step in ('known_good', 'current'):
        with pytest.raises(OSError):
            archive.promote(root['root'], third, _fail_after=step)
        assert links(root['root']) == (second, first), step  # nothing half-done
        assert not list(root['root'].glob('.*.tmp-*'))  # no temporary link left
    assert [d['release_id'] for d in json.loads((root['root'] / 'deployments.json').read_text())] == [first, second]
    with pytest.raises(release.InputError, match='already current'):
        deploy.promote(root['root'], second, root['allowed'])
    with pytest.raises(release.Refused, match='not_sealed'):
        (root['root'] / 'releases' / third).chmod(0o755)  # unsealed
        deploy.promote(root['root'], third, root['allowed'])
    (root['root'] / 'releases' / third).chmod(0o555)


def test_rollback_only_to_known_good_or_older_still_present_and_on_the_current_schema(root):
    old, good, now_, newer = root['make'](1), root['make'](2), root['make'](3), root['make'](4)
    with pytest.raises(release.Refused, match='no_current_or_known_good'):
        deploy.rollback_check(root['root'], good)
    for rid in (old, good, now_):
        deploy.promote(root['root'], rid, root['allowed'])
    assert links(root['root']) == (now_, good)
    assert deploy.rollback_check(root['root'], good)['rollback_allowed'] is True
    assert deploy.rollback_check(root['root'], old)['target_release'] == old  # older than known_good, same schema here
    for target, reason in ((newer, 'target_newer_than_known_good'), (now_, 'target_newer_than_known_good'), ('20261011-000000000000-9999', 'target_unknown_or_not_sealed')):
        with pytest.raises(release.Refused) as refused:
            deploy.rollback_check(root['root'], target)
        assert reason in json.loads(str(refused.value))['reasons'] and 'fix forward' in str(refused.value)
    # The schema rule of release.rollback_check still applies (a database ahead of what this checkout knows is refused).
    with pytest.raises(release.Refused, match='migrations_between_unknown|schema_not_backward_compatible'):
        deploy.rollback_check(root['root'], good, database_head='9999')
    # A target whose image archives were pruned is refused: a rebuilt image would be another release.
    images = root['root'] / 'releases' / old / 'images'
    images.chmod(0o755)
    for path in images.glob('*.tar'):
        path.unlink()
    images.chmod(0o555)
    with pytest.raises(release.Refused, match='target_images_pruned'):
        deploy.rollback_check(root['root'], old)


def test_retention_never_prunes_the_current_or_the_known_good_release(root):
    ids = [root['make'](n) for n in range(1, 4)]
    deploy.promote(root['root'], ids[0], root['allowed'])
    deploy.promote(root['root'], ids[1], root['allowed'])
    assert links(root['root']) == (ids[1], ids[0])
    unprotected = {'keep_last': 0, 'deployed_within_days': 0, 'keep_current_and_previous_known_good': False}
    plan = archive.retention_plan(root['root'], policy=unprotected)
    assert plan['keep'] == {ids[0]: ['known_good_link'], ids[1]: ['current_link']} and plan['remove_images'] == [ids[2]]
    out = archive.prune(root['root'], apply=True, policy=unprotected)
    assert out['removed'] == [ids[2]]
    for rid in ids[:2]:
        assert list((root['root'] / 'releases' / rid / 'images').glob('*.tar'))


def test_cli_contracts(root, tmp_path):
    import subprocess
    rid = root['make'](1)
    script = str(ROOT / 'scripts/deploy/deploy.py')
    promoted = subprocess.run([sys.executable, script, 'promote', '--release-root', str(root['root']), '--release-id', rid, '--allowed-signers', str(root['allowed'])],
                              capture_output=True, text=True)
    assert promoted.returncode == 0 and json.loads(promoted.stdout)['current'] == rid
    refused = subprocess.run([sys.executable, script, 'rollback-check', '--release-root', str(root['root']), '--target', rid], capture_output=True, text=True)
    assert refused.returncode == 1 and json.loads(refused.stdout)['ok'] is False
    bad = subprocess.run([sys.executable, script, 'promote', '--release-root', str(root['root']), '--release-id', '../x', '--allowed-signers', str(root['allowed'])],
                         capture_output=True, text=True)
    assert bad.returncode == 2
