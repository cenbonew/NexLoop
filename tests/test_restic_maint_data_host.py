"""scripts/deploy/restic_maint_data_host.py (DATA_HOST, owner): the installed restic maintenance unit's forget lines grouped by tag, with a
dry-run preview in the unit's own sandbox before anything is written. A temporary unit file and a scripted runner (no root, no systemd,
no restic); the trust check runs for real against this test's own uid (the directories above tmp_path stand for root's). The guard's
source is a copy in tmp_path (L6 S3: the checkout's own path - /tmp, say - is never trusted here)."""
import io
import json
import os
import shutil
import stat as st
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import restic_maint_data_host as R  # noqa: E402
import restic_future_guard as G  # noqa: E402
import rest_server_data_host as S  # noqa: E402
from support.writable_tree import make_writable  # noqa: E402

EXAMPLE = (ROOT / 'deploy/stage/systemd/nexloop-restic-maint.service.example').read_text()
DEPLOYED = R.reverted(EXAMPLE)  # the deploy days' copy: restic's default grouping, no guard
GROUPED = R.reverted(EXAMPLE).replace(R.OLD, R.NEW)  # this script's first version (group-by, no guard yet)
GUARD_OK = json.dumps({'ok': True, 'snapshots': 4, 'future': [], 'unreadable': 0})
OWNERS = (0, os.getuid())
GROUPS = [{'host': 'nexloop', 'tags': ['journal'], 'paths': ['/srv/nexloop-backup/files/journal'], 'keep': [{'id': 'a'}] * 3, 'remove': [{'id': 'b'}]},
          {'host': 'nexloop', 'tags': ['files_artifact'], 'paths': ['/x/stage-1', '/x/stage-2'], 'keep': [{'id': 'c'}] * 2, 'remove': None}]


@pytest.fixture(autouse=True)
def trust_root_at_tmp_path(tmp_path, monkeypatch):
    above = {str(p) for p in Path(tmp_path).parents} | {str(p) for p in Path(os.path.realpath(tmp_path)).parents}
    lstat = os.lstat

    def fake(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) in above:
            return os.stat_result((st.S_IFDIR | 0o755, info.st_ino, info.st_dev, info.st_nlink, 0, 0, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return info
    monkeypatch.setattr(os, 'lstat', fake)
    yield
    make_writable(tmp_path)  # the guard's directory is made read-only (support.writable_tree)


class Runner:
    def __init__(self, answer=None, exit_code=0, guard_code=0, rest_server='active', guard=GUARD_OK):
        self.calls, self.answer, self.exit_code, self.guard_code, self.rest_server = [], answer, exit_code, guard_code, rest_server
        self.guard = guard

    def __call__(self, command, *, check=True):
        self.calls.append(list(command))
        if command[0] == 'systemd-run' and any(a.endswith('restic_future_guard.py') for a in command):
            if self.guard_code and check:
                raise R.Failed(f'systemd-run exited {self.guard_code}')
            return self.guard + '\n'
        if command[0] == 'systemd-run':
            if self.exit_code and check:  # as R.run: a non-zero exit is a failure when checked
                raise R.Failed(f'systemd-run exited {self.exit_code}')
            return self.answer if self.answer is not None else json.dumps(GROUPS) + '\n'
        if command == ['systemctl', 'is-active', R.REST_UNIT]:
            return self.rest_server + '\n'
        return ''


def unit(tmp_path, text=DEPLOYED):
    systemd = tmp_path / 'etc/systemd/system'
    systemd.mkdir(parents=True)
    path = systemd / 'nexloop-restic-maint.service'
    path.write_text(text)
    os.chmod(path, 0o644)
    return path


def main(path, *args, runner=None, systemctl=True):
    out, runner = io.StringIO(), runner or Runner()
    root = path.parents[3]  # <tmp>/etc/systemd/system/<unit>: the guard goes to <tmp>/opt/nexloop/restic-maint
    source = root / 'release/scripts/deploy/restic_future_guard.py'  # L6 S3: the release's guard, copied below tmp_path
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'scripts/deploy/restic_future_guard.py', source)
    if systemctl:
        (root / 'usr/bin').mkdir(parents=True, exist_ok=True)
        (root / 'usr/bin/systemctl').write_text('#!/bin/sh\n')
    import file_backup_install as A
    code = R.main(list(args), out=out, runner=runner, unit=str(path), trust=lambda p, *, what: R.root_owned(p, what=what, owners=OWNERS),
                  root=str(root), trust_python=lambda p, *, what: A.interpreter_owned(p, what=what, owners=OWNERS), guard_source=source)
    return code, out.getvalue(), runner


def test_previews_in_the_units_sandbox_then_rewrites_only_the_forget_lines(tmp_path):
    path = unit(tmp_path)
    code, text, runner = main(path)
    assert code == 0, text
    assert path.read_text() == EXAMPLE and path.read_text().count(R.NEW) == 2
    assert (path.parent / 'nexloop-restic-maint.service.before-groupby').read_text() == DEPLOYED
    assert st.S_IMODE(path.stat().st_mode) == 0o644 and runner.calls[-1] == ['systemctl', 'daemon-reload']
    guards = [c for c in runner.calls if c[0] == 'systemd-run' and any(a.endswith('restic_future_guard.py') for a in c)]
    previews = [c for c in runner.calls if c[0] == 'systemd-run' and c not in guards]
    assert len(previews) == 2 and runner.calls.index(previews[-1]) < runner.calls.index(['systemctl', 'daemon-reload'])
    # L6 F1: each forget is previewed after its guard, and the unit runs the guard before it.
    assert len(guards) == 2 and all(runner.calls.index(g) < runner.calls.index(f) for g, f in zip(guards, previews))
    assert guards[0][guards[0].index('--') + 1:] == ['/usr/local/bin/restic', '--no-cache', '--repository-file', '${RESTIC_REPO1_REPOSITORY_FILE}',
                                                  '--password-file', '${RESTIC_REPO1_PASSWORD_FILE}']
    guard = tmp_path / 'opt/nexloop/restic-maint/restic_future_guard.py'
    assert guard.read_text() == (ROOT / 'scripts/deploy/restic_future_guard.py').read_text() and st.S_IMODE(guard.stat().st_mode) == 0o444
    assert st.S_IMODE(guard.parent.stat().st_mode) == 0o555
    for command in previews:
        assert '--expand-environment=yes' in command[:command.index('-p')]  # L6 S1: the ${VAR} paths expanded explicitly
        props = [command[i + 1] for i, a in enumerate(command) if a == '-p']
        assert {'User=nexloop-backup', 'Group=nexloop-backup', 'EnvironmentFile=/etc/nexloop/restic.env', 'ProtectSystem=strict'} <= set(props)
        line = ' '.join(command[command.index('/usr/local/bin/restic'):])
        assert 'forget --retry-lock 10m --group-by host,tags --keep-within 14d --keep-monthly 1 --dry-run --json' in line and '--prune' not in line and line.count('--retry-lock') == 1
    assert '$RESTIC_REPO2_OPTIONS' in previews[1] and '${RESTIC_REPO2_REPOSITORY_FILE}' in previews[1]
    assert 'preview repo1: {"host": "nexloop", "keep": 3, "remove": 1, "tags": ["journal"]}' in text
    assert 'preview repo2: {"host": "nexloop", "keep": 2, "remove": 0, "tags": ["files_artifact"]}' in text
    assert '/x/stage-1' not in text  # counts and tags, not the snapshots' paths
    code, text, runner = main(path)
    assert code == 0 and 'nothing to do' in text and runner.calls == []


def test_revert_puts_restic_default_grouping_back_without_a_preview(tmp_path):
    path = unit(tmp_path, EXAMPLE)
    code, text, runner = main(path, '--revert')
    assert code == 0 and path.read_text() == DEPLOYED and not any(c[0] == 'systemd-run' for c in runner.calls)


@pytest.mark.parametrize('answer', ['', 'not json\n', '{"not": "a list"}\n'])
def test_a_failed_preview_writes_nothing(tmp_path, answer):
    path = unit(tmp_path)
    code, text, runner = main(path, runner=Runner(answer))
    assert code == 1 and 'nothing written' in text and path.read_text() == DEPLOYED
    assert not (path.parent / 'nexloop-restic-maint.service.before-groupby').exists() and ['systemctl', 'daemon-reload'] not in runner.calls


def test_a_preview_exiting_non_zero_writes_nothing_even_with_a_json_last_line(tmp_path):
    """L6 S3: restic printed its groups, then failed (or systemd-run refused --expand-environment): the exit code decides."""
    path = unit(tmp_path)
    code, text, runner = main(path, runner=Runner(exit_code=1))
    assert 'systemd-run exited 1' in text and 'older than 254' in text
    assert code == 1 and 'nothing written' in text and path.read_text() == DEPLOYED
    assert not (path.parent / 'nexloop-restic-maint.service.before-groupby').exists() and ['systemctl', 'daemon-reload'] not in runner.calls


@pytest.mark.parametrize('edit', [
    lambda t: t.replace('--keep-within 14d --keep-monthly 1 --prune', '--keep-within 30d --keep-monthly 1 --prune', 1),  # hand-tuned retention
    lambda t: t + 'ExecStart=/usr/local/bin/restic --repository-file /x forget --keep-within 14d --keep-monthly 1 --prune\n' * 2,  # a 3rd and 4th
    lambda t: t.replace(R.OLD, R.NEW, 1),                                                                                    # half done
    lambda t: t + '# forget --keep-within 14d --keep-monthly 1 --prune\n',                                                  # also in a comment
    lambda t: t.replace('UMask=0077', 'UMask=0077 \\'),                                                                     # a continuation
])
def test_a_hand_edited_unit_is_refused_untouched(tmp_path, edit):
    path = unit(tmp_path, edit(DEPLOYED))
    before = path.read_text()
    code, text, runner = main(path)
    assert code == 2 and path.read_text() == before and runner.calls == [], text


def test_a_link_or_a_writable_directory_is_refused(tmp_path):
    path = unit(tmp_path)
    os.chmod(path.parent, 0o777)
    code, text, runner = main(path)
    assert code == 2 and 'must be owned by root' in text and runner.calls == []
    os.chmod(path.parent, 0o755)
    real = path.with_name('real.service')
    os.rename(path, real)
    os.symlink(real, path)
    code, text, _ = main(path)
    assert code == 2 and 'symbolic link' in text
    os.unlink(path)
    os.rename(real, path)
    os.symlink(tmp_path / 'elsewhere', path.with_name('nexloop-restic-maint.service.before-groupby'))
    code, text, _ = main(path)
    assert code == 2 and 'before-groupby is a symbolic link' in text and path.read_text() == DEPLOYED


def test_the_example_is_already_grouped_by_tag_and_guarded():
    assert EXAMPLE.count(R.NEW) == 2 and R.OLD not in EXAMPLE and R.guarded(EXAMPLE) == EXAMPLE
    lines = [l for l in EXAMPLE.splitlines() if l.startswith('ExecStart=')]
    for i, line in enumerate(lines):
        if ' forget ' in line and not R.is_guard(line):
            assert R.is_guard(lines[i - 1]) and lines[i - 1].endswith(line[len('ExecStart='):line.index(' forget ')])


def test_the_first_version_gains_the_guards_and_a_future_snapshot_stops_the_rewrite(tmp_path):
    """L6 F1: a unit already grouped by tag (this script's first version) gains a guard before each forget; when a guard finds a
    future-dated snapshot nothing is written."""
    path = unit(tmp_path, GROUPED)
    code, text, runner = main(path, runner=Runner(guard_code=1))
    assert code == 1 and 'future-snapshot guard refused' in text and path.read_text() == GROUPED
    assert not any(c[0] == 'systemd-run' and not any(a.endswith('restic_future_guard.py') for a in c) for c in runner.calls)  # no forget preview
    code, text, runner = main(path)
    assert code == 0 and path.read_text() == EXAMPLE, text


def files_setup(tmp_path):
    repo = tmp_path / 'backups/files-repo1/restic'
    repo.mkdir(parents=True)
    (repo / 'config').write_text('synthetic\n')
    password = tmp_path / 'secrets/files-repo/restic.pass'
    password.parent.mkdir(parents=True)
    password.write_text('synthetic\n')
    return repo, password, f'/usr/local/bin/restic --no-cache -r {repo} --password-file {password}'


def test_the_files_repository_gets_its_own_guarded_maintenance_once(tmp_path):
    """L6 F1 (b): the files repository the rest-server serves, maintained here like the others: guard, forget per tag, check, the
    Sunday deep check, and writable to the unit."""
    repo, password, common = files_setup(tmp_path)
    path = unit(tmp_path)
    code, text, runner = main(path, '--files-repo', str(repo), '--files-password-file', str(password))
    assert code == 0, text
    now = path.read_text()
    for line in (f'ExecStart={R.GUARD_CALL}{common}', f'ExecStart={common} {R.NEW}', f'ExecStart={common} check --retry-lock 10m', f'ReadWritePaths={repo}'):
        assert now.count(line + '\n') == 1, line
    assert now.index(f'ExecStart={R.GUARD_CALL}{common}') < now.index(f'ExecStart={common} {R.NEW}') < now.index(f'ReadWritePaths={repo}')
    assert len([c for c in runner.calls if c[0] == 'systemd-run']) == 6  # three guards, three forget previews
    assert not any('stop' in c for c in runner.calls)  # the preview never stops the rest-server
    assert not any(a.startswith('ExecStopPost=') for c in runner.calls for a in c)  # nor runs ExecStopPost in its sandbox
    code, text, runner = main(path, '--files-repo', str(repo), '--files-password-file', str(password))
    assert code == 0 and 'nothing to do' in text
    code, text, runner = main(path, '--revert')
    assert code == 0 and path.read_text() == DEPLOYED and str(repo) not in path.read_text()


def test_the_rest_server_is_stopped_before_the_guard_and_started_after_forget_on_every_path(tmp_path):
    """L6 F2: the order is fixed - stale locks removed, no lock held, the rest-server stopped, the guard, forget/prune, the rest-server
    started, the checks; ExecStopPost starts it on every path and fails the unit unless it is active 3 seconds later."""
    repo, password, common = files_setup(tmp_path)
    path = unit(tmp_path)
    assert main(path, '--files-repo', str(repo), '--files-password-file', str(password))[0] == 0
    service = [(k, v) for s, k, v in R.sections(path.read_text()) if s == 'Service']
    lines = path.read_text().splitlines()
    after = lines[lines.index(R.FILES_MARK) + 1:]
    block = [tuple(line.split('=', 1)) for line in after[:next((i for i, l in enumerate(after) if not l.startswith(R.FILES_KEYS)), len(after))]]
    assert block == [
        ('ExecStart', f'{common} unlock'),
        ('ExecStart', f'{R.PYTHON} -I -B {R.GUARD} --wait-unlocked 600 -- {common}'),
        ('ExecStart', f'+/usr/bin/systemctl stop {S.UNIT}'),
        ('ExecStart', f'{R.GUARD_CALL}{common}'),
        ('ExecStart', f'{common} {R.NEW}'),
        ('ExecStart', f'+/usr/bin/systemctl start {S.UNIT}'),
        ('ExecStart', f'{common} check --retry-lock 10m'),
        ('ExecStart', f'/bin/sh -c \'if [ "$$(date +%%u)" = 7 ]; then exec {common} check --read-data-subset 1/10; fi\''),
        ('ExecStopPost', f'+/usr/bin/systemctl start {S.UNIT}'),
        ('ExecStopPost', '/bin/sleep 3'),
        ('ExecStopPost', f'+/usr/bin/systemctl is-active --quiet {S.UNIT}'),
        ('ReadWritePaths', str(repo)),
        ('TimeoutStartSec', '6h'),  # L6 S5: a hanging forget or prune does not keep the rest-server stopped
    ]
    # Only this block touches the rest-server; the journal repositories' lines are as before, and the block runs after them.
    keys = [k for k, v in service]
    assert keys.count('ExecStopPost') == 3 and [v for k, v in service if R.REST_UNIT in v and k == 'ExecStart'] == [block[2][1], block[5][1]]
    assert service.index(block[0]) > max(i for i, (k, v) in enumerate(service) if 'RESTIC_REPO2' in v)


NOW = datetime(2026, 10, 27, 3, 30, tzinfo=UTC)
FUTURE = {'id': 'f' * 64, 'short_id': 'ffffffff', 'time': '2030-01-01T00:00:00Z', 'tags': ['files_artifact']}


class Host:
    """DATA_HOST running the unit's lines for the files repository in order, against a model: the rest-server up or stopped, the
    repository's snapshots (one a day for 30 days), and APP_HOST adding a 2030 snapshot through the server the moment the guard has passed -
    while forget waits for the lock APP_HOST holds (L6 F2). forget is restic's rule: per tag, the newest snapshot's last 14 days and
    the newest of its month are kept."""
    def __init__(self, repo):
        self.repo, self.up = str(repo), True
        self.snapshots = [{'id': f'{i:064x}', 'short_id': f'{i:08x}', 'time': (NOW - timedelta(days=i)).isoformat(), 'tags': ['files_artifact']}
                          for i in range(30)]

    def forget(self):
        newest = max(G.parse_time(s['time']) for s in self.snapshots)
        month = max((s for s in self.snapshots), key=lambda s: G.parse_time(s['time']))['id']
        return sorted(s['id'] for s in self.snapshots if G.parse_time(s['time']) < newest - timedelta(days=14) and s['id'] != month)

    def run(self, text, attack):
        removed, failed = None, False
        for key, value in [(k, v) for s, k, v in R.sections(text) if s == 'Service' and k in ('ExecStart', 'ExecStopPost')]:
            if (self.repo not in value and R.REST_UNIT not in value) or (key == 'ExecStart' and failed):
                continue
            argv = value.lstrip('+').split()
            if argv[0] == R.SYSTEMCTL and argv[1] in ('stop', 'start'):
                self.up = argv[1] == 'start'
            elif R.is_guard('ExecStart=' + value):
                failed = not G.check(self.snapshots, NOW, 3600)['ok']
                if attack and self.up:  # APP_HOST's write reaches the repository only through the rest-server
                    self.snapshots.append(dict(FUTURE))
            elif ' forget ' in value:
                removed = self.forget()
        return removed, self.up


def test_a_future_snapshot_added_after_the_guard_does_not_change_what_forget_removes(tmp_path):
    """L6 F2: with the rest-server stopped before the guard, a snapshot APP_HOST tries to add between the guard and forget never
    arrives, and forget removes exactly what it would have removed anyway; 55271922's block (no stop) lost every real snapshot."""
    repo, password, common = files_setup(tmp_path)
    old_block = (f'{R.FILES_MARK}\nExecStart={R.GUARD_CALL}{common}\nExecStart={common} {R.NEW}\nExecStart={common} check --retry-lock 10m\n'
                 f'ReadWritePaths={repo}\n')
    before = R.guarded(DEPLOYED.replace('UMask=0077\n', 'UMask=0077\n' + old_block))
    honest = Host(repo).run(before, attack=False)[0]
    assert len(honest) == 15  # days 15-29: older than the newest snapshot's 14 days
    attacked, _ = Host(repo).run(before, attack=True)
    assert len(attacked) == 30 and set(honest) < set(attacked)  # the model does show the race: every real snapshot goes
    path = unit(tmp_path, before)  # the earlier block is replaced, not kept beside the new one
    assert main(path, '--files-repo', str(repo), '--files-password-file', str(password))[0] == 0
    now = path.read_text()
    assert now.count(R.FILES_MARK) == 1 and now.count(f'ExecStart={common} {R.NEW}\n') == 1
    host = Host(repo)
    assert host.run(now, attack=True) == (honest, True) and FUTURE['id'] not in [s['id'] for s in host.snapshots]
    assert Host(repo).run(now, attack=False) == (honest, True)


def test_a_failing_guard_still_starts_the_rest_server_again(tmp_path):
    repo, password, common = files_setup(tmp_path)
    path = unit(tmp_path)
    assert main(path, '--files-repo', str(repo), '--files-password-file', str(password))[0] == 0
    host = Host(repo)
    host.snapshots.append(dict(FUTURE, id='e' * 64))  # already there before the run: the guard refuses, forget never runs
    assert host.run(path.read_text(), attack=False) == (None, True)


def test_a_unit_with_its_own_start_timeout_is_refused_with_the_files_repository(tmp_path):
    """L6 S5: the block's TimeoutStartSec=6h is the unit's only one (systemd would take the last of two)."""
    repo, password, common = files_setup(tmp_path)
    path = unit(tmp_path, DEPLOYED.replace('UMask=0077', 'TimeoutStartSec=infinity\nUMask=0077'))
    before = path.read_text()
    code, text, runner = main(path, '--files-repo', str(repo), '--files-password-file', str(password))
    assert code == 2 and 'TimeoutStartSec' in text and path.read_text() == before
    assert main(path)[0] == 0  # without the files repository the unit's own timeout is left as it is
    assert 'TimeoutStartSec=infinity' in path.read_text()


def test_a_guard_source_others_can_write_is_refused(tmp_path):
    path = unit(tmp_path)
    source = tmp_path / 'release/scripts/deploy'
    source.mkdir(parents=True)
    os.chmod(source, 0o777)
    code, text, runner = main(path)
    assert code == 2 and 'guard source' in text and path.read_text() == DEPLOYED and not (tmp_path / 'opt/nexloop/restic-maint').exists()


def test_the_files_repository_arguments_are_checked(tmp_path):
    path = unit(tmp_path)
    password = tmp_path / 'p'
    password.write_text('synthetic\n')
    for args in (['--files-repo', str(tmp_path / 'absent')], ['--files-repo', str(tmp_path / 'absent'), '--files-password-file', str(password)],
                 ['--files-repo', str(tmp_path), '--files-password-file', str(tmp_path / 'absent')],
                 ['--revert', '--files-repo', str(tmp_path), '--files-password-file', str(tmp_path)]):
        code, text, runner = main(path, *args)
        assert code == 2 and path.read_text() == DEPLOYED and not any(c[0] == 'systemd-run' for c in runner.calls), args
    repo, password, common = files_setup(tmp_path / 'f')
    args = ['--files-repo', str(repo), '--files-password-file', str(password)]
    code, text, runner = main(path, *args, runner=Runner(rest_server='inactive'))
    assert code == 2 and 'is not active' in text and path.read_text() == DEPLOYED and not any(c[0] == 'systemd-run' for c in runner.calls)
    os.unlink(tmp_path / 'usr/bin/systemctl')
    code, text, runner = main(path, *args, systemctl=False)
    assert code == 2 and '/usr/bin/systemctl missing' in text and path.read_text() == DEPLOYED


EMPTY = json.dumps({'future': [], 'ok': True, 'snapshots': 0, 'unreadable': 0})


@pytest.mark.parametrize('answer', ['', '\n', 'null\n', '[]\n', 'Applying Policy: keep 1 monthly snapshots and all within 14d\n'])
def test_an_empty_files_repository_has_nothing_to_remove_and_gets_its_block(tmp_path, answer):
    """DATA_HOST 2026-10-27: the run sheet adds the block before APP_HOST's first backup, so the files repository is empty; restic
    0.18.0 forget --json prints no JSON without a group. With the guard's count 0 and exit 0 that is "no snapshot", not a failure."""
    repo, password, common = files_setup(tmp_path)
    path = unit(tmp_path)
    code, text, runner = main(path, '--files-repo', str(repo), '--files-password-file', str(password), runner=Runner(answer, guard=EMPTY))
    assert code == 0, text
    assert text.count('preview repo1: no snapshot') == 1 and text.count('preview files: no snapshot') == 1
    assert path.read_text().count(f'ExecStart={common} {R.NEW}\n') == 1 and ['systemctl', 'daemon-reload'] in runner.calls
    # the guard's JSON line is its last one, as printed after "guard: "
    code, text, _ = main(unit(tmp_path / 'again'), runner=Runner(answer, guard='a line before it\n' + EMPTY))
    assert code == 0 and text.count('guard: ' + EMPTY) == 2, text


@pytest.mark.parametrize('guard, answer, exit_code', [
    (GUARD_OK, '', 0),                                  # snapshots there, but no answer: as before, a failure
    (GUARD_OK, 'null\n', 0),
    (json.dumps({'ok': True}), '', 0),                   # a guard line without its count proves nothing
    (json.dumps({'ok': True, 'snapshots': '0'}), '', 0),
    (json.dumps({'ok': True, 'snapshots': False}), '', 0),  # False == 0 in Python: the count must be an int
    ('', '', 0),
    (EMPTY, '', 1),                                     # empty, but restic failed (a lock held 10 minutes, a wrong password)
    (EMPTY, 'Fatal: wrong password or no key found\n', 0),
    (EMPTY, '{"not": "a list"}\n', 0)])
def test_an_empty_answer_counts_as_no_snapshot_only_after_a_guard_count_of_0_and_exit_0(tmp_path, guard, answer, exit_code):
    repo, password, common = files_setup(tmp_path)
    path = unit(tmp_path)
    code, text, runner = main(path, '--files-repo', str(repo), '--files-password-file', str(password),
                              runner=Runner(answer, exit_code=exit_code, guard=guard))
    assert code == 1 and 'nothing written' in text and path.read_text() == DEPLOYED, text
    assert ['systemctl', 'daemon-reload'] not in runner.calls
