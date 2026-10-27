"""NX-035 slice 3: scripts/deploy/file_backup_install.py (APP_HOST steps 2-6) into a temporary root with a scripted runner (no root,
no systemd, no database, no restic): --prepare, install, rerun, the passwords never in output or argv, restic only from the archive
the lock names, secrets/backup holding nothing else, the sandbox check in the unit's own sandbox (D3: its verdicts stop the
install), one real run, both host-health files (file-backup and provider-drift) or neither, uninstall. The trust checks run for
real against this test's own uid; the directories above tmp_path stand for root's (a CI host puts tmp_path under /tmp).
The sandbox check script and the unit's entry run for real with a stand-in restic."""
import bz2
import functools
import hashlib
import io
import json
import os
import stat as st
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import file_backup_install as I  # noqa: E402
from support.writable_tree import make_writable  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_tree_writable_after(tmp_path):
    """Teardown: the release trees this test made read-only become removable again (support.writable_tree)."""
    yield
    make_writable(tmp_path)


SHA = 'b' * 40
RECORDER_PW = 'recorder-Pw-synthetic-0123456789'
REPO1_PW = 'repo1-Pw-synthetic-0123456789abcdef'
REST_PW = 'Rest-Pw_synthetic_0123456789abcdef'
STDIN = f'recorder\t{RECORDER_PW}\nrepo1\t{REPO1_PW}\nrepo1_rest\t{REST_PW}\n'
HOST_KEY = 'ssh-ed25519 ' + 'A' * 68
BINARY = b'#!/bin/sh\necho synthetic restic\n'
OWNERS = (0, os.getuid())
SECRETS = 'srv/nexloop/secrets/backup'
KEY_MARK = 'PRIVATE' + ' KEY'  # built at runtime: no key-shaped literal in the repository (check_publication)
KEY_TEXT = f'-----BEGIN OPENSSH {KEY_MARK}-----\nsynthetic\n-----END OPENSSH {KEY_MARK}-----\n'


@pytest.fixture(autouse=True)
def trust_root_at_tmp_path(tmp_path, monkeypatch):
    """Test only: the directories above this test's tmp_path stand for root's own (lstat reports them as root's 0755 directories);
    everything at or below tmp_path is checked as it really is. The production checks have no such stop."""
    above = {str(p) for p in Path(tmp_path).parents} | {str(p) for p in Path(os.path.realpath(tmp_path)).parents}
    lstat = os.lstat
    UIDS.clear()

    def fake(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) in above:
            return os.stat_result((st.S_IFDIR | 0o755, info.st_ino, info.st_dev, info.st_nlink, 0, 0, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        if info.st_ino in UIDS:  # the deployment user's directories (no root here to chown them)
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, UIDS[info.st_ino], DEPLOY, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return info
    monkeypatch.setattr(os, 'lstat', fake)


UIDS = {}  # test only: inode -> the uid lstat reports
DEPLOY = 4242


def account(name):
    uids = {'deploy-user': DEPLOY, 'nexloop-file-backup': DEPLOY + 3}
    if name not in uids:
        raise KeyError(name)
    return type('Entry', (), {'pw_uid': uids[name], 'pw_gid': uids[name], 'pw_name': name})


def private(path, value, text=None, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else json.dumps(value))
    os.chmod(path, mode)
    return str(path)


def good_check(command):
    """The sandbox check's answer when everything is as the unit should see it, from the arguments it was given."""
    args = command[command.index('--restic'):]
    pick = lambda flag: [args[i + 1] for i, a in enumerate(args) if a == flag]  # noqa: E731
    expected = {'/srv/nexloop': ['data', 'secrets'], '/srv/nexloop/secrets': ['backup'], '/srv/nexloop/data': ['artifacts', 'runtime']}
    return {'listing': {d: expected[d] for d in pick('--list')}, 'read': {p: True for p in pick('--read')}, 'hidden': {p: True for p in pick('--hidden')},
            'restic': 'restic 0.18.0', 'handles': {'open_by_handle_at': 'EPERM', 'name_to_handle_at': 'EPERM'},
            'repos': {r.split(',')[0]: 0 for r in pick('--repo')}, 'init': {},
            'append_only': {r: {'backup': 0, 'forget': 0, 'refused': True, 'still_listed': True} for r in pick('--append-only-probe')}}


RESULT = {'ok': True, 'kind': 'files_artifact', 'backup_id': 'c' * 64, 'verify_status': 'verified', 'file_count': 0, 'bytes': 0,
          'file_manifest_digest': 'd' * 64, 'listed': 0, 'skipped': {}, 'absent': 0, 'restic': 'ok', 'check': 'ok'}


MAINT_OK = {'ok': True, 'forget': 'ok', 'removed': 0, 'dry_run': True, 'check': 'not_run'}


class Runner:
    def __init__(self, tmp_path, *, check=None, journal=None, result='success', user_exists=False, drift=True, maint=None):
        self.calls, self.tmp_path = [], tmp_path
        self.check, self.journal, self.result, self.user_exists, self.drift = check, journal, result, user_exists, drift
        self.maint = maint

    def __call__(self, command, *, stdin=None, check=True):
        self.calls.append(list(command))
        if command[:2] == ['env', '-i']:
            assert command[:8] == I.GIT  # the minimal environment and --no-replace-objects, spelled out
            path = command[-1].split(':', 1)[1]
            if path == 'versions.lock.json':
                lock = json.loads((ROOT / path).read_text())
                lock['deployment_tools']['restic']['archive_sha256'] = hashlib.sha256(bz2.compress(BINARY)).hexdigest()
                return json.dumps(lock)
            return (ROOT / path).read_text()
        if command[:2] == ['id', '-u']:
            return '998\n' if self.user_exists else ''
        if command[0] == 'ssh-keygen':
            key = Path(command[command.index('-f') + 1])
            key.write_text(KEY_TEXT)
            Path(str(key) + '.pub').write_text('ssh-ed25519 ' + 'B' * 68 + ' nexloop-file-backup\n')
            return ''
        if command[0] == 'systemd-run' and any(a.endswith('/file_backup_maint.py') for a in command):
            doc = MAINT_OK if self.maint is None else self.maint(dict(MAINT_OK))
            return '' if doc is None else json.dumps(doc) + '\n'
        if command[0] == 'systemd-run':
            doc = good_check(command)
            if self.check:
                doc = self.check(doc)
            return '' if doc is None else json.dumps(doc) + '\n'
        if command[:3] == ['systemctl', 'show', '-p']:
            return {'InvocationID': 'a' * 32 + '\n', 'Result': self.result + '\n'}[command[3]]
        if command[0] == 'journalctl':
            lines = self.journal if self.journal is not None else [dict(RESULT, kind=k) for k in ('files_artifact', 'files_artifact', 'files_runtime', 'files_runtime')]
            return 'Starting nexloop-file-backup...\n' + ''.join(json.dumps(x) + '\n' for x in lines)
        if command[:2] == ['systemctl', 'list-unit-files']:
            return 'nexloop-provider-drift.timer enabled enabled\n' if self.drift else ''
        return ''


def venv(tmp_path):
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


def setup(tmp_path, repo2=True, **over):
    root = tmp_path / 'root'
    for d in ('srv/nexloop/data/artifacts', 'srv/nexloop/data/runtime', 'srv/nexloop/secrets/api', 'srv/nexloop/release', 'etc/ssh'):
        (root / d).mkdir(parents=True)
    private(root / 'srv/nexloop/secrets/api/database.dsn', None, 'another service secret\n')
    private(root / 'etc/ssh/ssh_host_ed25519_key', None, 'host key\n')
    probe = {'schema': 'nexloop-host-health/1', 'host_role': 'app_host', 'timers': []}
    private(root / 'etc/nexloop/host-health.json', probe)
    private(root / 'root/host-health-install.app-host.json', {'schema': 'nexloop-host-health-install/1', 'probe': {'timers': []}})
    if repo2:
        for name in I.REPO2:
            private(root / SECRETS / name, None, 'synthetic repo2 file\n')
    checkout = tmp_path / 'release'
    (checkout / '.git/refs/heads').mkdir(parents=True)
    (checkout / '.git/HEAD').write_text('ref: refs/heads/main\n')
    archive = tmp_path / 'download/restic_0.18.0_linux_amd64.bz2'
    archive.parent.mkdir()
    archive.write_bytes(bz2.compress(BINARY))
    ca = tmp_path / 'pki/ca.crt'
    ca.parent.mkdir()
    ca.write_text('-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n')
    bundle = tmp_path / 'pki/bundle.crt'
    bundle.write_text('-----BEGIN CERTIFICATE-----\nsynthetic intermediate\n-----END CERTIFICATE-----\n'
                      '-----BEGIN CERTIFICATE-----\nsynthetic root\n-----END CERTIFICATE-----\n')
    cfg = json.loads((ROOT / 'deploy/examples/file-backup-install.app-host.example.json').read_text())
    cfg.update({'release_sha': SHA, 'release_checkout': str(checkout), 'python': venv(tmp_path), 'restic_archive': str(archive),
                'database': {'host': 'pg.data-host.example', 'port': 5432, 'dbname': 'nexloop_stage', 'sslrootcert': str(ca)},
                'repo1': {'host': 'data-host.example', 'port': 8000, 'cacert': str(bundle)}, **over})
    return root, private(tmp_path / 'config.json', cfg)


def main(root, config, *args, runner=None, stdin=STDIN, asked=None, tmp_path=None):
    out, runner = io.StringIO(), runner or Runner(tmp_path)

    def trust(path, *, what, **kw):
        if asked is not None:
            asked.append((what, str(path)))
        I.root_owned(path, what=what, owners=OWNERS, **kw)

    code = I.main([config, *args], stdin=io.StringIO(stdin), out=out, runner=runner, root=root, trust=trust,
                  trust_python=functools.partial(I.interpreter_owned, owners=OWNERS), account=account)
    return code, out.getvalue(), runner


def secret_free(text, runner):
    argv = '\n'.join(' '.join(c) for c in runner.calls)
    return all(s not in text and s not in argv for s in (RECORDER_PW, REPO1_PW, 'synthetic repo2 file', KEY_MARK))


def test_installs_reruns_and_never_shows_a_secret(tmp_path):
    root, config = setup(tmp_path)
    asked = []
    code, text, runner = main(root, config, asked=asked)
    assert code == 0, text
    assert {'release_checkout', 'release .git', 'release base', 'secrets/backup', 'restic directory', 'home'} <= {w for w, _ in asked}
    assert secret_free(text, runner)
    for n in range(2, 7):
        assert f'step {n}:' in text
    # The secrets: the DSN over hostssl, verify-full, the copied CA; the service user's files 0600 and chowned, the directory root's.
    secrets = root / SECRETS
    dsn = (secrets / 'recorder.dsn').read_text()
    assert f"user='nexloop_backup_recorder' password='{RECORDER_PW}'" in dsn and "sslmode='verify-full' sslrootcert='/srv/nexloop/secrets/backup/pg-ca.crt'" in dsn
    assert (secrets / 'restic-files-repo1.repository').read_text() == f'rest:https://nexloop-file-backup:{REST_PW}@data-host.example:8000/restic/\n'
    assert (secrets / 'restic-files-repo1.pass').read_text() == REPO1_PW + '\n'
    assert st.S_IMODE(secrets.stat().st_mode) == 0o700 and st.S_IMODE((secrets / 'pg-ca.crt').stat().st_mode) == 0o600
    for name in ('recorder.dsn', 'restic-files-repo1.repository', 'restic-files-repo1.pass'):
        assert st.S_IMODE((secrets / name).stat().st_mode) == 0o600 and ['chown', 'nexloop-file-backup:nexloop-file-backup', str(secrets / name)] in runner.calls
    assert not any(c[0] == 'chown' and c[-1] in (str(secrets), str(secrets / 'pg-ca.crt')) for c in runner.calls)
    # D4 changed (owner 2026-10-27): no SSH at all - repo1 is the rest-server.
    assert not (root / 'var/lib/nexloop-file-backup/.ssh').exists() and not any(c[0] == 'ssh-keygen' for c in runner.calls)
    # restic from the archive, root's and read-only.
    restic = root / 'opt/nexloop/file-backup/restic-0.18.0/restic'
    assert restic.read_bytes() == BINARY and st.S_IMODE(restic.stat().st_mode) == 0o555
    # The env file and the unit, tokens filled.
    env = (root / 'etc/nexloop/file-backup.env').read_text()
    assert 'FILE_BACKUP_KEY_REF=restic-files' in env and 'OFFSITE_ENVIRONMENT_FILE=/srv/nexloop/secrets/backup/offsite.env' in env
    service = (root / 'etc/systemd/system/nexloop-file-backup.service').read_text()
    assert '--restic /opt/nexloop/file-backup/restic-0.18.0/restic' in service and 'MemoryDenyWriteExecute=false' in service
    assert service.count(tmp_path.name) == 4 and '@' not in ''.join(l for l in service.splitlines() if not l.startswith('#'))  # python, 4 lines
    # Step 5: the check in the unit's own sandbox, every other secret listed as hidden.
    dry = next(c for c in runner.calls if c[0] == 'systemd-run')
    props = [dry[i + 1] for i, a in enumerate(dry) if a == '-p']
    for p in ('User=nexloop-file-backup', 'AmbientCapabilities=CAP_DAC_READ_SEARCH', 'SystemCallFilter=~open_by_handle_at name_to_handle_at',
              'SystemCallErrorNumber=EPERM', 'SystemCallArchitectures=native', 'CapabilityBoundingSet=CAP_DAC_READ_SEARCH',
              'TemporaryFileSystem=/srv/nexloop:ro', 'EnvironmentFile=/etc/nexloop/file-backup.env', 'ReadWritePaths=/var/lib/nexloop-file-backup/staging',
              'BindReadOnlyPaths=/srv/nexloop/data/artifacts /srv/nexloop/data/runtime /srv/nexloop/secrets/backup'):
        assert p in props, p
    hidden = [dry[i + 1] for i, a in enumerate(dry) if a == '--hidden']
    assert {'/etc/nexloop', '/srv/nexloop/secrets/api', '/srv/nexloop/release', '/etc/ssh/ssh_host_ed25519_key'} <= set(hidden)
    assert '/srv/nexloop/secrets/backup' not in hidden and '/etc/shadow' not in hidden  # only what exists in this root
    assert '--init' in dry and 'repo2' == dry[dry.index('--init') + 1]
    # repo2's maintenance: its unit rendered like the backup's, a dry run in its own (stricter) sandbox before the real run.
    maint_unit = (root / 'etc/systemd/system/nexloop-file-backup-maint.service').read_text()
    assert '--restic /opt/nexloop/file-backup/restic-0.18.0/restic' in maint_unit and '@' not in ''.join(l for l in maint_unit.splitlines() if not l.startswith('#'))
    assert (root / 'etc/systemd/system/nexloop-file-backup-maint.timer').read_text().count('OnCalendar=*-*-* 04:07:00') == 1
    maint = next(c for c in runner.calls if c[0] == 'systemd-run' and any(a.endswith('/file_backup_maint.py') for a in c))
    mprops = [maint[i + 1] for i, a in enumerate(maint) if a == '-p']
    assert {'User=nexloop-file-backup', 'CapabilityBoundingSet=', 'SystemCallFilter=~open_by_handle_at name_to_handle_at', 'TemporaryFileSystem=/srv/nexloop:ro',
            'EnvironmentFile=/etc/nexloop/file-backup.env',
            'BindReadOnlyPaths=/srv/nexloop/secrets/backup/restic-files-repo2.repository /srv/nexloop/secrets/backup/restic-files-repo2.pass '
            '/srv/nexloop/secrets/backup/offsite.env'} <= set(mprops)
    assert not any(p.startswith(('AmbientCapabilities=', 'ReadWritePaths=')) for p in mprops)
    assert maint[-1] == '--dry-run' and maint[maint.index('--repository-file') + 1] == '/srv/nexloop/secrets/backup/restic-files-repo2.repository'
    assert 'restic-files-repo1' not in ' '.join(maint) and 'maint dry-run: {"error": null, "forget": "ok", "ok": true, "removed": 0}' in text
    # The real run, then host-health (both files) and both timers last.
    start = runner.calls.index(['systemctl', 'start', 'nexloop-file-backup.service'])
    assert runner.calls.index(dry) < runner.calls.index(maint) < start
    assert runner.calls[-1] == ['systemctl', 'enable', '--now', 'nexloop-file-backup.timer', 'nexloop-file-backup-maint.timer']
    assert text.count('"verify_status": "verified"') == 4 and 'run runtime/repo2' in text
    for path, nested in ((root / 'etc/nexloop/host-health.json', False), (root / 'root/host-health-install.app-host.json', True)):
        value = json.loads(path.read_text())
        assert [t['name'] for t in (value['probe'] if nested else value)['timers']] == ['file-backup', 'file-backup-maint', 'provider-drift']
        assert st.S_IMODE(path.stat().st_mode) == 0o600
    # A rerun without stdin keeps the secret files; nothing generated twice.
    code, text, runner = main(root, config, stdin='', runner=Runner(tmp_path, user_exists=True))
    assert code == 0, text
    assert 'recorder.dsn kept' in text and 'release bbbbbbbbbbbb reused' in text and 'restic 0.18.0 unchanged' in text
    assert 'already listed' in text and not any(c[0] in ('useradd', 'ssh-keygen') for c in runner.calls)


def test_prepare_writes_the_user_and_the_env_file_and_no_secret(tmp_path):
    root, config = setup(tmp_path, repo2=False)
    ssh = root / 'var/lib/nexloop-file-backup/.ssh'  # an earlier sftp version's files: --prepare removes them too
    ssh.mkdir(parents=True)
    (ssh / 'id_ed25519').write_text('left by the sftp version\n')
    code, text, runner = main(root, config, '--prepare', stdin='')
    assert code == 0, text
    assert 'install_repo2_secrets.sh files' in text and KEY_MARK not in text and 'public key' not in text
    assert (root / 'etc/nexloop/file-backup.env').exists() and (root / SECRETS).is_dir() and list((root / SECRETS).iterdir()) == []
    assert not any(c[0] in ('systemd-run', 'systemctl', 'ssh-keygen') for c in runner.calls) and not (root / 'opt/nexloop/file-backup').exists()
    assert not (root / 'var/lib/nexloop-file-backup/.ssh').exists()


def test_the_restic_archive_must_be_the_one_the_lock_names(tmp_path):
    root, config = setup(tmp_path)
    archive = Path(json.loads(Path(config).read_text())['restic_archive'])
    archive.write_bytes(bz2.compress(b'#!/bin/sh\necho another binary\n'))
    code, text, runner = main(root, config)
    assert code == 2 and 'sha256 differs' in text and not (root / 'opt/nexloop/file-backup/restic-0.18.0/restic').exists()
    root, config = setup(tmp_path / 'n', restic_archive=str(archive.parent / 'restic_0.17.3_linux_amd64.bz2'))
    code, text, _ = main(root, config)
    assert code == 2 and 'restic_0.18.0_linux_amd64.bz2' in text


@pytest.mark.parametrize('break_it, why', [
    (lambda d: {**d, 'hidden': {**d['hidden'], '/srv/nexloop/secrets/api': False}}, '/srv/nexloop/secrets/api visible'),
    (lambda d: {**d, 'hidden': {**d['hidden'], '/etc/nexloop': False}}, '/etc/nexloop visible'),
    (lambda d: {**d, 'listing': {**d['listing'], '/srv/nexloop/secrets': ['api', 'backup']}}, "/srv/nexloop/secrets shows ['api', 'backup']"),
    (lambda d: {**d, 'read': {**d['read'], '/srv/nexloop/data/runtime': False}}, '/srv/nexloop/data/runtime unreadable'),
    (lambda d: {**d, 'restic': 'restic 0.17.3'}, "restic is 'restic 0.17.3'"),
    (lambda d: {**d, 'repos': {'repo1': 10, 'repo2': 0}}, 'repo1: does not exist'),
    (lambda d: {**d, 'repos': {'repo1': 0, 'repo2': 12}}, 'repo2: wrong password'),
    (lambda d: None, 'gave no answer'),
    # L6 F1: the handle calls not filtered - the capability would open inodes past every hidden mount.
    (lambda d: {**d, 'handles': {'open_by_handle_at': 'ESTALE', 'name_to_handle_at': 'EPERM'}}, 'open_by_handle_at is not filtered (ESTALE)'),
    (lambda d: {**d, 'handles': {'open_by_handle_at': 'EPERM', 'name_to_handle_at': 'succeeded'}}, 'name_to_handle_at is not filtered (succeeded)'),
    (lambda d: {k: v for k, v in d.items() if k != 'handles'}, 'open_by_handle_at is not filtered (None)'),
])
def test_a_sandbox_verdict_stops_the_install_before_any_run(tmp_path, break_it, why):
    root, config = setup(tmp_path)
    code, text, runner = main(root, config, runner=Runner(tmp_path, check=break_it))
    assert code == 1 and why in text and 'timer not enabled' in text, text
    assert not any(c[:2] in (['systemctl', 'start'], ['systemctl', 'enable']) for c in runner.calls)
    assert json.loads((root / 'etc/nexloop/host-health.json').read_text())['timers'] == []


@pytest.mark.parametrize('journal, result', [
    ([dict(RESULT), dict(RESULT, ok=False, verify_status='failed', restic='failed', check='not_run')], 'exit-code'),
    ([dict(RESULT)] * 3, 'success'),
    ([dict(RESULT)] * 4, 'exit-code'),
])
def test_an_unverified_run_does_not_enable_the_timer(tmp_path, journal, result):
    root, config = setup(tmp_path)
    code, text, runner = main(root, config, runner=Runner(tmp_path, journal=journal, result=result))
    assert code == 1 and 'not verified' in text and not any(c[:2] == ['systemctl', 'enable'] for c in runner.calls)


def test_missing_repo2_files_and_foreign_entries_in_secrets_backup(tmp_path):
    root, config = setup(tmp_path, repo2=False)
    code, text, runner = main(root, config)
    assert code == 1 and 'install_repo2_secrets.sh files' in text and not any(c[0] == 'systemd-run' for c in runner.calls)
    root, config = setup(tmp_path / 'f')
    private(root / SECRETS / 'metrics.dsn', None, 'not this unit\'s\n')
    code, text, runner = main(root, config)
    assert code == 2 and 'holds 1 other entry (metrics.dsn)' in text and not (root / SECRETS / 'recorder.dsn').exists()


def test_stdin_lines_are_checked_and_a_first_install_needs_them(tmp_path):
    root, config = setup(tmp_path)
    for bad in (f'recorder\t{RECORDER_PW}\nrecorder\t{RECORDER_PW}\n', 'recorder\tshort\n', f'repo2\t{REPO1_PW}\n', f'recorder {RECORDER_PW}\n',
                f'recorder\t{RECORDER_PW}\nrepo1\t{REPO1_PW} x\n', f'recorder\t{RECORDER_PW}\nrepo1\t{REPO1_PW}\x07\n'):
        code, text, _ = main(root, config, stdin=bad)
        assert code == 2 and 'stdin: recorder<TAB>password' in text and RECORDER_PW not in text and REPO1_PW not in text
    code, text, _ = main(root, config, stdin=f'repo1\t{REPO1_PW}\nrepo1_rest\t{REST_PW}\n')
    assert code == 2 and 'recorder.dsn is not written yet' in text


def test_trust_refuses_a_writable_directory_alternates_and_a_pointer_file(tmp_path):
    root, config = setup(tmp_path)
    os.chmod(tmp_path / 'release/.git/refs', 0o777)
    code, text, runner = main(root, config)
    assert code == 2 and 'release .git' in text and runner.calls == []
    os.chmod(tmp_path / 'release/.git/refs', 0o755)
    os.chmod(tmp_path / 'opt/nexloop/host-health' / SHA / 'venv/bin', 0o777)
    code, text, _ = main(root, config)
    assert code == 2 and 'python' in text
    for n, make in enumerate((lambda g: (g / 'objects/info').mkdir(parents=True) or (g / 'objects/info/alternates').write_text('/elsewhere\n'),
                              lambda g: (g / 'commondir').write_text('../elsewhere\n'))):
        root, config = setup(tmp_path / str(n))
        make(tmp_path / str(n) / 'release/.git')
        code, text, runner = main(root, config)
        assert code == 2 and 'objects from elsewhere' in text and runner.calls == []
    root, config = setup(tmp_path / 'w')
    import shutil
    shutil.rmtree(tmp_path / 'w/release/.git')
    (tmp_path / 'w/release/.git').write_text('gitdir: /elsewhere/.git/worktrees/x\n')
    code, text, _ = main(root, config)
    assert code == 2 and 'worktree pointer' in text


def test_a_link_on_the_way_and_a_shared_configuration_are_refused(tmp_path):
    root, config = setup(tmp_path)
    os.rename(tmp_path / 'release', tmp_path / 'release-real')
    os.symlink(tmp_path / 'release-real', tmp_path / 'release')
    code, text, runner = main(root, config)
    assert code == 2 and 'is a symbolic link' in text and runner.calls == []
    root, config = setup(tmp_path / 'm')
    os.chmod(config, 0o644)
    code, text, runner = main(root, config)
    assert code == 2 and 'a regular 0600 file of the caller' in text and runner.calls == []


def test_an_unfilled_token_stops(tmp_path):
    with pytest.raises(I.Failed, match='unfilled token'):
        I.render_service('[Service]\nExecStart=@PYTHON@ --other @OTHER@\n', {'python': '/usr/bin/python3'}, '/opt/restic')


def test_repo2_is_initialised_only_when_the_configuration_says_so(tmp_path):
    root, config = setup(tmp_path, repo2_init=False)
    code, text, runner = main(root, config)
    assert code == 0 and '--init' not in next(c for c in runner.calls if c[0] == 'systemd-run')


def test_a_reused_release_is_checked_file_by_file(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    changed = root / 'opt/nexloop/file-backup' / SHA / 'packages/eios-core/src/nexloop_eios/file_backup.py'
    os.chmod(changed.parent, 0o755)
    os.chmod(changed, 0o644)
    changed.write_text('print("not the commit")\n')
    os.chmod(changed, 0o444)
    code, text, _ = main(root, config, stdin='')
    assert code == 1 and 'differs from the commit' in text


def test_the_second_host_health_write_failing_restores_the_first(tmp_path, monkeypatch):
    root, config = setup(tmp_path)
    first = root / 'etc/nexloop/host-health.json'
    before = first.read_text()
    real = I.write_private

    def failing(path, text, mode=0o600):
        if path.name == 'host-health-install.app-host.json':
            raise OSError('disk full')
        return real(path, text, mode)
    monkeypatch.setattr(I, 'write_private', failing)
    code, text, runner = main(root, config)
    assert code == 1 and first.read_text() == before and not any(c[:2] == ['systemctl', 'enable'] for c in runner.calls)


def test_a_malformed_host_health_file_is_refused_before_anything(tmp_path):
    root, config = setup(tmp_path)
    private(root / 'root/host-health-install.app-host.json', {'schema': 'x', 'probe': {'timers': 'not a list'}})
    code, text, runner = main(root, config)
    assert code == 2 and 'probe timers' in text and runner.calls == [] and not (root / SECRETS / 'recorder.dsn').exists()


def test_provider_drift_is_listed_only_when_its_timer_is_installed(tmp_path):
    root, config = setup(tmp_path)
    code, text, _ = main(root, config, runner=Runner(tmp_path, drift=False))
    assert code == 0 and 'no nexloop-provider-drift.timer here' in text
    assert [t['name'] for t in json.loads((root / 'etc/nexloop/host-health.json').read_text())['timers']] == ['file-backup', 'file-backup-maint']


def test_the_env_file_keeps_the_repo2_option_install_repo2_secrets_wrote(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    env = root / 'etc/nexloop/file-backup.env'
    os.chmod(env, 0o600)
    env.write_text(env.read_text() + 'FILE_BACKUP_REPO2_OPTIONS=--s3-bucket-lookup dns\nFILE_BACKUP_STAGING=/elsewhere\n')
    code, text, runner = main(root, config, stdin='')
    assert code == 0, text
    lines = env.read_text().splitlines()
    assert 'FILE_BACKUP_REPO2_OPTIONS=--s3-bucket-lookup dns' in lines and 'FILE_BACKUP_STAGING=/elsewhere' not in lines
    dry = next(c for c in runner.calls if c[0] == 'systemd-run')
    assert dry[[i for i, a in enumerate(dry) if a == '--repo'][1] + 1].endswith('/offsite.env,dns')


def test_uninstall_removes_everything_but_the_user(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    code, text, runner = main(root, config, '--uninstall', stdin='')
    assert code == 0, text
    assert runner.calls[0] == ['systemctl', 'disable', '--now', 'nexloop-file-backup.timer', 'nexloop-file-backup-maint.timer']
    for gone in ('etc/systemd/system/nexloop-file-backup.service', 'etc/systemd/system/nexloop-file-backup.timer', 'etc/nexloop/file-backup.env',
                 'etc/systemd/system/nexloop-file-backup-maint.service', 'etc/systemd/system/nexloop-file-backup-maint.timer',
                 'opt/nexloop/file-backup', 'var/lib/nexloop-file-backup/staging', f'{SECRETS}/recorder.dsn', f'{SECRETS}/offsite.env'):
        assert not (root / gone).exists(), gone
    assert (root / 'var/lib/nexloop-file-backup').is_dir() and 'its home stay' in text
    assert [t['name'] for t in json.loads((root / 'etc/nexloop/host-health.json').read_text())['timers']] == ['provider-drift']
    assert (root / 'srv/nexloop/secrets/api/database.dsn').exists()


def test_uninstall_removes_the_staging_tree_without_touching_what_a_link_in_it_names(tmp_path):
    """L6 S2: the staging tree is the service user's; it is removed as it is, nothing in it (or behind a link in it) chmod'ed."""
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    outside = tmp_path / 'outside'
    outside.write_text('not the installer\'s')
    os.chmod(outside, 0o640)
    staging = root / 'var/lib/nexloop-file-backup/staging'
    (staging / 'stage-left').mkdir()
    os.symlink(outside, staging / 'stage-left/link')
    assert main(root, config, '--uninstall', stdin='')[0] == 0
    assert not staging.exists() and outside.exists() and st.S_IMODE(outside.stat().st_mode) == 0o640


def test_uninstall_refuses_a_malformed_host_health_file_before_removing_anything(tmp_path):
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    private(root / 'root/host-health-install.app-host.json', {'schema': 'x', 'probe': {'timers': 'not a list'}})
    code, text, runner = main(root, config, '--uninstall', stdin='')
    assert code == 2 and (root / 'etc/systemd/system/nexloop-file-backup.service').exists() and runner.calls == []


def test_a_malformed_configuration_is_refused(tmp_path):
    for n, over in enumerate(({'key_ref': 'bad label'}, {'repo1': {'host': 'h', 'port': 22, 'host_key': 'ssh-ed25519 short', 'path': '/restic'}},
                              {'database': {'host': '/var/run/postgresql', 'port': 5432, 'dbname': 'x', 'sslrootcert': '/x'}}, {'repo2_init': 'yes'},
                              {'unexpected': 1})):
        root, config = setup(tmp_path / str(n), **over)
        code, text, _ = main(root, config)
        assert code == 2 and 'configuration' in text, over


@pytest.mark.parametrize('answer, why', [
    (lambda d: None, 'gave no answer'),
    (lambda d: {'ok': False, 'error': 'NotRepo2'}, 'not ok (NotRepo2)'),
    (lambda d: {**d, 'ok': False, 'forget': 'exit 10'}, 'not ok (exit 10)'),
    (lambda d: {**d, 'forget': 'exit 1'}, 'not ok (exit 1)'),  # ok true but forget failed: still refused
])
def test_a_failing_repo2_maintenance_dry_run_stops_before_the_run_and_the_timers(tmp_path, answer, why):
    root, config = setup(tmp_path)
    code, text, runner = main(root, config, runner=Runner(tmp_path, maint=answer))
    assert code == 1 and why in text and 'timers not enabled' in text, text
    assert not any(c[:2] in (['systemctl', 'start'], ['systemctl', 'enable']) for c in runner.calls)
    assert json.loads((root / 'etc/nexloop/host-health.json').read_text())['timers'] == []


@pytest.mark.parametrize('broken', [
    dict(check=lambda d: {**d, 'hidden': {**d['hidden'], '/etc/nexloop': False}}),
    dict(maint=lambda d: {**d, 'ok': False, 'forget': 'exit 12'}),
    dict(result='exit-code'),
])
def test_a_rerun_whose_gate_fails_disables_both_timers_an_earlier_install_enabled(tmp_path, broken):
    """L6 S2: the first install enabled the timers; a rerun fails step 5 - the timers are stopped before the run ends."""
    root, config = setup(tmp_path)
    assert main(root, config)[0] == 0
    code, text, runner = main(root, config, stdin='', runner=Runner(tmp_path, user_exists=True, **broken))
    disable = ['systemctl', 'disable', '--now', 'nexloop-file-backup.timer', 'nexloop-file-backup-maint.timer']
    assert code == 1 and runner.calls[-1] == disable and 'both timers disabled' in text, text
    assert 'step 5 failed: nexloop-file-backup.timer and nexloop-file-backup-maint.timer disabled' in text
    assert not any(c[:2] == ['systemctl', 'enable'] for c in runner.calls)


def test_the_maintenance_unit_is_the_backups_sandbox_without_what_it_does_not_need():
    """One sandbox for both units; repo2's maintenance reads no data, so it has no capability, no data root, nothing writable."""
    read = lambda name: dict(I.sandbox((ROOT / 'deploy/stage/systemd' / name).read_text().replace('@MDWE@', 'true')))  # noqa: E731
    backup, maint = read('nexloop-file-backup.service.example'), read('nexloop-file-backup-maint.service.example')
    # L6 S1: of /srv/nexloop only repo2's three files (recorder.dsn, repo1's files unreadable), and the SSH key hidden.
    assert maint.pop('CapabilityBoundingSet') == '' and maint.pop('BindReadOnlyPaths').split() == [f'{I.SECRETS}/{n}' for n in I.REPO2]
    hidden = maint.pop('InaccessiblePaths').split()
    assert hidden[0] == '-/var/lib/nexloop-file-backup/.ssh' and hidden[1:] == backup.pop('InaccessiblePaths').split()
    for key in ('AmbientCapabilities', 'CapabilityBoundingSet', 'BindReadOnlyPaths', 'ReadWritePaths'):
        backup.pop(key)
    assert maint == backup
    text = (ROOT / 'deploy/stage/systemd/nexloop-file-backup-maint.service.example').read_text()
    command = next(l for l in text.splitlines() if l.startswith('ExecStart='))
    assert '${RESTIC_REPO2_REPOSITORY_FILE}' in command and 'REPO1' not in command and command.count('ExecStart=') == 1
    assert text.count('ExecStart=') == 1 and 'scripts/deploy/file_backup_maint.py' in I.FILES


def test_the_unit_and_the_installer_agree(tmp_path):
    """The secret file names, the staging directory and the data roots the installer writes are the ones the unit names."""
    service = (ROOT / 'deploy/stage/systemd/nexloop-file-backup.service.example').read_text()
    props = dict(I.sandbox(service.replace('@MDWE@', 'true')))
    assert props['BindReadOnlyPaths'].split() == [*I.DATA_ROOTS, I.SECRETS] and props['ReadWritePaths'] == I.STAGING
    assert '${BACKUP_SECRETS}/recorder.dsn' in service and 'recorder.dsn' in I.OWN and service.count('/opt/nexloop/file-backup/current/') == 4
    assert all(f in I.FILES for f in ('scripts/deploy/file_backup_run.py', 'scripts/deploy/file_backup_sandbox_check.py'))


# The two release scripts, for real: the check script with a stand-in restic, the unit's entry under -I.

def fake_restic(tmp_path, codes):
    state = tmp_path / 'restic-state'
    state.write_text(json.dumps(codes))
    script = tmp_path / 'restic'
    script.write_text(f'#!{sys.executable}\nimport json,sys\ns=json.load(open({str(state)!r}))\na=sys.argv[1:]\n'
                      "if a==['version']:\n    print('restic 0.18.0 compiled with go1.24 on linux/amd64');sys.exit(0)\n"
                      "repo=open(a[a.index('--repository-file')+1]).read().strip()\n"
                      "if a[-1]=='init':\n    s[repo]=0;json.dump(s,open(" + repr(str(state)) + ",'w'));sys.exit(0)\n"
                      "sys.exit(s.get(repo,1))\n")
    script.chmod(0o755)
    return str(script)


def test_the_sandbox_check_script_reports_what_it_sees(tmp_path):
    import file_backup_sandbox_check as C
    visible = tmp_path / 'visible'
    visible.mkdir()
    (visible / 'secret').write_text('x')
    empty = tmp_path / 'empty-dir'
    empty.mkdir()
    blank = tmp_path / 'blank'
    blank.write_text('')
    full = tmp_path / 'full'
    full.write_text('content')
    files = {}
    for name in ('r1', 'p1', 'r2', 'p2'):
        files[name] = tmp_path / name
        files[name].write_text(name + '\n')
        files[name].chmod(0o600)
    restic = fake_restic(tmp_path, {'r1': 10, 'r2': 10})
    out = io.StringIO()
    sys_stdout, sys.stdout = sys.stdout, out
    try:
        C.main(['--restic', restic, '--list', str(visible), '--read', str(visible / 'secret'), '--read', str(tmp_path / 'absent'),
                '--hidden', str(visible), '--hidden', str(empty), '--hidden', str(blank), '--hidden', str(full), '--hidden', str(tmp_path / 'absent'),
                '--repo', f'repo1,{files["r1"]},{files["p1"]}', '--repo', f'repo2,{files["r2"]},{files["p2"]},,', '--init', 'repo2'])
    finally:
        sys.stdout = sys_stdout
    doc = json.loads(out.getvalue())
    assert doc['listing'] == {str(visible): ['secret']} and doc['read'] == {str(visible / 'secret'): True, str(tmp_path / 'absent'): False}
    assert doc['hidden'] == {str(visible): False, str(empty): True, str(blank): True, str(full): False, str(tmp_path / 'absent'): True}
    # repo1 is never initialised (the restore journal's repository must already exist); repo2 only because --init names it.
    assert doc['restic'] == 'restic 0.18.0' and doc['repos'] == {'repo1': 10, 'repo2': 0} and doc['init'] == {'repo2': 0}
    assert 'r1\n' not in out.getvalue() and 'p1' not in out.getvalue().replace(str(files['p1']), '')


class FakeLibc:
    """Both handle calls as a filtered (or an unfiltered) kernel answers them."""
    def __init__(self, codes):
        self.codes = codes

    def __getattr__(self, name):
        if name not in self.codes:
            raise AttributeError(name)

        def call(*args):
            import ctypes
            code = self.codes[name]
            if code == 0:
                return 0 if name == 'name_to_handle_at' else os.open(os.devnull, os.O_RDONLY)
            ctypes.set_errno(code)
            return -1
        return call


def test_the_check_reports_each_handle_call_errno():
    import errno
    import file_backup_sandbox_check as C
    assert C.handle_calls(FakeLibc({'open_by_handle_at': errno.EPERM, 'name_to_handle_at': errno.EPERM})) == {
        'open_by_handle_at': 'EPERM', 'name_to_handle_at': 'EPERM'}
    assert C.handle_calls(FakeLibc({'open_by_handle_at': errno.ESTALE, 'name_to_handle_at': 0})) == {
        'open_by_handle_at': 'ESTALE', 'name_to_handle_at': 'succeeded'}
    assert C.handle_calls(FakeLibc({'open_by_handle_at': 0, 'name_to_handle_at': errno.EINVAL}))['open_by_handle_at'] == 'succeeded'
    assert C.handle_calls(FakeLibc({})) == {'open_by_handle_at': 'unavailable', 'name_to_handle_at': 'unavailable'}  # not Linux


def test_the_unit_entry_imports_the_executor_under_isolated_mode():
    done = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'scripts/deploy/file_backup_run.py')], capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and 'configuration unavailable' in done.stderr, done.stderr


# repo1 over DATA_HOST's rest-server --append-only (owner 2026-10-27: D4 changed to the file backup's own repository; no sftp).


def to_rest(tmp_path, config, **over):
    bundle = tmp_path / 'pki/bundle.crt'
    bundle.write_text('-----BEGIN CERTIFICATE-----\nsynthetic intermediate\n-----END CERTIFICATE-----\n'
                      '-----BEGIN CERTIFICATE-----\nsynthetic root\n-----END CERTIFICATE-----\n')
    cfg = json.loads(Path(config).read_text())
    cfg['repo1'] = {'transport': 'rest', 'host': 'data-host.example', 'port': 8000, 'cacert': str(bundle), **over}
    cfg['repo1'] = {k: v for k, v in cfg['repo1'].items() if v is not None}
    return private(Path(config), cfg)


def test_repo1_is_the_rest_server_the_ca_pinned_old_sftp_files_removed_and_append_only_proven(tmp_path):
    root, config = setup(tmp_path)
    ssh = root / 'var/lib/nexloop-file-backup/.ssh'  # what an earlier D4 (sftp) version left: removed
    ssh.mkdir(parents=True)
    for name in ('config', 'known_hosts', 'id_ed25519', 'id_ed25519.pub'):
        (ssh / name).write_text('left by the sftp version\n')
    private(root / SECRETS / 'restic-files-repo1.repository', None, 'sftp:nexloop-repo1:/restic\n')
    code, text, runner = main(root, config, stdin=f'recorder\t{RECORDER_PW}\nrepo1\t{REPO1_PW}\n')
    assert code == 2 and 'repo1_rest<TAB>password' in text  # an sftp location is never taken for the rest one
    assert not ssh.exists() and 'the SSH files of an earlier sftp version removed' in text
    code, text, runner = main(root, config)
    assert code == 0, text
    secrets = root / SECRETS
    assert (secrets / 'restic-files-repo1.repository').read_text() == f'rest:https://nexloop-file-backup:{REST_PW}@data-host.example:8000/restic/\n'
    assert st.S_IMODE((secrets / 'restic-files-repo1.repository').stat().st_mode) == 0o600
    assert 'synthetic root' in (secrets / 'rest-ca.crt').read_text() and st.S_IMODE((secrets / 'rest-ca.crt').stat().st_mode) == 0o600
    assert not any(c[0] == 'chown' and c[-1] == str(secrets / 'rest-ca.crt') for c in runner.calls)
    env = (root / 'etc/nexloop/file-backup.env').read_text()
    assert 'FILE_BACKUP_REPO1_OPTIONS=--cacert /srv/nexloop/secrets/backup/rest-ca.crt' in env
    assert REST_PW not in text and not any(REST_PW in ' '.join(c) for c in runner.calls)
    check = next(c for c in runner.calls if c[0] == 'systemd-run' and any(a.endswith('/file_backup_sandbox_check.py') for a in c))
    repos = [check[i + 1] for i, a in enumerate(check) if a == '--repo']
    assert repos[0] == 'repo1,/srv/nexloop/secrets/backup/restic-files-repo1.repository,/srv/nexloop/secrets/backup/restic-files-repo1.pass,,,' \
                       '/srv/nexloop/secrets/backup/rest-ca.crt'
    assert check[check.index('--append-only-probe') + 1] == 'repo1' and check[check.index('--probe-dir') + 1] == '/var/lib/nexloop-file-backup/staging'
    reads = [check[i + 1] for i, a in enumerate(check) if a == '--read']
    assert '/srv/nexloop/secrets/backup/rest-ca.crt' in reads and '/var/lib/nexloop-file-backup/.ssh/id_ed25519' not in reads
    assert '"append_only": {"repo1": {"backup": 0, "forget": 0, "refused": true, "still_listed": true}}' in text
    code, text, _ = main(root, config, stdin='', runner=Runner(tmp_path, user_exists=True))
    assert code == 0 and 'restic-files-repo1.repository kept' in text, text


@pytest.mark.parametrize('probe, why', [
    ({'backup': 0, 'forget': 0, 'refused': False, 'still_listed': False}, 'not append-only'),  # forget succeeded: APP_HOST can delete
    ({'backup': 0, 'forget': 0, 'refused': False, 'still_listed': True}, 'not append-only'),   # nothing removed, nothing refused: a no-op
    ({'backup': 0, 'forget': 1, 'refused': False, 'still_listed': True}, 'not append-only'),   # failed before any removal (a lock, say)
    ({'backup': 0, 'forget': 0, 'refused': True, 'still_listed': False}, 'not append-only'),   # refused, yet the snapshot is gone
    ({'backup': 0, 'forget': 0, 'still_listed': True}, 'not append-only'),                     # the old answer, without refused
    ({'backup': 0, 'forget': None, 'refused': True, 'still_listed': True}, 'not append-only'), # restic never ran
    ({'backup': 0, 'forget': 0, 'refused': 'yes', 'still_listed': True}, 'not append-only'),
    ({'backup': 0, 'forget': 0, 'refused': True, 'still_listed': 1}, 'not append-only'),
    ({'backup': 1}, 'not append-only'),                                        # no snapshot could be written
    ({'backup': 1, 'forget': 0, 'refused': True, 'still_listed': True}, 'not append-only'),  # each field is checked
    (None, 'not append-only'),                                                 # no probe answer at all
])
def test_repo1_over_the_rest_server_must_refuse_a_forget(tmp_path, probe, why):
    root, config = setup(tmp_path)
    to_rest(tmp_path, config)
    change = lambda d: {**d, 'append_only': {} if probe is None else {'repo1': probe}}  # noqa: E731
    code, text, runner = main(root, config, stdin=STDIN, runner=Runner(tmp_path, check=change))
    assert code == 1 and why in text and 'timer not enabled' in text, text
    assert not any(c[:2] in (['systemctl', 'start'], ['systemctl', 'enable']) for c in runner.calls)


def test_the_rest_configuration_and_password_are_checked(tmp_path):
    root, config = setup(tmp_path)
    for n, over in enumerate(({'port': 80}, {'cacert': 'relative'}, {'host_key': HOST_KEY}, {'transport': 'sftp'}, {'transport': 'https'},
                              {'cacert': None})):
        to_rest(tmp_path, config, **over)
        code, text, _ = main(root, config, stdin=STDIN)
        assert code == 2 and 'configuration' in text, over
    to_rest(tmp_path, config)
    for bad in (REST_PW + '@x', 'short-but-plain', REST_PW + ':y'):
        code, text, _ = main(root, config, stdin=f'recorder\t{RECORDER_PW}\nrepo1\t{REPO1_PW}\nrepo1_rest\t{bad}\n')
        assert code == 2 and 'repo1_rest' in text and bad not in text, bad


def probe_restic(tmp_path, *, forget_code, keep, says=''):
    """A stand-in for the append-only probe: backup prints a summary with a snapshot id, forget prints `says` (with {id}) to stderr and
    exits forget_code, snapshots lists the snapshot when keep."""
    snapshot = 'e' * 64
    says = says.format(id=snapshot)
    script = tmp_path / 'probe-restic'
    log = tmp_path / 'probe-calls.jsonl'
    script.write_text(f'#!{sys.executable}\nimport json,os,sys\na=sys.argv[1:]\nopen({str(log)!r},"a").write(json.dumps({{"args":a,"cwd":os.getcwd()}})+"\\n")\n'
                      'sub=a[a.index("--password-file")+2]\n'
                      f'if sub=="backup":print(json.dumps({{"message_type":"summary","snapshot_id":{snapshot!r}}}));sys.exit(0)\n'
                      f'if sub=="forget":sys.stderr.write({says!r});sys.exit({forget_code})\n'
                      f'if sub=="snapshots":print(json.dumps([{{"id":{snapshot!r}}}] if {keep!r} else []));sys.exit(0)\n'
                      'sys.exit(2)\n')
    script.chmod(0o755)
    return str(script), log, snapshot


REFUSED = 'unable to remove snapshot/{id} from the repository\n'  # restic 0.18.0 cmd_forget.go, on stderr; it then exits 0


@pytest.mark.parametrize('forget_code, keep, says, expected', [
    (0, True, REFUSED, {'backup': 0, 'forget': 0, 'refused': True, 'still_listed': True}),       # restic 0.18.0 on --append-only
    (1, True, 'x\n' + REFUSED + 'y\n', {'backup': 0, 'forget': 1, 'refused': True, 'still_listed': True}),
    (0, False, '', {'backup': 0, 'forget': 0, 'refused': False, 'still_listed': False}),         # removed
    (0, True, 'Ignoring "{id}": no matching ID found\n', {'backup': 0, 'forget': 0, 'refused': False, 'still_listed': True}),  # a no-op
    (0, True, REFUSED.replace('{id}', 'f' * 64), {'backup': 0, 'forget': 0, 'refused': False, 'still_listed': True}),  # another snapshot's
    (0, True, REFUSED.replace('snapshot/', 'index/'), {'backup': 0, 'forget': 0, 'refused': False, 'still_listed': True}),
])
def test_the_append_only_probe_takes_a_snapshot_tries_to_forget_it_and_looks_again(tmp_path, forget_code, keep, says, expected):
    """APP_HOST 2026-10-27: restic 0.18.0 exits 0 when rest-server --append-only refuses the DELETE; only its line naming this snapshot
    tells a refused removal from a forget that removed nothing."""
    import file_backup_sandbox_check as C
    restic, log, snapshot = probe_restic(tmp_path, forget_code=forget_code, keep=keep, says=says)
    staging = tmp_path / 'staging'
    staging.mkdir(mode=0o700)
    common = {'repository_file': tmp_path / 'r', 'password_file': tmp_path / 'p', 'environment': {'PATH': os.environ['PATH']}, 'timeout': 60,
              'options': ('--cacert', str(tmp_path / 'ca.crt'))}
    assert C.append_only(restic, common, staging) == expected
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    subs = [c['args'][c['args'].index('--password-file') + 2:] for c in calls]
    assert subs[0][:5] == ['backup', '--json', '--tag', 'nexloop-append-only-probe', '--host'] and subs[1] == ['forget', '--retry-lock', '1m', snapshot]
    assert subs[2] == ['snapshots', '--json', '--tag', 'nexloop-append-only-probe'] and all(c['args'][1:3] == ['--cacert', str(tmp_path / 'ca.crt')] for c in calls)
    assert Path(calls[0]['cwd']).parent == staging and list(staging.iterdir()) == []  # the probe directory removed afterwards
    assert C.append_only(restic, common, None) == {'error': 'no --probe-dir'}


def test_a_cacert_that_is_no_certificate_is_refused(tmp_path):
    root, config = setup(tmp_path)
    to_rest(tmp_path, config)
    (tmp_path / 'pki/bundle.crt').write_text('not a certificate\n')
    code, text, _ = main(root, config, stdin=STDIN)
    assert code == 2 and 'repo1.cacert' in text and not (root / SECRETS / 'rest-ca.crt').exists()


def test_the_check_pins_the_ca_for_the_repository_that_names_one(tmp_path, capsys):
    import file_backup_sandbox_check as C
    restic, log, _ = probe_restic(tmp_path, forget_code=1, keep=True)
    for name in ('r1', 'p1', 'r2', 'p2'):
        (tmp_path / name).write_text(name)
    C.main(['--restic', restic, '--repo', f'repo1,{tmp_path}/r1,{tmp_path}/p1,,,{tmp_path}/ca.crt', '--repo', f'repo2,{tmp_path}/r2,{tmp_path}/p2'])
    capsys.readouterr()
    calls = [json.loads(l)['args'] for l in log.read_text().splitlines()]
    repo1 = [c for c in calls if f'{tmp_path}/r1' in c]
    repo2 = [c for c in calls if f'{tmp_path}/r2' in c]
    assert repo1 and all(c[1:3] == ['--cacert', f'{tmp_path}/ca.crt'] for c in repo1) and repo2 and not any('--cacert' in c for c in repo2)


def test_a_srv_nexloop_of_the_deployment_user_needs_trusted_owner_and_secrets_backup_stays_roots(tmp_path):
    """Dispatcher 2026-10-27: APP_HOST's /srv/nexloop is the deployment user's too (751). secrets/backup's check would refuse it the way
    DATA_HOST's R2 was refused: without trusted_owner refused, saying what to configure; with it installed, secrets/backup root's 0700."""
    root, config = setup(tmp_path)
    UIDS[os.stat(root / 'srv/nexloop').st_ino] = DEPLOY
    os.chmod(root / 'srv/nexloop', 0o751)
    code, text, _ = main(root, config, tmp_path=tmp_path)
    assert code == 2 and 'secrets/backup' in text and 'name that user as trusted_owner' in text, text
    cfg = json.loads(Path(config).read_text())
    private(Path(config), {**cfg, 'trusted_owner': 'deploy-user'})
    code, text, _ = main(root, config, tmp_path=tmp_path)
    assert code == 0, text
    info = os.lstat(root / SECRETS)
    assert info.st_uid in OWNERS and st.S_IMODE(info.st_mode) == 0o700
    # What the installer owns is never the trusted owner's: secrets/backup of the deployment user is refused.
    UIDS[info.st_ino] = DEPLOY
    code, text, _ = main(root, config, stdin='', tmp_path=tmp_path)
    assert code == 2 and 'secrets/backup' in text and 'must be owned by root and not' in text, text


def test_trusted_owner_is_never_the_service_user_and_must_exist(tmp_path):
    for n, (name, why) in enumerate((('nexloop-file-backup', 'not nexloop-file-backup'), ('nobody-here', 'no such user'))):
        root, config = setup(tmp_path / str(n), trusted_owner=name)
        code, text, runner = main(root, config, tmp_path=tmp_path / str(n))
        assert code == 2 and why in text and runner.calls == [], text


def test_root_owned_widens_only_the_ancestors_and_never_for_a_link(tmp_path):
    top = tmp_path / 'srv'
    path = top / 'secrets' / 'backup'
    path.mkdir(parents=True)
    UIDS[os.stat(top).st_ino] = DEPLOY
    with pytest.raises(I.Refused, match='name that user as trusted_owner'):
        I.root_owned(path, what='x', owners=OWNERS)
    I.root_owned(path, what='x', owners=OWNERS, ancestors=(DEPLOY,))
    UIDS[os.stat(path).st_ino] = DEPLOY
    with pytest.raises(I.Refused, match='backup must be owned by root and not'):
        I.root_owned(path, what='x', owners=OWNERS, ancestors=(DEPLOY,))  # the path itself: root's only
    I.root_owned(path, what='x', owners=OWNERS, ancestors=(DEPLOY,), own=(DEPLOY,))
    UIDS.pop(os.stat(path).st_ino)
    os.chmod(top, 0o771)
    with pytest.raises(I.Refused, match='or trusted_owner and not group/other-writable'):
        I.root_owned(path, what='x', owners=OWNERS, ancestors=(DEPLOY,))
    os.chmod(top, 0o751)
    os.rename(top / 'secrets', top / 'real')
    os.symlink(top / 'real', top / 'secrets')
    UIDS[os.lstat(top / 'secrets').st_ino] = DEPLOY
    with pytest.raises(I.Refused, match='symbolic link'):
        I.root_owned(path, what='x', owners=OWNERS, ancestors=(DEPLOY,))


def backup_problems(root):
    """The deploy gate's own secret_distribution on this host's secrets tree: its findings for secrets/backup."""
    from nexloop_eios import deploy_checks
    found = deploy_checks.secret_distribution(root / 'srv/nexloop/secrets')['details']['secrets']['problems']
    return [p for p in found if p.endswith(':backup')]


def test_secrets_backup_passes_the_deploy_gates_secret_distribution_and_a_rerun_moves_the_earlier_layout(tmp_path):
    """Release 51 (2026-10-27): secret_distribution failed with directory_permissions:backup - secrets/backup was root 0711 and its two
    CA files root 0644. Now the directory is root 0700 and every file 0600; a rerun moves an earlier install to it."""
    root, config = setup(tmp_path)
    code, text, _ = main(root, config, tmp_path=tmp_path)
    assert code == 0, text
    secrets = root / SECRETS
    assert backup_problems(root) == []
    assert st.S_IMODE(secrets.stat().st_mode) == 0o700 and all(st.S_IMODE(f.stat().st_mode) == 0o600 for f in secrets.iterdir())
    os.chmod(secrets, 0o711)  # what release 50's installer left
    for name in ('pg-ca.crt', 'rest-ca.crt'):
        os.chmod(secrets / name, 0o644)
    assert backup_problems(root) == ['directory_permissions:backup']
    code, text, _ = main(root, config, stdin='', tmp_path=tmp_path)
    assert code == 0 and 'pg-ca.crt written' in text and 'rest-ca.crt written' in text, text  # same bytes, the mode moved
    assert backup_problems(root) == [] and st.S_IMODE(secrets.stat().st_mode) == 0o700
    assert [st.S_IMODE((secrets / n).stat().st_mode) for n in ('pg-ca.crt', 'rest-ca.crt')] == [0o600, 0o600]


def test_the_unit_reads_a_root_0700_directory_only_through_its_capability_and_the_maintenance_binds_files_one_by_one():
    """What secrets/backup root 0700 with root's 0600 CA files relies on (NX-035-file-backup.md §6.7): the file backup unit traverses
    and reads them with CAP_DAC_READ_SEARCH and binds the directory itself; nexloop-file-backup-maint, without any capability, binds
    its three repo2 files one by one (systemd resolves the sources as root), never the directory; and step 5 reads every secret
    from inside the sandbox, so a unit that lost the capability fails there, before any timer."""
    units = ROOT / 'deploy/stage/systemd'
    backup = (units / 'nexloop-file-backup.service.example').read_text()
    maint = (units / 'nexloop-file-backup-maint.service.example').read_text()
    assert 'AmbientCapabilities=CAP_DAC_READ_SEARCH\n' in backup and 'CapabilityBoundingSet=CAP_DAC_READ_SEARCH\n' in backup
    assert any(line.startswith('BindReadOnlyPaths=') and '/srv/nexloop/secrets/backup' in line.split('=', 1)[1].split() for line in backup.splitlines())
    [binds] = [line.split('=', 1)[1].split() for line in maint.splitlines() if line.startswith('BindReadOnlyPaths=')]
    assert '/srv/nexloop/secrets/backup' not in binds and all(b.startswith('/srv/nexloop/secrets/backup/') for b in binds)
    assert 'CapabilityBoundingSet=\n' in maint
