"""NX-054 P4a: scripts/deploy/relation_gaps_install.py into a temporary root with a scripted runner (no root, no systemd, no
database): install, rerun unchanged, the DSN never in output or argv, the dry run's answers (0 and 1 go on, 2 stops before the
timer), the host-health timers gaining relation-read-gaps in both files, the drill drop-in, uninstall."""
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import relation_gaps_install as I  # noqa: E402

SHA = 'b' * 40
DSN = "host='/var/run/postgresql' dbname='nexloop_stage' user='nexloop_metrics' password='metrics-Pw-synthetic-0123456789'"


def private(path, value, text=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else json.dumps(value))
    os.chmod(path, 0o600)
    return str(path)


class Runner:
    def __init__(self, exit_doc=None, user_exists=False):
        self.calls, self.exit_doc, self.user_exists = [], exit_doc, user_exists

    def __call__(self, command, *, stdin=None, check=True):
        self.calls.append(list(command))
        if command[0] == 'git':
            return (ROOT / command[-1].split(':', 1)[1]).read_text()
        if command[:2] == ['id', '-u']:
            return '998\n' if self.user_exists else ''
        if command[0] == 'systemd-run':
            return json.dumps(self.exit_doc if self.exit_doc is not None else {'gaps': 0, 'gaps_erasing': 0, 'dangling': 0, 'relation_only': 0, 'evidence': '/x'}) + '\n'
        return ''


def setup(tmp_path, **over):
    root = tmp_path / 'root'
    probe = {'schema': 'nexloop-host-health/1', 'host_role': 'data_host', 'timers': [{'name': 'pg-diff', 'timer': 'nexloop-pg-diff.timer', 'service': 'nexloop-pg-diff.service'}]}
    private(root / 'etc/nexloop/host-health.json', probe)
    private(root / 'root/host-health-install.data-host.json', {'schema': 'nexloop-host-health-install/1', 'probe': {'timers': list(probe['timers'])}})
    private(root / 'srv/nexloop/secrets/backup/metrics.dsn', None, DSN + '\n')
    cfg = json.loads((ROOT / 'deploy/examples/relation-gaps-install.data-host.example.json').read_text())
    cfg.update({'release_sha': SHA, 'tenant_id': 'tenant-synthetic-1', **over})
    cfg['metrics_dsn_file'] = str(root / 'srv/nexloop/secrets/backup/metrics.dsn')
    return root, private(tmp_path / 'config.json', cfg)


def trusting(path, *, what):
    pass


def main(root, config, *args, runner=None, stdin=''):
    out, runner = io.StringIO(), runner or Runner()
    code = I.main([config, *args], stdin=io.StringIO(stdin), out=out, runner=runner, root=root, trust=trusting,
                  clock=lambda: datetime(2026, 10, 26, 10, 0))
    return code, out.getvalue(), runner


def test_installs_reruns_and_never_shows_the_dsn(tmp_path):
    root, config = setup(tmp_path)
    code, text, runner = main(root, config)
    assert code == 0, text
    assert 'step 7: timer enabled' in text and 'dry-run: {"dangling": 0, "gaps": 0' in text
    assert runner.calls[-1] == ['systemctl', 'enable', '--now', 'nexloop-relation-read-gaps.timer']
    assert ['useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', 'nexloop-relation-gaps'] in runner.calls
    dsn = root / 'etc/nexloop/relation-gaps/metrics.dsn'
    assert dsn.read_text() == DSN + '\n' and dsn.stat().st_mode & 0o777 == 0o600
    assert 'metrics-Pw' not in text and not any('metrics-Pw' in ' '.join(c) for c in runner.calls)
    service = (root / 'etc/systemd/system/nexloop-relation-read-gaps.service').read_text()
    assert '@' not in ''.join(l for l in service.splitlines() if not l.startswith('#'))
    assert '--tenant tenant-synthetic-1 --evidence-dir /srv/nexloop/evidence/relation-read-gaps' in service and 'ReadWritePaths=/srv/nexloop/evidence/relation-read-gaps' in service
    release = root / 'opt/nexloop/relation-gaps' / SHA
    assert os.readlink(root / 'opt/nexloop/relation-gaps/current') == SHA
    assert (release / 'scripts/acceptance/relation_read_gaps.py').read_text() == (ROOT / 'scripts/acceptance/relation_read_gaps.py').read_text()
    for path, nested in ((root / 'etc/nexloop/host-health.json', False), (root / 'root/host-health-install.data-host.json', True)):
        value = json.loads(path.read_text())
        timers = (value['probe'] if nested else value)['timers']
        assert I.ENTRY in timers and path.stat().st_mode & 0o777 == 0o600
    # A rerun changes nothing: same DSN, no second entry, the user exists.
    code, text, runner = main(root, config, runner=Runner(user_exists=True))
    assert code == 0 and 'metrics DSN unchanged' in text and 'already listed' in text and not any(c[0] == 'useradd' for c in runner.calls)
    assert sum(t['name'] == 'relation-read-gaps' for t in json.loads((root / 'etc/nexloop/host-health.json').read_text())['timers']) == 1


def test_gaps_still_enable_a_refusal_does_not(tmp_path):
    root, config = setup(tmp_path)
    code, text, runner = main(root, config, runner=Runner({'gaps': 2, 'gaps_erasing': 0, 'dangling': 1, 'relation_only': 0, 'evidence': '/x'}))
    assert code == 0 and 'gaps > 0' in text and runner.calls[-1][:2] == ['systemctl', 'enable']
    for doc in ({'refused': '--dsn-file: the read-only nexloop_metrics login only'}, {'gaps': 0, 'evidence': None}):
        root2, config2 = setup(tmp_path / str(len(str(doc))))
        code, text, runner = main(root2, config2, runner=Runner(doc))
        assert code == 1 and not any(c[:2] == ['systemctl', 'enable'] for c in runner.calls), text


def test_the_dsn_from_stdin_and_its_checks(tmp_path):
    root, config = setup(tmp_path)
    code, text, _ = main(root, config, '--dsn-stdin', stdin=DSN + '\n')
    assert code == 0, text
    for bad in ("host='/var/run/postgresql' user='nexloop_api' password='x'", DSN + '\n' + DSN, ''):
        code, text, _ = main(root, config, '--dsn-stdin', stdin=bad)
        assert code == 2 and 'nexloop_metrics' in text and 'password' not in text


def test_the_drill_and_uninstall(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    code, text, runner = main(root, config, '--drill-arm', '3')
    dropin = root / 'etc/systemd/system/nexloop-relation-read-gaps.timer.d/50-drill.conf'
    assert code == 0 and 'OnCalendar=2026-10-26 10:03:00' in dropin.read_text() and ['systemctl', 'restart', 'nexloop-relation-read-gaps.timer'] in runner.calls
    assert main(root, config, '--drill-arm', '0')[0] == 2
    assert main(root, config, '--drill-clear')[0] == 0 and not dropin.exists()
    code, text, runner = main(root, config, '--uninstall')
    assert code == 0 and not (root / 'etc/systemd/system/nexloop-relation-read-gaps.service').exists() and not (root / 'opt/nexloop/relation-gaps').exists()
    assert not (root / 'etc/nexloop/relation-gaps/metrics.dsn').exists()
    assert I.ENTRY not in json.loads((root / 'etc/nexloop/host-health.json').read_text())['timers']


def test_a_malformed_configuration_is_refused(tmp_path):
    root, config = setup(tmp_path, tenant_id='bad tenant')
    assert main(root, config)[0] == 2
    root, config = setup(tmp_path / 'b', evidence_dir='relative/path')
    assert main(root, config)[0] == 2
