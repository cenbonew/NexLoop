"""ADR-045 v3 (dispatcher 2026-11-07, option A; L6 C1): the stage-only capability manifest deploy/stage/capabilities.stage.v1.json.
Only stage_secrets publish --stage-capabilities merges it, and only for a deployment whose private marker says environment stage
(refused before anything is compiled or sent otherwise; no environment variable is read); the result names the manifest and its
sha256; compile_catalog refuses any other file or place; the system catalog alone has no fault Action; check_publication forbids the
manifest, the Host key and the Action outside deploy/stage; a Run failed with synthetic_model_fault is a failure in the S6 attribution
(design §6 8a). No database."""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from nexloop_eios import catalog_publish as P
from nexloop_eios.evolution.attribution import _failed
from nexloop_eios.evolution_traces import sample_from_run

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'deploy/stage/capabilities.stage.v1.json'
ACTION = 'nexloop.synthetic_fault.set'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = load(ROOT / 'scripts/deploy/stage_secrets.py', 'stage_secrets_under_test')
CP = load(ROOT / 'scripts/check_publication.py', 'check_publication_under_test')


def compiled(**extra):
    return P.compile_catalog('tenant-synthetic', **S.CATALOG, **extra)


def names(catalog):
    return {a['definition']['stable_name'] for a in catalog['actions']}


def test_the_system_catalog_has_no_fault_action_and_the_stage_manifest_adds_exactly_it():
    plain, staged = compiled(), compiled(stage_capabilities_path=STAGE)
    assert ACTION not in names(plain) and 'stage_manifest' not in plain
    assert names(staged) - names(plain) == {ACTION}
    assert staged['stage_manifest'] == {'path': 'deploy/stage/capabilities.stage.v1.json', 'sha256': hashlib.sha256(STAGE.read_bytes()).hexdigest()}


def test_compile_refuses_a_stage_manifest_from_any_other_place_or_name(tmp_path):
    elsewhere = tmp_path / 'capabilities.stage.v1.json'
    shutil.copy(STAGE, elsewhere)
    renamed = ROOT / 'deploy/stage/capabilities.extra.json'
    for path in (elsewhere, ROOT / 'deploy/configuration/capabilities.v1.json', renamed):
        with pytest.raises(P.CatalogPublishRefused, match='only deploy/stage/capabilities.stage'):
            compiled(stage_capabilities_path=path)


def marker(tmp_path, environment, name='marker.json'):
    path = tmp_path / name
    path.write_text(json.dumps({'schema': 'nexloop-deployment-marker/1', 'environment': environment}))
    path.chmod(0o600)
    return path


def publish_args(tmp_path, **extra):
    base = dict(out=tmp_path, tenant='tenant-synthetic', configurator_dsn_file=None, bootstrap_dsn_file=None, signing_key_id=None, dry_run=True,
                emit_config=None, stage_capabilities=None, deployment_marker=None)
    return argparse.Namespace(**{**base, **extra})


@pytest.fixture
def nothing_sent(monkeypatch):
    calls = []
    real = P.compile_catalog
    monkeypatch.setattr(P, 'compile_catalog', lambda *a, **k: calls.append('compile') or real(*a, **k))
    monkeypatch.setattr(P, 'publish', lambda *a, **k: calls.append('publish') or pytest.fail('nothing may be published'))
    return calls


def test_publish_merges_the_stage_manifest_only_for_a_deployment_marked_stage(tmp_path, nothing_sent):
    result = S.publish_main(publish_args(tmp_path, stage_capabilities=STAGE, deployment_marker=marker(tmp_path, 'stage')))
    assert result['dry_run'] is True and result['stage_manifest']['path'] == 'deploy/stage/capabilities.stage.v1.json'
    assert nothing_sent == ['compile']
    assert 'stage_manifest' not in S.publish_main(publish_args(tmp_path))  # without the flag: as before


@pytest.mark.parametrize('case', ['production', 'no_marker', 'marker_without_manifest', 'unreadable', 'wrong_shape'])
def test_publish_refuses_the_stage_manifest_before_compiling_or_sending_anything(tmp_path, nothing_sent, case, monkeypatch):
    monkeypatch.setenv('NEXLOOP_ENVIRONMENT', 'stage')  # never read: the marker file alone decides
    args = {'production': lambda: dict(stage_capabilities=STAGE, deployment_marker=marker(tmp_path, 'production')),
            'no_marker': lambda: dict(stage_capabilities=STAGE),
            'marker_without_manifest': lambda: dict(deployment_marker=marker(tmp_path, 'stage')),
            'unreadable': lambda: dict(stage_capabilities=STAGE, deployment_marker=tmp_path / 'missing.json'),
            'wrong_shape': lambda: dict(stage_capabilities=STAGE, deployment_marker=tmp_path / 'shape.json')}[case]()
    if case == 'wrong_shape':
        (tmp_path / 'shape.json').write_text(json.dumps({'environment': 'stage'}))
        os.chmod(tmp_path / 'shape.json', 0o600)
    with pytest.raises(S.Refused):
        S.publish_main(publish_args(tmp_path, dry_run=False, configurator_dsn_file=tmp_path / 'dsn', signing_key_id='k', **args))
    assert nothing_sent == []


def test_the_command_line_refuses_with_a_nonzero_exit(tmp_path, capsys):
    code = S.main(['publish', '--out', str(tmp_path), '--tenant', 'tenant-synthetic', '--dry-run', '--stage-capabilities', str(STAGE),
                   '--deployment-marker', str(marker(tmp_path, 'production'))])
    assert code != 0 and 'not marked stage' in capsys.readouterr().out


def test_check_publication_keeps_the_stage_only_names_under_deploy_stage():
    for token in ('capabilities.stage.v1.json', '"synthetic_model_fault": true', ACTION):
        assert CP.violation('deploy/stage/agent-host.runtime-config.example.json', token.encode()) is None
        assert CP.violation('deploy/examples/production.example.json', token.encode()) == 'stage-only capability outside deploy/stage'
        assert CP.violation('docs/design/synthetic-model-fault.md', token.encode()) is None
    assert CP.violation('deploy/authorization/api-routes.v1.json', b'"capability": "synthetic_fault.set"') is None  # the global route entry


def test_a_run_failed_by_the_fault_is_a_failure_in_the_s6_attribution():
    sample = sample_from_run({'run_id': '00000000-0000-4000-8000-000000000001', 'kind': 'message', 'issued_at': '2026-11-07T00:00:00Z',
                              'consumer_id': 'c' * 64, 'persona': 'simulated_agent', 'reply_fallback': False,
                              'evidence': {'failure': {'failure_code': 'synthetic_model_fault'}, 'model': {'calls': 0, 'failed': 0, 'unknown': 0}}})
    assert sample['failures'] == [{'failure_code': 'synthetic_model_fault'}] and sample['model']['failed'] == 0
    assert _failed(sample) is True
    assert _failed({**sample, 'failures': []}) is False  # the failure record alone makes it one (no model.failed)


def test_the_stage_manifest_is_valid_and_names_only_stage_entries():
    value = json.loads(STAGE.read_text())
    assert value['schema_version'] == 'nexloop-stage-capabilities/1'
    assert [c['capability_name'] for c in value['capabilities']] == ['synthetic_fault.set']
    assert [a['stable_name'] for a in value['business_actions']] == [ACTION]
    assert 'synthetic_fault.set' not in json.dumps(json.loads((ROOT / 'deploy/configuration/capabilities.v1.json').read_text()))


def test_the_owner_role_gets_the_stage_actions_only_for_a_deployment_marked_stage(tmp_path):
    from nexloop_eios import workbench_roles as R
    roles = R.validate_roles(R.load(ROOT / 'deploy/authorization/workbench-roles.v1.json'))
    grant = 'eios:action:nexloop.synthetic_fault.set:1'
    assert grant not in roles['roles']['owner'] and R.with_stage_owner_actions(roles, None, None) is roles  # no flags: unchanged
    staged = R.with_stage_owner_actions(roles, STAGE, marker(tmp_path, 'stage'))
    assert set(staged['roles']['owner']) - set(roles['roles']['owner']) == {grant}
    assert all(grant not in actions for role, actions in staged['roles'].items() if role != 'owner')
    for args in ((STAGE, marker(tmp_path, 'production', 'p.json')), (STAGE, None), (None, marker(tmp_path, 'stage', 's.json'))):
        with pytest.raises(R.WorkbenchRolesRejected):
            R.with_stage_owner_actions(roles, *args)
    assert grant not in json.dumps(json.loads((ROOT / 'deploy/authorization/workbench-roles.v1.json').read_text()))
