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
    """NX-058 slices D2/D3/E: the owner-only member Actions (versions 2-4); NX-056 budget approval: goals.budget.loosen (version 5, applied over
    versions 1-4, deployment history)."""
    current = ROOT / 'deploy/configuration/approval-policies.v1.json'
    code, out = run('--manifest', str(current), '--check')
    assert code == 0 and (out['version'], out['expected_revision'], out['policies']) == (5, 4, 12)
    policies = {p['capability']: p for p in json.loads(current.read_text())['policies']}
    v1 = {p['capability']: p for p in json.loads(MANIFEST.read_text())['policies']}
    assert all(policies[k] == v for k, v in v1.items())  # version 1's policies unchanged
    owner = [p for k, p in policies.items() if k.startswith('workbench_member.')]
    assert len(owner) == 8 and all(p['condition'] is None and p['quorum'] == 1 and p['approver_roles'] == ['owner'] and p['solo_mode'] == 'not_applicable'
                                   and p.get('target_subject_field') == ('target_principal' if p['capability'] != 'workbench_member.invite_owner' else None) for p in owner)
    v2 = {p['capability']: p for p in json.loads((ROOT / 'deploy/configuration/history/approval-policies.version2.json').read_text())['policies']}
    assert len(v2) == 8 and all(policies[k] == v for k, v in v2.items())  # version 2's policies unchanged
    assert {k: p['summary_fields'] for k, p in policies.items() if k.startswith('workbench_member.') and p['summary_fields']} == {'workbench_member.demote_owner': ['new_role']}
    v4 = {p['capability']: p for p in json.loads((ROOT / 'deploy/configuration/history/approval-policies.version4.json').read_text())['policies']}
    assert len(v4) == 11 and all(policies[k] == v for k, v in v4.items())  # version 4's policies unchanged
    budget = policies['goals.budget.loosen']
    assert (budget['condition'], budget['quorum'], budget['approver_roles'], budget['solo_mode'], budget['expires_after_seconds'], budget.get('target_subject_field')) == (
        None, 1, ['owner'], 'allowed', 86400, None)  # Q2: every budget kind; Q5: single-person mode allowed; Q6: quorum 1
    assert budget['summary_fields'] == ['budget_kind', 'unit', 'from_limit_amount', 'limit_amount', 'from_period_start', 'from_period_end', 'period_start', 'period_end']


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
