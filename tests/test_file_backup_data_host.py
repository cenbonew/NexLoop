"""NX-035 slice 3, step 1: scripts/deploy/file_backup_data_host.py (DATA_HOST, owner) into a temporary root with a scripted runner (no
root, no container, no sshd): the pg_hba template adds exactly the recorder's hostssl line (D2), the in-place write with the
container check, the reload and the rollbacks; the chrooted sftp account (D4): the bind-mount unit, the restricted authorized key,
the sshd drop-in and the effective settings read back (anything else wins in sshd: refused, the drop-in removed); uninstall. The
trust checks run for real against this test's own uid; the directories above tmp_path stand for root's."""
import functools
import io
import json
import os
import re
import stat as st
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import file_backup_data_host as D  # noqa: E402

TEMPLATE = ROOT / 'deploy/stage/config/pg_hba.conf.template'
ADDRESS = '.'.join(('192', '0', '2', '10'))  # TEST-NET-1, built at runtime
VALUES = {'__NEXLOOP_DB__': 'nexloop_stage', '__APP_HOST_ADDR__': ADDRESS, '__APP_PG_ROLES__': 'nexloop_api,nexloop_domain_worker'}
KEY = 'ssh-ed25519 ' + 'B' * 68 + ' nexloop-file-backup'
OWNERS = (0, os.getuid())


@pytest.fixture(autouse=True)
def trust_root_at_tmp_path(tmp_path, monkeypatch):
    """Test only: the directories above this test's tmp_path and above the repository's scripts/deploy stand for root's own."""
    above = set()
    for base in (Path(tmp_path), Path(os.path.realpath(tmp_path)), ROOT / 'scripts/deploy'):
        above |= {str(p) for p in base.parents}
    lstat = os.lstat

    def fake(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) in above:
            return os.stat_result((st.S_IFDIR | 0o755, info.st_ino, info.st_dev, info.st_nlink, 0, 0, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return info
    monkeypatch.setattr(os, 'lstat', fake)


def rendered(template=None):
    text = (template or TEMPLATE.read_text())
    for k, v in VALUES.items():
        text = text.replace(k, v)
    return text


def without_recorder_line(text):
    return '\n'.join(l for l in text.splitlines() if not D.HBA_LINE.fullmatch(' '.join(l.split()))) + '\n'


EFFECTIVE_OK = '\n'.join(f'{k} {v}' for k, v in D.EFFECTIVE.items()) + '\nport 22\nallowusers ops nexloop-backup\n'


class Runner:
    def __init__(self, root, hba, *, effective=EFFECTIVE_OK, container=None, logs='LOG:  received SIGHUP, reloading configuration files\n',
                 template=None, user_uid=None):
        self.calls, self.root, self.hba = [], root, hba
        self.effective, self.container, self.logs, self.template, self.user_uid = effective, container, logs, template, user_uid

    def __call__(self, command, *, stdin=None, check=True):
        self.calls.append(list(command))
        if command[:2] == ['env', '-i']:
            raise AssertionError('no git here')
        if command[1:3] == ['-I', '-B'] and command[3].endswith('render.py'):
            Path(command[command.index('--out') + 1]).write_text(rendered(self.template))
            return ''
        if command[:2] == ['id', '-nG']:
            return 'nexloop-backup docker\n'
        if command[:2] == ['id', '-u']:
            return f'{self.user_uid if self.user_uid is not None else os.getuid()}\n'
        if command[:2] == ['docker', 'compose']:
            if 'cat' in command:
                return self.container(self.hba.read_text()) if self.container else self.hba.read_text()
            return self.logs if 'logs' in command else ''
        if command[:3] == ['systemctl', 'enable', '--now']:
            mount = self.root / 'srv/nexloop-sftp/restic'
            (mount / 'config').write_text('restic repository config (synthetic)\n')
            return ''
        if command[:2] == ['sshd', '-T']:
            return self.effective
        return ''


def setup(tmp_path, **over):
    root = tmp_path / 'root'
    (root / 'srv').mkdir(parents=True)
    (root / 'etc/ssh/sshd_config.d').mkdir(parents=True)
    checkout = tmp_path / 'release'
    (checkout / '.git/refs/heads').mkdir(parents=True)
    (checkout / '.git/HEAD').write_text('ref: refs/heads/main\n')
    (checkout / 'scripts/deploy').mkdir(parents=True)
    (checkout / 'scripts/deploy/render.py').write_text('# synthetic\n')
    (checkout / 'packages/eios-core/src').mkdir(parents=True)
    python = tmp_path / 'usr/bin/python3.12'
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\n')
    os.chmod(python, 0o755)
    nexloop = tmp_path / 'nexloop'
    (nexloop / 'config').mkdir(parents=True)
    hba = nexloop / 'config/pg_hba.conf'
    hba.write_text(without_recorder_line(rendered()))
    stage_env = tmp_path / 'stage.env'
    stage_env.write_text(f'NEXLOOP_DB=nexloop_stage\nAPP_HOST_ADDR={ADDRESS}\n')
    os.chmod(stage_env, 0o600)
    repo = tmp_path / 'backup/files/restic'
    repo.mkdir(parents=True)
    (repo / 'config').write_text('synthetic\n')
    cfg = {'schema': D.SCHEMA, 'release_checkout': str(checkout), 'python': str(python), 'nexloop_root': str(nexloop), 'stage_env': str(stage_env),
           'repo_path': str(repo), 'app_host_public_key': KEY, **over}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(cfg))
    os.chmod(config, 0o600)
    return root, str(config), hba


def main(root, config, hba, *args, runner=None, **kw):
    out, runner = io.StringIO(), runner or Runner(root, hba, **kw)
    code = D.main([config, *args], out=out, runner=runner, root=root, trust=functools.partial(D.A.root_owned, owners=OWNERS), sleep=lambda s: None)
    return code, out.getvalue(), runner


def test_the_template_adds_exactly_the_recorder_line_and_the_pattern_accepts_only_it():
    full = D.rules(rendered())
    before = D.rules(without_recorder_line(rendered()))
    assert [l for l in full if l not in before] == [f'hostssl nexloop_stage nexloop_backup_recorder {ADDRESS}/32 scram-sha-256']
    assert 'local all nexloop_backup_recorder scram-sha-256' in before  # the DATA_HOST socket line stays
    for line, ok in ((f'hostssl nexloop_stage nexloop_backup_recorder {ADDRESS}/32 scram-sha-256', True),
                     ('hostssl nexloop_stage nexloop_backup_recorder 0.0.0.0/0 scram-sha-256', False),
                     ('hostssl nexloop_stage nexloop_backup_recorder 0.0.0.0/32 scram-sha-256', True),  # the address is compared separately
                     (f'hostssl nexloop_stage nexloop_backup_recorder {ADDRESS}/24 scram-sha-256', False),
                     (f'hostssl nexloop_stage nexloop_backup_recorder {ADDRESS}/32 trust', False),
                     (f'host nexloop_stage nexloop_backup_recorder {ADDRESS}/32 scram-sha-256', False),
                     (f'hostssl nexloop_stage nexloop_backup,nexloop_backup_recorder {ADDRESS}/32 scram-sha-256', False)):
        assert bool(D.HBA_LINE.fullmatch(line)) is ok, line


def test_installs_both_parts_and_a_rerun_changes_nothing(tmp_path):
    root, config, hba = setup(tmp_path)
    code, text, runner = main(root, config, hba)
    assert code == 0, text
    assert f'hostssl nexloop_stage nexloop_backup_recorder {ADDRESS}/32 scram-sha-256' in D.rules(hba.read_text())
    assert (hba.parent / 'pg_hba.conf.before-file-backup').read_text() == without_recorder_line(rendered())
    assert ['docker', 'compose', '-p', 'nexloop-data', 'kill', '-s', 'HUP', 'postgres'] in runner.calls
    # The chroot: root's 0755, restic/ bound from the repository by the mount unit.
    chroot = root / 'srv/nexloop-sftp'
    assert st.S_IMODE(chroot.stat().st_mode) == 0o755
    unit = root / 'etc/systemd/system' / 'srv-nexloop\\x2dsftp-restic.mount'
    text_unit = unit.read_text()
    assert f'What={tmp_path}/backup/files/restic\nWhere=/srv/nexloop-sftp/restic\nType=none\nOptions=bind,nosuid,nodev,noexec' in text_unit
    assert ['systemctl', 'enable', '--now', unit.name] in runner.calls
    # The key: restricted to APP_HOST's address; the drop-in: internal-sftp in the chroot, nothing else.
    assert (root / 'etc/ssh/nexloop-authorized-keys/nexloop-backup').read_text() == f'restrict,from="{ADDRESS}" {KEY}\n'
    dropin = (root / 'etc/ssh/sshd_config.d/00-nexloop-file-backup.conf').read_text()
    assert 'Match User nexloop-backup\n' in dropin and dropin.rstrip().endswith('Match all')
    for setting in ('ChrootDirectory /srv/nexloop-sftp', 'ForceCommand internal-sftp -u 0077', 'AuthenticationMethods publickey', 'PermitTTY no',
                    'AllowTcpForwarding no', 'AuthorizedKeysFile /etc/ssh/nexloop-authorized-keys/%u'):
        assert f'    {setting}\n' in dropin, setting
    assert ['sshd', '-t'] in runner.calls and ['sshd', '-T', '-C', f'user=nexloop-backup,host={ADDRESS},addr={ADDRESS}'] in runner.calls
    assert runner.calls.index(['sshd', '-t']) < runner.calls.index(['systemctl', 'try-reload-or-restart', 'ssh.service'])
    assert 'step 1a' in text and 'step 1b' in text and text.rstrip().endswith('(NX-035-file-backup.md §6.1)')
    code, text, runner = main(root, config, hba)
    assert code == 0 and 'already installed' in text and 'unchanged' in text, text
    assert not any('HUP' in c for c in runner.calls) and ['systemctl', 'try-reload-or-restart', 'ssh.service'] not in runner.calls


@pytest.mark.parametrize('installed, why', [
    (lambda text: text + 'hostssl all nexloop_extra 0.0.0.0/0 scram-sha-256\n', 'removes 1 rule line'),
    (lambda text: text.replace('local   all            nexloop_metrics', '#local   all            nexloop_metrics'), 'expected exactly one added rule'),
])
def test_the_pg_hba_preflight_refuses_any_other_difference(tmp_path, installed, why):
    root, config, hba = setup(tmp_path)
    before = installed(hba.read_text())
    hba.write_text(before)
    code, text, runner = main(root, config, hba)
    assert code == 1 and why in text and hba.read_text() == before and not any('HUP' in c for c in runner.calls)


def test_the_recorder_line_must_name_app_host(tmp_path):
    root, config, hba = setup(tmp_path)
    other = '.'.join(('192', '0', '2', '99'))
    code, text, _ = main(root, config, hba, template=TEMPLATE.read_text().replace('nexloop_backup_recorder __APP_HOST_ADDR__', f'nexloop_backup_recorder {other}'))
    assert code == 1 and 'expected exactly one added rule' in text
    # Installed already, with another address than the stage.env's: refused, not taken as done.
    other_template = TEMPLATE.read_text().replace('nexloop_backup_recorder __APP_HOST_ADDR__', f'nexloop_backup_recorder {other}')
    hba.write_text(rendered(other_template))
    code, text, _ = main(root, config, hba, template=other_template)
    assert code == 1 and 'another address than APP_HOST_ADDR' in text


@pytest.mark.parametrize('kw, why', [
    ({'container': lambda text: 'another inode\n'}, 'the container does not see the new file'),
    ({'logs': 'LOG:  received SIGHUP\nLOG:  pg_hba.conf was not reloaded\n'}, 'the reload was not clean'),
])
def test_a_container_or_reload_failure_writes_the_old_content_back(tmp_path, kw, why):
    root, config, hba = setup(tmp_path)
    before, inode = hba.read_text(), os.stat(hba).st_ino
    code, text, runner = main(root, config, hba, **kw)
    assert code == 1 and why in text and hba.read_text() == before and os.stat(hba).st_ino == inode
    assert not (root / 'etc/ssh/sshd_config.d/00-nexloop-file-backup.conf').exists()  # step 1b never started


@pytest.mark.parametrize('effective, why', [
    (EFFECTIVE_OK.replace('passwordauthentication no', 'passwordauthentication yes'), "passwordauthentication is 'yes'"),
    (EFFECTIVE_OK.replace('chrootdirectory /srv/nexloop-sftp\n', ''), "chrootdirectory is 'unset'"),
    (EFFECTIVE_OK.replace('allowusers ops nexloop-backup', 'allowusers ops'), 'AllowUsers does not name nexloop-backup'),
    (EFFECTIVE_OK + 'denyusers nexloop-backup\n', 'DenyUsers names nexloop-backup'),
    # L6 S4: the groups (the stand-in's `id -nG`: nexloop-backup docker).
    (EFFECTIVE_OK + 'allowgroups ops\n', "AllowGroups names none of nexloop-backup's groups"),
    (EFFECTIVE_OK + 'denygroups docker\n', 'DenyGroups names a group of nexloop-backup'),
])
def test_an_effective_sshd_setting_other_than_the_drop_in_removes_it(tmp_path, effective, why):
    root, config, hba = setup(tmp_path)
    code, text, runner = main(root, config, hba, effective=effective)
    assert code == 1 and why in text, text
    assert not (root / 'etc/ssh/sshd_config.d/00-nexloop-file-backup.conf').exists() and not (root / 'etc/ssh/nexloop-authorized-keys/nexloop-backup').exists()
    assert ['systemctl', 'try-reload-or-restart', 'ssh.service'] not in runner.calls


def test_a_failed_check_restores_a_previous_drop_in(tmp_path):
    root, config, hba = setup(tmp_path)
    assert main(root, config, hba)[0] == 0
    dropin = root / 'etc/ssh/sshd_config.d/00-nexloop-file-backup.conf'
    os.chmod(dropin, 0o644)
    dropin.write_text(dropin.read_text() + '# kept\n')
    code, text, _ = main(root, config, hba, effective=EFFECTIVE_OK.replace('permittty no', 'permittty yes'))
    assert code == 1 and dropin.read_text().endswith('# kept\n')


def test_the_repository_and_its_user_are_checked(tmp_path):
    root, config, hba = setup(tmp_path)
    code, text, _ = main(root, config, hba, user_uid=4242)
    assert code == 2 and 'owned by nexloop-backup' in text
    os.unlink(Path(json.loads(Path(config).read_text())['repo_path']) / 'config')
    code, text, _ = main(root, config, hba)
    assert code == 2 and 'restic config file' in text


def test_a_writable_chroot_component_or_checkout_is_refused(tmp_path):
    root, config, hba = setup(tmp_path)
    os.chmod(root / 'srv', 0o777)
    code, text, _ = main(root, config, hba)
    assert code == 2 and 'chroot' in text and not (root / 'etc/ssh/sshd_config.d/00-nexloop-file-backup.conf').exists()
    root, config, hba = setup(tmp_path / 'c')
    os.chmod(tmp_path / 'c/release/scripts/deploy', 0o777)
    code, text, runner = main(root, config, hba)
    assert code == 2 and 'scripts/deploy' in text and runner.calls == []


def test_the_configuration_and_the_stage_env_are_checked(tmp_path):
    for n, over in enumerate(({'app_host_public_key': 'ssh-rsa AAAA'}, {'repo_path': 'relative'}, {'unexpected': 1})):
        root, config, hba = setup(tmp_path / str(n), **over)
        code, text, _ = main(root, config, hba)
        assert code == 2 and 'configuration' in text, over
    root, config, hba = setup(tmp_path / 'e')
    stage_env = Path(json.loads(Path(config).read_text())['stage_env'])
    stage_env.write_text('NEXLOOP_DB=nexloop_stage\n')
    code, text, runner = main(root, config, hba)
    assert code == 2 and 'APP_HOST_ADDR' in text and not any(c[0] == 'docker' for c in runner.calls)


def test_uninstall_removes_the_account_but_keeps_the_pg_hba_line(tmp_path):
    root, config, hba = setup(tmp_path)
    assert main(root, config, hba)[0] == 0
    os.unlink(root / 'srv/nexloop-sftp/restic/config')  # the bind mount gone once the unit is stopped
    code, text, runner = main(root, config, hba, '--uninstall')
    assert code == 0, text
    for gone in ('etc/ssh/sshd_config.d/00-nexloop-file-backup.conf', 'etc/ssh/nexloop-authorized-keys/nexloop-backup', 'srv/nexloop-sftp',
                 'etc/systemd/system/srv-nexloop\\x2dsftp-restic.mount'):
        assert not (root / gone).exists(), gone
    assert ['systemctl', 'disable', '--now', 'srv-nexloop\\x2dsftp-restic.mount'] in runner.calls
    assert f'nexloop_backup_recorder {ADDRESS}/32' in hba.read_text()


def test_the_mount_unit_name_is_systemd_escape():
    assert D.unit_name('/srv/nexloop-sftp/restic') == 'srv-nexloop\\x2dsftp-restic.mount'
    assert D.unit_name('/srv/nexloop sftp/r') == 'srv-nexloop\\x20sftp-r.mount'


def test_the_wrapper_needs_root_and_reads_no_stdin():
    text = (ROOT / 'scripts/deploy/file_backup_data_host.sh').read_text()
    assert 'set -euo pipefail' in text and '</dev/null' in text and re.search(r'id -u\)" -eq 0', text)


def test_the_stage_env_address_must_be_one_ipv4_line(tmp_path):
    for n, body in enumerate((f'APP_HOST_ADDR={ADDRESS}\nAPP_HOST_ADDR={ADDRESS}\n', 'APP_HOST_ADDR=app-host.example\n')):
        root, config, hba = setup(tmp_path / str(n))
        Path(json.loads(Path(config).read_text())['stage_env']).write_text(body)
        code, text, _ = main(root, config, hba)
        assert code == 2 and 'one APP_HOST_ADDR=' in text


def test_a_bind_mount_that_does_not_show_the_repository_stops_before_sshd(tmp_path):
    root, config, hba = setup(tmp_path)
    runner = Runner(root, hba)
    real = runner.__call__

    def no_mount(command, **kw):
        return '' if command[:3] == ['systemctl', 'enable', '--now'] else real(command, **kw)
    code, text, _ = main(root, config, hba, runner=no_mount)
    assert code == 1 and 'the bind mount does not show the repository' in text
    assert not (root / 'etc/ssh/sshd_config.d/00-nexloop-file-backup.conf').exists()


def test_a_writable_authorized_keys_directory_and_alternates_are_refused(tmp_path):
    root, config, hba = setup(tmp_path)
    keys = root / 'etc/ssh/nexloop-authorized-keys'
    keys.mkdir(parents=True)
    os.chmod(keys, 0o777)
    code, text, _ = main(root, config, hba)
    assert code == 2 and 'authorized keys' in text and st.S_IMODE(os.stat(keys).st_mode) == 0o777
    root, config, hba = setup(tmp_path / 'a')
    (tmp_path / 'a/release/.git/objects/info').mkdir(parents=True)
    (tmp_path / 'a/release/.git/objects/info/alternates').write_text('/elsewhere\n')
    code, text, runner = main(root, config, hba)
    assert code == 2 and 'objects from elsewhere' in text and runner.calls == []


def test_allow_groups_naming_one_of_the_users_groups_passes(tmp_path):
    root, config, hba = setup(tmp_path)
    code, text, _ = main(root, config, hba, effective=EFFECTIVE_OK + 'allowgroups ops docker\n')
    assert code == 0, text


def test_a_linked_or_writable_scratch_directory_is_refused_before_any_chmod(tmp_path):
    """L6 I1: /var/lib/nexloop/file-backup-data-host is trusted before root chmods it or writes in it."""
    root, config, hba = setup(tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o755)
    (root / 'var/lib/nexloop').mkdir(parents=True)
    os.symlink(elsewhere, root / 'var/lib/nexloop/file-backup-data-host')
    code, text, runner = main(root, config, hba)
    assert code == 2 and 'scratch directory' in text and st.S_IMODE(elsewhere.stat().st_mode) == 0o755 and list(elsewhere.iterdir()) == []
    assert not any(c[1:3] == ['-I', '-B'] for c in runner.calls)  # render never ran
    root, config, hba = setup(tmp_path / 'w')
    scratch = root / 'var/lib/nexloop/file-backup-data-host'
    scratch.mkdir(parents=True)
    os.chmod(root / 'var/lib/nexloop', 0o777)
    code, text, _ = main(root, config, hba)
    assert code == 2 and 'scratch directory' in text


def test_the_pg_hba_directory_is_trusted_and_a_linked_backup_is_refused(tmp_path):
    """L6 I2: root writes the backup into config/ and rewrites pg_hba there: the directory root's, the backup never a link."""
    root, config, hba = setup(tmp_path)
    os.chmod(hba.parent, 0o777)
    code, text, runner = main(root, config, hba)
    assert code == 2 and 'pg_hba directory' in text and not any(c[1:3] == ['-I', '-B'] for c in runner.calls)
    os.chmod(hba.parent, 0o755)
    target = tmp_path / 'target'
    target.write_text('not pg_hba\n')
    os.symlink(target, hba.parent / 'pg_hba.conf.before-file-backup')
    before = hba.read_text()
    code, text, _ = main(root, config, hba)
    assert code == 2 and 'a symbolic link' in text and target.read_text() == 'not pg_hba\n' and hba.read_text() == before


def test_the_backup_is_written_atomically_and_private(tmp_path):
    root, config, hba = setup(tmp_path)
    before = hba.read_text()
    assert main(root, config, hba)[0] == 0
    backup = hba.parent / 'pg_hba.conf.before-file-backup'
    assert backup.read_text() == before and st.S_IMODE(backup.stat().st_mode) == 0o600 and not backup.is_symlink()


def test_write_in_place_never_follows_a_link(tmp_path):
    target = tmp_path / 'target'
    target.write_text('kept\n')
    link = tmp_path / 'pg_hba.conf'
    os.symlink(target, link)
    with pytest.raises(OSError):
        D.write_in_place(link, 'replaced\n')
    assert target.read_text() == 'kept\n'
