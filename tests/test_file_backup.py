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
