"""scripts/deploy/file_backup_maint.py (APP_HOST, repo2 of the file backup): forget per tag with --prune and --retry-lock, then check, a
deep check on Sundays; --dry-run for the installer; repo1 (anything but S3) refused; nothing configured is never a silent pass.
A stand-in restic records its arguments and environment key names; synthetic values only."""
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import file_backup_maint as M  # noqa: E402

PASSWORD = 'synthetic-repo2-password-0123456789'
SECRET = 'synthetic/secret+key'
GROUPS = [{'host': 'nexloop', 'tags': ['files_artifact'], 'keep': [{'id': 'a'}], 'remove': [{'id': 'b'}, {'id': 'c'}]},
          {'host': 'nexloop', 'tags': ['files_runtime'], 'keep': [{'id': 'd'}], 'remove': None}]


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


SNAPSHOTS = [{'id': 'a' * 64, 'short_id': 'aaaaaaaa', 'time': '2026-10-26T04:07:10.123456789+08:00', 'tags': ['files_artifact']}]


def restic(tmp_path, codes=None, snapshots=None):
    """A stand-in: appends {args, environment keys} to calls.jsonl; exit codes per subcommand from codes; forget prints groups,
    snapshots prints the given list."""
    log, state = tmp_path / 'calls.jsonl', tmp_path / 'codes.json'
    state.write_text(json.dumps(codes or {}))
    (tmp_path / 'snapshots.json').write_text(json.dumps(SNAPSHOTS if snapshots is None else snapshots))
    script = tmp_path / 'restic'
    script.write_text(f'#!{sys.executable}\nimport json,os,sys\na=sys.argv[1:]\n'
                      f'open({str(log)!r},"a").write(json.dumps({{"args":a,"env":sorted(os.environ)}})+"\\n")\n'
                      f'codes=json.load(open({str(state)!r}))\nsub=a[a.index("--password-file")+2]\n'
                      'deep="--read-data-subset" in a\nkey="deep" if deep else sub\n'
                      f'if sub=="forget":print({json.dumps(GROUPS)!r})\n'
                      f'if sub=="snapshots":print(open({str(tmp_path / "snapshots.json")!r}).read())\n'
                      'sys.exit(codes.get(key,0))\n')
    script.chmod(0o755)
    return str(script), log


def setup(tmp_path, location='s3:https://tos-s3-cn-shanghai.volces.com/synthetic-bucket/nexloop/restic-files', env=None):
    repository = private(tmp_path / 'restic-files-repo2.repository', location + '\n')
    password = private(tmp_path / 'restic-files-repo2.pass', PASSWORD + '\n')
    offsite = private(tmp_path / 'offsite.env', env if env is not None else f'AWS_ACCESS_KEY_ID=SYNTHETICID\nAWS_SECRET_ACCESS_KEY={SECRET}\nAWS_DEFAULT_REGION=cn-shanghai\n')
    return ['--repository-file', str(repository), '--password-file', str(password), '--environment-file', str(offsite)]


def run(args, clock=time.localtime):
    out = io.StringIO()
    with redirect_stdout(out):
        code = M.main(args, clock=clock)
    return code, out.getvalue()


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


SUNDAY = lambda: time.struct_time((2026, 11, 1, 4, 7, 0, 6, 305, 0))  # noqa: E731
MONDAY = lambda: time.struct_time((2026, 11, 2, 4, 7, 0, 0, 306, 0))  # noqa: E731


def test_forget_per_tag_with_prune_and_a_lock_wait_then_check_and_the_deep_check_on_sundays(tmp_path):
    bin_, log = restic(tmp_path)
    code, out = run(['--restic', bin_, *setup(tmp_path), '--s3-bucket-lookup', 'dns'], clock=SUNDAY)
    doc = json.loads(out)
    assert code == 0 and doc == {'ok': True, 'forget': 'ok', 'removed': 2, 'dry_run': False, 'check': 'ok', 'deep': True, 'deep_check': 'ok'}
    seen = calls(log)
    commands = [c['args'][c['args'].index('--password-file') + 2:] for c in seen]
    assert commands == [['snapshots', '--json', '--no-lock'], ['forget', '--retry-lock', '10m', '--group-by', 'host,tags', '--keep-within', '14d', '--keep-monthly', '1', '--prune', '--json'],
                        ['check', '--retry-lock', '10m'], ['check', '--retry-lock', '10m', '--read-data-subset', '1/10']]
    assert all(c['args'][:3] == ['--no-cache', '-o', 's3.bucket-lookup=dns'] for c in seen)
    assert all(set(c['env']) - {'LC_CTYPE', '__CF_USER_TEXT_ENCODING'} <= {'PATH', 'LANG', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION'}
               for c in seen)
    for secret in (PASSWORD, SECRET, 'synthetic-bucket'):
        assert secret not in out and secret not in json.dumps([c['args'] for c in seen])
    code, out = run(['--restic', bin_, *setup(tmp_path)], clock=MONDAY)
    assert code == 0 and json.loads(out)['deep'] is False and 'deep_check' not in json.loads(out)


def test_the_dry_run_forgets_nothing_and_checks_nothing(tmp_path):
    bin_, log = restic(tmp_path)
    code, out = run(['--restic', bin_, *setup(tmp_path), '--dry-run'], clock=SUNDAY)
    assert code == 0 and json.loads(out) == {'ok': True, 'forget': 'ok', 'removed': 2, 'dry_run': True, 'check': 'not_run'}
    listed, only = calls(log)
    assert 'snapshots' in listed['args'] and '--dry-run' in only['args'] and '--prune' not in only['args'] and 'check' not in only['args']


@pytest.mark.parametrize('codes, expected', [
    ({'forget': 1}, {'forget': 'exit 1', 'check': 'not_run'}),
    ({'check': 1}, {'forget': 'ok', 'check': 'exit 1'}),
    ({'deep': 1}, {'forget': 'ok', 'check': 'ok', 'deep_check': 'exit 1'}),
    ({'forget': 10}, {'forget': 'exit 10', 'check': 'not_run'}),  # the repository does not exist: never a pass
])
def test_any_failed_step_fails_the_run(tmp_path, codes, expected):
    bin_, log = restic(tmp_path, codes)
    code, out = run(['--restic', bin_, *setup(tmp_path)], clock=SUNDAY)
    doc = json.loads(out)
    assert code == 1 and doc['ok'] is False and {k: doc[k] for k in expected} == expected


@pytest.mark.parametrize('kw, error', [
    ({'location': 'sftp:nexloop-repo1:/restic'}, 'NotRepo2'),          # repo1 is DATA_HOST's to maintain
    ({'location': '/srv/nexloop-backup/files/restic'}, 'NotRepo2'),
    ({'env': 'AWS_DEFAULT_REGION=cn-shanghai\n'}, 'OffsiteKeysMissing'),
    ({'env': 'RESTIC_PASSWORD=x\n'}, 'BackupUnavailable'),             # only the three AWS keys are accepted
])
def test_repo1_and_an_unconfigured_repo2_are_refused_before_restic_runs(tmp_path, kw, error):
    bin_, log = restic(tmp_path)
    code, out = run(['--restic', bin_, *setup(tmp_path, **kw)])
    assert code == 1 and json.loads(out) == {'ok': False, 'error': error} and calls(log) == []


def test_missing_or_shared_files_and_missing_arguments_never_pass(tmp_path):
    bin_, log = restic(tmp_path)
    args = setup(tmp_path)
    os.chmod(args[3], 0o644)  # the password file readable by others
    code, out = run(['--restic', bin_, *args])
    assert code == 1 and json.loads(out)['error'] == 'PermissionError'
    os.unlink(args[1])
    code, out = run(['--restic', bin_, *args])
    assert code == 1 and json.loads(out)['error'] == 'FileNotFoundError' and calls(log) == []
    with pytest.raises(SystemExit) as stop:
        M.main(['--restic', bin_])
    assert stop.value.code == 2


def test_rules_v21_are_v20_plus_the_maintenance_timer_rule():
    from nexloop_eios.observability import load_rules, validate_rules
    rules = ROOT / 'deploy/configuration'
    v20, v21 = json.loads((rules / 'alert-rules.v20.json').read_text()), load_rules(rules / 'alert-rules.v21.json')
    assert v21['version'] == 21 and v21['rules'][:len(v20['rules'])] == v20['rules'] and len(v21['rules']) == len(v20['rules']) + 1 == 78
    restic_maint = next(r for r in v20['rules'] if r['rule_id'] == 'timer_stale_restic_maint')
    assert v21['rules'][-1] == {**restic_maint, 'rule_id': 'timer_stale_file_backup_maint', 'selector': 'app_host:file-backup-maint',
                                'purpose': v21['rules'][-1]['purpose']}  # DATA_HOST's daily maintenance caliber: 26 hours
    assert v21['decision'].startswith(v20['decision']) and 'v21 (NX-035 repo2 maintenance' in v21['decision']
    validate_rules(v21)



@pytest.mark.parametrize('snapshots, expected', [
    ([{**SNAPSHOTS[0]}, {'id': 'f' * 64, 'short_id': 'ffffffff', 'time': '2030-01-01T00:00:00Z', 'tags': ['files_artifact']}],
     {'forget': 'refused_future_snapshots', 'future': ['ffffffff'], 'unreadable': 0}),
    ([{'id': 'b' * 64, 'short_id': 'bbbbbbbb', 'time': 'yesterday', 'tags': ['files_runtime']}],
     {'forget': 'refused_future_snapshots', 'future': [], 'unreadable': 1}),
])
def test_a_future_dated_or_unreadable_snapshot_stops_the_run_before_forget(tmp_path, snapshots, expected):
    """L6 F1: keep-within counts from a group's newest snapshot; a future-dated one would make forget remove every real one."""
    bin_, log = restic(tmp_path, snapshots=snapshots)
    code, out = run(['--restic', bin_, *setup(tmp_path)], clock=SUNDAY)
    doc = json.loads(out)
    assert code == 1 and doc['ok'] is False and {k: doc[k] for k in expected} == expected and doc['check'] == 'not_run'
    assert [c['args'][c['args'].index('--password-file') + 2] for c in calls(log)] == ['snapshots']  # no forget, no check


def test_a_failed_snapshot_listing_never_passes(tmp_path):
    bin_, log = restic(tmp_path, codes={'snapshots': 1})
    code, out = run(['--restic', bin_, *setup(tmp_path), '--dry-run'])
    assert code == 1 and json.loads(out)['forget'] == 'snapshots exit 1'
