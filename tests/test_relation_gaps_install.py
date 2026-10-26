"""NX-054 P4a: scripts/deploy/relation_gaps_install.py into a temporary root with a scripted runner (no root, no systemd, no
database): install, rerun unchanged, the DSN never in output or argv, the dry run in the unit's sandbox (User/Group, a scratch
ReadWritePaths) and its answers (0 and 1 go on, 2 stops before the timer), the host-health timers gaining relation-read-gaps in both
files (or neither), the drill drop-in, uninstall. The trust checks run for real against this test's own uid (L6 review of 861be53e:
a venv interpreter that is a link chain passes, a group-writable directory on the way is refused; the release checkout, its .git
and the release base are all checked)."""
import functools
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
OWNERS = (0, os.getuid())


def tempfile_dir():
    import tempfile
    return tempfile.gettempdir()


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
        if command[:2] == ['env', '-i']:
            assert command[:8] == ['env', '-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent', 'GIT_CONFIG_NOSYSTEM=1', 'GIT_CONFIG_GLOBAL=/dev/null',
                                   'git', '--no-replace-objects']  # the minimal environment, spelled out (I2)
            return (ROOT / command[-1].split(':', 1)[1]).read_text()
        if command[:2] == ['id', '-u']:
            return '998\n' if self.user_exists else ''
        if command[0] == 'systemd-run':
            return json.dumps(self.exit_doc if self.exit_doc is not None else {'gaps': 0, 'gaps_erasing': 0, 'dangling': 0, 'relation_only': 0, 'evidence': '/x'}) + '\n'
        return ''


def venv(tmp_path):
    """The host-health layout: current -> <sha>, venv/bin/python -> python3 -> a real interpreter file."""
    base = tmp_path / 'opt/nexloop/host-health'
    real = tmp_path / 'usr/bin/python3.12'
    real.parent.mkdir(parents=True)
    real.write_text('#!/bin/sh\n')
    os.chmod(real, 0o755)
    bin_dir = base / SHA / 'venv/bin'
    bin_dir.mkdir(parents=True)
    os.symlink(real, bin_dir / 'python3')
    os.symlink('python3', bin_dir / 'python')
    os.symlink(SHA, base / 'current')
    return str(base / 'current/venv/bin/python')


def setup(tmp_path, **over):
    root = tmp_path / 'root'
    probe = {'schema': 'nexloop-host-health/1', 'host_role': 'data_host', 'timers': [{'name': 'pg-diff', 'timer': 'nexloop-pg-diff.timer', 'service': 'nexloop-pg-diff.service'}]}
    private(root / 'etc/nexloop/host-health.json', probe)
    private(root / 'root/host-health-install.data-host.json', {'schema': 'nexloop-host-health-install/1', 'probe': {'timers': list(probe['timers'])}})
    private(root / 'srv/nexloop/secrets/backup/metrics.dsn', None, DSN + '\n')
    checkout = tmp_path / 'release'
    (checkout / '.git/refs/heads').mkdir(parents=True)
    (checkout / '.git/HEAD').write_text('ref: refs/heads/main\n')
    cfg = json.loads((ROOT / 'deploy/examples/relation-gaps-install.data-host.example.json').read_text())
    cfg.update({'release_sha': SHA, 'tenant_id': 'tenant-synthetic-1', 'release_checkout': str(checkout), 'python': venv(tmp_path), **over})
    cfg['metrics_dsn_file'] = str(root / 'srv/nexloop/secrets/backup/metrics.dsn')
    return root, private(tmp_path / 'config.json', cfg)


def main(root, config, *args, runner=None, stdin='', asked=None):
    out, runner = io.StringIO(), runner or Runner()

    def trust(path, *, what):
        if asked is not None:
            asked.append((what, str(path)))
        I.root_owned(path, what=what, owners=OWNERS)

    code = I.main([config, *args], stdin=io.StringIO(stdin), out=out, runner=runner, root=root, trust=trust,
                  trust_python=functools.partial(I.interpreter_owned, owners=OWNERS), clock=lambda: datetime(2026, 10, 26, 10, 0))
    return code, out.getvalue(), runner


def test_installs_reruns_and_never_shows_the_dsn(tmp_path):
    root, config = setup(tmp_path)
    asked = []
    code, text, runner = main(root, config, asked=asked)
    assert code == 0, text
    whats = {w for w, _ in asked}
    assert {'release_checkout', 'release .git', 'release base'} <= whats  # L6: the mutants that dropped these checks
    assert ('release_checkout', str(tmp_path / 'release')) in asked and ('release .git', str(tmp_path / 'release/.git/HEAD')) in asked
    assert 'step 7: timer enabled' in text and 'dry-run: {"dangling": 0, "gaps": 0' in text
    assert runner.calls[-1] == ['systemctl', 'enable', '--now', 'nexloop-relation-read-gaps.timer']
    dsn = root / 'etc/nexloop/relation-gaps/metrics.dsn'
    assert dsn.read_text() == DSN + '\n' and dsn.stat().st_mode & 0o777 == 0o600 and dsn.parent.stat().st_mode & 0o777 == 0o711
    assert ['chown', 'nexloop-relation-gaps:nexloop-relation-gaps', str(dsn)] in runner.calls
    assert not any(c[:1] == ['chown'] and str(dsn.parent) == c[-1] for c in runner.calls)  # S1: the directory stays root's
    assert 'metrics-Pw' not in text and not any('metrics-Pw' in ' '.join(c) for c in runner.calls)
    service = (root / 'etc/systemd/system/nexloop-relation-read-gaps.service').read_text()
    assert '--tenant tenant-synthetic-1 --evidence-dir /srv/nexloop/evidence/relation-read-gaps' in service
    assert 'CapabilityBoundingSet=' in service and 'RestrictAddressFamilies=AF_UNIX' in service and 'MemoryDenyWriteExecute=true' in service
    # The dry run: the unit's [Service] as properties, its user and group, a scratch ReadWritePaths (never the evidence directory).
    dry = next(c for c in runner.calls if c[0] == 'systemd-run')
    props = [dry[i + 1] for i, a in enumerate(dry) if a == '-p']
    assert 'User=nexloop-relation-gaps' in props and 'Group=nexloop-relation-gaps' in props and 'PrivateTmp=true' in props
    rw = [p for p in props if p.startswith('ReadWritePaths=')]
    # L6 C2: the unit's PrivateTmp hides the host /tmp, so the scratch directory is under /var/lib, never /tmp.
    assert 'PrivateTmp=true' in props and len(rw) == 1 and rw[0].startswith('ReadWritePaths=' + str(root / 'var/lib/nexloop/relation-gaps-dry/run-'))
    assert '/srv/nexloop/evidence' not in rw[0] and not rw[0].split('=', 1)[1].startswith(('/tmp', '/private/tmp', tempfile_dir()))
    dry_dir = root / 'var/lib/nexloop/relation-gaps-dry'
    assert list(dry_dir.iterdir()) == [] and dry_dir.stat().st_mode & 0o777 == 0o711  # the scratch removed; 0711 (I4)
    for path, nested in ((root / 'etc/nexloop/host-health.json', False), (root / 'root/host-health-install.data-host.json', True)):
        value = json.loads(path.read_text())
        assert I.ENTRY in (value['probe'] if nested else value)['timers'] and path.stat().st_mode & 0o777 == 0o600
    code, text, runner = main(root, config, runner=Runner(user_exists=True))
    assert code == 0 and 'metrics DSN unchanged' in text and 'already listed' in text and not any(c[0] == 'useradd' for c in runner.calls)


def test_a_writable_directory_on_the_way_is_refused(tmp_path):
    root, config = setup(tmp_path)
    os.chmod(tmp_path / 'release/.git/refs', 0o777)
    code, text, runner = main(root, config)
    assert code == 2 and 'release .git' in text and not any(c[0] == 'git' for c in runner.calls)
    os.chmod(tmp_path / 'release/.git/refs', 0o755)
    os.chmod(tmp_path / 'opt/nexloop/host-health' / SHA / 'venv/bin', 0o777)  # whoever can write there chooses the interpreter
    code, text, _ = main(root, config)
    assert code == 2 and 'python' in text


def test_a_reused_release_is_checked_file_by_file(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    changed = root / 'opt/nexloop/relation-gaps' / SHA / 'scripts/acceptance/relation_read_gaps.py'
    os.chmod(changed.parent, 0o755)
    os.chmod(changed, 0o644)
    changed.write_text('print("not the commit")\n')
    os.chmod(changed, 0o444)
    code, text, _ = main(root, config)
    assert code == 1 and 'differs from the commit' in text


def test_gaps_still_enable_a_refusal_does_not(tmp_path):
    root, config = setup(tmp_path)
    code, text, runner = main(root, config, runner=Runner({'gaps': 2, 'gaps_erasing': 0, 'dangling': 1, 'relation_only': 0, 'evidence': '/x'}))
    assert code == 0 and 'gaps > 0' in text and runner.calls[-1][:2] == ['systemctl', 'enable']
    for n, doc in enumerate(({'refused': '--dsn-file: the read-only nexloop_metrics login only'}, {'gaps': 0, 'evidence': None})):
        root2, config2 = setup(tmp_path / str(n))
        code, text, runner = main(root2, config2, runner=Runner(doc))
        assert code == 1 and not any(c[:2] == ['systemctl', 'enable'] for c in runner.calls), text


def test_the_dsn_checks(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config, '--dsn-stdin', stdin=DSN + '\n')[0] == 0
    for bad in ("host='/var/run/postgresql' user='nexloop_api' password='x'", DSN + '\n' + DSN, '',
                DSN + " user='nexloop_owner'"):  # S4: exactly one user=
        code, text, _ = main(root, config, '--dsn-stdin', stdin=bad)
        assert code == 2 and 'nexloop_metrics' in text and 'metrics-Pw' not in text


def test_a_malformed_host_health_file_is_refused_and_both_or_neither_change(tmp_path):
    root, config = setup(tmp_path)
    private(root / 'root/host-health-install.data-host.json', {'schema': 'x', 'probe': {'timers': 'not a list'}})
    code, text, _ = main(root, config)
    assert code == 2 and 'probe timers' in text
    assert I.ENTRY not in json.loads((root / 'etc/nexloop/host-health.json').read_text())['timers']  # the first file untouched


def test_the_drill_and_uninstall(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    code, text, runner = main(root, config, '--drill-arm', '3')
    dropin = root / 'etc/systemd/system/nexloop-relation-read-gaps.timer.d/50-drill.conf'
    assert code == 0 and 'OnCalendar=2026-10-26 10:03:00' in dropin.read_text() and ['systemctl', 'restart', 'nexloop-relation-read-gaps.timer'] in runner.calls
    assert main(root, config, '--drill-arm', '0')[0] == 2
    assert main(root, config, '--drill-clear')[0] == 0 and not dropin.exists()
    assert main(root, config, '--drill-arm', '5')[0] == 0
    code, text, runner = main(root, config, '--uninstall')
    assert code == 0 and not dropin.parent.exists()  # L6: the drop-in directory goes too
    assert not (root / 'etc/systemd/system/nexloop-relation-read-gaps.service').exists() and not (root / 'opt/nexloop/relation-gaps').exists()
    assert not (root / 'etc/nexloop/relation-gaps/metrics.dsn').exists() and 'user nexloop-relation-gaps stay' in text
    assert not (root / 'var/lib/nexloop/relation-gaps-dry').exists()
    assert I.ENTRY not in json.loads((root / 'etc/nexloop/host-health.json').read_text())['timers']


def test_a_malformed_configuration_is_refused(tmp_path):
    root, config = setup(tmp_path, tenant_id='bad tenant')
    assert main(root, config)[0] == 2
    root, config = setup(tmp_path / 'b', evidence_dir='relative/path')
    assert main(root, config)[0] == 2


def test_every_hop_of_the_interpreter_link_chain_is_checked(tmp_path, monkeypatch):
    """L6 I3: python -> <a 0777 directory>/py3 -> the real file is refused; a group-writable real file is refused; a link not root's
    is refused (its owner faked: this test cannot chown)."""
    real = tmp_path / 'usr/bin/python3.12'
    real.parent.mkdir(parents=True)
    real.write_text('#!/bin/sh\n')
    os.chmod(real, 0o755)
    loose = tmp_path / 'loose'
    loose.mkdir()
    os.chmod(loose, 0o777)
    os.symlink(real, loose / 'py3')
    good = tmp_path / 'good'
    good.mkdir()
    os.symlink(loose / 'py3', good / 'python')
    check = functools.partial(I.interpreter_owned, what='python', owners=OWNERS)
    with pytest.raises(I.Refused, match='loose'):
        check(str(good / 'python'))
    os.chmod(loose, 0o755)
    check(str(good / 'python'))  # every hop root's (here: this user's) and not writable
    os.chmod(real, 0o775)
    with pytest.raises(I.Refused, match='python3.12'):
        check(str(good / 'python'))
    os.chmod(real, 0o755)
    lstat = os.lstat

    def foreign(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) == str(good / 'python'):
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, 4242, info.st_gid, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return info
    monkeypatch.setattr(os, 'lstat', foreign)
    with pytest.raises(I.Refused, match='a link'):
        check(str(good / 'python'))


def test_a_git_pointer_file_or_alternates_are_refused(tmp_path):
    root, config = setup(tmp_path)
    (tmp_path / 'release/.git/objects/info').mkdir(parents=True)
    (tmp_path / 'release/.git/objects/info/alternates').write_text('/elsewhere/objects\n')
    assert main(root, config)[0] == 2
    root, config = setup(tmp_path / 'w')
    import shutil
    shutil.rmtree(tmp_path / 'w/release/.git')
    (tmp_path / 'w/release/.git').write_text('gitdir: /elsewhere/.git/worktrees/x\n')
    code, text, _ = main(root, config)
    assert code == 2 and 'worktree pointer' in text


def test_the_second_host_health_write_failing_restores_the_first(tmp_path, monkeypatch):
    root, config = setup(tmp_path)
    first = root / 'etc/nexloop/host-health.json'
    before = first.read_text()
    real = I.write_private

    def failing(path, text, mode=0o600):
        if path.name == 'host-health-install.data-host.json':
            raise OSError('disk full')
        return real(path, text, mode)
    monkeypatch.setattr(I, 'write_private', failing)
    code, text, runner = main(root, config)
    assert code == 1 and first.read_text() == before and not any(c[:2] == ['systemctl', 'enable'] for c in runner.calls)


def test_uninstall_refuses_a_malformed_host_health_file_before_removing_anything(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    private(root / 'root/host-health-install.data-host.json', {'schema': 'x', 'probe': {'timers': 'not a list'}})
    code, text, runner = main(root, config, '--uninstall')
    assert code == 2 and (root / 'etc/systemd/system/nexloop-relation-read-gaps.service').exists() and runner.calls == []


def test_a_linked_dry_run_directory_is_refused(tmp_path):
    """L6 I4: /var/lib/nexloop/relation-gaps-dry as a link elsewhere: refused before any chmod or chown reaches the target."""
    root, config = setup(tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o700)
    (root / 'var/lib/nexloop').mkdir(parents=True)
    os.symlink(elsewhere, root / 'var/lib/nexloop/relation-gaps-dry')
    code, text, runner = main(root, config)
    assert code == 2 and 'dry-run directory' in text and elsewhere.stat().st_mode & 0o777 == 0o700 and list(elsewhere.iterdir()) == []
    assert not any(c[0] == 'systemd-run' for c in runner.calls)
    code, text, runner = main(root, config, '--uninstall')
    assert code == 2 and runner.calls == []


def test_commondir_alone_is_refused(tmp_path):
    root, config = setup(tmp_path)
    (tmp_path / 'release/.git/commondir').write_text('../elsewhere\n')
    code, text, _ = main(root, config)
    assert code == 2 and 'commondir' in text


def test_the_root_directory_is_checked_and_a_link_loop_ends(tmp_path, monkeypatch):
    import signal
    check = functools.partial(I.interpreter_owned, what='python', owners=OWNERS)
    loop = tmp_path / 'loop'
    loop.mkdir()
    os.symlink(loop / 'b', loop / 'a')
    os.symlink(loop / 'a', loop / 'b')

    def stop(*_):
        raise TimeoutError('the link loop did not end')
    previous = signal.signal(signal.SIGALRM, stop)
    signal.alarm(5)
    try:
        with pytest.raises(I.Refused, match='a link'):
            check(str(loop / 'a'))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    lstat = os.lstat

    def writable_root(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) == '/':
            return os.stat_result((info.st_mode | 0o002, info.st_ino, info.st_dev, info.st_nlink, info.st_uid, info.st_gid, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return info
    monkeypatch.setattr(os, 'lstat', writable_root)
    with pytest.raises(I.Refused, match='/ must be owned'):
        check(sys.executable)
