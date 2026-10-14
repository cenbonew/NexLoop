"""NX-035 slice 3: the file backup executor ``nexloop-file-backup`` (APP_HOST; design §4.1, §4.3; deploy runbook §3).

One run backs up one kind into one restic repository and records one manifest row (0146 ``authz.nexloop_record_backup``):

- ``artifact`` (``files_artifact``): the Artifacts the owner-side inventory lists (0194: available and inside their
  retention, world real). Each file is copied into a fresh private staging directory and hashed again after the copy; a
  file whose copy does not match the inventory's sha256 and size is left out and counted.
- ``runtime`` (``files_runtime``): the finished Pi Runs the inventory lists (every bound task terminal, no NX-029 purge
  queued or done). Only ``runtime.sqlite`` is copied, and only when no non-empty ``-wal`` file is beside it (the Host has
  closed and checkpointed the store); the copy must pass SQLite ``PRAGMA integrity_check``. An active Run is never listed,
  and copying a database without its WAL is refused (design §4.3). A listed Run whose directory is gone is counted as
  absent: an NX-029 purge or a Run that never created its store.

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


def stage_runs(entries, source_root, staging):
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
        try:
            wal = os.lstat(Path(source_root) / ref / 'runtime.sqlite-wal')
            if not stat.S_ISREG(wal.st_mode) or wal.st_size > 0:
                skipped['not_checkpointed'] += 1
                continue
        except FileNotFoundError:
            pass
        try:
            source = _open_source(source_root, f'{ref}/runtime.sqlite')
        except (FileNotFoundError, NotADirectoryError, OSError):
            skipped['missing'] += 1
            continue
        target = staging / 'runs' / ref / 'runtime.sqlite'
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


def _restic(restic, arguments, *, repository_file, password_file, environment, cwd=None, timeout=3600):
    return subprocess.run([restic, '--no-cache', '--repository-file', str(repository_file), '--password-file', str(password_file), *arguments],
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
                    repo='repo1', key_ref=None, restic='restic'):
    if kind not in KINDS or repo not in ('repo1', 'repo2') or (key_ref is not None and not _LABEL.fullmatch(key_ref)):
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
        stage = stage_artifacts if kind == 'artifact' else stage_runs
        rows, skipped, absent = stage(entries, source_root, staging)
        digest = manifest_digest(rows)
        body = json.dumps({'schema': MANIFEST_SCHEMA, 'kind': manifest_kind, 'file_manifest_digest': digest,
                           'entries': sorted(rows)}, separators=(',', ':'), sort_keys=True)
        fd = os.open(staging / MANIFEST_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        common = {'repository_file': repository_file, 'password_file': password_file, 'environment': environment}
        saved = _restic(restic, ['backup', '--json', '--tag', manifest_kind, '--host', 'nexloop', '.'], cwd=staging, **common)
        snapshot = _snapshot_id(saved.stdout) if saved.returncode == 0 else None
        checked = snapshot is not None and _restic(restic, ['check'], **common).returncode == 0
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
    args = parser.parse_args(argv)
    try:
        result = run_file_backup(kind=args.kind, recorder_conninfo=read_private_text(args.recorder_dsn_file, maximum=4096),
                                 source_root=args.source_root, staging_root=args.staging_root, repository_file=args.repository_file,
                                 password_file=args.password_file, environment_file=args.environment_file, repo=args.repo,
                                 key_ref=args.key_ref, restic=args.restic)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 1
    ok = result['verify_status'] == 'verified'
    print(json.dumps({'ok': ok, **result}))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
