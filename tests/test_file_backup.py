"""NX-035 slice 3: nexloop-file-backup without PostgreSQL. The inventory and the manifest recorder are replaced by in-memory
functions, restic by tests/support/restic_stand_in.py; the PostgreSQL side is in test_file_backup_pg.py.

Covers: copies are hashed again and mismatches left out; links are not followed; only checkpointed Run stores that pass
integrity_check are copied; the manifest digest and FILE-MANIFEST.json; restic and check failures give a failed row; the
staging directory is removed; credentials stay in files (never printed, only AWS_* keys reach restic)."""
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

import nexloop_eios.file_backup as fb
from support.restic_stand_in import write_executable

PASSWORD = 'synthetic-restic-password-not-a-secret'
SECRET_KEY = 'synthetic-object-storage-secret-value'


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    source, staging, repo = tmp_path / 'source', tmp_path / 'staging', tmp_path / 'repo'
    source.mkdir()
    staging.mkdir(mode=0o700)
    listed, recorded = {'files_artifact': [], 'files_runtime': []}, []
    monkeypatch.setattr(fb, 'inventory', lambda conninfo, kind, page=500: list(listed[kind]))
    monkeypatch.setattr(fb, 'record', lambda conninfo, entry: recorded.append(entry) or {'replay': False})
    return {'source': source, 'staging': staging, 'repo': repo, 'listed': listed, 'recorded': recorded,
            'repository_file': private(tmp_path / 'repository', str(repo)), 'password_file': private(tmp_path / 'password', PASSWORD),
            'restic': str(write_executable(tmp_path / 'restic'))}


def run(e, kind, **change):
    arguments = dict(kind=kind, recorder_conninfo='unused', source_root=e['source'], staging_root=e['staging'],
                     repository_file=e['repository_file'], password_file=e['password_file'], restic=e['restic'])
    return fb.run_file_backup(**{**arguments, **change})


def artifact(e, payload, *, sha256=None, size=None):
    ref = hashlib.sha256(b'tenant\0real').hexdigest() + '/' + uuid.uuid4().hex
    path = e['source'] / ref
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(payload)
    e['listed']['files_artifact'].append({'ref': ref, 'sha256': sha256 or hashlib.sha256(payload).hexdigest(), 'bytes': size or len(payload)})
    return ref


def run_store(e, *, wal=b'', corrupt=False):
    run_id = str(uuid.uuid4())
    directory = e['source'] / run_id
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / 'runtime.sqlite')
    db.execute('create table t(x)')
    db.execute("insert into t values('synthetic')")
    db.commit()
    db.close()
    if corrupt:
        with open(directory / 'runtime.sqlite', 'r+b') as stream:
            stream.seek(100)
            stream.write(b'\xff' * 3000)
    if wal:
        (directory / 'runtime.sqlite-wal').write_bytes(wal)
    e['listed']['files_runtime'].append({'ref': run_id})
    return run_id


def snapshot(e, result):
    return e['repo'] / 'snapshots' / result['backup_id']


def test_artifacts_are_copied_hashed_again_and_recorded_as_one_verified_row(env):
    e = env
    refs = [artifact(e, b'{"synthetic": 1}'), artifact(e, b'x' * 3000)]
    result = run(e, 'artifact', key_ref='restic-repo1')
    assert result['verify_status'] == 'verified' and result['file_count'] == 2 and result['bytes'] == 3016
    [row] = e['recorded']
    assert row == {'backup_id': result['backup_id'], 'repo': 'repo1', 'kind': 'files_artifact', 'verify_status': 'verified',
                   'started_at': row['started_at'], 'completed_at': row['completed_at'], 'file_manifest_digest': result['file_manifest_digest'],
                   'file_count': 2, 'bytes': 3016, 'key_ref': 'restic-repo1'}
    copied = snapshot(e, result)
    for ref in refs:
        assert (copied / 'artifacts' / ref).read_bytes() == (e['source'] / ref).read_bytes()
    manifest = json.loads((copied / fb.MANIFEST_NAME).read_text())
    pairs = sorted([f'artifacts/{ref}', hashlib.sha256((e['source'] / ref).read_bytes()).hexdigest()] for ref in refs)
    assert manifest['file_manifest_digest'] == hashlib.sha256(json.dumps(pairs, separators=(',', ':')).encode()).hexdigest() == row['file_manifest_digest']
    assert list(e['staging'].iterdir()) == []  # staging removed
    tags = json.loads((copied / '.tags').read_text())
    assert tags[:5] == ['backup', '--json', '--tag', 'files_artifact', '--host']


def test_a_copy_that_does_not_match_the_inventory_or_a_missing_or_linked_file_is_left_out_and_the_row_is_failed(env):
    e = env
    artifact(e, b'good')
    artifact(e, b'changed on disk', sha256='0' * 64)
    artifact(e, b'short', size=999)
    gone = artifact(e, b'gone')
    (e['source'] / gone).unlink()
    linked = artifact(e, b'target')
    outside = e['source'].parent / 'outside'
    outside.write_bytes(b'target')
    (e['source'] / linked).unlink()
    (e['source'] / linked).symlink_to(outside)
    result = run(e, 'artifact')
    assert result['skipped'] == {'missing': 2, 'mismatch': 2} and result['file_count'] == 1
    assert result['verify_status'] == 'failed' and e['recorded'][0]['verify_status'] == 'failed'
    assert sorted(p.name for p in (snapshot(e, result) / 'artifacts').rglob('*') if p.is_file()) == [Path(e['listed']['files_artifact'][0]['ref']).name]


def test_only_closed_checkpointed_run_stores_that_pass_integrity_check_are_copied(env):
    e = env
    good = run_store(e)
    empty_wal = run_store(e, wal=b'')
    (e['source'] / empty_wal / 'runtime.sqlite-wal').write_bytes(b'')
    open_store = run_store(e, wal=b'pending frames')
    broken = run_store(e, corrupt=True)
    absent = str(uuid.uuid4())
    e['listed']['files_runtime'].append({'ref': absent})
    result = run(e, 'runtime')
    assert result['skipped'] == {'missing': 0, 'not_checkpointed': 1, 'integrity': 1} and result['absent'] == 1
    assert result['file_count'] == 2 and result['verify_status'] == 'failed'
    copied = snapshot(e, result) / 'runs'
    assert sorted(p.name for p in copied.iterdir()) == sorted([good, empty_wal])
    assert not (copied / open_store).exists() and not (copied / broken).exists()
    assert not list(copied.rglob('*-wal')) and not list(copied.rglob('*-shm'))
    # Without the open and the broken store the same run is verified; an absent (purged) directory is not a failure.
    e['listed']['files_runtime'] = [x for x in e['listed']['files_runtime'] if x['ref'] not in (open_store, broken)]
    e['recorded'].clear()
    again = run(e, 'runtime')
    assert again['verify_status'] == 'verified' and again['absent'] == 1 and e['recorded'][0]['kind'] == 'files_runtime'


def test_restic_or_check_failure_records_a_failed_row_and_nothing_is_kept_in_staging(env):
    e = env
    artifact(e, b'x')
    e['repo'].mkdir()
    (e['repo'] / 'FAIL_BACKUP').touch()
    failed = run(e, 'artifact')
    assert failed['verify_status'] == 'failed' and failed['restic'] == 'failed' and failed['check'] == 'not_run'
    assert failed['backup_id'].startswith('files-a-') and e['recorded'][-1]['backup_id'] == failed['backup_id']
    (e['repo'] / 'FAIL_BACKUP').unlink()
    (e['repo'] / 'FAIL_CHECK').touch()
    unchecked = run(e, 'artifact')
    assert unchecked['verify_status'] == 'failed' and unchecked['restic'] == 'ok' and unchecked['check'] == 'failed'
    assert list(e['staging'].iterdir()) == []


def test_credentials_stay_in_private_files_and_only_object_storage_keys_reach_restic(env, tmp_path, capsys):
    e = env
    artifact(e, b'x')
    environment = private(tmp_path / 'object-storage.env', f'AWS_ACCESS_KEY_ID=synthetic-id\nAWS_SECRET_ACCESS_KEY={SECRET_KEY}\n')
    dsn = private(tmp_path / 'recorder.dsn', 'dbname=unused')
    code = fb.main(['--kind', 'artifact', '--recorder-dsn-file', str(dsn), '--source-root', str(e['source']), '--staging-root', str(e['staging']),
                    '--repository-file', str(e['repository_file']), '--password-file', str(e['password_file']),
                    '--environment-file', str(environment), '--restic', e['restic']])
    out = capsys.readouterr()
    assert code == 0 and json.loads(out.out)['ok'] is True
    for secret in (PASSWORD, SECRET_KEY, str(e['repo'])):
        assert secret not in out.out + out.err
    calls = [json.loads(line) for line in (e['repo'] / 'calls.jsonl').read_text().splitlines()]
    assert [c['command'][0] for c in calls] == ['backup', 'check'] and all(c['cwd_is_staging'] for c in calls[:1])
    # A maintenance prune holding the exclusive lock is waited for (both repositories have one), not a failed row.
    assert all(c['command'][c['command'].index('--retry-lock') + 1] == '5m' for c in calls)
    for call in calls:
        assert set(call['environment_keys']) <= {'PATH', 'LANG', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'LC_CTYPE', '__CF_USER_TEXT_ENCODING'}
        assert PASSWORD not in json.dumps(call['command'])


@pytest.mark.parametrize('problem', ['password_not_private', 'repository_not_private', 'unknown_environment_key', 'staging_not_private',
                                     'bad_key_ref'])
def test_unsafe_configuration_is_refused_before_anything_is_copied_or_recorded(env, tmp_path, problem):
    e = env
    artifact(e, b'x')
    change = {}
    if problem == 'password_not_private':
        e['password_file'].chmod(0o644)
    elif problem == 'repository_not_private':
        e['repository_file'].chmod(0o640)
    elif problem == 'unknown_environment_key':
        change['environment_file'] = private(tmp_path / 'bad.env', 'RESTIC_PASSWORD=inline\n')
    elif problem == 'staging_not_private':
        e['staging'].chmod(0o755)
    else:
        change['key_ref'] = 'the password is in the vault'
    with pytest.raises((PermissionError, fb.BackupUnavailable)):
        run(e, 'artifact', **change)
    assert e['recorded'] == [] and not e['repo'].exists()


def test_an_inventory_entry_outside_the_ref_shapes_stops_the_run(env):
    e = env
    e['listed']['files_artifact'].append({'ref': '../etc/passwd', 'sha256': '0' * 64, 'bytes': 1})
    with pytest.raises(fb.BackupUnavailable, match='inventory entry invalid'):
        run(e, 'artifact')
    e['listed']['files_runtime'].append({'ref': '../../run'})
    with pytest.raises(fb.BackupUnavailable, match='inventory entry invalid'):
        run(e, 'runtime')
    assert e['recorded'] == [] and list(e['staging'].iterdir()) == []


def test_cli_reports_failure_without_text_and_exits_1(env, tmp_path, capsys):
    e = env
    dsn = private(tmp_path / 'recorder.dsn', 'dbname=unused')
    e['password_file'].chmod(0o644)
    code = fb.main(['--kind', 'runtime', '--recorder-dsn-file', str(dsn), '--source-root', str(e['source']), '--staging-root', str(e['staging']),
                    '--repository-file', str(e['repository_file']), '--password-file', str(e['password_file']), '--restic', e['restic']])
    assert code == 1 and json.loads(capsys.readouterr().out) == {'ok': False, 'error': 'PermissionError'}
    assert os.access(e['staging'], os.W_OK)


def test_repo2_bucket_lookup_reaches_restic_as_an_extended_option_only_when_asked(env, tmp_path, capsys):
    # repo2 activation (2026-10-18): TOS may refuse restic's path-style default; --s3-bucket-lookup dns gives -o s3.bucket-lookup=dns.
    e = env
    artifact(e, b'x')
    dsn = private(tmp_path / 'recorder.dsn', 'dbname=unused')
    base = ['--kind', 'artifact', '--recorder-dsn-file', str(dsn), '--source-root', str(e['source']), '--staging-root', str(e['staging']),
            '--repository-file', str(e['repository_file']), '--password-file', str(e['password_file']), '--restic', e['restic'], '--repo', 'repo2']
    assert fb.main([*base, '--s3-bucket-lookup', 'dns']) == 0 and fb.main(base) == 0
    calls = [json.loads(line) for line in (e['repo'] / 'calls.jsonl').read_text().splitlines()]
    assert [c['options'] for c in calls] == [['s3.bucket-lookup=dns']] * 2 + [[]] * 2
    capsys.readouterr()
    with pytest.raises(SystemExit):
        fb.main([*base, '--s3-bucket-lookup', 'virtual'])


def test_repo1_over_the_rest_server_pins_the_stage_ca_with_cacert(env, tmp_path, capsys):
    """Owner 2026-10-27: repo1 behind DATA_HOST's rest-server; --cacert reaches restic (it then trusts that bundle alone)."""
    e = env
    artifact(e, b'x')
    dsn = private(tmp_path / 'recorder.dsn', 'dbname=unused')
    ca = tmp_path / 'rest-ca.crt'
    ca.write_text('-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n')
    base = ['--kind', 'artifact', '--recorder-dsn-file', str(dsn), '--source-root', str(e['source']), '--staging-root', str(e['staging']),
            '--repository-file', str(e['repository_file']), '--password-file', str(e['password_file']), '--restic', e['restic'], '--repo', 'repo1']
    assert fb.main([*base, '--cacert', str(ca)]) == 0
    calls = [json.loads(line) for line in (e['repo'] / 'calls.jsonl').read_text().splitlines()]
    assert [c['options'] for c in calls] == [[f'cacert={ca}']] * 2
    capsys.readouterr()
    assert fb.main([*base, '--cacert', str(tmp_path / 'absent.crt')]) == 1  # a CA file that is not there: no run, no row
    assert 'BackupUnavailable' in capsys.readouterr().out


def stranded_store(e, *, rows=3, age=3600, written_back=False):
    """A finished Run whose Host stopped without closing the store: the database and a real, non-empty WAL with committed frames
    that were never checkpointed (copied from a connection still open), both last modified `age` seconds ago."""
    run_id = str(uuid.uuid4())
    directory = e['source'] / run_id
    directory.mkdir(mode=0o700)
    live = e['source'].parent / ('live-' + run_id)
    live.mkdir()
    db = sqlite3.connect(live / 'runtime.sqlite')
    db.execute('pragma journal_mode=wal')
    db.execute('pragma wal_autocheckpoint=0')
    db.execute('create table t(x)')
    db.executemany('insert into t values(?)', [(f'synthetic {n}',) for n in range(rows)])
    db.commit()
    if written_back:  # an earlier automatic checkpoint wrote every frame back; the Host was killed before the WAL was truncated
        assert db.execute('pragma wal_checkpoint(PASSIVE)').fetchone()[2] > 0
    for name in ('runtime.sqlite', 'runtime.sqlite-wal'):
        (directory / name).write_bytes((live / name).read_bytes())
    db.close()
    old = (directory / 'runtime.sqlite').stat().st_mtime - age
    for name in ('runtime.sqlite', 'runtime.sqlite-wal'):
        os.utime(directory / name, (old, old))
    assert (directory / 'runtime.sqlite-wal').stat().st_size > 0
    e['listed']['files_runtime'].append({'ref': run_id})
    return run_id


def test_a_quiet_stranded_wal_is_copied_with_its_store_checkpointed_in_staging_and_verified(env):
    e = env
    good = run_store(e)
    stranded = stranded_store(e, rows=5)
    source = sorted(p.name for p in (e['source'] / stranded).iterdir())
    before = {p.name: p.read_bytes() for p in (e['source'] / stranded).iterdir()}
    e['listed']['files_runtime'].append({'ref': str(uuid.uuid4())})  # an absent one alongside: reported, not a failure
    result = run(e, 'runtime')
    assert result['verify_status'] == 'verified' and result['recovered_wal'] == 1 and result['absent'] == 1
    assert result['skipped'] == {'missing': 0, 'not_checkpointed': 0, 'integrity': 0} and result['file_count'] == 2
    copied = snapshot(e, result) / 'runs' / stranded
    assert sorted(p.name for p in copied.iterdir()) == ['runtime.sqlite']  # one checkpointed file, no WAL or shm in the snapshot
    db = sqlite3.connect(f'file:{copied / "runtime.sqlite"}?mode=ro&immutable=1', uri=True)
    assert db.execute('select count(*) from t').fetchone() == (5,)  # the WAL's committed rows are in the copy
    db.close()
    # The source is only read: the same files, the same bytes.
    assert sorted(p.name for p in (e['source'] / stranded).iterdir()) == source
    assert {p.name: p.read_bytes() for p in (e['source'] / stranded).iterdir()} == before
    assert (snapshot(e, result) / 'runs' / good / 'runtime.sqlite').exists()


def test_a_recent_wal_is_still_not_checkpointed(env):
    e = env
    stranded_store(e, age=60)  # changed a minute ago: the Host may still be writing
    result = run(e, 'runtime')
    assert result['skipped']['not_checkpointed'] == 1 and result['recovered_wal'] == 0 and result['verify_status'] == 'failed'


def test_an_unreadable_old_wal_is_an_integrity_failure_never_a_short_copy(env):
    e = env
    run_id = run_store(e, wal=b'\x00' * 4096)  # frames SQLite cannot read: it would drop them silently
    old = (e['source'] / run_id / 'runtime.sqlite').stat().st_mtime - 3600
    for name in ('runtime.sqlite', 'runtime.sqlite-wal'):
        os.utime(e['source'] / run_id / name, (old, old))
    result = run(e, 'runtime')
    assert result['skipped']['integrity'] == 1 and result['recovered_wal'] == 0 and result['verify_status'] == 'failed'
    assert not (snapshot(e, result) / 'runs' / run_id).exists()


def test_a_wal_that_changes_while_it_is_copied_is_not_checkpointed(env, monkeypatch):
    e = env
    stranded_store(e)
    original, calls = fb._signature, [0]

    def moving(path):
        calls[0] += 1
        value = original(path)
        return value if calls[0] <= 2 else (value[0], value[1] + 1, value[2], value[3])  # after the copy: another size
    monkeypatch.setattr(fb, '_signature', moving)
    result = run(e, 'runtime')
    assert result['skipped']['not_checkpointed'] == 1 and result['recovered_wal'] == 0 and result['verify_status'] == 'failed'


def test_a_wal_whose_frames_were_already_written_back_is_recovered_not_an_integrity_failure(env):
    e = env
    stranded = stranded_store(e, rows=4, written_back=True)
    assert fb.committed_wal_frames(e['source'] / stranded / 'runtime.sqlite-wal') > 0
    result = run(e, 'runtime')
    assert result['verify_status'] == 'verified' and result['recovered_wal'] == 1 and result['skipped']['integrity'] == 0
    db = sqlite3.connect(f'file:{snapshot(e, result) / "runs" / stranded / "runtime.sqlite"}?mode=ro&immutable=1', uri=True)
    assert db.execute('select count(*) from t').fetchone() == (4,)
    db.close()


def test_a_wal_with_a_broken_checksum_counts_no_frames(env):
    e = env
    stranded = stranded_store(e)
    wal = e['source'] / stranded / 'runtime.sqlite-wal'
    data = bytearray(wal.read_bytes())
    assert fb.committed_wal_frames(wal) > 0
    data[32 + 24 + 100] ^= 0xFF  # one byte of the first frame's page
    wal.write_bytes(bytes(data))
    old = (e['source'] / stranded / 'runtime.sqlite').stat().st_mtime
    os.utime(wal, (old, old))
    assert fb.committed_wal_frames(wal) == 0
    result = run(e, 'runtime')
    assert result['skipped']['integrity'] == 1 and result['recovered_wal'] == 0 and result['verify_status'] == 'failed'



def _aged(e, run_id):
    old = (e['source'] / run_id / 'runtime.sqlite').stat().st_mtime
    os.utime(e['source'] / run_id / 'runtime.sqlite-wal', (old, old))


def test_a_copy_changed_after_the_checkpoint_is_an_integrity_failure(env, monkeypatch):
    e = env
    stranded = stranded_store(e, rows=6)
    original = fb._checkpoint

    def tampered(target):
        busy = original(target)
        with open(target, 'r+b') as stream:  # the WAL checked; one committed page of the merged copy is not what it held
            stream.seek(4096 + 200)
            stream.write(b'\xaa' * 16)
        return busy
    monkeypatch.setattr(fb, '_checkpoint', tampered)
    result = run(e, 'runtime')
    assert result['skipped']['integrity'] == 1 and result['recovered_wal'] == 0 and result['verify_status'] == 'failed'
    assert not (snapshot(e, result) / 'runs' / stranded).exists()


def test_a_wal_of_another_format_version_counts_no_frames(env):
    e = env
    stranded = stranded_store(e)
    wal = e['source'] / stranded / 'runtime.sqlite-wal'
    data = bytearray(wal.read_bytes())
    data[4:8] = (3007001).to_bytes(4, 'big')
    wal.write_bytes(bytes(data));_aged(e, stranded)
    assert fb.committed_wal_frames(wal) == 0
    assert run(e, 'runtime')['skipped']['integrity'] == 1


def test_a_frame_with_page_number_0_ends_the_replay(env):
    e = env
    stranded = stranded_store(e)
    wal = e['source'] / stranded / 'runtime.sqlite-wal'
    data = bytearray(wal.read_bytes())
    data[32:36] = (0).to_bytes(4, 'big')  # the first frame's page number
    wal.write_bytes(bytes(data));_aged(e, stranded)
    assert fb.wal_committed(wal) == {'frames': 0, 'pages': {}, 'database_pages': 0, 'page_size': 0}
    assert run(e, 'runtime')['skipped']['integrity'] == 1


def test_the_replay_reports_the_last_version_of_each_committed_page():
    import tempfile
    directory = Path(tempfile.mkdtemp())
    db = sqlite3.connect(directory / 'w.sqlite')
    db.execute('pragma journal_mode=wal');db.execute('pragma wal_autocheckpoint=0')
    db.execute('create table t(x)');db.commit()
    db.execute("insert into t values('first')");db.commit()
    db.execute("update t set x='second'");db.commit()
    replay = fb.wal_committed(directory / 'w.sqlite-wal')
    count = db.execute('pragma page_count').fetchone()[0]
    db.execute('pragma wal_checkpoint(TRUNCATE)')
    merged = (directory / 'w.sqlite').read_bytes()
    db.close()
    assert replay['frames'] >= 3 and replay['database_pages'] == count and replay['page_size'] == 4096
    assert all(merged[(n - 1) * 4096:n * 4096] == content for n, content in replay['pages'].items())
