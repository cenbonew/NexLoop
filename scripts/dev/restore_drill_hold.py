"""Development machine only: the source side of the AT-072 local restore drill, held open until SIGTERM/SIGINT.

Run it only through scripts/dev/restore_drill.sh (outside `testpaths`, so CI never collects it). It reuses the test fixtures
unchanged, as the workbench smoke does, but on the disposable WAL-archived PostgreSQL 18 cluster of tests/backup_cluster.py
(so the drill can take a real base backup and restore to a point in time): bootstrap of the clean catalog, one customer
conversation, owner and operator workbench members by trusted configuration, and the tenant Actions the drill needs
(nexloop.consumer.erase, nexloop.restore.open_scope) published as the stage tenant publishes them. The production app
serves the workbench API over loopback HTTPS. Synthetic data only; REAL_DISPATCH_ENABLED=false; nothing reaches a real
channel or model. Passwords and the authority key are written only to 0600 files in <out>, never printed.
"""
import json
import os
import signal
from pathlib import Path

from conftest import admin  # noqa: F401
from backup_cluster import cluster, pg  # noqa: F401  (the archived cluster replaces the conftest `pg`)
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from commercial_fixture import publish_registry_actions
from workbench_stack_fixture import workbench_stack


def _private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(content if isinstance(content, bytes) else content.encode())


def test_hold_restore_drill_source(conversations, admin, cluster, pg, tmp_path):
    import uvicorn
    from nexloop_eios.http_api import create_app
    # From the start: a hang-up (terminal or pipe closed) must not kill the holder before its teardown; the script's trap
    # stops it with SIGTERM.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    assert os.environ.get('REAL_DISPATCH_ENABLED') == 'false', 'the restore drill runs with REAL_DISPATCH_ENABLED=false only'
    out = Path(os.environ['NEXLOOP_DRILL_DIR'])
    port = int(os.environ['NEXLOOP_DRILL_API_PORT'])
    (out / 'pg-root').write_text(str(cluster['root']) + '\n')
    config, certificate, key, credentials = workbench_stack(conversations, admin, pg, tmp_path, port=port, web=None)
    publish_registry_actions(admin, ('nexloop.consumer.erase', 'nexloop.restore.open_scope'))
    tenant = admin.execute('select tenant_id from control.nexloop_browser_applications where application_id=%s',
                           (config.workbench.application_id,)).fetchone()[0]
    # The drill's private files: connection strings of the archived cluster's roles (no passwords: trust on its own socket),
    # the authority key the restored API needs again, the loopback certificate, the accounts.
    for name, role in (('admin.dsn', 'nexloop_bootstrap'), ('recorder.dsn', 'nexloop_backup_recorder'), ('replication.dsn', 'nexloop_backup')):
        _private(out / name, cluster['as_role'](role))
    _private(out / 'authority-key', config.signing_key_file.read_bytes())
    _private(out / 'tls-cert.pem', certificate.read_bytes())
    _private(out / 'tls-key.pem', key.read_bytes())
    _private(out / 'credentials.json', json.dumps(credentials, ensure_ascii=False, indent=2))
    stack = {'pg_bin': str(cluster['bin']), 'pg_port': cluster['port'], 'pg_socket': str(cluster['socket']), 'pg_data': str(cluster['data']),
             'wal_archive': str(cluster['archive']), 'tenant_id': tenant, 'signing_key_id': config.signing_key_id,
             'browser_application_id': config.browser.application_id, 'workbench_application_id': config.workbench.application_id,
             'consumer_id': credentials['consumer_id']}
    _private(out / 'stack.json', json.dumps(stack, indent=2))
    (out / 'ready').write_text(f'https://127.0.0.1:{port}\n')
    # As in the workbench smoke: a no-op previous handler lets uvicorn.run return, so the fixtures' teardown stops PostgreSQL.
    for handled in (signal.SIGTERM, signal.SIGINT):
        signal.signal(handled, lambda *_: None)
    # Open keep-alive connections must not hold the graceful stop (and so the PostgreSQL teardown) indefinitely.
    uvicorn.run(create_app(config), host='127.0.0.1', port=port, ssl_certfile=str(certificate), ssl_keyfile=str(key),
                timeout_graceful_shutdown=5,
                access_log=False, log_level='warning')
