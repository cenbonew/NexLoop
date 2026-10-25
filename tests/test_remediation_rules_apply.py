"""NX-057 G1: the remediation rules publication command, without a database (the window and the publication are replaced;
tests/test_remediation_rule_publications_pg.py publishes through the real configurator). Output never carries the DSN."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'deploy/configuration/remediation-rules.v1.json'
spec = importlib.util.spec_from_file_location('remediation_rules_apply', ROOT / 'scripts/deploy/remediation_rules_apply.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def run(*args, **kw):
    out = io.StringIO()
    code = cli.main(list(args), out=out, **kw)
    return code, json.loads(out.getvalue())


@contextlib.contextmanager
def window(path, refused):
    yield 'synthetic-dsn-file'


APPLY = ('--manifest', str(MANIFEST), '--tenant', 'synthetic-tenant', '--bootstrap-dsn-file', 'f')


def test_check_validates_without_connecting_and_names_the_files_sha256():
    code, out = run('--manifest', str(MANIFEST), '--check')
    assert code == 0 and out == {'valid': True, 'manifest_version': 1, 'rules': 1, 'manifest_sha256': hashlib.sha256(MANIFEST.read_bytes()).hexdigest()}


def test_apply_needs_the_tenant_the_bootstrap_file_and_the_expected_revision(tmp_path):
    def nothing(*a, **k):
        raise AssertionError('no window, no publication')
    for args in (('--manifest', str(MANIFEST)), APPLY, (*APPLY, '--expected-revision', '-1'),
                 ('--manifest', str(MANIFEST), '--tenant', 'synthetic-tenant', '--expected-revision', '0')):
        code, out = run(*args, window=nothing, publish=nothing)
        assert code == 2 and out['applied'] is False, args
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({**json.loads(MANIFEST.read_text()), 'expected_revision': 0}))   # not a field of the rules manifest
    assert run('--manifest', str(bad), '--check')[0] == 2


def test_apply_publishes_the_files_exact_text_with_the_revision_and_reports_without_the_dsn():
    calls = []
    def publish(text, tenant, *, world, expected_revision, database_url_file):
        calls.append((text, tenant, world, expected_revision, database_url_file))
        return {'configured': True, 'manifest_version': 1, 'replay': False, 'expected_revision': 0}
    code, out = run(*APPLY, '--expected-revision', '0', window=window, publish=publish)
    assert code == 0 and out['applied'] is True and out['result']['replay'] is False
    assert out['expected_revision_passed'] == 0 and out['expected_revision_recorded'] == 0 and 'expected_revision' not in out
    # A replay passed another value: both are shown, named apart (the database keeps the first publication's).
    code, out = run(*APPLY, '--expected-revision', '1', window=window, publish=lambda *a, **k: {'configured': True, 'replay': True, 'expected_revision': 0})
    assert code == 0 and (out['expected_revision_passed'], out['expected_revision_recorded']) == (1, 0)
    assert calls == [(MANIFEST.read_text(encoding='utf-8'), 'synthetic-tenant', 'real', 0, 'synthetic-dsn-file')]
    def leaks(*a, **k):
        raise RuntimeError('postgresql://secret')
    code, out = run(*APPLY, '--expected-revision', '0', window=window, publish=leaks)
    assert code == 2 and out == {'applied': False, 'reason': 'RuntimeError'}       # never the DSN or the message
    class SerializationFailure(Exception):          # SQL's own refusal text, by SQLSTATE only (what psycopg's errors carry)
        sqlstate, diag = '40001', SimpleNamespace(message_primary='remediation rules: expected revision 0, current 1')
    def refused(*a, **k):
        raise SerializationFailure('postgresql://secret')
    code, out = run(*APPLY, '--expected-revision', '0', window=window, publish=refused)
    assert code == 2 and out == {'applied': False, 'reason': 'SerializationFailure: remediation rules: expected revision 0, current 1'}
