"""NX-028 slice 1: enterprise member identity (0140) and the owner workbench read port (0141) over real HTTP login cookies.

Design §12 items 1, 2, 13 (server side), 14 and the read part of 15. Synthetic data only. The `conversations` fixture's
Human is the customer (it owns a Consumer); the owner and operator are separate browser Humans of the same tenant who
log into the separate workbench application. Grant facts are those compiled by nexloop_eios.workbench_roles, written by
admin as the stand-in for the 0050 trusted-configuration apply (which is covered elsewhere); members go through the real
technical-configurator function.
"""
import json
import secrets
from datetime import UTC, datetime

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.identity.models import EncodedPasswordHash, LocalAccount, MembershipKind, Subject, SubjectKind, TenantMembership

from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app
from nexloop_eios.workbench_roles import apply_members, compile_member, compile_members, load, revoke_member
from test_conversation_messages import conversations
from test_browser_business_authorization import browser_business
from test_action_definitions import published_action
from test_browser_identity_reads import identity
from test_browser_session_creation import uow
from test_browser_http import ORIGIN, login
from pathlib import Path

TENANT = 'synthetic-a'
WORKBENCH_APP = 'synthetic-workbench-app'
CALLER = 'eios:application:synthetic-workbench'
ROLES = load(Path(__file__).resolve().parents[1] / 'deploy/authorization/workbench-roles.v1.json')
READ = 'eios:action:nexloop.workbench.read:1'


def private(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(0o600)
    return path


def add_human(admin, key):
    now = datetime.now(UTC)
    password = secrets.token_urlsafe(32)
    subject = Subject(subject_id=f'synthetic-staff-{key}', kind=SubjectKind.HUMAN, status='active', created_at=now, updated_at=now, revision=1)
    membership = TenantMembership(tenant_id=TENANT, principal_id=f'synthetic-staff-{key}-principal', subject_id=subject.subject_id, kind=MembershipKind.HOME,
        status='active', valid_from=now, valid_until=None, revision=1)
    account = LocalAccount(local_account_id=f'synthetic-staff-{key}-account', tenant_id=TENANT, subject_id=subject.subject_id, username=f'staff-{key}',
        verified_email=f'staff-{key}@example.invalid', password_hash=EncodedPasswordHash(PasswordHasher().hash(password)), status='active', failed_attempts=0,
        lockout_level=0, locked_until=None, must_change_password=False, session_epoch=1, created_at=now, updated_at=now, revision=1)
    admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)', (subject.subject_id, Jsonb(subject.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)', (TENANT, membership.principal_id, subject.subject_id, Jsonb(membership.model_dump(mode='json'))))
    payload = account.model_dump(mode='json')
    payload.pop('password_hash')
    payload.pop('password_history')
    admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',
        (TENANT, account.local_account_id, subject.subject_id, account.username, account.password_hash.get_secret_value(), [], Jsonb(payload)))
    return {'principal_id': membership.principal_id, 'subject_id': subject.subject_id, 'username': account.username, 'password': password}


def write_facts(admin, facts):
    for item in facts:
        kind, key, payload = (item['kind'], item['key'], item['payload']) if isinstance(item, dict) else (item[0], item[1], item[2].model_dump(mode='json'))
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) '
            'on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload', (TENANT, kind, key, Jsonb(payload)))


def members_file(*members):
    return {'schema_version': 'nexloop-workbench-members/1', 'tenant_id': TENANT, 'application_id': WORKBENCH_APP, 'caller_application_id': CALLER,
            'members': [{'principal_id': m['principal_id'], 'subject_id': m['subject_id'], 'role': role} for m, role in members]}


def workbench_login(client, human):
    return client.post('/api/v1/workbench/auth/login', headers={'Origin': ORIGIN}, json={'username': human['username'], 'password': human['password']})


@pytest.fixture
def workbench(conversations, admin, pg, tmp_path):
    f = conversations
    # The customer: the fixture's Human owns a Consumer and holds a Conversation with a refusal message.
    customer = {'principal_id': f['principal'], 'subject_id': f['base']['identity'][1].subject_id,
                'username': f['base']['identity'][3].username, 'password': f['base']['identity'][-1]}
    conversation = f['port'].create_conversation(idempotency_key='nx028-workbench-conversation')
    refusal = f['port'].accept_message(conversation_id=conversation['id'], idempotency_key='nx028-workbench-refusal', body='请不要再给我发短信了')['message']
    # The workbench application: its own browser application and business application (D1).
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    owner, operator, outsider = add_human(admin, 'owner'), add_human(admin, 'operator'), add_human(admin, 'outsider')
    admin.execute('alter role nexloop_configurator login')  # the deployment's technical configurator logs in
    configurator = private(tmp_path, 'configurator-dsn', make_conninfo(pg, user='nexloop_configurator'))
    reader = f['reader']
    config = ApiConfiguration(private(tmp_path, 'business-dsn', make_conninfo(pg, user='nexloop_api')), private(tmp_path, 'authority-key', reader.signer.material),
        tmp_path / 'artifacts', reader.signer.key_id,
        BrowserConfiguration(private(tmp_path, 'identity-dsn', f['base']['identity'][0]), private(tmp_path, 'rate-key', secrets.token_hex(32)), TENANT, 'synthetic-browser-app', ORIGIN),
        execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, ORIGIN))
    (tmp_path / 'artifacts').mkdir(mode=0o700)
    return dict(f=f, admin=admin, owner=owner, operator=operator, outsider=outsider, customer=customer, configurator=configurator,
                conversation=conversation, refusal=refusal, client=lambda: TestClient(create_app(config), base_url=ORIGIN))


def configure(w, *members):
    return apply_members(ROLES, members_file(*members), database_url_file=w['configurator'])


def provision(w):
    """Owner and operator members with their full role grants."""
    configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    write_facts(w['admin'], compile_members(ROLES, members_file((w['owner'], 'owner'), (w['operator'], 'operator'))))


def test_customer_never_member_or_grant_and_member_never_customer(workbench):
    w = workbench
    admin = w['admin']
    # A customer principal cannot be configured as a member.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        configure(w, (w['customer'], 'operator'))
    provision(w)
    # Nor can trusted configuration grant a customer any workbench Action, not even the read.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        write_facts(admin, compile_member(ROLES, TENANT, {**w['customer'], 'role': 'owner'}, valid_from=datetime(2026, 10, 10, tzinfo=UTC)))
    # An operator never receives an owner-only Action (ADR-023 §2.4: only the owner releases a restriction).
    owner_grant = [x for x in compile_member(ROLES, TENANT, {**w['operator'], 'role': 'owner'}, valid_from=datetime(2026, 10, 10, tzinfo=UTC))
                   if x[0] == 'grants' and x[1][1] == 'eios:action:Contact.release:1']
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        write_facts(admin, owner_grant)
    # A non-member never receives one either.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        write_facts(admin, compile_member(ROLES, TENANT, {**w['outsider'], 'role': 'operator'}, valid_from=datetime(2026, 10, 10, tzinfo=UTC)))
    # A member never becomes a customer owner.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("insert into control.nexloop_consumer_owners values(%s,'real',%s,%s,'nx028-ownership','nx028-member-owner')",
            (TENANT, w['owner']['principal_id'], 'd' * 64))
    # Shrinking a role while its grants remain is refused: revoke first, then reconfigure.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        configure(w, (w['owner'], 'owner'))
    write_facts(admin, revoke_member(ROLES, TENANT, w['operator']['principal_id']))
    assert configure(w, (w['owner'], 'owner'))['members'] == 1
    # Only the technical configurator configures; the API role cannot.
    with psycopg.connect(make_conninfo(w['f']['pg'], user='nexloop_api')) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("select control.nexloop_configure_workbench('synthetic-a','{}'::jsonb,'{}'::jsonb)")
    rows = admin.execute('select manifest_version,roles_version,members from control.nexloop_workbench_configurations order by manifest_version').fetchall()
    assert [r[:2] for r in rows] == [(1, 1), (2, 1)] and rows[-1][2] == {w['owner']['principal_id']: 'owner'}
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute('delete from control.nexloop_workbench_configurations')


def test_http_roles_401_403_and_separate_realm(workbench):
    w = workbench
    provision(w)
    with w['client']() as client:
        assert client.get('/api/v1/workbench/overview').status_code == 401
        # The WebChat cookie is not a workbench session.
        assert login(client, w['f']['base']['identity']).status_code == 200
        assert client.get('/api/v1/workbench/overview').status_code == 401
    with w['client']() as client:
        # The customer may authenticate (the identity is real) but holds nothing in the workbench: 403, no content.
        assert workbench_login(client, w['customer']).status_code == 200
        denied = client.get('/api/v1/workbench/contact')
        assert denied.status_code == 403 and '短信' not in denied.text
        assert client.get('/api/v1/workbench/goals').status_code == 403
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        overview = client.get('/api/v1/workbench/overview')
        assert overview.status_code == 200 and overview.headers['cache-control'] == 'no-store'
        sections = overview.json()
        assert {k: v['status'] for k, v in sections.items()} == {'goals': 'ok', 'commitments': 'ok', 'actions': 'ok', 'contact': 'ok',
            'takeovers': 'unavailable', 'backlog': 'unavailable', 'commercial': 'unavailable'}
        assert sections['contact']['data'] == {'restricted': 1, 'escalations': 0}
        settings = client.get('/api/v1/workbench/settings')
        assert settings.status_code == 200 and {m['role'] for m in settings.json()['members']} == {'owner', 'operator'}
        assert client.get('/api/v1/workbench/actions?state=sideways').status_code == 422
        assert client.get('/api/v1/workbench/consumers?limit=0').status_code == 422
        assert client.get('/api/v1/workbench/commitments/' + 'e' * 64).status_code == 404
        assert client.get('/api/v1/workbench/conversations/' + 'e' * 64).status_code == 404
        assert client.get('/api/v1/workbench/takeovers').json()['status'] == 'unavailable'
        assert client.get('/api/v1/workbench/overview', headers={'Host': 'other.example'}).status_code == 403
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        assert client.get('/api/v1/workbench/overview').status_code == 200
        # Settings and governance are owner-only (design §3).
        assert client.get('/api/v1/workbench/settings').status_code == 403


def test_partial_grants_give_partial_sections_and_revocation_is_immediate(workbench):
    w = workbench
    admin = w['admin']
    configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    # The operator holds only the workbench read (a subset of its role).
    facts = compile_members(ROLES, members_file((w['operator'], 'operator')))
    write_facts(admin, [x for x in facts if not (x['kind'] in ('grants', 'scope', 'controls', 'policies') and x['key'][1] != READ)])
    write_facts(admin, revoke_member(ROLES, TENANT, w['operator']['principal_id'], [a for a in ROLES['roles']['operator'] if a != READ]))
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        sections = client.get('/api/v1/workbench/overview').json()
        # AT-045: a section without its grant is "forbidden", never an empty success or a zero.
        assert sections['goals']['status'] == 'ok' and sections['contact'] == {'status': 'forbidden', 'data': None}
        assert sections['commitments'] == {'status': 'forbidden', 'data': None}
        assert client.get('/api/v1/workbench/contact').status_code == 403
        assert client.get('/api/v1/workbench/goals').status_code == 200
        # Revocation takes effect on the very next request (PG re-authentication per request, design §12 item 2).
        write_facts(admin, revoke_member(ROLES, TENANT, w['operator']['principal_id'], [READ]))
        assert client.get('/api/v1/workbench/goals').status_code == 403
        assert client.get('/api/v1/workbench/overview').json()['goals']['status'] == 'forbidden'


def test_owner_views_and_contract(workbench):
    """Slice 1 views over real data; since ADR-025 the owner's role derives the Message reads (negative cases:
    test_workbench_member_read_pg — restricted groups, non-evidence reviewer reads, revocation, audit failure)."""
    w = workbench
    provision(w)
    f = w['f']
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        contact = client.get('/api/v1/workbench/contact')
        assert contact.status_code == 200
        import jsonschema
        jsonschema.validate(contact.json(), json.loads((Path(__file__).resolve().parents[1] / 'packages/contracts/contact-restriction-view.schema.json').read_text()))
        restriction = contact.json()['restrictions'][0]
        assert restriction['consumer_id'] == f['consumer'] and restriction['matched_text_status'] == 'ok' and restriction['matched_text'] in '请不要再给我发短信了'
        assert restriction['rule_id'] == 'stop-contact' and restriction['hits'][0]['matched_text_status'] == 'ok'
        view = client.get('/api/v1/workbench/conversations/' + w['conversation']['id'])
        message = view.json()['messages'][0]
        assert message['id'] == w['refusal']['id'] and message['content']['status'] == 'ok' and message['direction'] == 'inbound'
        assert message['provider']['namespace'] == 'nexloop.api'
        consumer = client.get('/api/v1/workbench/consumers/' + f['consumer'])
        # The fixture's Consumer type has no properties: an empty, readable set (never "forbidden" for the owner).
        assert consumer.status_code == 200 and consumer.json()['properties'] == {'status': 'ok', 'values': {}, 'withheld': []}
        assert consumer.json()['restriction']['active'] is True and consumer.json()['conversations'][0]['conversation_id'] == w['conversation']['id']
        listed = client.get('/api/v1/workbench/consumers').json()
        assert listed['items'][0]['consumer_id'] == f['consumer'] and listed['items'][0]['restricted'] is True and listed['next_cursor'] is None


def test_read_port_refuses_services_and_other_applications(workbench):
    """The port accepts only a workbench-application Human session: the customer's WebChat session and any service fail closed."""
    w = workbench
    provision(w)
    f = w['f']
    from nexloop_eios.workbench_reads import WorkbenchForbidden, WorkbenchReader
    # The customer's WebChat-application session (even holding Consumer/Message grants): refused by SQL.
    with pytest.raises(WorkbenchForbidden):
        WorkbenchReader(f['reader'].pool, f['human'], f['reader'].signer).call('goals')
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_domain_worker')) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("select authz.nexloop_workbench_read('x','real','{}','x','{}')")
