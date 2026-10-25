"""NX-056 (L6 review 8): the approval policies publication command, without a database (the window, the tenant check and the apply
are replaced; tests/test_nx056_first_policies_pg.py applies through the real configurator). Output never carries the DSN."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'deploy/configuration/history/approval-policies.version1.json'  # version 1 (the current file is version 2)
spec = importlib.util.spec_from_file_location('approval_policies_apply', ROOT / 'scripts/deploy/approval_policies_apply.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def run(*args, **kw):
    out = io.StringIO()
    code = cli.main(list(args), out=out, **kw)
    return code, json.loads(out.getvalue())


@contextlib.contextmanager
def window(path, refused):
    yield 'synthetic-dsn-file'


def test_check_validates_without_connecting_and_names_the_files_sha256():
    code, out = run('--manifest', str(MANIFEST), '--check')
    assert code == 0 and out == {'valid': True, 'version': 1, 'expected_revision': 0, 'policies': 3,
                                 'manifest_sha256': hashlib.sha256(MANIFEST.read_bytes()).hexdigest()}


def test_the_current_file_is_version_2_over_version_1():
    """NX-058 slice D2: the deployed file adds the five owner-only member Actions (applied over version 1, deployment history)."""
    current = ROOT / 'deploy/configuration/approval-policies.v1.json'
    code, out = run('--manifest', str(current), '--check')
    assert code == 0 and (out['version'], out['expected_revision'], out['policies']) == (2, 1, 8)
    policies = {p['capability']: p for p in json.loads(current.read_text())['policies']}
    v1 = {p['capability']: p for p in json.loads(MANIFEST.read_text())['policies']}
    assert all(policies[k] == v for k, v in v1.items())  # version 1's policies unchanged
    owner = [p for k, p in policies.items() if k.startswith('workbench_member.')]
    assert len(owner) == 5 and all(p['condition'] is None and p['quorum'] == 1 and p['approver_roles'] == ['owner'] and p['solo_mode'] == 'not_applicable'
                                   and p['target_subject_field'] == 'target_principal' for p in owner)
    assert {k: p['summary_fields'] for k, p in policies.items() if k.startswith('workbench_member.') and p['summary_fields']} == {'workbench_member.demote_owner': ['new_role']}


def test_apply_needs_the_tenant_and_bootstrap_file_and_a_known_tenant(tmp_path):
    assert run('--manifest', str(MANIFEST))[0] == 2
    seen = []
    code, out = run('--manifest', str(MANIFEST), '--tenant', 'synthetic-tenant', '--bootstrap-dsn-file', 'f', window=window,
                    exists=lambda dsn, tenant: False, apply=lambda *a, **k: seen.append(1))
    assert code == 2 and out == {'applied': False, 'reason': 'tenant unknown'} and seen == []
    texts = []
    code, out = run('--manifest', str(MANIFEST), '--tenant', 'synthetic-tenant', '--bootstrap-dsn-file', 'f', window=window,
                    exists=lambda dsn, tenant: True, apply=lambda text, database_url_file: texts.append(text) or {'configured': True})
    assert code == 0 and out['applied'] is True and texts == [MANIFEST.read_text(encoding='utf-8')]  # the file's exact text
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({**json.loads(MANIFEST.read_text()), 'expected_revision': 3}))
    assert run('--manifest', str(bad), '--check')[0] == 2
    code, out = run('--manifest', str(MANIFEST), '--tenant', 'synthetic-tenant', '--bootstrap-dsn-file', 'f', window=window,
                    exists=lambda dsn, tenant: True, apply=lambda text, database_url_file: (_ for _ in ()).throw(RuntimeError('postgresql://secret')))
    assert code == 2 and out == {'applied': False, 'reason': 'RuntimeError'}  # never the DSN or the message
