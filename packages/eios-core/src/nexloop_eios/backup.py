"""NX-035 local part: PG base backup with manifest, restore-journal export, point-in-time restore behind the restore fence, and
the migration gate (docs/implementation/NX-035-design.md; local implementation notes in NX-035-local-implementation.md).

Local tools are PostgreSQL's own: pg_basebackup over the replication protocol as the non-superuser ``nexloop_backup`` role,
pg_verifybackup for verification, WAL archived by ``archive_command``. At deployment pgBackRest (repo1/repo2, ``pgbackrest
verify``) and restic (files, client-side encryption) take their place (D1); the manifest rows and the journal format stay.

Journal segments are hash-chained JSON files written 0600 into a private directory; they hold sequence numbers, ids, hashes and
times only. Encryption at rest is the file backup repository's (restic) at deployment — a gate before the controlled trial
run; segments written locally are not encrypted (dispatcher ruling Q1, 2026-10-11).

Every credential is a 0600 file read with read_private_text; nothing is passed as an argument value or environment value.

    nexloop-backup  pg      --recorder-dsn-file F --replication-dsn-file F --pg-bin D --target D [--repo repo1] [--kind pg_full]
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
    pending = {'backup_id': backup_id, 'repo': repo, 'kind': kind, 'verify_status': 'pending'}
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


def _refuse_target(target, port, running_ports):
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise RestoreRefused('restore target is not empty')
    if port in running_ports or not _port_free(port):
        raise RestoreRefused('restore port is in use')


def _recovery_settings(target, *, port, socket_dir, owner_user, hba_method, extra):
    """Socket-only listener on the new port, archiving off, only the owner over the local socket (§6.1 step 2)."""
    settings = {**extra, 'recovery_target_action': 'promote', 'listen_addresses': '', 'port': str(port), 'unix_socket_directories': str(socket_dir),
                'archive_mode': 'off'}
    with open(Path(target) / 'postgresql.auto.conf', 'a') as conf:
        for key, value in settings.items():
            conf.write(f"{key} = '{value}'\n")
    (Path(target) / 'pg_hba.conf').write_text(f'local all {owner_user} {hba_method}\n')


def materialize_basebackup(*, base_backup, wal_archive, target, recovery_target_name=None, recovery_target_time=None, **settings):
    """Local path (NX-035 local part): copy a pg_basebackup directory and replay archived WAL with cp to the target."""
    base_backup, target = Path(base_backup), Path(target)
    if not (base_backup / 'backup_manifest').is_file():
        raise RestoreRefused('base backup manifest missing')
    shutil.copytree(base_backup, target, dirs_exist_ok=True)
    target.chmod(0o700)
    (target / 'recovery.signal').write_text('')
    extra = {'restore_command': f'cp "{Path(wal_archive)}/%f" "%p"'}
    if recovery_target_name:extra['recovery_target_name'] = recovery_target_name
    else:extra['recovery_target_time'] = recovery_target_time
    _recovery_settings(target, extra=extra, **settings)


def materialize_pgbackrest(*, pgbackrest, stanza, repo, config_file, target, recovery_target_name=None, recovery_target_time=None, set_label=None,
                           **settings):
    """Deployment path (D1): `pgbackrest restore` of the stanza's backup set into the new, empty target with a name or time
    target (PITR, promote). pgBackRest writes recovery.signal and its own restore_command (archive-get); the repository
    credentials stay in its private configuration file, which is never read or printed here."""
    target = Path(target)
    command = [str(pgbackrest), f'--config={config_file}', f'--stanza={stanza}', f'--repo={repo}', f'--pg1-path={target}',
               '--type=name' if recovery_target_name else '--type=time', f'--target={recovery_target_name or recovery_target_time}',
               '--target-action=promote', '--archive-mode=off', *([f'--set={set_label}'] if set_label else []), 'restore']
    done = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    if done.returncode != 0:
        raise RestoreRefused(f'pgbackrest restore failed (exit {done.returncode})')
    if not (target / 'PG_VERSION').is_file():
        raise RestoreRefused('pgbackrest restore produced no data directory')
    target.chmod(0o700)
    _recovery_settings(target, extra={}, **settings)


def _backup_timeline(pg_bin, target):
    """The timeline the restored data directory was taken on (pg_controldata, before the first start)."""
    out = subprocess.run([str(Path(pg_bin) / 'pg_controldata'), str(target)], capture_output=True, text=True, check=True,
                         env={'LC_ALL': 'C'}).stdout  # absolute path; English labels
    for line in out.splitlines():
        if line.startswith("Latest checkpoint's TimeLineID:"):
            return int(line.split(':', 1)[1])
    raise RestoreRefused('restored data directory has no timeline')


def restore(*, pg_bin, base_backup=None, wal_archive=None, target, socket_dir, port, owner_user, restore_ref, window_start, window_end,
            journal_dir, evidence, recovery_target_name=None, recovery_target_time=None, backup_id=None, running_ports=(),
            old_primary_fenced=False, hba_method='peer', restore_started_at=None, pgbackrest=None):
    """Restore into a new directory on a new, socket-only port, either from a pg_basebackup directory plus archived WAL (local)
    or with pgBackRest (`pgbackrest`: {pgbackrest, stanza, repo, config_file[, set_label]}; deployment, D1), to a named restore
    point or a time; set every application role NOLOGIN; write the fence; replay the journal; reconcile the effect window;
    verify the migration lineage; end in verified_readonly (design §6.1–§6.5). Returns the running instance and a numbers-only
    report."""
    started = time.monotonic()
    restore_started_at = restore_started_at or datetime.now(UTC)
    pg_bin, target, socket_dir = Path(pg_bin), Path(target), Path(socket_dir)
    if not _REF.fullmatch(restore_ref or ''):
        raise RestoreRefused('restore reference invalid')
    if not old_primary_fenced:
        raise RestoreRefused('old primary must be fenced first (--old-primary-fenced)')
    if (recovery_target_name is None) == (recovery_target_time is None):
        raise RestoreRefused('exactly one recovery target required')
    if (pgbackrest is None) == (base_backup is None):
        raise RestoreRefused('exactly one backup source required')
    _refuse_target(target, port, running_ports)
    settings = dict(target=target, recovery_target_name=recovery_target_name, recovery_target_time=recovery_target_time, port=port,
                    socket_dir=socket_dir, owner_user=owner_user, hba_method=hba_method)
    if pgbackrest is None:
        materialize_basebackup(base_backup=base_backup, wal_archive=wal_archive, **settings)
    else:
        materialize_pgbackrest(**pgbackrest, **settings)
    old_timeline = _backup_timeline(pg_bin, target)
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
    for name in ('recorder-dsn-file', 'replication-dsn-file', 'pg-bin', 'target'):
        pg.add_argument('--' + name, type=Path, required=True)
    pg.add_argument('--repo', choices=('repo1', 'repo2'), default='repo1')
    pg.add_argument('--kind', choices=('pg_full', 'pg_diff', 'pg_incr'), default='pg_full')
    journal = sub.add_parser('journal')
    journal.add_argument('--recorder-dsn-file', type=Path, required=True)
    journal.add_argument('--journal-dir', type=Path, required=True)
    journal.add_argument('--repo', choices=('repo1', 'repo2'), default='repo1')
    args = parser.parse_args(argv)
    try:
        recorder = read_private_text(args.recorder_dsn_file, maximum=4096)
        if args.command == 'pg':
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
