"""scripts/deploy/stage_release.py: the checkpointed stage release runner, with a fake command executor (no host is reached).

Covered: the step order and ranges; a dry run runs nothing and writes no log; every unexpected result stops the run with exit 1 and no
later step runs (CI digest, commit not on main, gate not allowed, definer, digests, gate failures, post); promote runs only after a gate
whose failures are all expected - also when promote runs on its own later; private configuration values and DSN- or password-shaped
output never reach the log or the output; the example configuration loads.
"""
import io
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import stage_release as R  # noqa: E402

SHA = 'a' * 40
DIGEST = 'b' * 64
PRIVATE = {'ci': 'ci-box.example.invalid', 'app': 'app-box.example.invalid', 'data': 'data-box.example.invalid',
           'user_ci': 'zzciuser', 'user_rel': 'zzreluser', 'user_dep': 'zzdepuser', 'root': '/srv/zz-private-root', 'tenant': 'zz-tenant-0001'}


def cmd(host, *argv, user='deploy'):
    return {'host': host, 'user': user, 'argv': list(argv)}


def config(tmp_path, **over):
    c = {'schema': 'nexloop-stage-release/1', 'release': {'sha': SHA, 'release_id': None},
         'hosts': {'ci': {'ssh': PRIVATE['ci']}, 'app': {'ssh': PRIVATE['app']}, 'data': {'ssh': PRIVATE['data']}},
         'users': {'ci': PRIVATE['user_ci'], 'release': PRIVATE['user_rel'], 'deploy': PRIVATE['user_dep']},
         'vars': {'root': PRIVATE['root'], 'tenant_id': PRIVATE['tenant']},
         'log': str(tmp_path / 'run.jsonl'), 'precheck_sql': str(tmp_path / 'precheck.json'),
         'expected_gate_failures': ['backup_status'], 'max_log_errors': 0,
         'commands': {
             'build': {'ci_result_sha256': cmd('ci', 'digest-ci', '{root}/ci/{sha}', user='ci'), 'build': cmd('ci', 'release-build', '{sha}', user='ci')},
             'sign': {'ci_result_sha256': cmd('ci', 'digest-ci', '{root}/ci/{sha}', user='ci'), 'on_main': cmd('ci', 'on-main', '{sha}', user='ci'),
                      'sign': cmd('ci', 'sign', '{release_id}', user='release'), 'seal': cmd('ci', 'seal', '{release_id}', user='release'),
                      'check': cmd('ci', 'check', '{release_id}', user='release')},
             'backup': {'backup': {'host': 'data', 'argv': ['sudo', '-n', 'systemctl', 'start', 'nexloop-pg-diff.service']}},
             'precheck': {'sql': cmd('data', 'psql-ro', '{root}/ro.dsn')},
             'migrate': {'gate': cmd('app', 'migration-gate'), 'migrate': cmd('app', 'migrate', '--tenant', '{tenant_id}'), 'definer': cmd('app', 'definer')},
             'publish': {'dry_run': cmd('app', 'publish', '--dry-run'), 'publish': cmd('app', 'publish')},
             'roles': {'apply': cmd('app', 'roles-apply')},
             'rebuild': {'load': cmd('app', 'docker-load', '{release_id}'), 'loaded_digests': cmd('app', 'loaded-digests'),
                         'manifest_digests': cmd('app', 'manifest-digests', '{release_id}'), 'env_backup': cmd('app', 'env-backup'),
                         'config_check': cmd('app', 'config-q'), 'up': cmd('app', 'up-d')},
             'gate': {'gate_dir': {'host': 'app', 'argv': ['sudo', '-n', 'gate-dir', '{root}/gate']}, 'doctor': cmd('app', 'doctor')},
             'promote': {'promote': cmd('app', 'promote', '{release_id}')},
             'post': {'post_deploy_check': cmd('app', 'post-check'), 'log_errors': cmd('app', 'log-errors')}}}
    c.update(over)
    (tmp_path / 'precheck.json').write_text(json.dumps([{'name': 'trigger_count', 'sql': 'select count(*) from pg_trigger', 'expect': '7'},
                                                        {'name': 'versions', 'sql': 'select max(version) from control.schema_migrations'}]))
    (tmp_path / 'precheck.json').chmod(0o600)
    path = tmp_path / 'stage-release.json'
    path.write_text(json.dumps(c))
    path.chmod(0o600)
    return path


OK = {
    'digest-ci': f'{DIGEST}  /path/result.json\n',
    'release-build': json.dumps({'ok': True, 'release_id': 'stage-20261021-1', 'images': {'backend': 'sha256:1', 'gateway': 'sha256:2'}}),
    'on-main': '', 'sign': '{"ok": true}', 'seal': '{"ok": true}', 'check': '{"ok": true}', 'sudo': 'started',
    'psql-ro': '7', 'migration-gate': '{"allowed": true}', 'migrate': '{"ok": true, "revision": "0290"}',
    'definer': 'definer search_path check: 900 routines pin search_path, 700 SECURITY DEFINER, 0 problems',
    'publish': '{"ok": true}', 'roles-apply': '{"ok": true}', 'docker-load': 'Loaded image',
    'loaded-digests': '{"backend": "sha256:1", "gateway": "sha256:2"}', 'manifest-digests': '{"backend": "sha256:1", "gateway": "sha256:2"}',
    'env-backup': '', 'config-q': '', 'up-d': '',
    'doctor': json.dumps({'checks': {'release_verified': True, 'backup_status': False, 'observability': {'alerts': True}}}),
    'promote': '{"ok": true}', 'post-check': '{"pass": true, "failed": []}', 'log-errors': '{"backend": 0, "gateway": 0}',
}


class Fake:
    """Records (host, user, argv) and answers by the first command word (the second for sudo -n <word>)."""

    def __init__(self, **answers):
        self.calls, self.answers = [], {**OK, **answers}

    def run(self, host, user, argv):
        self.calls.append((host, user, list(argv)))
        word = argv[2] if argv[:2] == ['sudo', '-n'] and argv[2] != 'systemctl' else argv[0]
        answer = self.answers[word if word in self.answers else 'sudo']
        if isinstance(answer, tuple):
            return answer
        if word == 'psql-ro' and 'schema_migrations' in argv[-1]:
            return 0, '0290'
        return 0, answer


def run(tmp_path, *argv, fake=None, **over):
    out = io.StringIO()
    fake = fake or Fake()
    code = R.main(['--config', str(config(tmp_path, **over)), *argv], executor=fake, out=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    return code, lines, fake


def log(tmp_path):
    p = tmp_path / 'run.jsonl'
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_the_whole_run_goes_in_order_and_each_step_logs_one_line(tmp_path):
    code, lines, fake = run(tmp_path, '--from', 'build', '--to', 'post')
    assert code == 0 and [l['step'] for l in lines] == list(R.STEPS) and all(l['ok'] for l in lines)
    assert [l['step'] for l in log(tmp_path)] == list(R.STEPS)
    words = [c[2][2] if c[2][:2] == ['sudo', '-n'] and c[2][2] != 'systemctl' else c[2][0] for c in fake.calls]
    assert words[:7] == ['digest-ci', 'release-build', 'digest-ci', 'on-main', 'sign', 'seal', 'check']
    assert words.index('migration-gate') < words.index('migrate') < words.index('definer') < words.index('publish')
    assert words.index('gate-dir') < words.index('doctor') < words.index('promote') < words.index('post-check')
    # The release id from build fills the later commands; users and hosts are the configured ones.
    assert ['sign', 'stage-20261021-1'] in [c[2] for c in fake.calls] and (PRIVATE['ci'], PRIVATE['user_rel'], ['sign', 'stage-20261021-1']) in fake.calls
    gate = next(l for l in lines if l['step'] == 'gate')
    assert gate['facts'] == {'failed': ['backup_status'], 'unexpected': [], 'expected': ['backup_status']}


def test_a_step_runs_alone_and_a_range_runs_only_its_steps(tmp_path):
    code, lines, fake = run(tmp_path, '--step', 'backup')
    assert code == 0 and [l['step'] for l in lines] == ['backup'] and fake.calls == [(PRIVATE['data'], None, ['sudo', '-n', 'systemctl', 'start', 'nexloop-pg-diff.service'])]
    code, lines, _ = run(tmp_path, '--from', 'precheck', '--to', 'roles')
    assert code == 0 and [l['step'] for l in lines] == ['precheck', 'migrate', 'publish', 'roles']
    assert lines[0]['facts']['precheck']['trigger_count']['matched'] is True and lines[0]['facts']['precheck']['versions']['expected'] is False
    assert run(tmp_path, '--from', 'post', '--to', 'build')[0] == 2


def test_a_dry_run_runs_nothing_and_writes_no_log(tmp_path):
    fake = Fake()
    code, lines, _ = run(tmp_path, '--from', 'build', '--to', 'post', '--dry-run', fake=fake)
    assert code == 0 and fake.calls == [] and not (tmp_path / 'run.jsonl').exists()
    assert [l['step'] for l in lines] == list(R.STEPS) and all(l['dry_run'] for l in lines)
    sign = next(l for l in lines if l['step'] == 'sign')
    assert any('{release_id}' in ' '.join(c['argv']) for c in sign['commands'])  # unknown until build: shown as the placeholder


@pytest.mark.parametrize('answers,stop_at,why', [
    ({'digest-ci': (0, 'not-a-digest')}, 'build', 'not a sha256'),
    ({'release-build': (0, '{"ok": false, "refused": "x"}')}, 'build', 'ok: true'),
    ({'on-main': (1, '')}, 'sign', 'on_main exited 1'),
    ({'migration-gate': (0, '{"allowed": false}')}, 'migrate', 'allowed: true'),
    ({'migrate': (3, 'Traceback')}, 'migrate', 'migrate exited 3'),
    ({'definer': (0, '2 problems')}, 'migrate', 'definer'),
    ({'loaded-digests': (0, '{"backend": "sha256:9", "gateway": "sha256:2"}')}, 'rebuild', 'digests differ'),
    ({'doctor': (0, json.dumps({'checks': {'backup_status': False, 'tls': False}}))}, 'gate', 'unexpected failures tls'),
    ({'post-check': (0, '{"pass": false}')}, 'post', 'pass: true'),
    ({'log-errors': (0, '{"backend": 2}')}, 'post', 'log errors'),
    ({'psql-ro': (0, '8')}, 'precheck', 'differs'),
])
def test_any_unexpected_result_stops_the_run_and_no_later_step_runs(tmp_path, answers, stop_at, why):
    code, lines, fake = run(tmp_path, '--from', 'build', '--to', 'post', fake=Fake(**answers))
    assert code == 1 and lines[-1]['step'] == stop_at and lines[-1]['ok'] is False and why in lines[-1]['error'], lines[-1]
    assert [l['step'] for l in lines] == list(R.STEPS[:R.STEPS.index(stop_at) + 1])
    later = {w for s in R.STEPS[R.STEPS.index(stop_at) + 1:] for w in ('promote', 'post-check') if s in ('promote', 'post')}
    assert not any(c[2][0] in later for c in fake.calls)


def test_sign_refuses_a_ci_result_changed_since_build(tmp_path):
    assert run(tmp_path, '--step', 'build')[0] == 0
    code, lines, fake = run(tmp_path, '--step', 'sign', fake=Fake(**{'digest-ci': (0, 'c' * 64 + '  result.json')}))
    assert code == 1 and 'differs from the one recorded at build' in lines[-1]['error'] and not any(c[2][0] == 'sign' for c in fake.calls)


def test_promote_runs_only_after_a_gate_with_expected_failures_alone(tmp_path):
    # Without a gate in the log: refused, nothing run.
    code, lines, fake = run(tmp_path, '--step', 'promote', release={'sha': SHA, 'release_id': 'stage-x'})
    assert code == 1 and 'run gate first' in lines[-1]['error'] and fake.calls == []
    # A gate with an unexpected failure (it stops), then promote on its own: refused, promote never runs.
    bad = Fake(doctor=(0, json.dumps({'checks': {'backup_status': False, 'tls': False}})))
    assert run(tmp_path, '--step', 'gate', fake=bad, release={'sha': SHA, 'release_id': 'stage-x'})[0] == 1
    code, lines, fake = run(tmp_path, '--step', 'promote', release={'sha': SHA, 'release_id': 'stage-x'})
    assert code == 1 and 'unexpected failures' in lines[-1]['error'] and fake.calls == []
    # A later gate with the expected failure only: promote runs.
    assert run(tmp_path, '--step', 'gate', release={'sha': SHA, 'release_id': 'stage-x'})[0] == 0
    code, lines, fake = run(tmp_path, '--step', 'promote', release={'sha': SHA, 'release_id': 'stage-x'})
    assert code == 0 and fake.calls == [(PRIVATE['app'], PRIVATE['user_dep'], ['promote', 'stage-x'])]
    assert lines[-1]['facts'] == {'gate_failed': ['backup_status']}


def test_no_private_value_or_dsn_reaches_the_log_or_the_output(tmp_path):
    leaky = Fake(**{'migrate': (0, '{"ok": true, "dsn": "postgresql://zzdepuser:hunter2@db.example.invalid/nexloop", "note": "password=hunter2"}'),
                    'doctor': (0, json.dumps({'checks': {'backup_status': False}, 'detail': f'host={PRIVATE["data"]} user=x'}))})
    code, lines, _ = run(tmp_path, '--from', 'build', '--to', 'post', fake=leaky)
    assert code == 0
    text = (tmp_path / 'run.jsonl').read_text() + json.dumps(lines)
    for secret in [*PRIVATE.values(), 'hunter2', 'postgresql://', 'db.example.invalid']:
        assert secret not in text, secret
    # Hosts and users appear only as their configuration keys (app, data, ci; deploy, release); arguments with private values as markers.
    entry = next(e for e in log(tmp_path) if e['step'] == 'migrate')
    assert {(c['host'], c['user']) for c in entry['commands']} == {('app', 'deploy')} and '<var.tenant_id>' in text
    # The log keeps what evidence needs: steps, exit codes, output digests, checked facts.
    entry = next(e for e in log(tmp_path) if e['step'] == 'migrate')
    assert all(re.fullmatch(r'[0-9a-f]{64}', c['output_sha256']) and c['exit'] == 0 for c in entry['commands'])
    assert (tmp_path / 'run.jsonl').stat().st_mode & 0o777 == 0o600


def test_inputs_are_refused(tmp_path):
    p = config(tmp_path)
    p.chmod(0o644)
    out = io.StringIO()
    assert R.main(['--config', str(p), '--step', 'build'], executor=Fake(), out=out) == 2
    for over in ({'schema': 'x'}, {'release': {'sha': 'not-a-sha'}}, {'commands': {'build': {'build': {'host': 'elsewhere', 'argv': ['x']}}}},
                 {'commands': {'build': {'ci_result_sha256': cmd('ci', 'x', user='nobody'), 'build': cmd('ci', 'y', user='ci')}}}):
        assert run(tmp_path, '--step', 'build', **over)[0] == 2
    # A precheck statement that writes or chains statements is refused before anything runs.
    fake = Fake()
    out = io.StringIO()
    path = config(tmp_path)
    (tmp_path / 'precheck.json').write_text(json.dumps([{'name': 'bad', 'sql': 'delete from x'}]))
    assert R.main(['--config', str(path), '--step', 'precheck'], executor=fake, out=out) == 2 and fake.calls == []


def test_the_example_configuration_loads_and_names_only_placeholders():
    text = (ROOT / 'deploy/examples/stage-release.example.json').read_text()
    c = R.load_config(text)
    assert set(c['commands']) == set(R.STEPS)
    for host in c['hosts'].values():
        assert host['ssh'].startswith('<') and host['ssh'].endswith('>')
    assert all(v.startswith('<') for v in c['users'].values()) and all(v.startswith('<') for v in c['vars'].values())
    assert not re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)
