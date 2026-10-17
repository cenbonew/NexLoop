"""Development machine only: a disposable workbench smoke environment, held open until SIGTERM/SIGINT.

Run it only through scripts/dev/workbench_smoke.sh (it lives outside `testpaths`, so CI never collects it). It reuses the
test fixtures unchanged: a throwaway PostgreSQL (initdb in a temp dir, bootstrap of the clean catalog), the governed browser
identities, the customer's own Conversation port, trusted configuration through the configurator (workbench members and
their role grants compiled from deploy/authorization/workbench-roles). Synthetic data only; REAL_DISPATCH_ENABLED=false and
the deterministic test profile; nothing reaches a real channel or model. The production app serves the built Web over loopback
HTTPS (a one-day self-signed certificate for 127.0.0.1) until the process is stopped; then the fixtures stop PostgreSQL and
remove their temporary directories. Passwords are written only to <out>/credentials.json (0600), never printed.
"""
import json
import os
import signal
from pathlib import Path

from conftest import admin, pg  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from workbench_stack_fixture import workbench_stack


def test_hold_workbench_smoke(conversations, admin, pg, tmp_path):
    import uvicorn
    from nexloop_eios.http_api import create_app
    assert os.environ.get('REAL_DISPATCH_ENABLED') == 'false', 'dev smoke runs with REAL_DISPATCH_ENABLED=false only'
    out = Path(os.environ['NEXLOOP_DEV_SMOKE_DIR'])
    # The fixture's throwaway cluster lives in its own mkdtemp directory (host= is its socket dir): record it so the wrapper
    # can stop that exact cluster if this process dies before its teardown (SIGKILL, timeout).
    (out / 'pg-root').write_text(str(Path(dict(x.split('=', 1) for x in pg.split())['host']).parent) + '\n')
    port = int(os.environ['NEXLOOP_DEV_SMOKE_PORT'])
    web = Path(os.environ['NEXLOOP_DEV_SMOKE_WEB'])
    origin = f'https://127.0.0.1:{port}'
    config, certificate, key, credentials = workbench_stack(conversations, admin, pg, tmp_path, port=port, web=web)
    descriptor = os.open(out / 'credentials.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(credentials, handle, ensure_ascii=False, indent=2)
    (out / 'ready').write_text(origin + '\n')
    # uvicorn (0.54) restores the previous handler after a graceful stop and re-raises the signal it caught; with the default
    # SIGTERM action that kills this process before the fixtures' teardown stops PostgreSQL. A no-op previous handler lets
    # uvicorn.run return, so the teardown runs.
    for handled in (signal.SIGTERM, signal.SIGINT):
        signal.signal(handled, lambda *_: None)
    uvicorn.run(create_app(config), host='127.0.0.1', port=port, ssl_certfile=str(certificate), ssl_keyfile=str(key),
                access_log=False, log_level='warning')
