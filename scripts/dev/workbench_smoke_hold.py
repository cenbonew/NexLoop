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
import subprocess
from pathlib import Path

from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from conftest import admin, pg  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from commitment_fixture import TAKEOVER_ACTIONS, publish_request_actions
from test_workbench_actions_http import publish_goal_actions
from test_workbench_read_pg import CALLER, ROLES, TENANT, WORKBENCH_APP, add_human, members_file, write_facts


def private(path, content):
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(0o600)
    return path


def test_hold_workbench_smoke(conversations, admin, pg, tmp_path):
    import uvicorn
    from nexloop_eios.browser_http import BrowserConfiguration
    from nexloop_eios.http_api import ApiConfiguration, create_app
    from nexloop_eios.workbench_roles import apply_members, compile_members
    assert os.environ.get('REAL_DISPATCH_ENABLED') == 'false', 'dev smoke runs with REAL_DISPATCH_ENABLED=false only'
    out = Path(os.environ['NEXLOOP_DEV_SMOKE_DIR'])
    port = int(os.environ['NEXLOOP_DEV_SMOKE_PORT'])
    web = Path(os.environ['NEXLOOP_DEV_SMOKE_WEB'])
    origin = f'https://127.0.0.1:{port}'
    f = conversations
    # The customer's conversation and one inbound message, through the customer's own governed port (first: later
    # authority seeding moves the directory the port's session was authenticated against).
    conversation = f['port'].create_conversation(idempotency_key='dev-smoke-conversation')
    f['port'].accept_message(conversation_id=conversation['id'], idempotency_key='dev-smoke-inbound-0001',
                             body='你好，我的订单一直没有发货，能帮我查一下吗？')
    consumer = admin.execute('select consumer_id from runtime.nexloop_conversations where conversation_id=%s', (conversation['id'],)).fetchone()[0]
    # Tenant Actions the workbench writes need (slices 2/3), compiled from the versioned manifests.
    publish_request_actions(admin)
    publish_request_actions(admin, names=TAKEOVER_ACTIONS)
    publish_goal_actions(admin)
    # The workbench: its own browser application; owner and operator members by trusted configuration (configurator).
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    owner, operator = add_human(admin, 'owner'), add_human(admin, 'operator')
    admin.execute('alter role nexloop_configurator login')
    members = members_file((owner, 'owner'), (operator, 'operator'))
    apply_members(ROLES, members, database_url_file=private(tmp_path / 'configurator-dsn', make_conninfo(pg, user='nexloop_configurator')))
    write_facts(admin, compile_members(ROLES, members))
    # Loopback HTTPS for the browser realms (their origins are fixed HTTPS).
    certificate, key = tmp_path / 'tls-cert.pem', tmp_path / 'tls-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(certificate), '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    certificate.chmod(0o600)
    key.chmod(0o600)
    reader = f['reader']
    (tmp_path / 'artifacts').mkdir(mode=0o700)
    identity_dsn = private(tmp_path / 'identity-dsn', f['base']['identity'][0])
    rate_key = private(tmp_path / 'rate-key', os.urandom(32).hex())
    config = ApiConfiguration(private(tmp_path / 'business-dsn', make_conninfo(pg, user='nexloop_api')), private(tmp_path / 'authority-key', reader.signer.material),
        tmp_path / 'artifacts', reader.signer.key_id, BrowserConfiguration(identity_dsn, rate_key, TENANT, 'synthetic-browser-app', origin),
        web_root=web, execution_profile='deterministic-test', workbench=BrowserConfiguration(identity_dsn, rate_key, TENANT, WORKBENCH_APP, origin))
    customer = f['base']['identity']
    credentials = {'origin': origin, 'workbench': origin + '/workbench', 'webchat': origin + '/',
                   'owner': {'username': owner['username'], 'password': owner['password']},
                   'operator': {'username': operator['username'], 'password': operator['password']},
                   'customer': {'username': customer[3].username, 'password': customer[-1]},
                   'consumer_id': consumer, 'conversation_id': conversation['id']}
    descriptor = os.open(out / 'credentials.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(credentials, handle, ensure_ascii=False, indent=2)
    (out / 'ready').write_text(origin + '\n')
    uvicorn.run(create_app(config), host='127.0.0.1', port=port, ssl_certfile=str(certificate), ssl_keyfile=str(key),
                access_log=False, log_level='warning')
