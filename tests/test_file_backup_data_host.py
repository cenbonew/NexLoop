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


UIDS = {}  # test only: inode -> the uid lstat and fstat report for it (a deployment user's files without root)
DEPLOY = 4242


@pytest.fixture(autouse=True)
def trust_root_at_tmp_path(tmp_path, monkeypatch):
    """Test only: the directories above this test's tmp_path and above the repository's scripts/deploy stand for root's own."""
    above = set()
    for base in (Path(tmp_path), Path(os.path.realpath(tmp_path)), ROOT / 'scripts/deploy'):
        above |= {str(p) for p in base.parents}
    lstat, fstat = os.lstat, os.fstat
    UIDS.clear()

    def owned(info):
        if info.st_ino not in UIDS:
            return info
        return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, UIDS[info.st_ino], DEPLOY + 1, info.st_size,
                               int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))

    def fake(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) in above:
            return os.stat_result((st.S_IFDIR | 0o755, info.st_ino, info.st_dev, info.st_nlink, 0, 0, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return owned(info)
    monkeypatch.setattr(os, 'lstat', fake)
    monkeypatch.setattr(os, 'fstat', lambda fd: owned(fstat(fd)))


def rendered(template=None):
    text = (template or TEMPLATE.read_text())
    for k, v in VALUES.items():
        text = text.replace(k, v)
    return text


def without_recorder_line(text):
    return '\n'.join(l for l in text.splitlines() if not D.HBA_LINE.fullmatch(' '.join(l.split()))) + '\n'




class Runner:
    def __init__(self, root, hba, *, effective='', container=None, logs='LOG:  received SIGHUP, reloading configuration files\n',
                 template=None, user_uid=None, during_render=None):
        self.calls, self.root, self.hba = [], root, hba
        self.effective, self.container, self.logs, self.template, self.user_uid = effective, container, logs, template, user_uid
        self.during_render = during_render

    def __call__(self, command, *, stdin=None, check=True):
        self.calls.append(list(command))
        if command[:2] == ['env', '-i']:
            raise AssertionError('no git here')
        if command[1:3] == ['-I', '-B'] and command[3].endswith('render.py'):
            Path(command[command.index('--out') + 1]).write_text(rendered(self.template))
            if self.during_render:
                self.during_render(self.hba)
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
    cfg = {'schema': D.SCHEMA, 'release_checkout': str(checkout), 'python': str(python), 'nexloop_root': str(nexloop), 'stage_env': str(stage_env),
           **over}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(cfg))
    os.chmod(config, 0o600)
    return root, str(config), hba


def main(root, config, hba, *args, runner=None, writer=None, **kw):
    out, runner = io.StringIO(), runner or Runner(root, hba, **kw)
    code = D.main([config, *args], out=out, runner=runner, root=root, trust=functools.partial(D.A.root_owned, owners=OWNERS), sleep=lambda s: None,
                  **(writer or {}))
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



def test_installs_the_pg_hba_line_only_and_a_rerun_changes_nothing(tmp_path):
    """D4 changed (owner 2026-10-27): no sftp account, chroot, authorized key or sshd drop-in is installed any more."""
    root, config, hba = setup(tmp_path)
    code, text, runner = main(root, config, hba)
    assert code == 0, text
    assert f'hostssl nexloop_stage nexloop_backup_recorder {ADDRESS}/32 scram-sha-256' in D.rules(hba.read_text())
    assert (hba.parent / 'pg_hba.conf.before-file-backup').read_text() == without_recorder_line(rendered())
    assert ['docker', 'compose', '-p', 'nexloop-data', 'kill', '-s', 'HUP', 'postgres'] in runner.calls
    for absent in ('srv/nexloop-sftp', 'etc/ssh/nexloop-authorized-keys', 'etc/ssh/sshd_config.d/00-nexloop-file-backup.conf',
                   'etc/systemd/system/srv-nexloop\\x2dsftp-restic.mount'):
        assert not (root / absent).exists(), absent
    assert not any(c[0] in ('sshd', 'systemctl') for c in runner.calls)
    assert 'step 1a' in text and 'rest_server_data_host.sh' in text
    code, text, runner = main(root, config, hba)
    assert code == 0 and 'already installed' in text and not any('HUP' in c for c in runner.calls), text


def test_a_writable_checkout_tree_or_alternates_are_refused(tmp_path):
    root, config, hba = setup(tmp_path)
    os.chmod(tmp_path / 'release/scripts/deploy', 0o777)
    code, text, runner = main(root, config, hba)
    assert code == 2 and 'scripts/deploy' in text and runner.calls == []
    root, config, hba = setup(tmp_path / 'a')
    (tmp_path / 'a/release/.git/objects/info').mkdir(parents=True)
    (tmp_path / 'a/release/.git/objects/info/alternates').write_text('/elsewhere\n')
    code, text, runner = main(root, config, hba)
    assert code == 2 and 'objects from elsewhere' in text and runner.calls == []


def test_the_configuration_and_the_stage_env_are_checked(tmp_path):
    for n, over in enumerate(({'nexloop_root': 'relative'}, {'unexpected': 1})):
        root, config, hba = setup(tmp_path / str(n), **over)
        code, text, _ = main(root, config, hba)
        assert code == 2 and 'configuration' in text, over
    # An earlier configuration with the D4 sftp fields still loads (they are ignored).
    root, config, hba = setup(tmp_path / 'old', repo_path='/srv/nexloop/backups/file-repo1/restic', app_host_public_key=KEY)
    assert main(root, config, hba)[0] == 0
    root, config, hba = setup(tmp_path / 'e')
    stage_env = Path(json.loads(Path(config).read_text())['stage_env'])
    stage_env.write_text('NEXLOOP_DB=nexloop_stage\n')
    code, text, runner = main(root, config, hba)
    assert code == 2 and 'APP_HOST_ADDR' in text and not any(c[0] == 'docker' for c in runner.calls)


def plant_old_sftp_parts(root):
    """What the D4 version of this script installed (never on stage, but --uninstall must clear it wherever it is)."""
    (root / 'srv/nexloop-sftp/restic').mkdir(parents=True)
    (root / 'etc/ssh/nexloop-authorized-keys').mkdir(parents=True)
    (root / 'etc/ssh/nexloop-authorized-keys/nexloop-backup').write_text(f'restrict,from="{ADDRESS}" {KEY}\n')
    (root / 'etc/ssh/sshd_config.d/00-nexloop-file-backup.conf').write_text('Match User nexloop-backup\n    ForceCommand internal-sftp\nMatch all\n')
    (root / 'etc/systemd/system').mkdir(parents=True, exist_ok=True)
    (root / 'etc/systemd/system/srv-nexloop\\x2dsftp-restic.mount').write_text('[Mount]\n')


def test_uninstall_clears_old_sftp_parts_and_is_idempotent(tmp_path):
    root, config, hba = setup(tmp_path)
    assert main(root, config, hba)[0] == 0
    plant_old_sftp_parts(root)
    code, text, runner = main(root, config, hba, '--uninstall')
    assert code == 0, text
    for gone in ('etc/ssh/sshd_config.d/00-nexloop-file-backup.conf', 'etc/ssh/nexloop-authorized-keys', 'srv/nexloop-sftp',
                 'etc/systemd/system/srv-nexloop\\x2dsftp-restic.mount'):
        assert not (root / gone).exists(), gone
    assert ['systemctl', 'disable', '--now', 'srv-nexloop\\x2dsftp-restic.mount'] in runner.calls
    assert ['sshd', '-t'] in runner.calls and ['systemctl', 'try-reload-or-restart', 'ssh.service'] in runner.calls
    assert f'nexloop_backup_recorder {ADDRESS}/32' in hba.read_text()  # the pg_hba line stays
    code, text, runner = main(root, config, hba, '--uninstall')
    assert code == 0 and 'no D4 sftp part was there' in text and runner.calls == []


class AsUser:
    """Stands in for D.as_user (no root here): records each writer and whether the writes happened inside it."""
    def __init__(self, monkeypatch):
        self.writers, self.inside, self.writes = [], False, []
        for module, name in ((D, 'write_in_place'), (D.A, 'write_private')):
            real = getattr(module, name)
            monkeypatch.setattr(module, name, functools.partial(self.record, name, real))

    def record(self, name, real, *a, **k):
        self.writes.append((name, self.inside))
        return real(*a, **k)

    def __call__(self, writer, action):
        self.writers.append(writer)
        self.inside = True
        try:
            return action()
        finally:
            self.inside = False


def account(name):
    """Test only: the account database - deploy-user, a root-named alias and the service user."""
    uids = {'deploy-user': DEPLOY, 'toor': 0, 'nexloop-backup': DEPLOY + 3}
    if name not in uids:
        raise KeyError(name)
    return type('Entry', (), {'pw_uid': uids[name], 'pw_gid': DEPLOY + 1, 'pw_name': name})


def deployment_user_owns(hba, uid=DEPLOY):
    """config/, pg_hba.conf and nexloop_root as on stage: the deployment user's 751/750/644."""
    for path in (hba, hba.parent, hba.parent.parent):
        UIDS[os.stat(path).st_ino] = uid
    os.chmod(hba.parent.parent, 0o751)
    os.chmod(hba.parent, 0o750)
    os.chmod(hba, 0o644)


def deployed(tmp_path, monkeypatch, **over):
    root, config, hba = setup(tmp_path, **{'trusted_owner': 'deploy-user', **over})
    deployment_user_owns(hba)
    return root, config, hba, AsUser(monkeypatch)


def test_a_config_directory_of_the_trusted_owner_is_written_as_that_user(tmp_path, monkeypatch):
    """Dispatcher 2026-10-27 (stage refused step 1a): config/ and pg_hba.conf of the deployment user, named as trusted_owner - pg_hba,
    its backup and nothing else are written with that user's identity; root only renders (in its own scratch) and reads."""
    root, config, hba, as_user = deployed(tmp_path, monkeypatch)
    before = hba.read_text()
    code, text, runner = main(root, config, hba, writer={'account': account, 'as_user': as_user})
    assert code == 0 and 'written as deploy-user' in text, text
    assert as_user.writers == [(DEPLOY, DEPLOY + 1, 'deploy-user')]
    assert as_user.writes == [('write_private', True), ('write_in_place', True)]
    assert f'nexloop_backup_recorder {ADDRESS}/32' in hba.read_text() and (hba.parent / 'pg_hba.conf.before-file-backup').read_text() == before
    assert ['docker', 'compose', '-p', 'nexloop-data', 'kill', '-s', 'HUP', 'postgres'] in runner.calls
    as_user.writers.clear()
    code, text, _ = main(root, config, hba, writer={'account': account, 'as_user': as_user})
    assert code == 0 and 'already installed' in text and as_user.writers == []


def test_without_trusted_owner_the_deployment_users_config_is_refused(tmp_path, monkeypatch):
    """Never by default: the same stage layout without trusted_owner is refused, saying what to configure."""
    root, config, hba, as_user = deployed(tmp_path, monkeypatch)
    cfg = json.loads(Path(config).read_text())
    del cfg['trusted_owner']
    Path(config).write_text(json.dumps(cfg))
    code, text, runner = main(root, config, hba, writer={'account': account, 'as_user': as_user})
    assert code == 2 and 'pg_hba directory' in text and 'name that user as trusted_owner' in text, text
    assert as_user.writers == [] and not any(c[1:3] == ['-I', '-B'] for c in runner.calls)


@pytest.mark.parametrize('kw', [{'container': lambda text: 'another inode\n'}, {'logs': 'LOG:  pg_hba.conf was not reloaded\n'}])
def test_the_write_back_is_done_as_the_trusted_owner_too(tmp_path, monkeypatch, kw):
    root, config, hba, as_user = deployed(tmp_path, monkeypatch)
    before = hba.read_text()
    code, text, _ = main(root, config, hba, writer={'account': account, 'as_user': as_user}, **kw)
    assert code == 1 and hba.read_text() == before
    assert as_user.writers == [(DEPLOY, DEPLOY + 1, 'deploy-user')] * 2 and all(inside for _, inside in as_user.writes)
    assert [name for name, _ in as_user.writes] == ['write_private', 'write_in_place', 'write_in_place']


def test_root_writes_itself_when_config_is_roots_alone(tmp_path, monkeypatch):
    root, config, hba = setup(tmp_path, trusted_owner='deploy-user')
    as_user = AsUser(monkeypatch)
    code, text, _ = main(root, config, hba, writer={'account': account, 'as_user': as_user})
    assert code == 0 and 'written as root' in text and as_user.writers == [None]


@pytest.mark.parametrize('arrange, why', [
    (lambda hba: UIDS.pop(os.stat(hba).st_ino), 'must be owned by trusted_owner'),      # pg_hba.conf not the trusted owner's
    (lambda hba: UIDS.__setitem__(os.stat(hba).st_ino, DEPLOY + 7), 'must be owned by trusted_owner'),
    (lambda hba: os.chmod(hba, 0o664), 'must be owned by trusted_owner'),               # pg_hba.conf group-writable
    (lambda hba: os.chmod(hba.parent, 0o770), 'or trusted_owner'),                      # config/ group-writable
    (lambda hba: UIDS.__setitem__(os.stat(hba.parent).st_ino, DEPLOY + 7), 'or trusted_owner'),          # config/ a third user's
    (lambda hba: os.chmod(hba.parent.parent, 0o771), 'or trusted_owner'),               # the user's directory above config/ writable
    (lambda hba: UIDS.__setitem__(os.stat(hba.parent.parent).st_ino, DEPLOY + 7), 'or trusted_owner'),   # a third user above
])
def test_a_layout_others_than_the_trusted_owner_could_change_is_refused_before_anything(tmp_path, monkeypatch, arrange, why):
    root, config, hba, as_user = deployed(tmp_path, monkeypatch)
    arrange(hba)
    before = hba.read_text()
    code, text, runner = main(root, config, hba, writer={'account': account, 'as_user': as_user})
    assert code == 2 and why in text, text
    assert as_user.writers == [] and hba.read_text() == before and not any(c[1:3] == ['-I', '-B'] for c in runner.calls)


@pytest.mark.parametrize('name, why', [('nobody-here', 'no such user'), ('toor', 'must not be root'), ('nexloop-backup', 'not nexloop-backup'),
                                       ('Deploy User', "the deployment user's name"), (7, "the deployment user's name")])
def test_trusted_owner_names_an_existing_user_neither_root_nor_the_service_user(tmp_path, monkeypatch, name, why):
    root, config, hba, as_user = deployed(tmp_path, monkeypatch, trusted_owner=name)
    code, text, runner = main(root, config, hba, writer={'account': account, 'as_user': as_user})
    assert code == 2 and 'trusted_owner' in text and why in text and runner.calls == [], text


def test_a_pg_hba_replaced_after_its_check_is_never_read(tmp_path, monkeypatch):
    """Root reads pg_hba only from the inode, owner and type lstat saw: a file swapped in meanwhile (say, a hard link to another
    file of the user's) is not read, written to the backup or diffed."""
    root, config, hba, as_user = deployed(tmp_path, monkeypatch)

    def swap(path):
        other = path.parent / 'other'
        other.write_text('secret of someone else\n')
        os.replace(other, path)
        UIDS[os.stat(path).st_ino] = DEPLOY
    code, text, _ = main(root, config, hba, writer={'account': account, 'as_user': as_user}, during_render=swap)
    assert code == 1 and 'not the file checked' in text and as_user.writers == []
    assert not (hba.parent / 'pg_hba.conf.before-file-backup').exists()


def test_read_seen_refuses_a_link_a_fifo_and_another_owner(tmp_path):
    path = tmp_path / 'pg_hba.conf'
    path.write_text('rules\n')
    seen = os.lstat(path)
    assert D.read_seen(path, seen) == 'rules\n'
    link = tmp_path / 'link'
    os.symlink(path, link)
    with pytest.raises(OSError):
        D.read_seen(link, seen)
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    with pytest.raises(D.Failed):
        D.read_seen(fifo, os.lstat(fifo))  # opened without blocking, refused as not a regular file
    UIDS[seen.st_ino] = DEPLOY
    with pytest.raises(D.Failed):
        D.read_seen(path, seen)  # same inode, another owner than lstat said


@pytest.mark.filterwarnings('ignore:This process.*fork')  # pytest-xdist's threads; the installer itself starts none
def test_as_user_runs_the_action_in_a_child_with_that_identity_and_reports_its_failure(tmp_path):
    me = (os.getuid(), os.getgid(), 'me')
    marker = tmp_path / 'written'
    assert D.as_user(me, lambda: marker.write_text(str(os.getpid()))) is None
    assert marker.read_text() != str(os.getpid())  # written by a child process, not here
    with pytest.raises(D.Failed, match='as me: boom'):
        D.as_user(me, lambda: (_ for _ in ()).throw(D.Failed('boom')))
    with pytest.raises(D.Failed, match='as me: PermissionError'):
        D.as_user(me, lambda: (_ for _ in ()).throw(PermissionError('x')))
    with pytest.raises(D.Failed, match='not running as other'):
        D.as_user((os.getuid(), os.getgid() + 1, 'other'), lambda: marker.write_text('no'))
    assert D.as_user(None, lambda: 'here') == 'here'  # root's own config/: no child


def test_a_linked_config_or_nexloop_root_of_the_user_is_refused(tmp_path, monkeypatch):
    for n, level in enumerate(('config', 'nexloop')):
        root, config, hba, as_user = deployed(tmp_path / str(n), monkeypatch)
        real = hba.parent if level == 'config' else hba.parent.parent
        moved = real.with_name(real.name + '-real')
        os.rename(real, moved)
        os.symlink(moved, real)
        UIDS[os.lstat(real).st_ino] = DEPLOY
        code, text, _ = main(root, config, hba, writer={'account': account, 'as_user': as_user})
        assert code == 2 and 'symbolic link' in text and as_user.writers == [], (level, text)


def test_a_missing_pg_hba_is_refused_in_either_layout(tmp_path, monkeypatch):
    for n, deploy in enumerate((False, True)):
        if deploy:
            root, config, hba, as_user = deployed(tmp_path / str(n), monkeypatch)
        else:
            root, config, hba = setup(tmp_path / str(n))
            as_user = AsUser(monkeypatch)
        hba.unlink()
        code, text, _ = main(root, config, hba, writer={'account': account, 'as_user': as_user})
        assert code == 2 and 'pg_hba.conf' in text and as_user.writers == [], (deploy, text)
