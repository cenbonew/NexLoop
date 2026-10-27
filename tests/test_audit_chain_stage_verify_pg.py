"""NX-059 stage acceptance entry (scripts/ops/audit_chain_stage_verify.py, docs/release/run-nx059-stage-verify.md) on PostgreSQL. The
DATA_HOST step is replaced by running the very scripts the container would run, here, with the container paths mapped onto a temporary
tree (release tree = this checkout, bootstrap DSN = the admin DSN): the key goes only over stdin, the export comes back as a tar, the
offline verification and the agreement checks run as on the management machine. A tampered row, a wrong key and a release tree that
differs are each reported; the key is never printed; the configuration is strict. Synthetic tenants and audit rows."""
import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest

from support.tenants import TENANT_A
from test_audit_hash_chain_pg import as_tenant, chained, db  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ops'))
import audit_chain_stage_verify as S  # noqa: E402


def configuration(tmp_path, **change):
    out = tmp_path / 'exports'
    out.mkdir(mode=0o700, exist_ok=True)
    config = {'schema': S.SCHEMA, 'data_host': 'data-host', 'run_dir': '/srv/run', 'nexloop_root': '/srv/nexloop',
              'backend_image': 'registry.invalid/backend@sha256:' + '0' * 64, 'tenant_id': TENANT_A, 'out_dir': str(out), **change}
    path = tmp_path / 'verify.json'
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def local_host(chained, tmp_path, monkeypatch):
    """Runs the container's python -c script here: /opt/release -> this checkout, /run/bootstrap-dsn -> the admin DSN, /tmp and
    /export -> private temporary directories (as the container's tmpfs)."""
    dsn = tmp_path / 'bootstrap-dsn'
    dsn.write_text(chained['admin'].info.dsn)
    dsn.chmod(0o600)
    calls = []

    def remote(config, command, *, stdin=b'', dry):
        assert not dry
        words = shlex.split(command)
        assert words[:2] == ['docker', 'run'] and '--network' in words and words[words.index('--user') + 1] == '999:999'
        assert stdin == b'' or b'audit_chain.py' in words[words.index('-c') + 1].encode()  # only the verify step gets the key
        script, args = words[words.index('-c') + 1], words[words.index('-c') + 2:]
        box = tmp_path / f'container-{len(calls)}'
        for name in ('tmp', 'export'):
            (box / name).mkdir(parents=True, mode=0o700)
        for container, here in (('/opt/release/', str(ROOT) + '/'), ('/run/bootstrap-dsn', str(dsn)), ("'/tmp/", f"'{box}/tmp/"),
                                ("'/export/", f"'{box}/export/")):
            script = script.replace(container, here)
        calls.append({'stdin': bool(stdin), 'args': args})
        done = subprocess.run([sys.executable, '-c', script, *args], input=stdin, capture_output=True,
                              env={'PYTHONPATH': str(ROOT / 'packages/eios-core/src'), 'PATH': '/usr/bin:/bin'})
        assert not list((box / 'tmp').iterdir())  # the key file is gone when the step ends
        return done.returncode, done.stdout, done.stderr
    monkeypatch.setattr(S, 'remote', remote)
    monkeypatch.setattr(S.sys, 'stdin', None)
    return calls


def run(monkeypatch, key, config, capsys):
    class Pipe:
        def isatty(self):
            return False

        def read(self, n):
            return key
    monkeypatch.setattr(S.sys, 'stdin', Pipe())
    code = S.main(['--config', str(config)])
    out = capsys.readouterr()
    return code, [json.loads(line) for line in out.out.splitlines()], out


def key_text(chained):
    return next(iter(chained['keys'].values())).hex() + '\n'


def test_a_clean_chain_verifies_on_the_host_and_offline(chained, local_host, tmp_path, monkeypatch, capsys):
    code, steps, out = run(monkeypatch, key_text(chained), configuration(tmp_path), capsys)
    assert code == 0, steps
    by = {s['step']: s for s in steps}
    assert by['preflight']['ok'] and by['verify']['valid'] and by['offline']['valid'] and by['summary']['valid']
    assert (by['verify']['entries'], by['verify']['seals'], by['export']['entries'], by['offline']['seals']) == (5, 1, 5, 1)
    assert by['summary']['agreement_problems'] == [] and pathlib.Path(by['export']['directory'], 'manifest.json').is_file()
    assert [c['stdin'] for c in local_host] == [False, True, False]
    assert by['preflight']['files'] == sorted([S.TOOL, S.MODULE, S.PRIVATE])
    assert key_text(chained).strip() not in out.out + out.err


def test_a_tampered_row_fails_both_verifications(chained, local_host, tmp_path, monkeypatch, capsys):
    as_tenant(chained['admin'], TENANT_A, "update runtime.audit_events set actor_id='someone-else' where event_id=%s", (chained['events'][0],))
    code, steps, _ = run(monkeypatch, key_text(chained), configuration(tmp_path), capsys)
    by = {s['step']: s for s in steps}
    assert code == 1 and not by['summary']['valid']
    assert 'record_altered' in {p['code'] for p in by['verify']['problems']}
    assert by['offline']['valid'] is False or by['offline']['problems']  # the export carries the changed row's text
    assert 'record_altered' in {p['code'] for p in by['offline']['problems']}


def test_another_key_fails_on_the_seal(chained, local_host, tmp_path, monkeypatch, capsys):
    code, steps, _ = run(monkeypatch, 'ab' * 32 + '\n', configuration(tmp_path), capsys)
    by = {s['step']: s for s in steps}
    assert code == 1 and {p['code'] for p in by['verify']['problems']} == {'seal_key_unknown'}
    assert {p['code'] for p in by['offline']['problems']} == {'seal_key_unknown'}


def test_a_release_tree_that_differs_stops_before_any_key_is_sent(chained, local_host, tmp_path, monkeypatch, capsys):
    real = S.remote

    def differing(config, command, *, stdin=b'', dry):
        code, out, err = real(config, command, stdin=stdin, dry=dry)
        return (code, out.replace(b'scripts/ops/audit_chain.py', b'scripts/ops/other.py'), err) if 'hashlib' in command else (code, out, err)
    monkeypatch.setattr(S, 'remote', differing)
    code, steps, _ = run(monkeypatch, key_text(chained), configuration(tmp_path), capsys)
    assert code == 2 and steps == [{'step': 'preflight', 'ok': False, 'code': 'release_tree_differs'}]
    assert [c['stdin'] for c in local_host] == [False]


@pytest.mark.parametrize('change', [{'backend_image': 'registry.invalid/backend:latest'}, {'extra': 1}, {'schema': 'other'}, {'tenant_id': ''}])
def test_the_configuration_is_strict(tmp_path, change, capsys):
    assert S.main(['--config', str(configuration(tmp_path, **change)), '--dry-run']) == 2


@pytest.mark.parametrize('key', ['', 'not-hex\n', 'ab' * 31 + '\n', ('ab' * 32 + '\n') * 9])
def test_the_key_on_stdin_is_checked(tmp_path, monkeypatch, key, capsys):
    code, steps, _ = run(monkeypatch, key, configuration(tmp_path), capsys)
    assert code == 2 and steps == []


def test_the_dry_run_prints_the_plan_without_a_key(tmp_path, capsys):
    assert S.main(['--config', str(configuration(tmp_path)), '--dry-run']) == 0
    plan = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    commands = [p['command'] for p in plan if p.get('host') == 'DATA_HOST']
    assert len(commands) == 3 and all(c.startswith('docker run -i --rm --network none --read-only --user 999:999') for c in commands)
    assert all('/srv/run/boot.dsn:/run/bootstrap-dsn:ro' in c and '@sha256:' in c for c in commands)
    assert 'verify' in commands[1] and '--key-file' in commands[1] and 'export' in commands[2] and '/export' in commands[2]


def test_an_export_behind_the_verified_head_is_a_disagreement(chained, local_host, tmp_path, monkeypatch, capsys):
    real = S.remote

    def ahead(config, command, *, stdin=b'', dry):
        code, out, err = real(config, command, stdin=stdin, dry=dry)
        if stdin:  # the verify step: as if the database had a longer, more sealed chain than the export shows
            result = json.loads(out)
            result.update(head_seq=result['head_seq'] + 1, seals=result['seals'] + 1)
            out = (json.dumps(result) + '\n').encode()
        return code, out, err
    monkeypatch.setattr(S, 'remote', ahead)
    code, steps, _ = run(monkeypatch, key_text(chained), configuration(tmp_path), capsys)
    summary = steps[-1]
    assert code == 1 and summary['step'] == 'summary' and not summary['valid']
    assert summary['agreement_problems'] == ['export_behind_verified_head', 'export_has_fewer_seals']


@pytest.fixture
def local_only():
    path = ROOT / 'local-only' / 'test-audit-export'
    path.mkdir(parents=True, mode=0o700)
    yield path
    path.rmdir()
    if not any((ROOT / 'local-only').iterdir()):
        (ROOT / 'local-only').rmdir()


def test_the_export_directory_is_private(tmp_path, local_only, capsys, request):
    """I1 (L6): the export holds audit row texts - only local-only/ of the checkout or a private directory outside it."""
    tracked = ROOT / 'docs' / 'test-audit-export'  # private mode, but a work tree path git could track
    tracked.mkdir(mode=0o700)
    request.addfinalizer(tracked.rmdir)
    inside = tmp_path / 'link-into-docs'
    inside.symlink_to(tracked)
    loose = tmp_path / 'loose'
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    for refused in (str(tracked), str(inside), str(ROOT), str(loose), str(tmp_path / 'missing')):
        assert S.main(['--config', str(configuration(tmp_path, out_dir=refused)), '--dry-run']) == 2, refused
        assert 'out_dir' in capsys.readouterr().err
    assert S.main(['--config', str(configuration(tmp_path, out_dir=str(local_only))), '--dry-run']) == 0
    assert S.main(['--config', str(configuration(tmp_path)), '--dry-run']) == 0  # a private directory outside the checkout
    assert S.load(configuration(tmp_path, out_dir=str(local_only)))['out_dir'] == os.path.realpath(local_only)
