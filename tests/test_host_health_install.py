"""Host health rollout tools (docs/implementation/host-health.md §4), offline: host_health_install.py into a temporary root with a
scripted runner (git, venv, pip, systemctl, the probe's dry run, the DATA_HOST pg_hba and password steps), host_recorder_password.py
against a scripted connection, and host_health_hba_data_host.sh's rule checks. Install, idempotent rerun, uninstall, a blind dry
run stopping before the timer, refusals - and no password in any output, argv or SQL."""
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import host_health_install as I  # noqa: E402
import host_recorder_password as P  # noqa: E402

SHA = 'a' * 40
APP_PW, DATA_PW = 'app-Pw-synthetic-0123456789', 'data-Pw-synthetic-9876543210'


def private(path, value):
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)
    return str(path)


def example(name, **over):
    value = json.loads((ROOT / f'deploy/examples/host-health-install.{name}.example.json').read_text())
    value.update({'release_checkout': '/srv/release', 'release_sha': SHA, 'pip_index_url': 'https://mirror.example.invalid/simple', **over})
    if name == 'app-host':
        value['database'].update(host='pg.example.invalid', sslrootcert='/etc/nexloop/pg-ca.crt')
    else:
        value.update(bootstrap_dsn_file='/srv/nexloop/secrets/bootstrap.dsn', hba={'nexloop_root': '/srv/nexloop', 'stage_env': '/srv/nexloop/stage.env'})
    return value


class Runner:
    def __init__(self, blind=False):
        self.calls, self.stdins, self.blind = [], [], blind

    def __call__(self, command, *, stdin=None, check=True):
        self.calls.append(list(command))
        self.stdins.append(stdin)
        if command[0] == 'git':
            return (ROOT / command[-1].split(':', 1)[1]).read_text()
        if command[1:3] == ['-m', 'venv']:
            (Path(command[3]) / 'bin').mkdir(parents=True)
            for name in ('python', 'pip'):
                (Path(command[3]) / 'bin' / name).write_text('#!/bin/sh\n')
                os.chmod(Path(command[3]) / 'bin' / name, 0o755)
            return ''
        if command[-1] == '--dry-run':
            return json.dumps({'host_role': 'x', 'blind': self.blind, 'units': 3, 'entry': {'units': []}}) + '\n'
        if command[-1].endswith('hba.conf'):
            return 'done: host health lines installed and reloaded (2 rule(s) added)\n'
        if any(str(a).endswith('host_recorder_password.py') for a in command):
            return '{"set": ["nexloop_host_recorder_app_host", "nexloop_host_recorder_data_host"]}\n'
        return ''


TRUSTED = []


def trusting(path, *, what):
    """The temporary root is not uid 0: record what the installer asks to trust (the real check is tested below)."""
    TRUSTED.append((what, str(path)))


def install(tmp_path, cfg, stdin, runner=None, *args, trust=trusting):
    out, runner = io.StringIO(), runner or Runner()
    code = I.main([private(tmp_path / 'config.json', cfg), *args], stdin=io.StringIO(stdin), out=out, runner=runner, root=tmp_path / 'root', trust=trust)
    return code, out.getvalue(), runner


def secrets_absent(text, runner):
    assert APP_PW not in text and DATA_PW not in text
    assert not any(APP_PW in ' '.join(c) or DATA_PW in ' '.join(c) for c in runner.calls)  # never argv


def test_app_host_installs_reruns_unchanged_and_uninstalls(tmp_path):
    cfg = example('app-host')
    code, text, runner = install(tmp_path, cfg, f'app_host\t{APP_PW}\n')
    assert code == 0, text
    root = tmp_path / 'root'
    release = root / 'opt/nexloop/host-health' / SHA
    assert os.readlink(root / 'opt/nexloop/host-health/current') == SHA and (release / '.complete').exists()
    for path in [release, *release.rglob('*')]:
        if not path.is_symlink():
            assert not path.stat().st_mode & 0o022, path  # nothing group/other-writable
    assert (release / 'host_health_probe.py').read_text() == (ROOT / 'scripts/deploy/host_health_probe.py').read_text()
    for name in ('host-health.json', 'host-recorder.dsn'):
        assert stat.S_IMODE((root / 'etc/nexloop' / name).stat().st_mode) == 0o600
    dsn = (root / 'etc/nexloop/host-recorder.dsn').read_text()
    assert f"password='{APP_PW}'" in dsn and "user='nexloop_host_recorder_app_host'" in dsn and "sslmode='verify-full'" in dsn
    probe = json.loads((root / 'etc/nexloop/host-health.json').read_text())
    assert probe['host_role'] == 'app_host' and probe['recorder_dsn_file'] == '/etc/nexloop/host-recorder.dsn' and probe['timers'][0]['name'] == 'provider-drift'
    assert (root / 'etc/systemd/system/nexloop-host-health.timer').exists()
    pip = [c for c in runner.calls if c[0].endswith('/pip')]
    assert pip and '--require-hashes' in pip[0] and '--no-deps' in pip[0]
    order = [c[:3] for c in runner.calls if c[0] == 'systemctl' or c[-1] == '--dry-run']
    assert order[-2][-1] != 'enable' and runner.calls[-1] == ['systemctl', 'enable', '--now', 'nexloop-host-health.timer']  # dry run first
    assert 'dry-run: {"blind": false' in text and not any('host_health_hba_data_host.sh' in ' '.join(c) for c in runner.calls)  # no pg_hba on APP_HOST
    secrets_absent(text, runner)
    # Rerun: the release is reused (no pip), the files are unchanged.
    code, text, runner = install(tmp_path, cfg, f'app_host\t{APP_PW}\n')
    assert code == 0 and 'reused' in text and 'host-health.json unchanged' in text and 'host-recorder.dsn unchanged' in text
    assert not any(c[0].endswith('/pip') for c in runner.calls)
    # A new password rewrites the DSN only.
    code, text, _ = install(tmp_path, cfg, f'app_host\t{APP_PW}-rotated\n')
    assert 'host-recorder.dsn written' in text and 'host-health.json unchanged' in text
    # Uninstall removes everything it installed.
    code, text, runner = install(tmp_path, cfg, '', None, '--uninstall')
    assert code == 0 and ['systemctl', 'disable', '--now', 'nexloop-host-health.timer'] in runner.calls
    assert not (root / 'opt/nexloop/host-health').exists() and not (root / 'etc/nexloop/host-recorder.dsn').exists()
    assert not (root / 'etc/systemd/system/nexloop-host-health.service').exists()


def test_data_host_sets_pg_hba_and_both_passwords_through_stdin(tmp_path):
    code, text, runner = install(tmp_path, example('data-host'), f'data_host\t{DATA_PW}\napp_host\t{APP_PW}\n')
    assert code == 0, text
    hba = [c for c in runner.calls if 'host_health_hba_data_host.sh' in ' '.join(c)]
    assert hba and hba[0][:2] == ['env', f'HOST_HEALTH_PYTHON={example("data-host")["python"]}']  # S2: the configured interpreter
    password = [(c, s) for c, s in zip(runner.calls, runner.stdins) if any(str(a).endswith('host_recorder_password.py') for a in c)]
    assert password and password[0][0][0].endswith('host-health/current/venv/bin/python')  # the venv's psycopg, not the system python
    assert password[0][1] == f'app_host\t{APP_PW}\ndata_host\t{DATA_PW}\n'
    dsn = (tmp_path / 'root/etc/nexloop/host-recorder.dsn').read_text()
    assert f"password='{DATA_PW}'" in dsn and APP_PW not in dsn and "host='/srv/nexloop/run/postgresql'" in dsn
    secrets_absent(text, runner)


def test_a_blind_dry_run_stops_before_the_timer(tmp_path):
    code, text, runner = install(tmp_path, example('app-host'), f'app_host\t{APP_PW}\n', Runner(blind=True))
    assert code == 1 and 'blind' in text and ['systemctl', 'enable', '--now', 'nexloop-host-health.timer'] not in runner.calls


@pytest.mark.parametrize('stdin', ['', f'app_host {APP_PW}\n', f'data_host\t{DATA_PW}\n', 'app_host\tshort\n', f'app_host\t{APP_PW}\napp_host\t{APP_PW}\n'])
def test_wrong_passwords_on_stdin_are_refused_unechoed(tmp_path, stdin):
    code, text, runner = install(tmp_path, example('app-host'), stdin)
    assert code == 2 and 'stdin' in text and runner.calls == []
    assert APP_PW not in text and DATA_PW not in text


@pytest.mark.parametrize('over', [{'host_role': 'third_host'}, {'release_sha': 'main'}, {'pip_index_url': 'http://mirror'},
                                  {'release_checkout': 'relative/path'}, {'database': {'host': '/socket', 'dbname': 'nexloop_stage'}}])
def test_configuration_is_refused(tmp_path, over):
    code, text, _ = install(tmp_path, example('app-host', **over), f'app_host\t{APP_PW}\n')
    assert code == 2 and 'configuration' in text


def test_the_data_host_needs_the_socket_and_its_owner_steps(tmp_path):
    cfg = example('data-host')
    cfg['database']['host'] = 'pg.example.invalid'
    assert install(tmp_path, cfg, f'data_host\t{DATA_PW}\napp_host\t{APP_PW}\n')[0] == 2
    cfg = example('data-host')
    del cfg['hba']
    assert install(tmp_path, cfg, f'data_host\t{DATA_PW}\napp_host\t{APP_PW}\n')[0] == 2


def test_the_examples_match_the_alert_rules_v13_selectors():
    rules = json.loads((ROOT / 'deploy/configuration/alert-rules.v13.json').read_text())['rules']
    stale = {r['selector'] for r in rules if r['rule_id'].startswith('timer_stale_')}
    names = set()
    for name, host in (('app-host', 'app_host'), ('data-host', 'data_host')):
        value = json.loads((ROOT / f'deploy/examples/host-health-install.{name}.example.json').read_text())
        names |= {f'{host}:{t["name"]}' for t in value['probe']['timers']}
    assert names == stale
    # The same timers as L7's probe example (B9: the file backup's scheduled rounds). The runtime worker runs as the container
    # nexloop-app-runtime-worker-1 on stage (C1, read-only check 2026-10-23), not as a systemd unit.
    l7 = json.loads((ROOT / 'deploy/examples/host-health.app-host.example.json').read_text())
    app = json.loads((ROOT / 'deploy/examples/host-health-install.app-host.example.json').read_text())['probe']
    assert app['timers'] == l7['timers'] and app['services'] == l7['services'] == []
    runtime_worker = {'name': 'runtime-worker', 'container': 'nexloop-app-runtime-worker-1'}
    assert runtime_worker in app['containers'] and runtime_worker in l7['containers']


class Db:
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args):
        self.sql.append(sql)


def test_the_password_tool_sends_verifiers_only(tmp_path):
    db, out = Db(), io.StringIO()
    code = P.main(['--bootstrap-dsn-file', '/unused'], stdin=io.StringIO(f'data_host\t{DATA_PW}\napp_host\t{APP_PW}\n'), out=out, connect=lambda: db)
    assert code == 0 and json.loads(out.getvalue()) == {'set': ['nexloop_host_recorder_app_host', 'nexloop_host_recorder_data_host']}
    assert len(db.sql) == 2 and all(s.startswith('alter role nexloop_host_recorder_') and "password 'SCRAM-SHA-256$4096:" in s for s in db.sql)
    assert not any(APP_PW in s or DATA_PW in s for s in db.sql) and APP_PW not in out.getvalue()
    for bad in ('', f'other\t{APP_PW}\n', 'app_host\tshort\n'):
        out = io.StringIO()
        assert P.main(['--bootstrap-dsn-file', '/unused'], stdin=io.StringIO(bad), out=out, connect=lambda: db) == 2


def test_the_password_tool_takes_the_bootstrap_socket_dsn_only(tmp_path):
    sockets = tmp_path / 'run-postgresql'
    sockets.mkdir()
    (sockets / '.s.PGSQL.5432').write_text('')  # stands for the server's socket
    for dsn, ok in ((f'host={sockets} user=nexloop_bootstrap dbname=nexloop_stage', True),
                    ('host=pg.example.invalid user=nexloop_bootstrap dbname=nexloop_stage', False),
                    (f'host={sockets} user=nexloop_api dbname=nexloop_stage', False)):
        path = tmp_path / 'dsn'
        path.write_text(dsn)
        os.chmod(path, 0o600)
        if ok:
            assert P.bootstrap_dsn(str(path)) == dsn
        else:
            with pytest.raises(P.Refused):
                P.bootstrap_dsn(str(path))


def test_a_socket_directory_without_the_server_socket_is_named(tmp_path):
    """The container's DSN (host=/var/run/postgresql) used on the host: refused before connecting, saying which socket is missing;
    another port's socket in the directory is not the one."""
    sockets = tmp_path / 'var-run-postgresql'
    sockets.mkdir()
    (sockets / '.s.PGSQL.5433').write_text('')
    path = tmp_path / 'dsn'
    path.write_text(f'host={sockets} port=5432 user=nexloop_bootstrap dbname=nexloop_stage password=not-printed-0123')
    os.chmod(path, 0o600)
    with pytest.raises(P.Refused, match=r'no PostgreSQL socket \.s\.PGSQL\.5432 in the socket directory') as refused:
        P.bootstrap_dsn(str(path))
    assert 'not-printed' not in str(refused.value)
    out = io.StringIO()
    assert P.main(['--bootstrap-dsn-file', str(path)], stdin=io.StringIO(f'data_host\t{DATA_PW}\n'), out=out) == 2
    assert 'socket directory' in json.loads(out.getvalue())['refused'] and DATA_PW not in out.getvalue()


def test_a_failed_step_carries_the_tools_refusal(tmp_path):
    """The installer's step failure names the called tool's refusal (its last JSON line), not only its exit code."""
    tool = tmp_path / 'tool.py'
    tool.write_text('import json,sys\nprint(json.dumps({"refused": "--bootstrap-dsn-file: no PostgreSQL socket .s.PGSQL.5432"}))\nsys.exit(2)\n')
    with pytest.raises(I.Failed, match=r'exited 2: --bootstrap-dsn-file: no PostgreSQL socket'):
        I.run([sys.executable, str(tool)])
    tool.write_text('print("plain text, not json")\nraise SystemExit(1)\n')
    with pytest.raises(I.Failed) as failed:
        I.run([sys.executable, str(tool)])
    assert str(failed.value).endswith('exited 1')


HBA = ROOT / 'scripts/deploy/host_health_hba_data_host.sh'


def lib(snippet, *args):
    return subprocess.run(['bash', '-c', f'set -euo pipefail; HOST_HEALTH_HBA_LIB=1 source "$0"; {snippet}', str(HBA), *args], capture_output=True, text=True)


def test_the_hba_script_adds_exactly_the_two_host_health_lines(tmp_path):
    assert subprocess.run(['bash', '-n', str(HBA)], capture_output=True).returncode == 0
    template = (ROOT / 'deploy/stage/config/pg_hba.conf.template').read_text().replace('__NEXLOOP_DB__', 'nexloop_stage').replace('__APP_HOST_ADDR__', '192.0.2.10')
    before = '\n'.join(row for row in template.splitlines() if 'nexloop_host_recorder' not in row or row.lstrip().startswith('#')) + '\n'
    (tmp_path / 'before').write_text(before)
    (tmp_path / 'now').write_text(template)
    added = lib('diff <(rules "$1") <(rules "$2") | sed -n "s/^> //p"', str(tmp_path / 'before'), str(tmp_path / 'now')).stdout.splitlines()
    assert added == ['local nexloop_stage nexloop_host_recorder_data_host scram-sha-256',
                     'hostssl nexloop_stage nexloop_host_recorder_app_host 192.0.2.10/32 scram-sha-256']
    assert lib('check_added "$1"', '\n'.join(added)).returncode == 0
    for bad in (added[1].replace('192.0.2.10/32', '0.0.0.0/0'), added[1].replace('scram-sha-256', 'trust'), added[0] + '\n' + added[0],
                'hostssl nexloop_stage nexloop_api 192.0.2.10/32 scram-sha-256'):
        assert lib('check_added "$1"', bad).returncode != 0, bad
    assert lib('check_removed "$1"', 'local all nexloop_backup scram-sha-256').returncode != 0
    # The reject lines stay last: the new lines come before them.
    lines = [row for row in template.splitlines() if row and not row.startswith('#')]
    assert lines.index('hostssl nexloop_stage nexloop_host_recorder_app_host 192.0.2.10/32  scram-sha-256') < lines.index(
        next(row for row in lines if row.startswith('hostnossl all')))


def test_alert_rules_apply_opens_the_window_and_applies_v12():
    import contextlib
    import alert_rules_apply as A
    seen = {}

    @contextlib.contextmanager
    def window(path, *, refused):
        seen['bootstrap'] = path
        yield '/tmp/configurator.dsn'

    def apply(manifest, tenant, *, database_url_file):
        seen.update(version=manifest['version'], tenant=tenant, dsn=database_url_file)
        return {'configured': True}
    out = io.StringIO()
    manifest = str(ROOT / 'deploy/configuration/alert-rules.v12.json')
    assert A.main(['--manifest', manifest, '--check'], out=out) == 0 and json.loads(out.getvalue())['version'] == 12
    out = io.StringIO()
    assert A.main(['--manifest', manifest, '--tenant', 't', '--bootstrap-dsn-file', '/b'], out=out, window=window, apply=apply) == 0
    assert seen == {'bootstrap': '/b', 'version': 12, 'tenant': 't', 'dsn': '/tmp/configurator.dsn'}
    assert json.loads(out.getvalue())['applied'] is True and '/tmp/configurator.dsn' not in out.getvalue()
    assert A.main(['--manifest', manifest], out=io.StringIO()) == 2


def test_root_executes_only_root_owned_code(tmp_path):
    """L6 rollout review I1: the checkout, the interpreter and the installer's scripts are checked before anything runs; a
    path (or a directory above it) that is not root's alone, or a symbolic link, is refused."""
    TRUSTED.clear()
    code, text, runner = install(tmp_path, example('data-host'), f'data_host\t{DATA_PW}\napp_host\t{APP_PW}\n')
    assert code == 0, text
    what = {w for w, _ in TRUSTED}
    assert {'release_checkout', 'python', 'installer', 'release base'} <= what
    assert ('release_checkout', '/srv/release') in TRUSTED and ('python', example('data-host')['python']) in TRUSTED
    # The real check: this test's files are not uid 0 (refused); a symbolic link is refused too.
    with pytest.raises(I.Refused, match='owned by root'):
        I.root_owned(tmp_path / 'config.json', what='x')
    (tmp_path / 'link').symlink_to(tmp_path / 'config.json')
    with pytest.raises(I.Refused, match='symbolic link|owned by root'):
        I.root_owned(tmp_path / 'link', what='x')
    # Refused before anything runs.
    def refuse(path, *, what):
        if what == 'release_checkout':
            raise I.Refused('release_checkout: must be owned by root')
    (tmp_path / 'other').mkdir()
    code, text, runner = install(tmp_path / 'other', example('app-host'), f'app_host\t{APP_PW}\n', None, trust=refuse)
    assert code == 2 and 'release_checkout' in text and runner.calls == []


def test_a_reused_release_is_checked_again(tmp_path):
    """L6 rollout review I2: a release another account could have changed is never reused."""
    assert install(tmp_path, example('app-host'), f'app_host\t{APP_PW}\n')[0] == 0
    def refuse_release(path, *, what):
        if what == 'release':
            raise I.Refused('release: must be owned by root')
    code, text, runner = install(tmp_path, example('app-host'), f'app_host\t{APP_PW}\n', None, trust=refuse_release)
    assert code == 2 and 'release' in text and not any(c[0] == 'systemctl' for c in runner.calls)


class FakeStat:
    def __init__(self, mode, uid=0):
        self.st_mode, self.st_uid = mode, uid


def test_the_real_check_refuses_a_link_a_writable_directory_or_another_owner_anywhere_on_the_path(monkeypatch):
    """S5: the root_owned check over a whole path, with lstat answers standing for a root-owned tree except one level."""
    def tree(bad=None, how=None):
        def lstat(path):
            path = str(path)
            if path == bad:
                return {'link': FakeStat(stat.S_IFLNK | 0o777), 'group': FakeStat(stat.S_IFDIR | 0o775), 'other': FakeStat(stat.S_IFDIR | 0o757),
                        'owner': FakeStat(stat.S_IFDIR | 0o755, uid=1000)}[how]
            return FakeStat(stat.S_IFDIR | 0o755)
        return lstat
    target = '/opt/nexloop/release-x/scripts/deploy/host_health_install.py'
    monkeypatch.setattr(I.os, 'lstat', tree())
    I.root_owned(target, what='x')  # all root's: accepted
    for how, text in (('link', 'symbolic link'), ('group', 'not group/other-writable'), ('other', 'not group/other-writable'), ('owner', 'owned by root')):
        monkeypatch.setattr(I.os, 'lstat', tree('/opt/nexloop/release-x', how))  # an intermediate level
        with pytest.raises(I.Refused, match=text):
            I.root_owned(target, what='x')


def test_a_reused_release_with_a_file_of_another_owner_is_refused(tmp_path, monkeypatch):
    """S5 / I2: the real check over a reused release - one file not root's stops the run before systemctl."""
    assert install(tmp_path, example('app-host'), f'app_host\t{APP_PW}\n')[0] == 0
    planted = next((tmp_path / 'root/opt/nexloop/host-health' / SHA).rglob('host_health_probe.py'))

    def owner_check(path, *, what):
        if what == 'release':
            info = os.lstat(path)
            if str(path) == str(planted):
                raise I.Refused('release: must be owned by root')  # stands for a uid other than 0
            return info
        return None
    code, text, runner = install(tmp_path, example('app-host'), f'app_host\t{APP_PW}\n', None, trust=owner_check)
    assert code == 2 and 'release' in text and not any(c[0] == 'systemctl' for c in runner.calls)


def dry_run(runner):
    return next(c for c in runner.calls if c[-1] == '--dry-run')


def test_the_dry_run_runs_in_the_units_sandbox(tmp_path):
    """Stage 2026-10-25: the dry run ran outside the unit and missed two failures of the timer (MemoryDenyWriteExecute crashing the
    standalone CPython; ProtectHome hiding HOME from libpq). It now runs through systemd-run with the unit's settings."""
    code, text, runner = install(tmp_path, example('data-host'), f'data_host\t{DATA_PW}\napp_host\t{APP_PW}\n')
    assert code == 0, text
    command = dry_run(runner)
    assert command[:6] == ['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec']
    props = [command[i + 1] for i, a in enumerate(command) if a == '-p']
    assert {'ProtectHome=true', 'Environment=HOME=/var/lib/nexloop-host-health', 'StateDirectory=nexloop-host-health', 'NoNewPrivileges=true',
            'CapabilityBoundingSet=', 'MemoryDenyWriteExecute=true'} <= set(props)  # W^X on by default (L6 review 1)
    assert not any(p.startswith(('ExecStart=', 'Type=')) for p in props) and 'MemoryDenyWriteExecute=no' not in props
    assert command[-7:-1] == [str(tmp_path / 'root/opt/nexloop/host-health/current/venv/bin/python'), '-I', '-B',
                              str(tmp_path / 'root/opt/nexloop/host-health/current/host_health_probe.py'), '--config', '/etc/nexloop/host-health.json']
    unit = (tmp_path / 'root/etc/systemd/system/nexloop-host-health.service').read_text()
    assert 'Environment=HOME=/var/lib/nexloop-host-health' in unit and 'MemoryDenyWriteExecute=true' in unit
    assert not (tmp_path / 'root/etc/systemd/system/nexloop-host-health.service.d/50-installer-mdwe.conf').exists()


def test_only_an_explicit_false_turns_w_x_off_and_the_drop_ins_go_with_uninstall(tmp_path):
    cfg = example('app-host')  # the standalone CPython host: memory_deny_write_execute false
    assert cfg['memory_deny_write_execute'] is False and example('data-host')['memory_deny_write_execute'] is True
    code, text, runner = install(tmp_path, cfg, f'app_host\t{APP_PW}\n')
    assert code == 0 and 'MemoryDenyWriteExecute OFF for this host' in text
    dropins = tmp_path / 'root/etc/systemd/system/nexloop-host-health.service.d'
    assert (dropins / '50-installer-mdwe.conf').read_text() == '[Service]\nMemoryDenyWriteExecute=no\n'
    props = [c for c in dry_run(runner) if c.startswith('MemoryDenyWriteExecute=')]
    assert props == ['MemoryDenyWriteExecute=true', 'MemoryDenyWriteExecute=no']  # the drop-in applies after the unit, as systemd does
    # A hand-made drop-in (the site workaround) is part of the sandbox too.
    (dropins / '20-home.conf').write_text('[Service]\nEnvironment=HOME=/var/lib/elsewhere\n')
    del cfg['memory_deny_write_execute']  # absent: the default, W^X on
    code, text, runner = install(tmp_path, cfg, f'app_host\t{APP_PW}\n')
    assert code == 0 and not (dropins / '50-installer-mdwe.conf').exists() and 'OFF' not in text
    assert [c for c in dry_run(runner) if c.startswith('Environment=')] == ['Environment=HOME=/var/lib/nexloop-host-health', 'Environment=HOME=/var/lib/elsewhere']
    assert install(tmp_path, {**cfg, 'memory_deny_write_execute': 'no'}, f'app_host\t{APP_PW}\n')[0] == 2
    code, text, _ = install(tmp_path, cfg, '', None, '--uninstall')
    assert code == 0 and not dropins.exists()


@pytest.mark.parametrize('line', ['Environment="HOME=/var/lib/x"', "Environment='A=1 B=2'", 'ReadWritePaths=/a \\'])
def test_a_drop_in_the_sandbox_cannot_mirror_stops_the_install(tmp_path, line):
    """Continuation lines and quoted values: systemd reads them differently from a key=value split - refused, timer not enabled."""
    assert install(tmp_path, example('data-host'), f'data_host\t{DATA_PW}\napp_host\t{APP_PW}\n')[0] == 0
    dropins = tmp_path / 'root/etc/systemd/system/nexloop-host-health.service.d'
    dropins.mkdir(exist_ok=True)
    (dropins / '30-site.conf').write_text('[Service]\n' + line + '\n')
    code, text, runner = install(tmp_path, example('data-host'), f'data_host\t{DATA_PW}\napp_host\t{APP_PW}\n')
    assert code == 1 and 'continuation line or a quoted value' in text
    assert ['systemctl', 'enable', '--now', 'nexloop-host-health.timer'] not in runner.calls
