"""scripts/deploy/restic_maint_data_host.py (DATA_HOST, owner): the installed restic maintenance unit's forget lines grouped by tag, with a
dry-run preview in the unit's own sandbox before anything is written. A temporary unit file and a scripted runner (no root, no systemd,
no restic); the trust check runs for real against this test's own uid (the directories above tmp_path stand for root's)."""
import io
import json
import os
import stat as st
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import restic_maint_data_host as R  # noqa: E402

EXAMPLE = (ROOT / 'deploy/stage/systemd/nexloop-restic-maint.service.example').read_text()
DEPLOYED = EXAMPLE.replace(R.NEW, R.OLD)  # the deploy days' copy: restic's default grouping
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


class Runner:
    def __init__(self, answer=None, exit_code=0):
        self.calls, self.answer, self.exit_code = [], answer, exit_code

    def __call__(self, command, *, check=True):
        self.calls.append(list(command))
        if command[0] == 'systemd-run':
            if self.exit_code and check:  # as R.run: a non-zero exit is a failure when checked
                raise R.Failed(f'systemd-run exited {self.exit_code}')
            return self.answer if self.answer is not None else json.dumps(GROUPS) + '\n'
        return ''


def unit(tmp_path, text=DEPLOYED):
    systemd = tmp_path / 'etc/systemd/system'
    systemd.mkdir(parents=True)
    path = systemd / 'nexloop-restic-maint.service'
    path.write_text(text)
    os.chmod(path, 0o644)
    return path


def main(path, *args, runner=None):
    out, runner = io.StringIO(), runner or Runner()
    code = R.main(list(args), out=out, runner=runner, unit=str(path), trust=lambda p, *, what: R.root_owned(p, what=what, owners=OWNERS))
    return code, out.getvalue(), runner


def test_previews_in_the_units_sandbox_then_rewrites_only_the_forget_lines(tmp_path):
    path = unit(tmp_path)
    code, text, runner = main(path)
    assert code == 0, text
    assert path.read_text() == EXAMPLE and path.read_text().count(R.NEW) == 2
    assert (path.parent / 'nexloop-restic-maint.service.before-groupby').read_text() == DEPLOYED
    assert st.S_IMODE(path.stat().st_mode) == 0o644 and runner.calls[-1] == ['systemctl', 'daemon-reload']
    previews = [c for c in runner.calls if c[0] == 'systemd-run']
    assert len(previews) == 2 and runner.calls.index(previews[-1]) < runner.calls.index(['systemctl', 'daemon-reload'])
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
    lambda t: t + 'ExecStart=/usr/local/bin/restic --repository-file /x forget --keep-within 14d --keep-monthly 1 --prune\n',  # a third line
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


def test_the_example_is_already_grouped_by_tag():
    assert EXAMPLE.count(R.NEW) == 2 and R.OLD not in EXAMPLE
