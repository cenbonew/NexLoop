#!/usr/bin/env python3
"""Development machine only: serve the workbench API on the RESTORED instance of the AT-072 local restore drill.

    restore_drill_serve.py --drill-dir .dev-drill --pg-data <restored --pg-path> --pg-socket <restored --socket-dir> --pg-port <restored --port>

The restore left every application role NOLOGIN and pg_hba open only to the restoring owner (NX-035 §6.1). Before the owner
can open a scope through the workbench, the two roles the API connects as get NEW random passwords (the old ones came back with
the physical backup, design §6.5 "已轮换口令"), LOGIN, and a scram-sha-256 line on the instance's own socket; this happens
only in the drill's disposable instance (the data directory must be under --drill-dir). The authority key and the accounts
are the source's (restored with the database). Loopback HTTPS with the source's certificate; REAL_DISPATCH_ENABLED=false.
On Ctrl-C the two roles are set NOLOGIN again. Passwords go only to 0600 files in <drill-dir>/restored-api/, never printed.
"""
import argparse
import json
import os
import secrets
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
ROLES = ('nexloop_api', 'nexloop_identity')


def _private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(content)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--drill-dir', type=Path, required=True)
    parser.add_argument('--pg-data', type=Path, required=True)
    parser.add_argument('--pg-socket', type=Path, required=True)
    parser.add_argument('--pg-port', type=int, required=True)
    parser.add_argument('--api-port', type=int, default=8754)
    a = parser.parse_args(argv)
    if os.environ.get('REAL_DISPATCH_ENABLED') != 'false':
        print(json.dumps({'served': False, 'refused': 'set REAL_DISPATCH_ENABLED=false'}))
        return 2
    import psycopg
    import uvicorn
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.browser_http import BrowserConfiguration
    from nexloop_eios.http_api import ApiConfiguration, create_app
    drill, data = a.drill_dir.resolve(), a.pg_data.resolve()
    if drill not in data.parents or not (data / 'PG_VERSION').is_file():
        print(json.dumps({'served': False, 'refused': 'the restored data directory must be under --drill-dir'}))
        return 2
    stack = json.loads((drill / 'stack.json').read_text())
    out = drill / 'restored-api'
    out.mkdir(mode=0o700)
    owner = make_conninfo(host=str(a.pg_socket), port=a.pg_port, dbname='postgres', user='nexloop_bootstrap')
    files = {}
    with psycopg.connect(owner, autocommit=True) as db:
        state = db.execute('select state from control.nexloop_restore_state').fetchone()
        if state is None or state[0] != 'verified_readonly':
            print(json.dumps({'served': False, 'refused': 'the restored instance is not verified_readonly', 'state': state and state[0]}))
            return 1
        with open(data / 'pg_hba.conf', 'a') as hba:
            for role in ROLES:
                hba.write(f'local postgres {role} scram-sha-256\n')
        for role in ROLES:
            password = secrets.token_urlsafe(32)
            db.execute(sql.SQL('alter role {} login password {}').format(sql.Identifier(role), sql.Literal(password)))
            files[role] = _private(out / f'{role}.dsn', make_conninfo(host=str(a.pg_socket), port=a.pg_port, dbname='postgres', user=role, password=password))
        db.execute('select pg_reload_conf()')
    origin = f'https://127.0.0.1:{a.api_port}'
    (out / 'artifacts').mkdir(mode=0o700)
    rate_key = _private(out / 'rate-key', os.urandom(32).hex())
    browser = BrowserConfiguration(files['nexloop_identity'], rate_key, stack['tenant_id'], stack['browser_application_id'], origin)
    workbench = BrowserConfiguration(files['nexloop_identity'], rate_key, stack['tenant_id'], stack['workbench_application_id'], origin)
    config = ApiConfiguration(files['nexloop_api'], drill / 'authority-key', out / 'artifacts', stack['signing_key_id'], browser,
                              execution_profile='deterministic-test', workbench=workbench,mode='test')
    (out / 'ready').write_text(origin + '\n')
    print(json.dumps({'served': True, 'origin': origin, 'state': 'verified_readonly'}), flush=True)
    for handled in (signal.SIGTERM, signal.SIGINT):
        signal.signal(handled, lambda *_: None)
    try:
        uvicorn.run(create_app(config), host='127.0.0.1', port=a.api_port, ssl_certfile=str(drill / 'tls-cert.pem'),
                    ssl_keyfile=str(drill / 'tls-key.pem'), access_log=False, log_level='warning')
    finally:
        with psycopg.connect(owner, autocommit=True) as db:
            for role in ROLES:
                db.execute(sql.SQL('alter role {} nologin password null').format(sql.Identifier(role)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
