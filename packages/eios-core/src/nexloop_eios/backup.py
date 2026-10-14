"""NX-035 local part: PG base backup with manifest, restore-journal export, point-in-time restore behind the restore fence, and
the migration gate (docs/implementation/NX-035-design.md; local implementation notes in NX-035-local-implementation.md).

At deployment the PG backup is pgBackRest's (D1): ``nexloop-backup pg`` runs ``pgbackrest backup`` for one repo, then
``pgbackrest verify`` and ``pgbackrest info --output=json``, and records the manifest rows with the tool, its version and the
verification evidence (manifest schema nexloop-backup-manifest/2, docs/implementation/NX-035-pgbackrest.md). A failed verify is
recorded 'failed', which the migration gate refuses. ``--tool pg_basebackup`` keeps the local path: pg_basebackup over the
replication protocol as the non-superuser ``nexloop_backup`` role and pg_verifybackup. restic (files) is the file backup's.

Journal segments are hash-chained JSON files written 0600 into a private directory; they hold sequence numbers, ids, hashes and
times only. Encryption at rest is the file backup repository's (restic) at deployment — a gate before the controlled trial
run; segments written locally are not encrypted (dispatcher ruling Q1, 2026-10-11).

Every credential is a 0600 file read with read_private_text; nothing is passed as an argument value or environment value.

    nexloop-backup  pg      --recorder-dsn-file F --pgbackrest-command-file F --stanza S [--pgbackrest-config-file F]
                            [--expected-version V] [--repo repo1|repo2] [--kind pg_full|pg_diff|pg_incr]
    nexloop-backup  pg      --tool pg_basebackup --recorder-dsn-file F --replication-dsn-file F --pg-bin D --target D [--repo repo1] [--kind pg_full]
    nexloop-backup  journal --recorder-dsn-file F --journal-dir D [--repo repo1]
    nexloop-migrate         --database-url-file F
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from nexloop_eios.private_configuration import read_private_text

SEGMENT_SCHEMA = 'nexloop-restore-journal/1'
GENESIS = '0' * 64
_SEGMENT = re.compile(r'journal-(\d{12})-(\d{12})\.json')
_LABEL = re.compile(r'[A-Za-z0-9._:-]{1,64}')
_REF = re.compile(r'[A-Za-z0-9._:-]{1,64}')


class BackupUnavailable(RuntimeError):
    pass


class JournalBroken(RuntimeError):
    """The segment chain does not verify; the restore stops for manual handling (design §6.2)."""


class RestoreRefused(RuntimeError):
    pass


class MigrationRefused(RuntimeError):
    def __init__(self, reasons):
        super().__init__('migration refused: ' + ','.join(reasons))
        self.reasons = list(reasons)


def _now():
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def _call(conninfo, sql, params=()):
    with psycopg.connect(conninfo, autocommit=True) as db:
        return db.execute(sql, params).fetchone()[0]


def record(recorder_conninfo, entry):
    return _call(recorder_conninfo, 'select authz.nexloop_record_backup(%s)', (Jsonb(entry),))


# PG base backup ---------------------------------------------------------------------------------------------------------
def take_pg_backup(*, recorder_conninfo, replication_conninfo, pg_bin, target, repo='repo1', kind='pg_full', backup_id=None,
                   release_id=None, key_ref=None):
    """pg_basebackup (plain format, WAL streamed, SHA-256 manifest) into a new directory, then pg_verifybackup; one pending and
    one verified/failed manifest row. The target must not exist."""
    target, pg_bin = Path(target), Path(pg_bin)
    if target.exists():
        raise BackupUnavailable('backup target exists')
    if kind not in ('pg_full', 'pg_diff', 'pg_incr'):
        raise BackupUnavailable('backup kind invalid')
    backup_id = backup_id or datetime.now(UTC).strftime('%Y%m%d-%H%M%S') + '-' + kind.split('_')[1][0].upper()
    if not _LABEL.fullmatch(backup_id):
        raise BackupUnavailable('backup id invalid')
    pending = {'backup_id': backup_id, 'repo': repo, 'kind': kind, 'verify_status': 'pending', 'tool': 'pg_basebackup'}
    if release_id:pending['release_id'] = release_id
    if key_ref:pending['key_ref'] = key_ref
    record(recorder_conninfo, pending)
    taken = subprocess.run([str(pg_bin / 'pg_basebackup'), '-d', replication_conninfo, '-D', str(target), '-Fp', '-X', 'stream', '-c', 'fast',
                            '--manifest-checksums=SHA256', '-l', backup_id, '--no-password'], capture_output=True, text=True, timeout=3600)
    status, manifest = 'failed', None
    if taken.returncode == 0:
        manifest = json.loads((target / 'backup_manifest').read_text())
        checked = subprocess.run([str(pg_bin / 'pg_verifybackup'), str(target)], capture_output=True, text=True, timeout=3600)
        status = 'verified' if checked.returncode == 0 else 'failed'
    if manifest is None:
        # Nothing usable was written; the pending row stays as the trace of the attempt.
        raise BackupUnavailable('pg_basebackup failed')
    ranges = manifest['WAL-Ranges']
    final = {'backup_id': backup_id, 'repo': repo, 'kind': kind, 'verify_status': status, 'completed_at': _now(),
             'system_identifier': str(manifest['System-Identifier']), 'timeline': int(ranges[-1]['Timeline']),
             'start_lsn': ranges[0]['Start-LSN'], 'stop_lsn': ranges[-1]['End-LSN']}
    record(recorder_conninfo, final)
    return {'backup_id': backup_id, 'verify_status': status, 'system_identifier': final['system_identifier'], 'timeline': final['timeline'],
            'start_lsn': final['start_lsn'], 'stop_lsn': final['stop_lsn']}


# pgBackRest (deployment) ------------------------------------------------------------------------------------------------------
VERIFICATION_SCHEMA = 'nexloop-backup-verification/1'
PGBACKREST_TYPES = {'pg_full': 'full', 'pg_diff': 'diff', 'pg_incr': 'incr'}
_STANZA = re.compile(r'[A-Za-z0-9._-]{1,64}')
_VERSION = re.compile(r'pgBackRest ([0-9]+\.[0-9]+(?:\.[0-9]+)?)')
_SUMMARY_SAFE = re.compile(r'[^A-Za-z0-9 ._:/=,()\-]')


def _lsn(value):
    hi, lo = str(value).split('/')
    return f'{int(hi, 16):X}/{int(lo, 16):X}'


def _iso(epoch):
    return datetime.fromtimestamp(int(epoch), UTC).isoformat().replace('+00:00', 'Z')


class PgBackRest:
    """The pgbackrest command line for one stanza: ``command`` is the argv prefix that runs pgbackrest (for example through the
    PG container); the configuration with the repository passphrases and object-store keys is pgbackrest's own private file,
    named here but never read beyond its permission check and never logged."""

    def __init__(self, command, stanza, *, config_file=None, timeout=86400):
        if type(command) is not list or not command or any(type(x) is not str or not x or '\n' in x for x in command):
            raise BackupUnavailable('pgbackrest command invalid')
        if not _STANZA.fullmatch(stanza or ''):
            raise BackupUnavailable('stanza invalid')
        if config_file is not None:
            read_private_text(config_file, maximum=65536)  # 0600 and readable; the content stays pgbackrest's
        self.prefix = [*command, *([f'--config={config_file}'] if config_file is not None else []), f'--stanza={stanza}']
        self.stanza, self.timeout = stanza, timeout

    def run(self, *args, timeout=None):
        return subprocess.run([*self.prefix, *args], capture_output=True, text=True, timeout=timeout or self.timeout)

    def version(self):
        found = _VERSION.search(self.run('version', timeout=60).stdout)
        if not found:
            raise BackupUnavailable('pgbackrest version unavailable')
        return found.group(1)

    def info(self, repo_key):
        done = self.run(f'--repo={repo_key}', 'info', '--output=json', timeout=600)
        if done.returncode != 0:
            raise BackupUnavailable('pgbackrest info failed')
        stanzas = [s for s in json.loads(done.stdout) if s.get('name') == self.stanza]
        if len(stanzas) != 1:
            raise BackupUnavailable('pgbackrest stanza missing from info')
        return stanzas[0]


def take_pgbackrest_backup(*, recorder_conninfo, command, stanza, repo='repo1', kind='pg_full', config_file=None, expected_version=None,
                           backup_id=None, release_id=None, key_ref=None, tool=None):
    """One pgBackRest backup of ``kind`` into ``repo``, then ``verify`` and ``info``; one pending row and one verified/failed row
    with the verification evidence. A backup that pgBackRest did not complete leaves the pending row only (as pg_basebackup)."""
    if kind not in PGBACKREST_TYPES:
        raise BackupUnavailable('backup kind invalid')
    if repo not in ('repo1', 'repo2'):
        raise BackupUnavailable('repo invalid')
    repo_key = int(repo[-1])
    tool = tool or PgBackRest(command, stanza, config_file=config_file)
    version = tool.version()
    if expected_version is not None and version != expected_version:
        raise BackupUnavailable(f'pgbackrest version {version} is not the pinned {expected_version}')
    # Unique per attempt (microseconds): a second backup in the same second is a new attempt, never a replay of the first.
    backup_id = backup_id or datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f') + '-' + PGBACKREST_TYPES[kind][0].upper()
    if not _LABEL.fullmatch(backup_id):
        raise BackupUnavailable('backup id invalid')
    before = {b['label'] for b in tool.info(repo_key).get('backup', [])}
    pending = {'backup_id': backup_id, 'repo': repo, 'kind': kind, 'verify_status': 'pending', 'tool': 'pgbackrest', 'tool_version': version}
    if release_id:pending['release_id'] = release_id
    if key_ref:pending['key_ref'] = key_ref
    record(recorder_conninfo, pending)
    taken = tool.run(f'--repo={repo_key}', f'--type={PGBACKREST_TYPES[kind]}', 'backup')
    if taken.returncode != 0:
        raise BackupUnavailable('pgbackrest backup failed')
    verified = tool.run(f'--repo={repo_key}', 'verify')
    stanza_info = tool.info(repo_key)
    made = [b for b in stanza_info.get('backup', []) if b.get('label') not in before and b.get('type') == PGBACKREST_TYPES[kind]]
    if len(made) != 1:
        raise BackupUnavailable('pgbackrest backup not found in info')
    made = made[0]
    database = next((d for d in stanza_info.get('db', []) if d.get('id') == (made.get('database') or {}).get('id')
                     and d.get('repo-key', repo_key) == (made.get('database') or {}).get('repo-key', repo_key)), None)
    if database is None:
        raise BackupUnavailable('pgbackrest backup database missing from info')
    status_info = stanza_info.get('status') or {}
    summary = (verified.stdout.strip().splitlines() or verified.stderr.strip().splitlines() or [''])[-1]
    evidence = {'schema': VERIFICATION_SCHEMA, 'tool': 'pgbackrest', 'tool_version': version, 'stanza': stanza, 'repo_key': repo_key,
                'label': made['label'], 'type': made['type'], 'prior': made.get('prior'),
                'timestamp_start': _iso(made['timestamp']['start']), 'timestamp_stop': _iso(made['timestamp']['stop']),
                'archive_start': made['archive']['start'], 'archive_stop': made['archive']['stop'],
                'size': (made.get('info') or {}).get('size'), 'repository_size': ((made.get('info') or {}).get('repository') or {}).get('size'),
                'backup_error': bool(made.get('error')), 'info_status': {'code': status_info.get('code'), 'message': status_info.get('message')},
                'db_version': database.get('version'), 'backrest_format': (made.get('backrest') or {}).get('format'),
                'verify': {'exit_code': verified.returncode, 'summary': _SUMMARY_SAFE.sub('', summary)[:200]}}
    ok = verified.returncode == 0 and not evidence['backup_error'] and status_info.get('code') == 0
    status = 'verified' if ok else 'failed'
    final = {'backup_id': backup_id, 'repo': repo, 'kind': kind, 'verify_status': status, 'completed_at': evidence['timestamp_stop'],
             'system_identifier': str(database['system-id']), 'timeline': int(made['archive']['start'][:8], 16),
             'start_lsn': _lsn(made['lsn']['start']), 'stop_lsn': _lsn(made['lsn']['stop']), 'verification': evidence}
    record(recorder_conninfo, final)
    return {'backup_id': backup_id, 'verify_status': status, 'label': made['label'], 'tool_version': version, 'system_identifier': final['system_identifier'],
            'timeline': final['timeline'], 'start_lsn': final['start_lsn'], 'stop_lsn': final['stop_lsn'], 'verify_exit_code': verified.returncode}


# Restore journal --------------------------------------------------------------------------------------------------------
def _segments(journal_dir):
    found = []
    for path in Path(journal_dir).iterdir():
        match = _SEGMENT.fullmatch(path.name)
        if match:found.append((int(match.group(1)), int(match.group(2)), path))
    return sorted(found)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_journal(journal_dir, after_seq=0):
    """Every exported entry with seq > after_seq, after verifying the whole chain from the first segment."""
    previous, last_to, entries = GENESIS, 0, []
    for start, end, path in _segments(journal_dir):
        segment = json.loads(path.read_text())
        if (segment.get('schema') != SEGMENT_SCHEMA or segment.get('prev_sha256') != previous or segment.get('from_seq') != start
                or segment.get('to_seq') != end or start <= last_to or [e['seq'] for e in segment['entries']] != sorted(e['seq'] for e in segment['entries'])
                or not segment['entries'] or segment['entries'][0]['seq'] != start or segment['entries'][-1]['seq'] != end):
            raise JournalBroken('restore journal chain broken at ' + path.name)
        previous, last_to = _digest(path), end
        entries.extend(e for e in segment['entries'] if e['seq'] > after_seq)
    return entries


def export_journal(*, recorder_conninfo, journal_dir, repo='repo1', batch=10000):
    """Append the entries recorded since the last segment as new hash-chained segments (0600) and record the export position."""
    journal_dir = Path(journal_dir)
    segments = _segments(journal_dir)
    previous = _digest(segments[-1][2]) if segments else GENESIS
    after = segments[-1][1] if segments else 0
    read_journal(journal_dir)  # never extend a broken chain
    written, started = [], _now()
    while True:
        page = _call(recorder_conninfo, 'select authz.nexloop_restore_journal_since(%s,%s)', (after, batch))
        if not page['entries']:break
        start, end = page['entries'][0]['seq'], page['entries'][-1]['seq']
        body = json.dumps({'schema': SEGMENT_SCHEMA, 'from_seq': start, 'to_seq': end, 'prev_sha256': previous, 'entries': page['entries']},
                          ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        path = journal_dir / f'journal-{start:012d}-{end:012d}.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        previous, after = _digest(path), end
        written.append(path.name)
    if written:
        record(recorder_conninfo, {'backup_id': f'journal-{after:012d}', 'repo': repo, 'kind': 'journal', 'verify_status': 'verified',
                                   'started_at': started, 'completed_at': _now(), 'journal_seq': after})
    return {'segments': written, 'exported_seq': after}


# Drill ------------------------------------------------------------------------------------------------------------------
def drill_mark(recorder_conninfo, drill_id):
    return _call(recorder_conninfo, 'select authz.nexloop_drill_mark(%s)', (drill_id,))


def record_drill_result(recorder_conninfo, drill_id, *, passed, rto_seconds, rpo_seconds, checks):
    return _call(recorder_conninfo, 'select authz.nexloop_record_drill_result(%s,%s,%s::numeric,%s::numeric,%s)',
                 (drill_id, 'passed' if passed else 'failed', rto_seconds, rpo_seconds, Jsonb(checks)))


# Point-in-time restore --------------------------------------------------------------------------------------------------
def _port_free(port):
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


class RestoredInstance:
    def __init__(self, pg_bin, data, socket_dir, port, owner_user):
        self.pg_bin, self.data, self.socket_dir, self.port, self.owner_user = Path(pg_bin), Path(data), Path(socket_dir), port, owner_user

    @property
    def conninfo(self):
        return f'host={self.socket_dir} port={self.port} dbname=postgres user={self.owner_user}'

    def stop(self):
        subprocess.run([str(self.pg_bin / 'pg_ctl'), '-D', str(self.data), '-m', 'fast', '-w', 'stop'], check=True, capture_output=True)


def restore(*, pg_bin, base_backup, wal_archive, target, socket_dir, port, owner_user, restore_ref, window_start, window_end,
            journal_dir, evidence, recovery_target_name=None, recovery_target_time=None, backup_id=None, running_ports=(),
            old_primary_fenced=False, hba_method='peer', restore_started_at=None):
    """Restore a base backup plus archived WAL into a new directory on a new, socket-only port; set every application role
    NOLOGIN; write the fence; replay the journal; reconcile the effect window; verify the migration lineage; end in
    verified_readonly (design §6.1–§6.5). Returns the running instance and a numbers-only report."""
    started = time.monotonic()
    restore_started_at = restore_started_at or datetime.now(UTC)
    pg_bin, base_backup, target, socket_dir = Path(pg_bin), Path(base_backup), Path(target), Path(socket_dir)
    if not _REF.fullmatch(restore_ref or ''):
        raise RestoreRefused('restore reference invalid')
    if not old_primary_fenced:
        raise RestoreRefused('old primary must be fenced first (--old-primary-fenced)')
    if (recovery_target_name is None) == (recovery_target_time is None):
        raise RestoreRefused('exactly one recovery target required')
    if target.exists() and any(target.iterdir()):
        raise RestoreRefused('restore target is not empty')
    if port in running_ports or not _port_free(port):
        raise RestoreRefused('restore port is in use')
    if not (base_backup / 'backup_manifest').is_file():
        raise RestoreRefused('base backup manifest missing')
    shutil.copytree(base_backup, target, dirs_exist_ok=True)
    target.chmod(0o700)
    (target / 'recovery.signal').write_text('')
    archive = str(Path(wal_archive))
    settings = {'restore_command': f'cp "{archive}/%f" "%p"', 'recovery_target_action': 'promote', 'listen_addresses': '',
                'port': str(port), 'unix_socket_directories': str(socket_dir), 'archive_mode': 'off'}
    if recovery_target_name:settings['recovery_target_name'] = recovery_target_name
    else:settings['recovery_target_time'] = recovery_target_time
    with open(target / 'postgresql.auto.conf', 'a') as conf:
        for key, value in settings.items():
            conf.write(f"{key} = '{value}'\n")
    # Only the owner, only over the local socket, until the instance is opened (§6.1 step 2).
    (target / 'pg_hba.conf').write_text(f'local all {owner_user} {hba_method}\n')
    manifest = json.loads((base_backup / 'backup_manifest').read_text())
    old_timeline = int(manifest['WAL-Ranges'][-1]['Timeline'])
    subprocess.run([str(pg_bin / 'pg_ctl'), '-D', str(target), '-l', str(target / 'restore.log'), '-w', '-t', '300', 'start'], check=True, capture_output=True)
    instance = RestoredInstance(pg_bin, target, socket_dir, port, owner_user)
    try:
        deadline = time.monotonic() + 300
        while True:
            with psycopg.connect(instance.conninfo, autocommit=True) as db:
                if not db.execute('select pg_is_in_recovery()').fetchone()[0]:break
            if time.monotonic() > deadline:raise RestoreRefused('restore did not reach its target')
            time.sleep(0.2)
        report = {'restore_ref': restore_ref}
        with psycopg.connect(instance.conninfo, autocommit=True) as db:
            # Application roles cannot log in; their passwords from the backup are never used again (rotation at opening).
            roles = [r for (r,) in db.execute("select rolname from pg_roles where left(rolname,8)='nexloop_' and rolcanlogin and rolname<>%s order by 1",
                                              (owner_user,)).fetchall()]
            for role in roles:
                db.execute(sql.SQL('alter role {} nologin').format(sql.Identifier(role)))
            report['roles_set_nologin'] = len(roles)
            source = {'backup_id': backup_id} if backup_id else {}
            if recovery_target_name:source['target_name'] = recovery_target_name
            else:source['target_time'] = recovery_target_time
            with db.transaction():
                db.execute('set local role nexloop_owner')
                begun = db.execute('select runtime.nexloop_restore_begin(%s,%s,%s,%s,%s)',
                                   (restore_ref, Jsonb(source), window_start, restore_started_at, old_timeline)).fetchone()[0]
            report['journal_from'] = begun['journal_from']
            entries = read_journal(journal_dir, begun['journal_from'])
            report['journal_entries'] = len(entries)
            with db.transaction():
                db.execute('set local role nexloop_owner')
                report['replay'] = db.execute('select runtime.nexloop_restore_replay(%s,%s)', (restore_ref, Jsonb(entries))).fetchone()[0]
                report['replayed'] = db.execute('select runtime.nexloop_restore_replayed(%s)', (restore_ref,)).fetchone()[0]
            with db.transaction():
                db.execute('set local role nexloop_owner')
                report['window'] = db.execute('select runtime.nexloop_restore_reconcile(%s,%s,%s)', (restore_ref, window_end, Jsonb(evidence))).fetchone()[0]
            from nexloop_eios.bootstrap import verify
            report['lineage'] = verify(db)
            nologin = db.execute("select count(*) from pg_roles where left(rolname,8)='nexloop_' and rolcanlogin and rolname<>%s", (owner_user,)).fetchone()[0]
            if nologin:
                raise RestoreRefused('an application role can still log in')
            with db.transaction():
                db.execute('set local role nexloop_owner')
                report['verified'] = db.execute('select runtime.nexloop_restore_verified(%s)', (restore_ref,)).fetchone()[0]
        report['tool_seconds'] = round(time.monotonic() - started, 3)
        return instance, report
    except BaseException:
        instance.stop()
        raise


# Migration gate ---------------------------------------------------------------------------------------------------------
def migration_gate(connection):
    return connection.execute('select authz.nexloop_migration_gate()').fetchone()[0]


def migrate(connection, *, migrations=None):
    """Apply reviewed migrations to an existing NexLoop database only when the gate allows it. Refused: nothing is applied and
    no migration file is read. There is no exemption (design §7.2; dispatcher ruling Q3)."""
    gate = migration_gate(connection)
    if not gate['allowed']:
        raise MigrationRefused(gate['reasons'])
    from eios.adapters.postgres.migrations import apply_migrations
    from nexloop_eios.bootstrap import catalog
    return apply_migrations(connection, migrations=catalog() if migrations is None else migrations)


# CLI --------------------------------------------------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, 'configuration unavailable\n')


def backup_main(argv=None):
    parser = _Parser(description='NexLoop backup executor (NX-035)')
    sub = parser.add_subparsers(dest='command', required=True)
    pg = sub.add_parser('pg')
    pg.add_argument('--tool', choices=('pgbackrest', 'pg_basebackup'), default='pgbackrest')
    pg.add_argument('--recorder-dsn-file', type=Path, required=True)
    for name in ('replication-dsn-file', 'pg-bin', 'target', 'pgbackrest-command-file', 'pgbackrest-config-file'):
        pg.add_argument('--' + name, type=Path)
    pg.add_argument('--stanza')
    pg.add_argument('--expected-version')
    pg.add_argument('--repo', choices=('repo1', 'repo2'), default='repo1')
    pg.add_argument('--kind', choices=('pg_full', 'pg_diff', 'pg_incr'), default='pg_full')
    journal = sub.add_parser('journal')
    journal.add_argument('--recorder-dsn-file', type=Path, required=True)
    journal.add_argument('--journal-dir', type=Path, required=True)
    journal.add_argument('--repo', choices=('repo1', 'repo2'), default='repo1')
    args = parser.parse_args(argv)
    try:
        recorder = read_private_text(args.recorder_dsn_file, maximum=4096)
        if args.command == 'pg' and args.tool == 'pgbackrest':
            if args.pgbackrest_command_file is None or args.stanza is None or args.replication_dsn_file or args.pg_bin or args.target:
                raise BackupUnavailable('pgbackrest arguments')
            result = take_pgbackrest_backup(recorder_conninfo=recorder, command=json.loads(Path(args.pgbackrest_command_file).read_text()),
                                            stanza=args.stanza, repo=args.repo, kind=args.kind, config_file=args.pgbackrest_config_file,
                                            expected_version=args.expected_version)
            code = 0 if result['verify_status'] == 'verified' else 1
        elif args.command == 'pg':
            if not (args.replication_dsn_file and args.pg_bin and args.target) or args.pgbackrest_command_file or args.stanza:
                raise BackupUnavailable('pg_basebackup arguments')
            result = take_pg_backup(recorder_conninfo=recorder, replication_conninfo=read_private_text(args.replication_dsn_file, maximum=4096),
                                    pg_bin=args.pg_bin, target=args.target, repo=args.repo, kind=args.kind)
            code = 0 if result['verify_status'] == 'verified' else 1
        else:
            result, code = export_journal(recorder_conninfo=recorder, journal_dir=args.journal_dir, repo=args.repo), 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 1
    print(json.dumps({'ok': code == 0, **result}))
    return code


def migrate_main(argv=None):
    parser = _Parser(description='Apply reviewed NexLoop migrations to an existing database behind the backup gate (NX-035)')
    parser.add_argument('--database-url-file', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with psycopg.connect(read_private_text(args.database_url_file, maximum=4096), autocommit=True) as db:
            applied = migrate(db)
    except MigrationRefused as refused:
        print(json.dumps({'migrated': False, 'reasons': refused.reasons}))
        return 1
    except Exception as error:
        print(json.dumps({'migrated': False, 'error': type(error).__name__}))
        return 1
    print(json.dumps({'migrated': True, 'applied': applied}))
    return 0
