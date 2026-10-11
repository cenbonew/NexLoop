"""NX-031 slice 0: governed loading of the synthetic manifest, and the template cluster (design §2.3).

Used by scripts/load/build_template.py (the template build) and tests/test_load_template_pg.py (small-scale check). Only
governed entries write business data:
  * customer identities are the trusted bootstrap rows the identity fixtures write (no self-registration exists);
  * Consumer objects through the registrar's governed `Consumer.create`, ownership through its `ConsumerOwnership.create`;
  * each customer's Action grants are the trusted authority facts the conversation fixture issues (Conversation.create,
    Message.create, conversation read), with the customer as subject;
  * conversations and messages through each customer's own browser session and ConversationMessagePort (0046 write, so the
    0109 refusal check and the NX-051 projection run on every message);
  * workbench members through the configurator.
Ordering keeps every authenticated snapshot current: identities, then consumers and ownership (registrar), then all grant
facts, then one session per customer for its messages, then workbench configuration last.
The template is an online, consistent pg_basebackup of the loaded cluster; a cell clones it into its own PGDATA and port.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from argon2 import PasswordHasher
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.identity.models import EncodedPasswordHash, LocalAccount, MembershipKind, Subject, SubjectKind, TenantMembership
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.conversation_messages import CREATE, MESSAGE, READ, ConversationMessagePort
from nexloop_eios.object_actions import GovernedObjectCreator
from authority_fixture import authority_records
from test_browser_evidence import authenticate
from test_browser_session_creation import create

TENANT = 'synthetic-a'
PG_BIN = Path(os.environ.get('NEXLOOP_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@18/bin'))


def _identity(admin, record, pg_identity_dsn, hasher):
    now = datetime.now(UTC)
    index = record['index']
    password = secrets.token_urlsafe(24)
    subject = Subject(subject_id=f'load-subject-{index:05d}', kind=SubjectKind.HUMAN, status='active', created_at=now, updated_at=now, revision=1)
    membership = TenantMembership(tenant_id=TENANT, principal_id=f'load-principal-{index:05d}', subject_id=subject.subject_id, kind=MembershipKind.HOME,
                                  status='active', valid_from=now, valid_until=None, revision=1)
    account = LocalAccount(local_account_id=f'load-account-{index:05d}', tenant_id=TENANT, subject_id=subject.subject_id, username=record['username'],
                           verified_email=f'{record["username"]}@example.invalid', password_hash=EncodedPasswordHash(hasher.hash(password)), status='active',
                           failed_attempts=0, lockout_level=0, locked_until=None, must_change_password=False, session_epoch=1, created_at=now, updated_at=now, revision=1)
    admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)', (subject.subject_id, Jsonb(subject.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)', (TENANT, membership.principal_id, subject.subject_id, Jsonb(membership.model_dump(mode='json'))))
    payload = account.model_dump(mode='json')
    payload.pop('password_hash')
    payload.pop('password_history')
    admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',
                  (TENANT, account.local_account_id, subject.subject_id, account.username, account.password_hash.get_secret_value(), [], Jsonb(payload)))
    return (pg_identity_dsn, subject, membership, account, password)


def _grant(admin, subject, principal):
    """The customer's conversation grants, exactly as tests/test_conversation_messages.conversations issues them."""
    for name in (CREATE, MESSAGE, READ):
        auth, _, rows = authority_records(TENANT, 'eios:action:' + name + ':1', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION)

        def convert(value):
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, list):
                return [convert(item) for item in value]
            if value == auth.subject_id:
                return subject
            if value == auth.subject_principal_id:
                return principal
            if value == 'service':
                return 'human'
            return value
        for kind, key, fact in rows:
            if kind in ('subject', 'membership', 'authentication', 'application'):
                continue
            body = convert(fact.model_dump(mode='json'))
            body.pop('snapshot_digest', None)
            actual = type(fact).model_validate_json(json.dumps(body))
            admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) '
                          'on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
                          (TENANT, kind, convert(key), Jsonb(actual.model_dump(mode='json'))))


def load(f, admin, records, *, progress=None):
    """Load manifest consumer records into the conversations fixture's tenant. Returns counts and per-phase seconds."""
    reader, uow = f['reader'], f['base']['uow']
    identity_dsn = f['base']['identity'][0]
    timings, counts = {}, {'consumers': 0, 'conversations': 0, 'messages': 0}
    hasher = PasswordHasher()
    started = time.monotonic()
    identities = [_identity(admin, record, identity_dsn, hasher) for record in records]
    timings['identities'] = time.monotonic() - started
    started = time.monotonic()
    creator = GovernedObjectCreator(reader.pool, f['registrar'].session, reader.signer)
    consumers = []
    for record, identity in zip(records, identities):
        consumer = creator.create(action_name='Consumer.create', action_version=1, intent_id=f'load-consumer-{record["index"]:05d}',
                                  type_name='Consumer', properties={})['object_id']
        f['registrar'].register_consumer_owner(consumer_id=consumer, principal_id=identity[2].principal_id,
                                               idempotency_key=f'load-owner-{record["index"]:05d}-key')
        consumers.append(consumer)
    timings['consumers'] = time.monotonic() - started
    started = time.monotonic()
    for identity in identities:
        _grant(admin, identity[1].subject_id, identity[2].principal_id)
    timings['grants'] = time.monotonic() - started
    started = time.monotonic()
    for n, (record, identity) in enumerate(zip(records, identities), 1):
        issued = create(uow, identity, authenticate(uow, identity).evidence)
        port = ConversationMessagePort(reader.pool, authenticate_browser_business(reader.pool, issued.session, world='real'), reader.signer)
        for conversation in record['conversations']:
            created = port.create_conversation(idempotency_key=f'load-{conversation["key"]}-conversation')
            counts['conversations'] += 1
            for m, message in enumerate(conversation['messages'], 1):
                port.accept_message(conversation_id=created['id'], idempotency_key=f'load-{conversation["key"]}-{m:04d}', body=message['body'])
                counts['messages'] += 1
        counts['consumers'] += 1
        if progress and n % 50 == 0:
            progress(n, time.monotonic() - started)
    timings['messages'] = time.monotonic() - started
    credentials = [{'username': i[3].username, 'password': i[-1], 'principal_id': i[2].principal_id, 'consumer_id': c}
                   for i, c in zip(identities, consumers)]
    return {'counts': counts, 'seconds': {k: round(v, 1) for k, v in timings.items()}, 'credentials': credentials}


def _dsn_parts(pg):
    return dict(item.split('=', 1) for item in pg.split())


def snapshot(pg, out):
    """An online, consistent copy of the running cluster (pg_basebackup, WAL streamed)."""
    parts = _dsn_parts(pg)
    target = Path(out) / 'pgdata'
    started = time.monotonic()
    subprocess.run([str(PG_BIN / 'pg_basebackup'), '-D', str(target), '-h', parts['host'], '-p', parts['port'], '-U', parts['user'],
                    '-X', 'stream', '-c', 'fast'], check=True, capture_output=True)
    return round(time.monotonic() - started, 1)


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Clone:
    """One cell's cluster: a copy of the template PGDATA, its own socket directory and port; stop() removes it."""

    def __init__(self, template, dest):
        self.root = Path(dest)
        # The socket directory is short (Unix socket paths are limited to ~103 bytes); the data stays under `dest`.
        self.data, self.sock = self.root / 'data', Path(tempfile.mkdtemp(prefix='nl-sock-'))
        started = time.monotonic()
        self.root.mkdir(mode=0o700)
        shutil.copytree(Path(template) / 'pgdata', self.data, symlinks=True)
        self.data.chmod(0o700)
        self.port = _free_port()
        self.copy_seconds = round(time.monotonic() - started, 1)
        started = time.monotonic()
        subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(self.data), '-l', str(self.root / 'postgres.log'), '-o',
                        f"-k {self.sock} -p {self.port} -c listen_addresses=''", '-w', 'start'], check=True, capture_output=True)
        self.start_seconds = round(time.monotonic() - started, 1)
        self.dsn = f'host={self.sock} port={self.port} dbname=postgres user=nexloop_bootstrap'

    def stop(self):
        subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(self.data), '-m', 'fast', '-w', 'stop'], check=True, capture_output=True)
        shutil.rmtree(self.root)
        shutil.rmtree(self.sock, ignore_errors=True)


def manifest_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_commercial(env, records, *, connector_id='load-shop'):
    """Commercial events of the manifest (D1: a test connector, data_mode test, world test), signed with the connector key and
    passed to the actual intake function as the webhook does; then the recorder drains the commercial-record feed. Customer
    refs stay unlinked (a test-world connector links only test-world Consumers). Returns counts and seconds."""
    started = time.monotonic()
    env.connector(connector_id, world='test', data_mode='test', currencies=('CNY',))
    statuses = {}
    for record in records:
        payment = None
        for event in record['commercial_events']:
            body = {'event_id': event['event_key'], 'external_id': event['event_key'], 'occurred_at': datetime.now(UTC).isoformat(),
                    'amount_minor': event['amount_minor'], 'currency': event['currency'], 'customer_ref': record['username']}
            if event['kind'] == 'payment' or payment is None:
                body['type'] = 'payment.succeeded'
                payment = payment or (event['event_key'], event['amount_minor'])
            else:
                body.update(type='refund.succeeded', amount_minor=min(event['amount_minor'], payment[1]),
                            related_kind='payment', related_external_id=payment[0])
            status = int(env.post(body, connector_id=connector_id).get('status', 0))
            statuses[status] = statuses.get(status, 0) + 1
    ingest_seconds = time.monotonic() - started
    started = time.monotonic()
    ticks = 0
    while True:
        summary = env.recorder('test').run_once()
        ticks += 1
        if not any(v for v in summary.values() if type(v) is int):
            break
    return {'statuses': statuses, 'ticks': ticks, 'seconds': {'ingest': round(ingest_seconds, 1), 'record': round(time.monotonic() - started, 1)}}


def apply_service_grants(admin, pg, private_dir, *, tenant=TENANT):
    """The repository service-grants manifest applied through the configurator (as deployed, NX-048), so every service process
    of a cell authenticates with its own credential. Tokens and the deployment signing key go to `private_dir` (0600)."""
    from psycopg.conninfo import make_conninfo
    from nexloop_eios import service_grants as G
    root = Path(__file__).resolve().parents[1]
    manifest = G.load(root / 'deploy/authorization/service-grants.v1.json')
    tokens = {p['credential_reference']: secrets.token_urlsafe(48) for p in manifest['principals']}
    key = secrets.token_bytes(32)
    admin.execute('alter role nexloop_configurator login')

    def private(name, content):
        path = Path(private_dir) / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(content)
        return path
    dsn = private('configurator-dsn.tmp', make_conninfo(pg, user='nexloop_configurator'))
    report = G.apply(manifest, tenant, database_url_file=dsn, signing_key_file=private('deploy-signing-key', key.hex()),
                     signing_key_id='load-deploy', service_secrets_file=private('service-tokens.json', json.dumps(tokens)),
                     credential_expires_at=datetime.now(UTC) + timedelta(days=30),
                     owner_restrictions=G.load_owner_restrictions(root / 'deploy/authorization/owner-property-restrictions.json'))
    dsn.unlink()
    principals = {p['role']: {'credential_reference': p['credential_reference'], 'database_role': p['database_role']} for p in manifest['principals']}
    private('service-principals.json', json.dumps(principals))
    return {'credentials_created': len(report['credentials_created']), 'grants_added': len(report['grants_added'])}


def load_commitments(f, admin, pg, records, private_dir, workdir, operator, *, tenant=TENANT, signer=None, identity_dsn=None,
                     browser_app='synthetic-browser-app', workbench_app='synthetic-workbench-app'):
    """Commitments of the manifest's `commitment` consumers, only through governed entries: a workbench operator (real login
    cookie, production app) takes the consumer's first conversation over, replies as staff to its commitment request with the
    synthetic promise, hands back; the deployed claim-extraction scheduler and worker (their own credentials) extract the window
    with the load-rules provider (nexloop_eios.load_rules); the deployed commitment keeper registers what the extraction marked. Returns counts and seconds."""
    from fastapi.testclient import TestClient
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.assembly import open_core
    from nexloop_eios.authorization import authenticate_service
    from nexloop_eios.browser_http import BrowserConfiguration
    from nexloop_eios.claim_extraction_jobs import ClaimExtractionScheduler, ClaimExtractionWorker
    from nexloop_eios.commitments import CommitmentKeeper, load_settings
    from nexloop_eios.http_api import ApiConfiguration, create_app
    from nexloop_eios.load_rules import LoadRulesExtractionProvider
    root = Path(__file__).resolve().parents[1]
    workdir = Path(workdir)
    workdir.mkdir(mode=0o700, exist_ok=True)

    def private(name, content):
        path = workdir / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(0o600)
        return path
    signer = signer or f['reader'].signer
    origin = 'https://load-template.invalid'
    identity = private('identity-dsn', identity_dsn or f['base']['identity'][0])
    rate = private('rate-key', secrets.token_hex(32))
    (workdir / 'artifacts').mkdir(mode=0o700, exist_ok=True)
    config = ApiConfiguration(private('business-dsn', make_conninfo(pg, user='nexloop_api')), private('authority-key', signer.material),
        workdir / 'artifacts', signer.key_id, BrowserConfiguration(identity, rate, tenant, browser_app, origin),
        execution_profile='deterministic-test', workbench=BrowserConfiguration(identity, rate, tenant, workbench_app, origin))
    started = time.monotonic()
    replies = {}
    with TestClient(create_app(config), base_url=origin) as client:
        login = client.post('/api/v1/workbench/auth/login', headers={'Origin': origin}, json={'username': operator['username'], 'password': operator['password']})
        login.raise_for_status()

        def act(operation, body, key):
            csrf = client.post('/api/v1/workbench/auth/csrf', headers={'Origin': origin}).json()['csrf_token']
            response = client.post('/api/v1/workbench/actions/' + operation, json=body, headers={'Origin': origin, 'X-CSRF-Token': csrf, 'Idempotency-Key': key})
            return response.status_code, response.json()
        for record in records:
            if not record['commitment']:
                continue
            first = record['conversations'][0]
            conversation, = admin.execute('select conversation_id from runtime.nexloop_conversations where idempotency_key=%s',
                                          (f'load-{first["key"]}-conversation',)).fetchone()
            ask = next(m + 1 for m, message in enumerate(first['messages']) if message['kind'] == 'commitment_ask')
            message, = admin.execute('select message_id from runtime.nexloop_conversation_messages where idempotency_key=%s',
                                     (f'load-{first["key"]}-{ask:04d}',)).fetchone()
            key = f'load-commitment-{record["index"]:05d}'
            status, taken = act('take_over_conversation', {'scope_kind': 'conversation', 'scope_ref': conversation, 'duration_seconds': 600,
                                                           'reason': '合成数据：人工跟进'}, key + '-takeover')
            if status != 200:
                replies[status] = replies.get(status, 0) + 1
                continue
            status, _ = act('send_staff_reply', {'conversation_id': conversation, 'reply_to': message, 'text': f'好的，我会在{1 + record["index"] % 3}天内给您回电话。'}, key + '-reply')
            replies[status] = replies.get(status, 0) + 1
            act('hand_back_conversation', {'takeover_id': taken['takeover_id'], 'reason': '合成数据：交还'}, key + '-handback')
    takeover_seconds = time.monotonic() - started
    tokens = json.loads((Path(private_dir) / 'service-tokens.json').read_text())
    principals = json.loads((Path(private_dir) / 'service-principals.json').read_text())

    def session(pool, role):
        return authenticate_service(pool, tokens[principals[role]['credential_reference']], world='real')
    # The Commitment type and its Actions (business manifests), and the owner's decision on the Commitment property groups with
    # the keeper's lifecycle rule (trusted configuration, as tests/commitment_fixture.py configures them).
    from commitment_fixture import lifecycle_rule, publish_commitment_type
    publish_commitment_type(admin, tenant)
    with open_core(make_conninfo(pg, user='nexloop_domain_worker')) as domain:
        lifecycle_rule(admin, session(domain, 'commitment_keeper').authentication.subject_principal_id, tenant)
    started = time.monotonic()
    extracted = registered = 0
    with open_core(make_conninfo(pg, user='nexloop_api')) as api, open_core(make_conninfo(pg, user='nexloop_domain_worker')) as domain:
        while ClaimExtractionScheduler(api, session(api, 'claim_extraction_scheduler'), signer, quiet_seconds=0).run_once(limit=100):
            pass
        worker = ClaimExtractionWorker(domain, session(domain, 'claim_extraction_worker'), signer, LoadRulesExtractionProvider(), timezone='Asia/Shanghai')
        outcomes = {}
        while True:
            result = worker.run_once()
            if result == 'idle':
                break
            label = result if isinstance(result, str) else json.dumps(result, sort_keys=True)[:80]
            outcomes[label] = outcomes.get(label, 0) + 1
            extracted += 1
        extraction_seconds = time.monotonic() - started
        started = time.monotonic()
        settings = load_settings(root / 'deploy/configuration/commitments.v1.json')
        while True:
            summary = CommitmentKeeper(domain, session(domain, 'commitment_keeper'), signer, settings=settings).run_once()
            registered += summary.get('registered', 0)
            if not any(v for v in summary.values() if type(v) is int):
                break
    return {'staff_replies': replies, 'extraction_tasks': extracted, 'extraction_outcomes': outcomes, 'registered': registered,
            'seconds': {'takeover_reply': round(takeover_seconds, 1), 'extraction': round(extraction_seconds, 1), 'keeper': round(time.monotonic() - started, 1)}}
