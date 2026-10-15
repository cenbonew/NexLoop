"""Deployment API mode and readiness (NX-032 / NX-035): `nexloop-api --mode stage` refuses an incomplete configuration and every
test-only switch; /health/ready reports the real readiness with fixed reason codes only (migration drift, restore in progress,
verified_readonly as read only, deletion replay, the master switch); doctor --backup against deploy/configuration/backup.v1.json.
Clean catalog PostgreSQL, synthetic files only."""
import json
import os
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios import deployment_readiness as readiness
from nexloop_eios import doctor
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.http_api import create_app, parse

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'deploy/configuration/backup.v1.json'


def private(path, text):
    path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def files(tmp_path):
    names = {'dsn': 'host=/nonexistent dbname=x', 'key': b'k' * 32, 'cert': 'synthetic', 'tls_key': 'synthetic', 'host_key': 'synthetic', 'host_ca': 'synthetic'}
    return {name: private(tmp_path / name, text) for name, text in names.items()}


def stage_args(files, **changes):
    values = {'--mode': 'stage', '--database-url-file': files['dsn'], '--signing-key-file': files['key'], '--artifact-root': files['dsn'].parent / 'artifacts',
              '--signing-key-id': 'synthetic-key', '--execution-profile': 'disabled', '--tls-certificate-file': files['cert'],
              '--tls-key-file': files['tls_key'], '--host-origin': 'https://127.0.0.1:8100', '--host-control-key-file': files['host_key'],
              '--host-ca-file': files['host_ca'], '--port': '8000'}
    values.update(changes)
    return [str(x) for k, v in values.items() if v is not None for x in (k, v)]


def test_a_complete_stage_configuration_starts_in_stage_mode(files):
    config, port, tls = parse(stage_args(files), environ={'REAL_DISPATCH_ENABLED': 'false'})
    assert config.mode == 'stage' and config.execution_profile == 'disabled' and port == 8000 and set(tls) == {'ssl_certfile', 'ssl_keyfile'}
    assert parse(stage_args(files, **{'--mode': 'test', '--execution-profile': 'deterministic-test', '--tls-certificate-file': None,
                                      '--tls-key-file': None}), environ={})[0].mode == 'test'  # test mode unchanged


@pytest.mark.parametrize('changes', [{'--signing-key-id': None}, {'--tls-certificate-file': None, '--tls-key-file': None},
                                     {'--host-origin': None, '--host-control-key-file': None, '--host-ca-file': None},
                                     {'--execution-profile': None}, {'--execution-profile': 'deterministic-test'}])
def test_stage_refuses_a_missing_setting_or_a_test_profile(files, changes, capsys):
    with pytest.raises(SystemExit) as stopped:
        parse(stage_args(files, **changes), environ={})
    assert stopped.value.code == 2 and 'stage configuration refused' in capsys.readouterr().err


@pytest.mark.parametrize('environ', [{'MODEL_PROVIDER': 'test'}, {'NEXLOOP_TEST_PG_BIN': '/x'}, {'NEXLOOP_TEST_GUARD_WORKERS': '4'},
                                     {'NEXLOOP_DATABASE_URL': 'host=x'}, {'MODEL_API_KEY': 'synthetic'}])
def test_stage_refuses_test_switches_and_secrets_in_the_environment(files, environ, capsys):
    with pytest.raises(SystemExit):
        parse(stage_args(files), environ=environ)
    assert 'refused' in capsys.readouterr().err and 'synthetic' not in capsys.readouterr().err


def test_stage_refuses_secret_files_that_are_not_private(files, capsys):
    files['dsn'].chmod(0o644)
    with pytest.raises(SystemExit):
        parse(stage_args(files), environ={})
    assert '--database-url-file' in capsys.readouterr().err


@pytest.mark.parametrize('material', [bytes(range(256))[-32:], bytes([0xff, 0xfe, 0x00, 0x80]) * 8, b'A' * 32])
def test_stage_accepts_any_32_byte_signing_key_read_raw(files, material):
    # Run-34 2026-10-15: the first stage key was 32 random binary bytes (not UTF-8); the precheck decoded it as text and refused it
    # although AuthoritySigner.from_file, which the backend uses, accepts it. The precheck now uses that same raw read.
    files['key'].write_bytes(material)
    assert parse(stage_args(files), environ={})[0].signing_key_file == files['key']


@pytest.mark.parametrize('material', [b'k' * 31, b'k' * 33, ('00' * 32).encode()])
def test_stage_refuses_a_signing_key_that_is_not_32_bytes(files, material, capsys):
    files['key'].write_bytes(material)
    with pytest.raises(SystemExit):
        parse(stage_args(files), environ={})
    assert '--signing-key-file' in capsys.readouterr().err


def test_stage_refuses_a_signing_key_that_is_not_private(files, capsys):
    files['key'].chmod(0o644)
    with pytest.raises(SystemExit):
        parse(stage_args(files), environ={})
    assert '--signing-key-file' in capsys.readouterr().err


def test_stage_ready_without_a_database_is_503_with_a_code_only(files):
    from fastapi.testclient import TestClient
    config = parse(stage_args(files), environ={})[0]
    with TestClient(create_app(config)) as client:
        response = client.get('/health/ready')
    assert response.status_code == 503
    assert response.json() == {'ready': False, 'mode': 'not_ready', 'reasons': ['database_unavailable'], 'dispatch_switch': 'closed',
                               'real_dispatch': {'tenants_open': None, 'actions_open': None}}


@pytest.fixture
def catalog_db(admin):
    bootstrap(admin)
    return admin


def owner(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute(sql, params).fetchone()[0]


def set_restore_state(admin, state):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        # The timestamps each state requires (the table's checks), as the restore tool's steps leave them.
        replayed, verified, opened = (state in ('replayed', 'verified_readonly', 'open'), state in ('verified_readonly', 'open'), state == 'open')
        admin.execute('update control.nexloop_restore_state set state=%s,replayed_at=case when %s then now() end,'
                      'reconciled_at=case when %s then now() end,verified_at=case when %s then now() end,window_end=case when %s then now() end,'
                      'opened_at=case when %s then now() end', (state, replayed, verified, verified, verified, opened))


def evaluate(pg, environ=None, user='nexloop_api', extra=()):
    return readiness.evaluate(lambda: psycopg.connect(make_conninfo(pg, user=user)), environ=environ or {}, extra=extra)


def test_readiness_states_on_a_clean_catalog(pg, catalog_db):
    admin = catalog_db
    assert evaluate(pg) == (200, {'ready': True, 'mode': 'normal', 'reasons': [], 'dispatch_switch': 'closed', 'real_dispatch': {'tenants_open': 0, 'actions_open': 0}})
    assert evaluate(pg, {'REAL_DISPATCH_ENABLED': 'true'})[1]['dispatch_switch'] == 'open'
    assert evaluate(pg, {'REAL_DISPATCH_ENABLED': 'yes'}) == (503, {'ready': False, 'mode': 'not_ready', 'reasons': ['dispatch_switch_invalid'], 'dispatch_switch': 'invalid', 'real_dispatch': {'tenants_open': 0, 'actions_open': 0}})
    assert evaluate(pg, user='nexloop_bootstrap')[1]['reasons'] == ['database_role_invalid']
    assert evaluate(pg, extra=(('signer_unavailable', False),))[1]['reasons'] == ['signer_unavailable']
    # Restore in progress: restored, then replayed — not ready; verified_readonly — reads only; open — normal.
    owner(admin, 'select runtime.nexloop_restore_begin(%s,%s,now(),now(),1)', ('ready-check', Jsonb({'backup_id': '20261011-010000F'})))
    assert evaluate(pg) == (503, {'ready': False, 'mode': 'not_ready', 'reasons': ['restore_in_progress'], 'dispatch_switch': 'closed', 'real_dispatch': {'tenants_open': 0, 'actions_open': 0}})
    set_restore_state(admin, 'replayed')
    assert evaluate(pg)[1]['reasons'] == ['restore_in_progress']
    set_restore_state(admin, 'verified_readonly')
    assert evaluate(pg) == (200, {'ready': True, 'mode': 'read_only', 'reasons': [], 'dispatch_switch': 'closed', 'real_dispatch': {'tenants_open': 0, 'actions_open': 0}})
    assert evaluate(pg, {'REAL_DISPATCH_ENABLED': 'true'})[1]['reasons'] == ['dispatch_switch_open_during_restore']
    set_restore_state(admin, 'open')
    assert evaluate(pg)[1] == {'ready': True, 'mode': 'normal', 'reasons': [], 'dispatch_switch': 'closed', 'real_dispatch': {'tenants_open': 0, 'actions_open': 0}}
    # A deletion replay open (NX-029) is not ready.
    owner(admin, 'select runtime.nexloop_replay_begin(%s,%s)', ('ready-replay', 0))
    assert evaluate(pg)[1]['reasons'] == ['deletion_replay_open']
    owner(admin, 'select runtime.nexloop_replay_finish(%s)', ('ready-replay',))
    assert evaluate(pg)[0] == 200


def test_migration_drift_is_not_ready(pg, catalog_db):
    admin = catalog_db
    (version, checksum) = admin.execute('select version,checksum from control.schema_migrations order by version desc limit 1').fetchone()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('update control.schema_migrations set checksum=%s where version=%s', ('0' * 64, version))
    try:
        assert evaluate(pg) == (503, {'ready': False, 'mode': 'not_ready', 'reasons': ['migration_drift'], 'dispatch_switch': 'closed', 'real_dispatch': {'tenants_open': None, 'actions_open': None}})
    finally:
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute('update control.schema_migrations set checksum=%s where version=%s', (checksum, version))
    assert evaluate(pg)[0] == 200


def test_readiness_functions_are_granted_to_service_roles_only(catalog_db):
    admin = catalog_db
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_metrics'):
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_service_readiness()','execute')", (role,)).fetchone() == (True,)
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_configurator'):
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_backup_status()','execute')", (role,)).fetchone() == (False,)
    assert admin.execute("select has_function_privilege('nexloop_metrics','authz.nexloop_backup_status()','execute')").fetchone() == (True,)
    assert admin.execute("select has_function_privilege('public','runtime.nexloop_erasure_readiness()','execute')").fetchone() == (False,)


GOOD = {'backup': {'status': 'ok', 'age_seconds': 3600, 'full_backup_age_seconds': 86400, 'repo_backup_age_seconds': {'repo1': 3600},
                   'verify_failed': 0, 'wal_last_archived_age_seconds': 30, 'wal_archive_failing': 0, 'journal_export_lag_seconds': 0,
                   'file_backup_age_seconds': 600, 'drill_age_seconds': 86400},
        'restore': {'ready': True, 'reads': True, 'dispatch': True, 'state': 'never_restored'}, 'erasure_ready': True}


def test_doctor_backup_passes_only_within_the_plan(tmp_path, monkeypatch):
    status = json.loads(json.dumps(GOOD))
    monkeypatch.setattr(doctor, 'read_backup_status', lambda path: status)
    result = doctor.backup(tmp_path / 'dsn', PLAN)
    assert all(result['checks'].values()) and result['details']['backup']['failed'] == []
    for mutate, failed in ((lambda s: s['backup'].update(age_seconds=93601), 'backup_recent'),
                           (lambda s: s['backup'].pop('full_backup_age_seconds'), 'full_backup_recent'),
                           (lambda s: s['backup'].update(repo_backup_age_seconds={'repo2': 1}), 'required_repos_recent'),
                           (lambda s: s['backup'].update(verify_failed=1), 'latest_backup_verified'),
                           (lambda s: s['backup'].update(wal_archive_failing=1), 'wal_archiving'),
                           (lambda s: s['backup'].update(journal_export_lag_seconds=301), 'journal_export'),
                           (lambda s: s['backup'].pop('file_backup_age_seconds'), 'file_backup_recent'),
                           (lambda s: s['backup'].update(drill_age_seconds=3024001), 'restore_drill_recent'),
                           (lambda s: s['restore'].update(state='verified_readonly'), 'restore_state_open'),
                           (lambda s: s.update(erasure_ready=False), 'deletion_replay_closed'),
                           (lambda s: s.update(backup={'status': 'unavailable'}), 'backup_manifest_present')):
        status = json.loads(json.dumps(GOOD))
        mutate(status)
        assert failed in doctor.backup(tmp_path / 'dsn', PLAN)['details']['backup']['failed'], failed


def test_doctor_backup_fails_without_a_plan_or_a_status(tmp_path, monkeypatch):
    assert doctor.backup(tmp_path / 'dsn', tmp_path / 'missing.json')['checks'] == {'backup_plan_file': False}
    plan = json.loads(PLAN.read_text())
    plan['limits']['max_backup_age_seconds'] = 0
    (tmp_path / 'plan.json').write_text(json.dumps(plan))
    assert doctor.backup(tmp_path / 'dsn', tmp_path / 'plan.json')['checks'] == {'backup_plan_file': False}
    assert doctor.backup(tmp_path / 'missing-dsn', PLAN)['checks'] == {'backup_plan_file': True, 'backup_status': False}


def test_doctor_backup_on_a_catalog_without_backups_fails_through_the_metrics_role(pg, catalog_db, tmp_path):
    dsn = private(tmp_path / 'metrics-dsn', make_conninfo(pg, user='nexloop_metrics'))
    result = doctor.backup(dsn, PLAN)
    assert result['checks']['backup_status'] is True and result['checks']['backup_manifest_present'] is False
    assert result['checks']['restore_state_open'] is True and result['checks']['deletion_replay_closed'] is True
    api = private(tmp_path / 'api-dsn', make_conninfo(pg, user='nexloop_api'))
    assert doctor.backup(api, PLAN)['checks']['backup_status'] is False  # only the read-only metrics role
