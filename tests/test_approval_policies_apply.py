"""NX-056 (L6 review 8): the approval policies publication command, without a database (the window, the tenant check and the apply
are replaced; tests/test_nx056_first_policies_pg.py applies through the real configurator). Output never carries the DSN."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'deploy/configuration/approval-policies.v1.json'
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
