"""NX-032 slice 3: the deployment dry-run — the signed release verified on the deploy side (AT-055), Compose images rendered from
the manifest, DATA_HOST images from versions.lock.json, blind rollback refused (AT-057), host templates rendered (including the
Gateway Caddyfile), steps listed and nothing executed. Synthetic releases in temporary directories; no host is contacted."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'scripts/release'))
import deploy  # noqa: E402
import release  # noqa: E402
from test_release_local import DIGEST_API, DIGEST_HOST, inputs, keypair, make, manifest_file, oci_archive  # noqa: E402,F401

APP_IMAGES = {'BACKEND_IMAGE': 'nexloop-api', 'AGENT_HOST_IMAGE': 'nexloop-host', 'GATEWAY_IMAGE': 'nexloop-gateway'}
DIGEST_GATEWAY = 'sha256:' + 'd' * 64


@pytest.fixture
def stage(tmp_path):
    path = tmp_path / 'stage.env'
    path.write_text((ROOT / 'deploy/stage/stage.env.example').read_text())
    path.chmod(0o600)
    return path


@pytest.fixture
def signed(inputs):
    # The synthetic release carries all three images the app host's Compose file requires.
    images = json.loads(inputs['images'].read_text())
    images['nexloop-gateway'] = {'archive': str(oci_archive(inputs['tmp'] / 'gateway.tar', DIGEST_GATEWAY)), 'digest': DIGEST_GATEWAY}
    inputs['images'].write_text(json.dumps(images))
    make(inputs)
    rel = inputs['tmp'] / 'release'
    key, allowed = keypair(inputs['tmp'], 'release-key')
    release.sign(rel, key)
    return {**inputs, 'release': rel, 'allowed': allowed}


def run(signed, stage, out, **kw):
    args = dict(host_role='app', stage_env=stage, out=signed['tmp'] / out, release_dir=signed['release'], allowed_signers=signed['allowed'],
                trusted_sha=signed['sha'], image_vars=APP_IMAGES)
    return deploy.dry_run(**{**args, **kw})


def test_app_dry_run_verifies_the_release_renders_the_host_and_executes_nothing(signed, stage):
    out = run(signed, stage, 'plan')
    assert out['ready'] is True and out['executed'] is False, out
    assert out['checks'] == {'release_verified': True, 'image_variables_mapped': True, 'compose_image_variables_complete': True, 'compose_images_from_manifest': True}
    assert out['details']['release']['commit'] == signed['sha']
    plan = signed['tmp'] / 'plan'
    assert sorted(p.name for p in plan.iterdir()) == ['Caddyfile', 'app-host.nft', 'compose.images.env']
    assert (plan / 'compose.images.env').read_text() == (f'AGENT_HOST_IMAGE=nexloop/nexloop-host@{DIGEST_HOST}\nBACKEND_IMAGE=nexloop/nexloop-api@{DIGEST_API}\n'
                                                         f'GATEWAY_IMAGE=nexloop/nexloop-gateway@{DIGEST_GATEWAY}\n')
    caddy = (plan / 'Caddyfile').read_text()
    assert 'https://app.stage.invalid:8443' in caddy and 'reverse_proxy https://127.0.0.1:8000' in caddy and 'respond "Not found" 404' in caddy
    assert '__' not in caddy and 'tls1.3' in caddy
    assert [s['step'] for s in out['steps']] == ['inventory_before', 'load_images', 'install_configuration', 'firewall', 'backup_gate',
                                                 'stop_old_agent_host', 'start_app', 'inventory_after_and_gate', 'promote']
    assert all('$SSH_ALIAS' in s['command'] or 'docker' in s['command'] or 'inventory' in s['command'] or 'backup' in s['command']
               or s['command'].startswith('deploy.py ') for s in out['steps'])  # promote: local release root only (NX-034 part 2)


def test_app_dry_run_refuses_an_untrusted_commit_a_foreign_digest_or_a_tampered_archive(signed, stage, tmp_path):
    other = run(signed, stage, 'p1', trusted_sha='1' * 40)
    assert other['ready'] is False and other['details']['release_refused'] == ['deploy_sha_differs_from_tested_commit']
    compose_env = tmp_path / 'compose.app.env'
    compose_env.write_text(f'BACKEND_IMAGE=mirror.invalid/nexloop/nexloop-api@{DIGEST_API}\nAGENT_HOST_IMAGE=nexloop/nexloop-host@sha256:{"c" * 64}\n')
    foreign = run(signed, stage, 'p2', compose_env=compose_env)
    assert foreign['ready'] is False and foreign['details']['compose_image_mismatch'] == ['AGENT_HOST_IMAGE']  # a mirror prefix alone is fine
    unmapped = run(signed, stage, 'p3', image_vars={'BACKEND_IMAGE': 'nexloop-api'})
    assert unmapped['ready'] is False and any(r.startswith('image_not_deployed_from_manifest') for r in unmapped['details']['release_refused'])
    archive = next((signed['release'] / 'images').iterdir())
    archive.write_bytes(archive.read_bytes() + b'x')
    tampered = run(signed, stage, 'p4')
    assert tampered['ready'] is False and any(r.startswith('archive_changed') for r in tampered['details']['release_refused'])


def test_app_dry_run_refuses_a_blind_rollback(signed, stage, tmp_path):
    target_head = json.loads((signed['release'] / 'manifest.json').read_text())['catalog']['head']
    ahead = manifest_file(tmp_path, 'current.json', '9999', 1, 'r-current')
    out = run(signed, stage, 'rb1', current_manifest=ahead)
    assert out['ready'] is False and out['checks']['rollback_allowed'] is False
    assert out['details']['rollback_refused']['hints'], out['details']['rollback_refused']
    same = manifest_file(tmp_path, 'same.json', target_head, 1, 'r-same')
    assert run(signed, stage, 'rb2', current_manifest=same)['checks']['rollback_allowed'] is True


def test_data_dry_run_uses_the_locked_images_and_renders_the_data_host(stage, tmp_path):
    out = deploy.dry_run(host_role='data', stage_env=stage, out=tmp_path / 'data')
    assert out['ready'] is True and sorted(out['rendered']) == ['compose.images.env', 'data-host.nft', 'pg_hba.conf', 'postgresql.conf', 'valkey.conf']
    lock = json.loads((ROOT / 'versions.lock.json').read_text())['images']
    env = (tmp_path / 'data' / 'compose.images.env').read_text()
    assert f"@{lock['postgres']['digest']}" in env and f"@{lock['valkey']['digest']}" in env
    wrong = tmp_path / 'compose.data.env'
    wrong.write_text('POSTGRES_IMAGE=docker.io/library/postgres@sha256:' + '0' * 64 + '\n')
    refused = deploy.dry_run(host_role='data', stage_env=stage, out=tmp_path / 'data2', compose_env=wrong)
    assert refused['ready'] is False and refused['details']['compose_image_mismatch'] == ['POSTGRES_IMAGE']


def test_cli_exit_codes_and_an_existing_output_is_refused(signed, stage, tmp_path):
    script = ROOT / 'scripts/deploy/deploy.py'
    base = ['dry-run', '--host-role', 'app', '--stage-env', str(stage), '--release-dir', str(signed['release']), '--allowed-signers', str(signed['allowed']),
            '--trusted-sha', signed['sha'], '--image-var', 'BACKEND_IMAGE=nexloop-api', '--image-var', 'AGENT_HOST_IMAGE=nexloop-host', '--image-var', 'GATEWAY_IMAGE=nexloop-gateway']
    ok = subprocess.run([sys.executable, str(script), *base, '--out', str(tmp_path / 'c1')], capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)['ready'] is True
    again = subprocess.run([sys.executable, str(script), *base, '--out', str(tmp_path / 'c1')], capture_output=True, text=True)
    assert again.returncode == 2 and json.loads(again.stdout)['detail'] == 'output directory exists'
    bad = subprocess.run([sys.executable, str(script), *base[:-2], '--trusted-sha', '1' * 40, '--out', str(tmp_path / 'c2')], capture_output=True, text=True)
    assert bad.returncode == 1


@pytest.mark.parametrize('omitted', ['BACKEND_IMAGE', 'AGENT_HOST_IMAGE', 'GATEWAY_IMAGE'])
def test_an_explicit_mapping_that_leaves_out_a_required_image_is_refused(signed, stage, omitted):
    """Every image variable the app host's Compose file requires is rendered; leaving one out of --image-var is refused here,
    not at `docker compose up` (found by the NX-034 local dry run)."""
    assert deploy.required_image_variables('app') == ['AGENT_HOST_IMAGE', 'BACKEND_IMAGE', 'GATEWAY_IMAGE']
    out = run(signed, stage, 'omit-' + omitted, image_vars={k: v for k, v in APP_IMAGES.items() if k != omitted})
    assert out['ready'] is False and out['checks']['compose_image_variables_complete'] is False
    assert out['details']['missing_image_variables'] == [omitted]
    assert out['checks']['image_variables_mapped'] is True  # every name given exists in the manifest; the gap is the omission
    assert omitted + '=' not in (signed['tmp'] / ('omit-' + omitted) / 'compose.images.env').read_text()


def test_the_data_host_renders_every_image_its_compose_file_requires(stage, tmp_path):
    assert deploy.required_image_variables('data') == sorted(deploy.DATA_IMAGES)
    out = deploy.dry_run(host_role='data', stage_env=stage, out=tmp_path / 'data')
    assert out['checks']['compose_image_variables_complete'] is True and out['details']['missing_image_variables'] == []
