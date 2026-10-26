"""NX-035 deploy step 5.1 and M8: the host-side unit examples in deploy/stage/systemd.

Static checks (systemd-analyze verify also runs when the tool exists; neither the development machine nor the CI user has it):
INI structure, every backup service a oneshot under its backup identity behind RequiresMountsFor on the encrypted volumes, a
private EnvironmentFile and no secret value inline, schedules equal to backup.v1.json, the pgBackRest TLS server for DATA_HOST
only, and no host name, address or tenant written into any example."""
import configparser
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / 'deploy/stage/systemd'
PLAN = json.loads((ROOT / 'deploy/configuration/backup.v1.json').read_text())
BACKUP = {'nexloop-pg-full': 'data', 'nexloop-pg-diff': 'data', 'nexloop-journal': 'data', 'nexloop-journal-restic@': 'data',
          'nexloop-restic-maint': 'data', 'nexloop-file-backup': 'app'}
ENCRYPTED = ('/srv/nexloop/secrets', '/srv/nexloop-backup/')


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=('=',))
    parser.optionxform = str
    text = (UNITS / name).read_text()
    # systemd allows repeated keys (several ExecStart=); keep them all.
    sections, current = {}, None
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        if line.startswith('['):
            current = sections.setdefault(line.strip('[]'), {})
            continue
        key, _, value = line.partition('=')
        current.setdefault(key, []).append(value)
    parser.read_string(text)  # also a plain INI parse must succeed
    return sections


def test_every_backup_service_is_a_private_oneshot_behind_the_encrypted_volumes():
    for name, host in BACKUP.items():
        s = unit(f'{name}.service.example')
        service = s['Service']
        assert service['Type'] == ['oneshot'], name
        assert service['User'] == [{'data': 'nexloop-backup', 'app': 'nexloop-file-backup'}[host]], name
        mounts = s['Unit']['RequiresMountsFor'][0].split()
        assert mounts and all(m.startswith(ENCRYPTED) for m in mounts), name
        assert service['EnvironmentFile'][0].startswith('/etc/nexloop/') and service['EnvironmentFile'][0].endswith('.env'), name
        assert service['UMask'] == ['0077'] and service['NoNewPrivileges'] == ['true'] and service['ProtectSystem'] == ['strict'], name
        for command in service['ExecStart']:
            assert command.split()[0].startswith(('/opt/nexloop/backup-venv/bin/', '/usr/local/bin/restic', '/bin/sh')), (name, command)
            # Only paths reach a command: no password, key or DSN value, no repository location on the command line.
            literal = re.sub(r'\$\{[A-Z0-9_]+\}', '', command)  # variable references hold paths from the private env file
            assert not re.search(r'password(?!-file)|sk-|postgres(ql)?://|s3:|sftp:|aws_', literal, re.I), (name, command)
            assert all(re.fullmatch(r'\$\{[A-Z0-9_]+\}(/[a-z_.-]+)?', a) for a in re.findall(r'--(?:password-file|repository-file|recorder-dsn-file) (\S+)', command)), command
    text = ''.join(p.read_text() for p in UNITS.glob('*.example'))
    addresses = set(re.findall(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', text))
    assert addresses <= {'127.0.0.1'} and '.invalid' not in text  # loopback only (the runtime worker's local Host)


def test_schedules_match_the_backup_plan():
    calendar = lambda name: unit(f'{name}.timer.example')['Timer']['OnCalendar'][0]
    assert PLAN['pg']['full_every_hours'] == 168 and calendar('nexloop-pg-full').startswith('Sun ')
    assert PLAN['pg']['differential_every_hours'] == 24 and calendar('nexloop-pg-diff').startswith('Mon..Sat ')
    assert PLAN['journal']['export_every_seconds'] == 60 and calendar('nexloop-journal') == '*-*-* *:*:00'
    assert PLAN['files']['snapshot_every_minutes'] == 15 and calendar('nexloop-file-backup').startswith('*-*-* *:0/15:')
    assert calendar('nexloop-journal-restic@repo1').startswith('*-*-* *:0/5:') and calendar('nexloop-journal-restic@repo2').startswith('*-*-* *:0/15:')
    for name in ('nexloop-pg-full', 'nexloop-pg-diff', 'nexloop-journal', 'nexloop-journal-restic@repo1', 'nexloop-journal-restic@repo2',
                 'nexloop-restic-maint', 'nexloop-file-backup'):
        t = unit(f'{name}.timer.example')
        assert t['Timer']['Persistent'] == ['true'] and t['Install']['WantedBy'] == ['timers.target'], name
    # PG backups go to both repositories; repo2 (offsite) after repo1.
    for name, kind in (('nexloop-pg-full', 'pg_full'), ('nexloop-pg-diff', 'pg_diff')):
        commands = unit(f'{name}.service.example')['Service']['ExecStart']
        assert [re.search(r'--repo (repo\d)', c).group(1) for c in commands] == ['repo1', 'repo2']
        assert all(f'--kind {kind}' in c and '--expected-version ${PGBACKREST_VERSION}' in c for c in commands)
    maint = ' '.join(unit('nexloop-restic-maint.service.example')['Service']['ExecStart'])
    # Grouped by tag (host,tags): the journal and each file snapshot kind keep D4's window; host,paths (restic's default) would keep
    # every file snapshot forever, each from its own staging directory.
    # --retry-lock (L6 S2): the exclusive prune lock waits for a backup's shared lock instead of failing the daily run.
    assert maint.count('forget --retry-lock 10m --group-by host,tags --keep-within 14d --keep-monthly 1 --prune') == 2 and maint.count('check --read-data-subset 1/10') == 2
    assert maint.count(' forget ') == 2
    assert '$$(date +%%u)' in maint  # systemd escapes: the shell sees $(date +%u)


def test_the_file_backup_unit_is_marked_not_enabled_for_the_deploy_days():
    text = (UNITS / 'nexloop-file-backup.service.example').read_text()
    assert 'NOT ENABLED' in text and 'M12' in text
    commands = unit('nexloop-file-backup.service.example')['Service']['ExecStart']
    assert sorted(re.search(r'--kind (\w+)', c).group(1) + '/' + re.search(r'--repo (repo\d)', c).group(1) for c in commands) == [
        'artifact/repo1', 'artifact/repo2', 'runtime/repo1', 'runtime/repo2']
    assert all(('--environment-file' in c) == ('repo2' in c) for c in commands)


def test_the_pgbackrest_tls_server_serves_repo1_to_the_data_host_client_only():
    s = unit('nexloop-pgbackrest-server.service.example')
    assert s['Service']['User'] == ['nexloop-pgbackrest'] and s['Service']['ExecStart'] == ['/usr/bin/pgbackrest --config=/etc/pgbackrest/pgbackrest-server.conf server']
    # repo1 is the owner's mount point of the encrypted volume (<PG_BACKUP_REPO>, the same value in the unit and the conf).
    assert s['Unit']['RequiresMountsFor'][0].split() == ['/srv/nexloop/secrets', '<PG_BACKUP_REPO>'] and s['Service']['ReadWritePaths'] == ['<PG_BACKUP_REPO>']
    conf = (UNITS / 'pgbackrest-server.conf.example').read_text()
    assert 'repo1-path=<PG_BACKUP_REPO>' in conf
    # The certificate files are the APP_HOST host files stage_secrets generates (pgbackrest-server/).
    files = dict(re.findall(r'^tls-server-(cert|key|ca)-file=(.*)$', conf, re.M))
    assert files == {k: f'/srv/nexloop/secrets/pgbackrest-server/{n}' for k, n in (('cert', 'server.crt'), ('key', 'server.key'), ('ca', 'ca.crt'))}
    assert 'tls-server-port=<PGBACKREST_TLS_PORT>' in conf and 'tls-server-address=<APP_HOST_ADDR>' in conf
    assert re.findall(r'^tls-server-auth=(.*)$', conf, re.M) == ['pgbackrest-data-client=nexloop']
    assert 'cipher-pass' not in conf


@pytest.mark.skipif(shutil.which('systemd-analyze') is None, reason='systemd-analyze is not installed here; the static checks above apply')
def test_systemd_analyze_verify_accepts_the_units(tmp_path):
    for path in UNITS.glob('*.example'):
        if path.name.endswith(('.service.example', '.timer.example')):
            (tmp_path / path.name.removesuffix('.example')).write_text(path.read_text())
    run = subprocess.run(['systemd-analyze', 'verify', '--man=no', *sorted(str(p) for p in tmp_path.glob('nexloop-*'))],
                         capture_output=True, text=True, timeout=60)
    errors = [l for l in run.stderr.splitlines() if 'Unknown' in l or 'Invalid' in l or 'Failed to parse' in l]
    assert not errors, errors
