"""NX-035 slice 3: the file backup executor ``nexloop-file-backup`` (APP_HOST; design §4.1, §4.3; deploy runbook §3).

One run backs up one kind into one restic repository and records one manifest row (0146 ``authz.nexloop_record_backup``):

- ``artifact`` (``files_artifact``): the Artifacts the owner-side inventory lists (0194: available and inside their
  retention, world real). Each file is copied into a fresh private staging directory and hashed again after the copy; a
  file whose copy does not match the inventory's sha256 and size is left out and counted.
- ``runtime`` (``files_runtime``): the finished Pi Runs the inventory lists (every bound task terminal, no NX-029 purge
  queued or done). Only ``runtime.sqlite`` is copied, and only when no non-empty ``-wal`` file is beside it (the Host has
  closed and checkpointed the store); the copy must pass SQLite ``PRAGMA integrity_check``. An active Run is never listed,
  and copying a database without its WAL is refused (design §4.3). A listed Run whose directory is gone is counted as
  absent: an NX-029 purge or a Run that never created its store; absent is reported, never a failure.
  A finished Run whose store still has a non-empty WAL that has not changed for WAL_QUIET_SECONDS (the Host stopped without
  closing it - killed, or its Run ended in PG first - so nothing will ever checkpoint it) is copied with its WAL as one pair: both
  files are stat'ed before and after the copy and must be unchanged, the staged copy is checkpointed (wal_checkpoint TRUNCATE) and
  must pass the integrity check; the source is only read. It is counted as recovered_wal. A younger WAL may still be written:
  skipped as not_checkpointed, as before, and the next round sees it again.

Then restic backs up the staging directory (client-side encryption, ``--no-cache``), ``restic check`` verifies the
repository, and the row is written: ``verified`` only when restic and the check both succeeded and nothing was left out,
otherwise ``failed`` (the file_backup_age alert keeps counting from the last verified row). The staging directory is
removed afterwards. The row carries the restic snapshot id as backup_id, the file manifest digest (sha256 of the sorted
``[ref, sha256]`` list, the same list written as FILE-MANIFEST.json into the snapshot), the file count and bytes.

Credentials: the recorder DSN, the restic repository location, the restic password and the optional object storage
environment are 0600 files owned by the executor's user. Only paths reach restic (``--repository-file``,
``--password-file``); the environment file's AWS_* values go to the restic process environment and nowhere else. Nothing
read from these files, nor restic's own output, is printed; the result line holds counts, labels and hashes only.

    nexloop-file-backup --kind artifact|runtime --recorder-dsn-file F --source-root D --staging-root D
        --repository-file F --password-file F [--environment-file F] [--repo repo1|repo2] [--key-ref LABEL] [--restic BIN]
        [--s3-bucket-lookup auto|dns|path] [--cacert F]

``--s3-bucket-lookup`` (repo2 on TOS, activation run sheet 2026-10-18): restic's ``-o s3.bucket-lookup=`` for an S3 store that
answers path-style requests with an error; ``dns`` is virtual-hosted addressing (<bucket>.<endpoint>). Omitted: restic's default.

``--cacert`` (repo1 over DATA_HOST's rest-server, owner 2026-10-27): restic's ``--cacert``, the stage CA bundle the server certificate
must chain to; restic then trusts it alone, not the system roots.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from nexloop_eios.backup import BackupUnavailable, _call, _now, record
from nexloop_eios.private_configuration import read_private_text

KINDS = {'artifact': 'files_artifact', 'runtime': 'files_runtime'}
MANIFEST_SCHEMA = 'nexloop-file-backup/1'
MANIFEST_NAME = 'FILE-MANIFEST.json'
_ARTIFACT_REF = re.compile(r'[a-f0-9]{64}/[a-f0-9]{32}')
_RUN_REF = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_SHA256 = re.compile(r'[a-f0-9]{64}')
_LABEL = re.compile(r'[A-Za-z0-9._:-]{1,64}')
_ENVIRONMENT_KEYS = frozenset({'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION'})
_CHUNK = 1 << 20


def inventory(recorder_conninfo, kind, *, page=500):
    """Every entry of the owner-side inventory for one manifest kind, in ref order (keyset paging)."""
    after, entries = None, []
    while True:
        result = _call(recorder_conninfo, 'select authz.nexloop_file_backup_inventory(%s,%s,%s)', (kind, after, page))
        batch = result['entries']
        entries.extend(batch)
        if len(batch) < page:
            return entries
        after = batch[-1]['ref']


def _private_directory(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError('private directory required')


def _open_source(root, relative):
    """A regular file under root, opened without following a link at any level below root."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = relative.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(source).st_mode):
        os.close(source)
        raise FileNotFoundError(relative)
    return source


def _copy(source_fd, target):
    """Copy into a new 0600 file, fsync it, and return (sha256, bytes) of what was written."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(source_fd, 'rb', closefd=True) as src, os.fdopen(out, 'wb', closefd=False) as dst:
            while chunk := src.read(_CHUNK):
                dst.write(chunk)
        os.fsync(out)
    finally:
        os.close(out)
    with open(target, 'rb') as written:
        while chunk := written.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def stage_artifacts(entries, source_root, staging):
    rows, skipped = [], {'missing': 0, 'mismatch': 0}
    for entry in entries:
        ref = entry.get('ref')
        if type(ref) is not str or not _ARTIFACT_REF.fullmatch(ref) or not _SHA256.fullmatch(str(entry.get('sha256'))):
            raise BackupUnavailable('inventory entry invalid')
        try:
            source = _open_source(source_root, ref)
        except (FileNotFoundError, NotADirectoryError, OSError):
            skipped['missing'] += 1
            continue
        target = staging / 'artifacts' / ref
        sha256, size = _copy(source, target)
        if sha256 != entry['sha256'] or size != entry['bytes']:
            target.unlink()
            skipped['mismatch'] += 1
            continue
        rows.append([f'artifacts/{ref}', sha256, size])
    return rows, skipped, 0


def _integrity_ok(path):
    try:
        db = sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)
    except sqlite3.Error:
        return False
    try:
        return db.execute('pragma integrity_check').fetchall() == [('ok',)]
    except sqlite3.Error:
        return False
    finally:
        db.close()


WAL_QUIET_SECONDS = 1800
_WAL_HEADER = 32


def wal_committed(path):
    """What SQLite would replay from a WAL file (sqlite.org/fileformat2.html#walformat), read from the file itself: frames whose
    salts match the header, whose page number is not 0 and whose cumulative checksums hold, up to the last commit frame.
    {frames, pages: {page number: the page's last committed content}, database_pages: the last commit frame's database size,
    page_size}; frames 0 for no WAL magic, another format version than 3007000, a bad page size or a header that does not check.
    Not from wal_checkpoint: opening the copy already recovers the WAL, so the pragma answers 0 frames whether or not they were read,
    and frames already written back by an earlier checkpoint leave the database bytes unchanged."""
    import struct
    empty = {'frames': 0, 'pages': {}, 'database_pages': 0, 'page_size': 0}
    data = Path(path).read_bytes()
    if len(data) < _WAL_HEADER or data[:4] not in (b'\x37\x7f\x06\x82', b'\x37\x7f\x06\x83'):
        return empty
    order = '>' if data[3] == 0x83 else '<'
    version, page = struct.unpack('>II', data[4:12])
    if version != 3007000 or page < 512 or page > 65536 or page & (page - 1):
        return empty

    def checksum(chunk, s0, s1):
        for x0, x1 in struct.iter_unpack(order + 'II', chunk):
            s0 = (s0 + x0 + s1) & 0xFFFFFFFF
            s1 = (s1 + x1 + s0) & 0xFFFFFFFF
        return s0, s1
    salts = data[16:24]
    s0, s1 = checksum(data[:24], 0, 0)
    if (s0, s1) != struct.unpack('>II', data[24:32]):
        return empty
    frames, committed, pending, pages, database_pages = 0, 0, {}, {}, 0
    offset, size = _WAL_HEADER, 24 + page
    while offset + size <= len(data):
        head = data[offset:offset + 24]
        number, commit = struct.unpack('>II', head[:8])
        if number == 0 or head[8:16] != salts:
            break
        s0, s1 = checksum(head[:8] + data[offset + 24:offset + size], s0, s1)
        if (s0, s1) != struct.unpack('>II', head[16:24]):
            break
        frames += 1
        pending[number] = data[offset + 24:offset + size]
        if commit:
            committed, database_pages = frames, commit
            pages.update(pending);pending = {}
        offset += size
    if not committed:
        return empty
    return {'frames': committed, 'pages': {n: b for n, b in pages.items() if n <= database_pages}, 'database_pages': database_pages, 'page_size': page}


def committed_wal_frames(path):
    return wal_committed(path)['frames']


def _checkpoint(target):
    """wal_checkpoint(TRUNCATE) on the staged copy; True when it was busy."""
    db = sqlite3.connect(target)
    try:
        return bool(db.execute('pragma wal_checkpoint(TRUNCATE)').fetchone()[0])
    finally:
        db.close()


def _merged_as_replayed(target, replay):
    """After the checkpoint (fail closed): the copy's page count is the last commit's database size, and every committed page is
    byte for byte the WAL's last version of it. A WAL written back before and not truncated holds the same pages: it passes."""
    db = sqlite3.connect(f'file:{target}?mode=ro&immutable=1', uri=True)
    try:
        count, size = db.execute('pragma page_count').fetchone()[0], db.execute('pragma page_size').fetchone()[0]
    finally:
        db.close()
    if count != replay['database_pages'] or size != replay['page_size']:
        return False
    with open(target, 'rb') as stream:
        for number, content in replay['pages'].items():
            stream.seek((number - 1) * size)
            if stream.read(size) != content:
                return False
    return True


def _signature(path):
    info = os.lstat(path)
    return (stat.S_ISREG(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ino)


def _stage_with_wal(source_root, ref, target):
    """A quiet finished store with its WAL: copy the pair, checkpoint the copy. 'recovered', 'not_checkpointed' or 'integrity'."""
    base = Path(source_root) / ref
    before = (_signature(base / 'runtime.sqlite'), _signature(base / 'runtime.sqlite-wal'))
    if not (before[0][0] and before[1][0]):
        return 'not_checkpointed'
    if datetime.now(UTC).timestamp() - max(before[0][2], before[1][2]) / 1e9 < WAL_QUIET_SECONDS:
        return 'not_checkpointed'
    _copy(_open_source(source_root, f'{ref}/runtime.sqlite'), target)
    _copy(_open_source(source_root, f'{ref}/runtime.sqlite-wal'), target.with_name('runtime.sqlite-wal'))
    if (_signature(base / 'runtime.sqlite'), _signature(base / 'runtime.sqlite-wal')) != before:
        return 'not_checkpointed'  # it changed while copied: a writer is still there
    # A WAL SQLite would not replay (no magic, a header or first frame that does not check) would be dropped silently and the copy
    # would be short: refused. Frames already written back by an earlier checkpoint still check, and replaying them is harmless.
    replay = wal_committed(target.with_name('runtime.sqlite-wal'))
    if replay['frames'] == 0:
        return 'integrity'
    try:
        if _checkpoint(target):
            return 'integrity'
        for leftover in ('runtime.sqlite-wal', 'runtime.sqlite-shm'):
            target.with_name(leftover).unlink(missing_ok=True)
        if not _merged_as_replayed(target, replay):
            return 'integrity'
    except sqlite3.Error:
        return 'integrity'
    return 'recovered'


def stage_runs(entries, source_root, staging, recovered=None):
    rows, skipped, absent = [], {'missing': 0, 'not_checkpointed': 0, 'integrity': 0}, 0
    for entry in entries:
        ref = entry.get('ref')
        if type(ref) is not str or not _RUN_REF.fullmatch(ref):
            raise BackupUnavailable('inventory entry invalid')
        try:
            info = os.lstat(Path(source_root) / ref)
        except FileNotFoundError:
            absent += 1
            continue
        if not stat.S_ISDIR(info.st_mode):
            skipped['missing'] += 1
            continue
        target = staging / 'runs' / ref / 'runtime.sqlite'
        try:
            wal = os.lstat(Path(source_root) / ref / 'runtime.sqlite-wal')
            if not stat.S_ISREG(wal.st_mode):
                skipped['not_checkpointed'] += 1
                continue
            pending = wal.st_size > 0
        except FileNotFoundError:
            pending = False
        if pending:
            try:
                outcome = _stage_with_wal(source_root, ref, target)
            except (FileNotFoundError, NotADirectoryError, OSError):
                outcome = 'missing'
            if outcome != 'recovered':
                shutil.rmtree(target.parent, ignore_errors=True)
                skipped[outcome] += 1
                continue
            if recovered is not None:
                recovered.append(ref)
            with open(target, 'rb') as merged:
                digest = hashlib.sha256(merged.read())
            sha256, size = digest.hexdigest(), target.stat().st_size
        else:
            try:
                source = _open_source(source_root, f'{ref}/runtime.sqlite')
            except (FileNotFoundError, NotADirectoryError, OSError):
                skipped['missing'] += 1
                continue
            sha256, size = _copy(source, target)
        if not _integrity_ok(target):
            shutil.rmtree(target.parent)
            skipped['integrity'] += 1
            continue
        rows.append([f'runs/{ref}/runtime.sqlite', sha256, size])
    return rows, skipped, absent


def manifest_digest(rows):
    """sha256 of the sorted [ref, sha256] list (design §4.2), canonical JSON."""
    pairs = sorted([ref, sha256] for ref, sha256, _ in rows)
    return hashlib.sha256(json.dumps(pairs, separators=(',', ':')).encode()).hexdigest()


def _environment(environment_file):
    environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8'}
    if environment_file is None:
        return environment
    for line in read_private_text(environment_file, maximum=4096).splitlines():
        key, separator, value = line.strip().partition('=')
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if not separator or key not in _ENVIRONMENT_KEYS or not value:
            raise BackupUnavailable('environment file invalid')
        environment[key] = value
    return environment


BUCKET_LOOKUPS = ('auto', 'dns', 'path')


def _restic(restic, arguments, *, repository_file, password_file, environment, cwd=None, timeout=3600, options=()):
    return subprocess.run([restic, '--no-cache', *options, '--repository-file', str(repository_file), '--password-file', str(password_file), *arguments],
                          cwd=cwd, env=environment, capture_output=True, text=True, timeout=timeout)


def _snapshot_id(stdout):
    for line in reversed(stdout.splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get('message_type') == 'summary':
            snapshot = message.get('snapshot_id')
            return snapshot if type(snapshot) is str and _SHA256.fullmatch(snapshot) else None
    return None


def run_file_backup(*, kind, recorder_conninfo, source_root, staging_root, repository_file, password_file, environment_file=None,
                    repo='repo1', key_ref=None, restic='restic', s3_bucket_lookup=None, cacert=None):
    if kind not in KINDS or repo not in ('repo1', 'repo2') or (key_ref is not None and not _LABEL.fullmatch(key_ref)) \
            or (s3_bucket_lookup is not None and s3_bucket_lookup not in BUCKET_LOOKUPS) or (cacert is not None and not Path(cacert).is_file()):
        raise BackupUnavailable('file backup arguments invalid')
    manifest_kind = KINDS[kind]
    # The repository location and the password stay in their files; reading them here only proves they are private.
    read_private_text(repository_file, maximum=4096)
    read_private_text(password_file, maximum=4096)
    environment = _environment(environment_file)
    _private_directory(staging_root)
    if not Path(source_root).is_dir():
        raise BackupUnavailable('source root missing')
    started = _now()
    entries = inventory(recorder_conninfo, manifest_kind)
    staging = Path(staging_root) / ('stage-' + uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    try:
        recovered = []
        rows, skipped, absent = (stage_artifacts(entries, source_root, staging) if kind == 'artifact'
                                 else stage_runs(entries, source_root, staging, recovered))
        digest = manifest_digest(rows)
        body = json.dumps({'schema': MANIFEST_SCHEMA, 'kind': manifest_kind, 'file_manifest_digest': digest,
                           'entries': sorted(rows)}, separators=(',', ':'), sort_keys=True)
        fd = os.open(staging / MANIFEST_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        common = {'repository_file': repository_file, 'password_file': password_file, 'environment': environment,
                  'options': (('-o', 's3.bucket-lookup=' + s3_bucket_lookup) if s3_bucket_lookup else ()) + (('--cacert', str(cacert)) if cacert else ())}
        # --retry-lock: repo2's daily maintenance (APP_HOST) and DATA_HOST's (repo1) prune under the exclusive lock; a round waits for it
        # instead of recording a failed row.
        saved = _restic(restic, ['backup', '--json', '--tag', manifest_kind, '--host', 'nexloop', '--retry-lock', '5m', '.'], cwd=staging, **common)
        snapshot = _snapshot_id(saved.stdout) if saved.returncode == 0 else None
        checked = snapshot is not None and _restic(restic, ['check', '--retry-lock', '5m'], **common).returncode == 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    left_out = sum(skipped.values())
    status = 'verified' if checked and left_out == 0 else 'failed'
    backup_id = snapshot or 'files-' + kind[0] + '-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    entry = {'backup_id': backup_id, 'repo': repo, 'kind': manifest_kind, 'verify_status': status, 'started_at': started,
             'completed_at': _now(), 'file_manifest_digest': digest, 'file_count': len(rows), 'bytes': sum(r[2] for r in rows)}
    if key_ref is not None:
        entry['key_ref'] = key_ref
    record(recorder_conninfo, entry)
    return {'kind': manifest_kind, 'backup_id': backup_id, 'verify_status': status, 'file_count': len(rows), 'bytes': entry['bytes'],
            'file_manifest_digest': digest, 'listed': len(entries), 'skipped': skipped, 'absent': absent,
            **({'recovered_wal': len(recovered)} if kind == 'runtime' else {}),
            'restic': 'ok' if snapshot else 'failed', 'check': 'ok' if checked else ('failed' if snapshot else 'not_run')}


class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, 'configuration unavailable\n')


def main(argv=None):
    parser = _Parser(description='NexLoop file backup executor (NX-035 slice 3)')
    parser.add_argument('--kind', choices=sorted(KINDS), required=True)
    for name in ('recorder-dsn-file', 'source-root', 'staging-root', 'repository-file', 'password-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--environment-file', type=Path)
    parser.add_argument('--repo', choices=('repo1', 'repo2'), default='repo1')
    parser.add_argument('--key-ref')
    parser.add_argument('--restic', default='restic')
    parser.add_argument('--s3-bucket-lookup', choices=BUCKET_LOOKUPS)
    parser.add_argument('--cacert', type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_file_backup(kind=args.kind, recorder_conninfo=read_private_text(args.recorder_dsn_file, maximum=4096),
                                 source_root=args.source_root, staging_root=args.staging_root, repository_file=args.repository_file,
                                 password_file=args.password_file, environment_file=args.environment_file, repo=args.repo,
                                 key_ref=args.key_ref, restic=args.restic, s3_bucket_lookup=args.s3_bucket_lookup, cacert=args.cacert)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 1
    ok = result['verify_status'] == 'verified'
    print(json.dumps({'ok': ok, **result}))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
