"""The workbench stack on a disposable PostgreSQL (shared by scripts/dev/workbench_smoke_hold.py and the real-browser workbench test).

Synthetic data only: one customer conversation with one inbound message through the customer's own governed port, the tenant
Actions the workbench writes, owner and operator workbench members by trusted configuration, and the production app configured
for loopback HTTPS (a one-day self-signed certificate for 127.0.0.1). REAL_DISPATCH_ENABLED must be false; nothing reaches a
real channel or model. Passwords are returned to the caller, never printed."""
import os
import subprocess

from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from commercial_fixture import publish_registry_actions
from commitment_fixture import TAKEOVER_ACTIONS, publish_request_actions
from test_workbench_actions_http import publish_goal_actions
from test_workbench_read_pg import CALLER, ROLES, TENANT, WORKBENCH_APP, add_human, members_file, write_facts


def private(path, content):
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(0o600)
    return path


def workbench_stack(conversations, admin, pg, tmp_path, *, port, web, seed=None):
    """Returns (ApiConfiguration, certificate, key, credentials) for https://127.0.0.1:<port> serving `web`. `seed`: optional
    synthetic records (tests/smoke_seed.large_records) loaded through tests/load_template.load: more customers, conversations and
    messages for layout checks; credentials then carry `seed` counts (no seeded passwords)."""
    from nexloop_eios.browser_http import BrowserConfiguration
    from nexloop_eios.http_api import ApiConfiguration
    from nexloop_eios.workbench_roles import apply_members, compile_members
    assert os.environ.get('REAL_DISPATCH_ENABLED') == 'false', 'the workbench stack runs with REAL_DISPATCH_ENABLED=false only'
    origin = f'https://127.0.0.1:{port}'
    f = conversations
    # The customer's conversation and one inbound message, through the customer's own governed port (first: later
    # authority seeding moves the directory the port's session was authenticated against).
    conversation = f['port'].create_conversation(idempotency_key='dev-smoke-conversation')
    f['port'].accept_message(conversation_id=conversation['id'], idempotency_key='dev-smoke-inbound-0001',
                             body='你好，我的订单一直没有发货，能帮我查一下吗？')
    consumer = admin.execute('select consumer_id from runtime.nexloop_conversations where conversation_id=%s', (conversation['id'],)).fetchone()[0]
    seeded = None
    if seed:
        # Before the workbench configuration (load_template's ordering: identities, consumers, grants, messages, then members).
        from load_template import load
        seeded = load(f, admin, seed)['counts']
    # Tenant Actions the workbench writes need (slices 2/3), compiled from the versioned manifests.
    publish_request_actions(admin)
    publish_request_actions(admin, names=TAKEOVER_ACTIONS)
    publish_goal_actions(admin)
    # The owner's real dispatch switch Action (0153), as the stage tenant publishes it; the switches start closed.
    publish_registry_actions(admin, ('nexloop.real_dispatch.set',))
    # Operator persona pages (0227-0229): the owner's persona, rules, assignment, AI disclosure and preference Actions.
    publish_registry_actions(admin, ('nexloop.operator_persona.publish', 'nexloop.operator_persona.assign', 'nexloop.operator_persona.rules_publish',
                                     'nexloop.ai_disclosure.set', 'nexloop.contact_preference.dismiss'))
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
        web_root=web, execution_profile='deterministic-test', workbench=BrowserConfiguration(identity_dsn, rate_key, TENANT, WORKBENCH_APP, origin),mode='test')
    customer = f['base']['identity']
    credentials = {'origin': origin, 'workbench': origin + '/workbench', 'webchat': origin + '/',
                   'owner': {'username': owner['username'], 'password': owner['password']},
                   'operator': {'username': operator['username'], 'password': operator['password']},
                   'customer': {'username': customer[3].username, 'password': customer[-1]},
                   'consumer_id': consumer, 'conversation_id': conversation['id'], **({'seed': seeded} if seeded else {})}
    return config, certificate, key, credentials
