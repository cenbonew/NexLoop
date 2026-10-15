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
        self.command = list(command)
        self.prefix = [*command, *([f'--config={config_file}'] if config_file is not None else []), f'--stanza={stanza}']
        self.stanza, self.timeout = stanza, timeout

    def run(self, *args, timeout=None):
        return subprocess.run([*self.prefix, *args], capture_output=True, text=True, timeout=timeout or self.timeout)

    def version(self):
        # `version` accepts no stanza or config option (pgBackRest 2.59: ERROR [031]); run it on the bare command.
        done = subprocess.run([*self.command, 'version'], capture_output=True, text=True, timeout=60)
        found = _VERSION.search(done.stdout)
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
    def __init__(self, pg_bin, data, socket_dir, port, owner_user, dbname='postgres'):
        self.pg_bin, self.data, self.socket_dir, self.port, self.owner_user = Path(pg_bin), Path(data), Path(socket_dir), port, owner_user
        # The NexLoop database of the restored cluster (a deployment's is not `postgres`: 2026-10-15 stage drill, InvalidSchemaName).
        if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', dbname or ''):
            raise RestoreRefused('database name invalid')
        self.dbname = dbname

    @property
    def conninfo(self):
        return f'host={self.socket_dir} port={self.port} dbname={self.dbname} user={self.owner_user}'

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
            old_primary_fenced=False, hba_method='peer', restore_started_at=None, pgbackrest=None, drill_mark=None, dbname='postgres'):
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
    instance = RestoredInstance(pg_bin, target, socket_dir, port, owner_user, dbname)
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
            report['checks'] = post_restore_checks(db, drill_mark=drill_mark)
            failing = sorted(name for name, held in report['checks']['controls'].items() if not held)
            if failing:
                raise RestoreRefused('compensating controls of restore.open_scope do not hold: ' + ','.join(failing)
                                     + ' ' + json.dumps(report['checks']['counts'], sort_keys=True))
        report['tool_seconds'] = round(time.monotonic() - started, 3)
        return instance, report
    except BaseException:
        instance.stop()
        raise


# Checks before any scope is opened ----------------------------------------------------------------------------------------
OPEN_SCOPE_HANDLER = 'control.nexloop_governed_restore_open_scope(text,text,text,text,text,jsonb)'


def post_restore_checks(db, *, drill_mark=None):
    """After verified_readonly and before nexloop.restore.open_scope (numbers and booleans only, run as the owner):
    the migration ledger and checksums (bootstrap.verify); row counts of the append-only tables, compared with a drill mark
    (counts up to the marked max ids and the sampled row hashes) when one is given; and the five compensating controls of the
    open_scope Action (owner decision 2026-10-11, local implementation note §7):
      owner_only        restore.open_scope is registered with its owner-checking handler, owned by nexloop_owner;
      per_scope         one closed real scope per active tenant, none opened yet;
      unverified_paused every channel that is not time-queryable or has unverifiable effects is paused (action_type scope);
      report_complete   the fence is verified_readonly with replay, reconciliation, verification and the window end recorded;
      opening_recorded  no scope is open without its opener and governed intent.
    `passed` is true only when the lineage holds, every control holds and (with a mark) no count or sample differs."""
    from nexloop_eios.bootstrap import verify
    out = {'lineage': verify(db)}
    with db.transaction():
        db.execute('set local role nexloop_owner')
        tables = [t for (t,) in db.execute('select unnest(runtime.nexloop_drill_tables())').fetchall()]
        out['row_counts'] = {t.split(':', 1)[0]: db.execute('select runtime.nexloop_drill_table_counts(%s,null)', (t,)).fetchone()[0]['count'] for t in tables}
        out['drill_compare'] = None if drill_mark is None else db.execute('select runtime.nexloop_drill_compare(%s)', (Jsonb(drill_mark),)).fetchone()[0]
        state = db.execute('select state,replayed_at is not null,reconciled_at is not null,verified_at is not null,window_end is not null '
                           'from control.nexloop_restore_state').fetchone()
        registered = db.execute("select count(*) from control.nexloop_governed_capabilities where capability='restore.open_scope' "
                                "and operation='open_restore_scope' and subject_rule='human' and handler=to_regprocedure(%s::text)", (OPEN_SCOPE_HANDLER,)).fetchone()[0]
        handler_owner = db.execute('select pg_get_userbyid(proowner) from pg_proc where oid=to_regprocedure(%s::text)', (OPEN_SCOPE_HANDLER,)).fetchone()
        tenants = db.execute("select count(*) from control.nexloop_tenants where status='active'").fetchone()[0]
        scopes = db.execute("select count(*),count(*) filter (where opened),count(*) filter (where world='real') from control.nexloop_restore_scopes").fetchone()
        # The NX-022 control scopes are tenant row-secured: each unverified channel is checked under its own tenant.
        unpaused = 0
        for tenant, world, action_type, paused in db.execute(
                'select tenant_id,world,action_type,paused from control.nexloop_restore_channels where unverifiable>0 or not time_queryable').fetchall():
            db.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
            scope_paused = db.execute("select exists(select 1 from control.nexloop_control_scopes where tenant_id=%s and world=%s and scope_kind='action_type' "
                                      "and scope_ref=%s and paused)", (tenant, world, action_type)).fetchone()[0]
            unpaused += 0 if (paused and scope_paused) else 1
        unrecorded = db.execute('select count(*) from control.nexloop_restore_scopes where opened and (opened_by is null or intent_id is null)').fetchone()[0]
        channels = db.execute('select count(*),count(*) filter (where paused) from control.nexloop_restore_channels').fetchone()
    out['controls'] = {
        'owner_only': registered == 1 and handler_owner is not None and handler_owner[0] == 'nexloop_owner',
        'per_scope': scopes[0] == scopes[2] == tenants and scopes[1] == 0,
        'unverified_paused': unpaused == 0,
        'report_complete': state is not None and state[0] == 'verified_readonly' and all(state[1:]),
        'opening_recorded': unrecorded == 0}
    out['counts'] = {'active_tenants': tenants, 'scopes': scopes[0], 'scopes_open': scopes[1], 'channels': channels[0], 'channels_paused': channels[1],
                     'unverified_channels_not_paused': unpaused}
    compare = out['drill_compare']
    out['passed'] = all(out['controls'].values()) and (compare is None or (compare['count_mismatches'] == 0 and compare['sample_mismatches'] == 0
                                                                and compare['catalog_equal'] is True))
    return out


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
    if argv is None:
        import sys
        argv = sys.argv[1:]
    if argv[:1] == ['drill-mark']:
        return drill_mark_main(argv[1:])
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


def _write_private(path, payload):
    """A new 0600 file (never overwritten)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + '\n')


def drill_mark_main(argv=None):
    """`nexloop-backup drill-mark`: mark a named restore point with counts and sampled row hashes (recorder identity) and keep
    the mark in a new private file for the restore's comparison (the mark row itself commits after the restore point)."""
    parser = _Parser(description='Mark a drill restore point (NX-035 §7.1)')
    parser.add_argument('--recorder-dsn-file', type=Path, required=True)
    parser.add_argument('--drill-id', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.out.exists():
            raise RestoreRefused('mark file exists')
        mark = drill_mark(read_private_text(args.recorder_dsn_file, maximum=4096), args.drill_id)
        _write_private(args.out, mark)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 1
    print(json.dumps({'ok': True, 'restore_point': mark['restore_point'], 'marked_at': mark['marked_at'], 'tables': len(mark['tables'])}))
    return 0


def restore_main(argv=None):
    """`nexloop-restore`: restore into a NEW instance (an empty --pg-path, a free --port, socket only) to a named restore point
    (--target-name) or a time (--target-time), from pgBackRest (deployment) or a pg_basebackup directory (local); then the
    fence, journal replay, window reconciliation, lineage and the open_scope compensating controls, ending in
    verified_readonly (design §6). It never overwrites a database and never opens dispatch (owner: nexloop.restore.open_scope).
    Credentials come only from private files: the pgBackRest configuration (its path is passed through, never read into this
    process's output), the evidence and the drill mark. Output: one numbers-only JSON report; exit 0 passed, 1 refused or
    failed, 2 configuration."""
    parser = _Parser(description='Restore NexLoop PostgreSQL into a new instance (NX-035)')
    parser.add_argument('--source', choices=('pgbackrest', 'basebackup'), required=True)
    parser.add_argument('--pgbackrest-bin', type=Path)
    parser.add_argument('--pgbackrest-config-file', type=Path)
    parser.add_argument('--stanza')
    parser.add_argument('--repo', choices=('1', '2'), default='1')
    parser.add_argument('--set', dest='set_label')
    parser.add_argument('--base-backup', type=Path)
    parser.add_argument('--wal-archive', type=Path)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--target-name', help='a named restore point (pg_create_restore_point, e.g. drill-<id>)')
    target.add_argument('--target-time', help='an ISO 8601 time with offset')
    parser.add_argument('--pg-bin', type=Path, required=True)
    parser.add_argument('--pg-path', type=Path, required=True, help='the new instance data directory (absent or empty)')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--socket-dir', type=Path, required=True)
    parser.add_argument('--owner-user', required=True)
    parser.add_argument('--dbname', default='postgres', help='the NexLoop database inside the restored cluster')
    parser.add_argument('--hba-method', choices=('peer', 'trust', 'scram-sha-256'), default='peer')
    parser.add_argument('--restore-ref', required=True)
    parser.add_argument('--journal-dir', type=Path, required=True)
    parser.add_argument('--window-start', help='default: --target-time, or the drill mark time')
    parser.add_argument('--window-end', help='default: now')
    parser.add_argument('--evidence-file', type=Path, help='private JSON list of channel time evidence (absent: every channel stays paused)')
    parser.add_argument('--drill-mark-file', type=Path, help='private JSON written by nexloop-backup drill-mark')
    parser.add_argument('--old-primary-fenced', action='store_true')
    parser.add_argument('--running-port', type=int, action='append', default=[])
    parser.add_argument('--report-file', type=Path)
    parser.add_argument('--stop', action='store_true', help='stop the restored instance after the checks (drills)')
    args = parser.parse_args(argv)
    try:
        if args.source == 'pgbackrest':
            if not (args.pgbackrest_bin and args.pgbackrest_config_file and args.stanza) or args.base_backup or args.wal_archive:
                parser.error('pgbackrest source')
            read_private_text(args.pgbackrest_config_file, maximum=65536)  # private (0600, owned) or refused; content not used here
            source = dict(pgbackrest={'pgbackrest': args.pgbackrest_bin, 'stanza': args.stanza, 'repo': args.repo,
                                      'config_file': args.pgbackrest_config_file, 'set_label': args.set_label})
        else:
            if not (args.base_backup and args.wal_archive) or args.pgbackrest_bin or args.pgbackrest_config_file or args.stanza:
                parser.error('basebackup source')
            source = dict(base_backup=args.base_backup, wal_archive=args.wal_archive)
        mark = json.loads(read_private_text(args.drill_mark_file, maximum=1048576)) if args.drill_mark_file else None
        evidence = json.loads(read_private_text(args.evidence_file, maximum=1048576)) if args.evidence_file else []
        if args.target_time:
            datetime.fromisoformat(args.target_time)
        window_start = args.window_start or args.target_time or (mark or {}).get('marked_at')
        if window_start is None:
            parser.error('window start')
        instance, report = restore(**source, pg_bin=args.pg_bin, target=args.pg_path, socket_dir=args.socket_dir, port=args.port,
                                   owner_user=args.owner_user, restore_ref=args.restore_ref, window_start=window_start,
                                   window_end=args.window_end or datetime.now(UTC), journal_dir=args.journal_dir, evidence=evidence,
                                   recovery_target_name=args.target_name, recovery_target_time=args.target_time, running_ports=tuple(args.running_port),
                                   old_primary_fenced=args.old_primary_fenced, hba_method=args.hba_method, drill_mark=mark, dbname=args.dbname)
    except SystemExit:
        raise
    except RestoreRefused as refused:
        print(json.dumps({'passed': False, 'refused': str(refused).split(' {', 1)[0]}))
        return 1
    except Exception as error:
        print(json.dumps({'passed': False, 'error': type(error).__name__}))
        return 1
    if args.stop:
        instance.stop()
    result = {'passed': report['checks']['passed'], 'state': report['verified']['state'], 'instance': 'stopped' if args.stop else 'running',
              'port': args.port, **report}
    if args.report_file:
        _write_private(args.report_file, result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result['passed'] else 1
