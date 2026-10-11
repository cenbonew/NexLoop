"""Relay multi-consumer (0160) on real PostgreSQL: one relay, several enrolled consumers.

Setup reuses the single-consumer relay fixture (test_message_relay.relay_plan) and adds consumers A, B, C, each with its own
browser Human, ownership, Conversation and inbound messages, EffectControl (owner = a separate business owner service that
also holds nexloop.relay.enroll / withdraw), ServiceOffering and binding. The Source's static per-object READ for all three
catalogs is one trusted declaration (dispatcher question on Source READ derivation pending). The fixture's own consumer stays
unenrolled. Synthetic data; real governed Actions; the relay runs in process (production MessageRelay, run profile mode).
"""
from datetime import UTC, datetime, timedelta
import hashlib, json, secrets, uuid

import psycopg
import pytest
from argon2 import PasswordHasher
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.applications import ResourceRestriction
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.identity.models import EncodedPasswordHash, LocalAccount, MembershipKind, Subject, SubjectKind, TenantMembership
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.conversation_messages import CREATE, MESSAGE, OWNERSHIP, READ, ConversationMessagePort, ConversationOwnershipRegistrar
from nexloop_eios.effect_contexts import CONFIGURE
from nexloop_eios.message_relay import MessageRelay, MessageRelayPort, MessageRelayUnavailable
from nexloop_eios.property_access import RelaySourceReadRule
from nexloop_eios import relay_enrollment
from nexloop_eios.run_credential_vault import RunCredentialVault
from nexloop_eios.service_offerings import BINDING_FIELDS, OFFERING_FIELDS, json_export_example
from authority_fixture import authority_records
from context_source_declarations import source_declarations
from message_offering_fixture import definitions, message_read_rule_row
from message_runtime_fixture import message_plan  # noqa: F401
from runtime_effect_fixture import seed_multi_uuid
from test_browser_evidence import authenticate
from test_browser_session_creation import create
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401

RUN_PROFILE_KEYS = ('role_ref', 'context_manifest_ref', 'runtime_profile', 'budget', 'runtime_owner_epoch', 'queue')


def action_targets(names):
    return [('eios:action:' + name + ':1', ResourceType.ACTION, Operation.EXECUTE) for name in names]


def add_human(f, admin, label):
    """A browser Human with the conversation grants of the message fixture (canonical identity rows, as message_plan)."""
    tenant, uow = f['tenant'], f['uow']
    now = datetime.now(UTC)
    password = secrets.token_urlsafe(32)
    subject = Subject(subject_id=f'webchat-human-{label}-' + str(uuid.uuid4()), kind=SubjectKind.HUMAN, status='active', created_at=now, updated_at=now, revision=1)
    membership = TenantMembership(tenant_id=tenant, principal_id=f'webchat-principal-{label}-' + str(uuid.uuid4()), subject_id=subject.subject_id,
                                  kind=MembershipKind.HOME, status='active', valid_from=now, valid_until=None, revision=1)
    account = LocalAccount(local_account_id=f'webchat-account-{label}-' + str(uuid.uuid4()), tenant_id=tenant, subject_id=subject.subject_id,
                           username=f'synthetic-user-{label}', verified_email=f'synthetic-{label}@example.invalid',
                           password_hash=EncodedPasswordHash(PasswordHasher().hash(password)), status='active', failed_attempts=0, lockout_level=0,
                           locked_until=None, must_change_password=False, session_epoch=1, created_at=now, updated_at=now, revision=1)
    admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)', (subject.subject_id, Jsonb(subject.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)', (tenant, membership.principal_id, subject.subject_id, Jsonb(membership.model_dump(mode='json'))))
    payload = account.model_dump(mode='json')
    payload.pop('password_hash')
    payload.pop('password_history')
    admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',
                  (tenant, account.local_account_id, subject.subject_id, account.username, account.password_hash.get_secret_value(), [], Jsonb(payload)))
    auth, _, _ = authority_records(tenant, 'eios:action:' + CREATE + ':1', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION, identity_suffix='-human')

    def convert(value):
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        if value == auth.subject_id:
            return subject.subject_id
        if value == auth.subject_principal_id:
            return membership.principal_id
        if value == 'service':
            return 'human'
        return value
    records = {}
    names = [CREATE, MESSAGE, READ]
    for name in names:
        _, _, rows = authority_records(tenant, 'eios:action:' + name + ':1', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION, identity_suffix='-human')
        for kind, key, fact in rows:
            if kind in ('subject', 'membership', 'authentication', 'application', 'revision'):
                continue  # application and catalog revision are tenant-wide facts the fixture already configured
            body = convert(fact.model_dump(mode='json'))
            body.pop('snapshot_digest', None)
            records[(kind, tuple(convert(key)))] = (kind, convert(key), type(fact).model_validate_json(json.dumps(body)))
    for kind, key, fact in records.values():
        if kind == 'scope':
            # The business application requests action.execute and function.execute (receipt_plan); scopes follow it.
            body = fact.model_dump(mode='json')
            body.pop('snapshot_digest', None)
            body['catalog_scopes'] = body['authorized_scopes'] = ['action.execute', 'function.execute']
            body['catalog_digest'] = F.canonical_authority_digest({'scopes': body['catalog_scopes']})
            fact = F.ScopeAuthorityFacts.model_validate_json(json.dumps(body))
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) '
                      'on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload', (tenant, kind, key, Jsonb(fact.model_dump(mode='json'))))
    identity = (f['identity'][0], subject, membership, account, password)
    return identity, create(uow, identity, authenticate(uow, identity).evidence)


@pytest.fixture
def multi(relay_plan, admin, tmp_path):
    f = relay_plan['f']
    api, tenant = f['api'], f['tenant']
    catalog_actions = [a['definition']['stable_name'] for a in definitions(tenant)]
    owner_token = seed_multi_uuid(admin, tenant, action_targets(['Consumer.create', 'EffectControl.create', CONFIGURE, OWNERSHIP, 'nexloop.relay.enroll', 'nexloop.relay.withdraw']),
                                  suffix='-business-owner')
    keeper_token = seed_multi_uuid(admin, tenant, action_targets(catalog_actions), suffix='-multi-catalog-maintainer')
    route_principal = api.authenticate(f['owner_token'], world='real')._session.authentication.subject_principal_id
    # A Source principal of its own for A, B and C (its static READ is declared once below, after the catalogs exist).
    source_principal = source_declarations(tenant, identity_suffix='-multi-source')[0].subject_principal_id
    executor_principal = api.authenticate(f['executor_token'], world='real')._session.authentication.subject_principal_id
    expiry = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    consumers = {}
    for label in ('A', 'B', 'C'):
        identity, issued = add_human(f, admin, label)
        owner = api.authenticate(owner_token, world='real')
        keeper = api.authenticate(keeper_token, world='real')
        consumer = owner.create_object(action_name='Consumer.create', action_version=1, intent_id=f'multi-consumer-{label}', type_name='Consumer', properties={})['object_id']
        control = owner.create_object(action_name='EffectControl.create', action_version=1, intent_id=f'multi-control-{label}', type_name='EffectControl',
            properties={'consumer_id': consumer, 'owner_principal': owner._session.authentication.subject_principal_id, 'executor_principal': executor_principal,
                        'budget_units': 3, 'allow_effect': True, 'valid_until': expiry})['object_id']
        owner.configure_effect_control(control_id=control, control_revision=1, executor_token=f['executor_token'])
        offered = keeper.create_object(action_name='ServiceOffering.create', action_version=1, intent_id=f'multi-offering-{label}', type_name='ServiceOffering',
                                       properties=json_export_example(valid_until=expiry))['object_id']
        binding = keeper.create_object(action_name='ConsumerServiceOffering.create', action_version=1, intent_id=f'multi-binding-{label}', type_name='ConsumerServiceOffering',
            properties={'consumer_id': consumer, 'offering_id': offered, 'offering_revision': 1, 'source_principal': source_principal, 'active': True})['object_id']
        owner = api.authenticate(owner_token, world='real')
        with api._lock:
            ConversationOwnershipRegistrar(api._pool, owner._session, api._signer).register_consumer_owner(
                consumer_id=consumer, principal_id=identity[2].principal_id, idempotency_key=f'multi-ownership-key-{label}')
        consumers[label] = dict(consumer=consumer, control=control, offering=offered, binding=binding, issued=issued, identity=identity)
    # The Source holds no per-object grant on any consumer, offering or binding: its READ on them is derived in SQL from the
    # relay enrollments (0161) under one trusted relay_source_read_rule [source] -> this relay (as service-grants v16 compiles it).
    declared, rows = source_declarations(tenant, identity_suffix='-multi-source')
    assert declared.subject_principal_id == source_principal
    rows.append(message_read_rule_row(tenant, source_principal, expiry))
    rule = RelaySourceReadRule(tenant_id=tenant, principal_id=source_principal, relay_principal=route_principal, active=True,
                               valid_until=datetime.now(UTC) + timedelta(hours=1))
    rows.append({'kind': 'relay_source_read_rule', 'key': [source_principal], 'payload': rule.model_dump(mode='json')})
    source_token = secrets.token_urlsafe(48)
    end = next(r['payload']['expires_at'] for r in rows if r['kind'] == 'authentication')
    admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
                  (hashlib.sha256(source_token.encode()).hexdigest(), tenant, declared.credential_id, Jsonb(declared.model_dump(mode='json')), ['real'], 'nexloop-core', 'active', end))
    for row in rows:
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) '
                      'on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload', (tenant, row['kind'], row['key'], Jsonb(row['payload'])))
    # Inbound messages, oldest first: A1 A2 A3 (two conversations of A), then B1, then C1.
    for label, bodies in (('A', ['A 第一条', 'A 第二条', 'A 第三条']), ('B', ['B 第一条']), ('C', ['C 第一条'])):
        c = consumers[label]
        human = authenticate_browser_business(api._pool, c['issued'].session, world='real')
        port = ConversationMessagePort(api._pool, human, api._signer)
        c['messages'] = []
        for index, body in enumerate(bodies):
            conversation = port.create_conversation(idempotency_key=f'multi-conversation-{label}-{index % 2}')
            c['messages'].append(port.accept_message(conversation_id=conversation['id'], idempotency_key=f'multi-message-{label}-{index}', body=body)['message']['id'])
    run_profile = {k: relay_plan['recipe'][k] for k in RUN_PROFILE_KEYS}
    vault_dir = tmp_path / 'multi-vault'
    vault_dir.mkdir(mode=0o700)

    def enroll(label, *, relay=route_principal, intent=None):
        c = consumers[label]
        return relay_enrollment.enroll(api.authenticate(owner_token, world='real'), consumer_id=c['consumer'], control_id=c['control'], offering_id=c['offering'],
                                       binding_id=c['binding'], relay_principal=relay, intent_id=intent or f'enroll-{label}-0001')

    def relay(vault, **extra):
        return MessageRelay(route=api.authenticate(f['owner_token'], world='real'), source=api.authenticate(source_token, world='real'),
                            planner=api.authenticate(relay_plan['planner_token'], world='real'), executor_token=f['executor_token'], vault=vault,
                            run_profile=run_profile, **extra)
    by_consumer = {c['consumer']: label for label, c in consumers.items()}
    return dict(f=f, api=api, admin=admin, tenant=tenant, consumers=consumers, owner_token=owner_token, route_principal=route_principal, source_token=source_token,
                keeper_token=keeper_token, executor_principal_id=executor_principal, source_principal=source_principal, expiry=expiry, relay_plan=relay_plan,
                executor_principal=executor_principal, enroll=enroll, relay=relay, vault_dir=vault_dir, by_consumer=by_consumer, run_profile=run_profile)


def claims(m):
    rows = m['admin'].execute('select consumer_id,message_id from runtime.nexloop_relay_claims order by claimed_at').fetchall()
    return [(m['by_consumer'].get(c, 'other'), message) for c, message in rows]


def test_one_relay_serves_each_enrolled_consumer_with_its_own_profile_in_rotation(multi):
    m = multi
    for label in 'ABC':
        assert m['enroll'](label)['state'] == 'active'
    with RunCredentialVault(m['vault_dir']) as vault:
        statuses = [m['relay'](vault).run_once() for _ in range(6)]
    assert statuses == ['queued'] * 5 + ['idle']
    order = claims(m)
    # D3 rotation: never-served consumers first, then the least recently served; D2: one consumer's messages in accepted order.
    assert [label for label, _ in order] == ['A', 'B', 'C', 'A', 'A']
    assert [message for label, message in order if label == 'A'] == m['consumers']['A']['messages']
    admin = m['admin']
    # Identity per Run: assignment, Goal, PlanStep and the Run command all carry the message's own consumer, control, offering.
    for label, message in order:
        c = m['consumers'][label]
        props = admin.execute("select properties from ontology.objects where type_name='MessageAssignment' and properties->>'message_id'=%s", (message,)).fetchone()[0]
        assert props['consumer_id'] == c['consumer'] and props['control_id'] == c['control']
        command = admin.execute('select command from runtime.nexloop_message_routes where message_id=%s', (message,)).fetchone()[0]
        assert command['consumer_ref'] == 'consumer:' + c['consumer']
        conversation_consumer = admin.execute('select cv.consumer_id from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv '
            'on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id where b.message_id=%s', (message,)).fetchone()[0]
        assert conversation_consumer == c['consumer']
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone() == (5,)
    # The fixture's own consumer is not enrolled: its message is never claimed and is counted by the relay metric.
    relay = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'relay'", (m['tenant'],)).fetchone()[0]
    assert relay['pending_messages'] == 1 and relay['unenrolled_consumers_pending'] == 1 and relay['consumers_pending'] == 1


def test_one_consumer_at_a_time_and_fair_share_under_backlog(multi):
    m = multi
    for label in 'ABC':
        m['enroll'](label)
    port = MessageRelayPort(m['api'].authenticate(m['f']['owner_token'], world='real'))
    first = [port.call('claim_next', lease_seconds=60) for _ in range(4)]
    # A has three pending messages but one is in flight: B and C come next, then nothing (A waits for its first).
    assert [m['by_consumer'][x['consumer_id']] for x in first[:3]] == ['A', 'B', 'C'] and first[3] is None
    assert first[0]['profile']['control_id'] == m['consumers']['A']['control'] and first[1]['profile']['binding_id'] == m['consumers']['B']['binding']
    leased = m['admin'].execute('select consumer_id,count(*) from runtime.nexloop_message_relay_leases where lease_until>clock_timestamp() group by consumer_id').fetchall()
    assert sorted(n for _, n in leased) == [1, 1, 1]


def test_withdrawal_erasure_takeover_and_other_relays_stop_only_their_consumer(multi):
    m = multi
    admin, tenant = m['admin'], m['tenant']
    for label in 'ABC':
        m['enroll'](label)
    owner = m['api'].authenticate(m['owner_token'], world='real')
    assert relay_enrollment.withdraw(owner, consumer_id=m['consumers']['B']['consumer'], intent_id='withdraw-B-0001')['state'] == 'withdrawn'
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute("insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark) "
                      "values(%s,'real','erase-C',%s,'owner','synthetic-owner','synthetic erasure','blocking',1)", (tenant, m['consumers']['C']['consumer']))
    port = MessageRelayPort(m['api'].authenticate(m['f']['owner_token'], world='real'))
    item = port.call('claim_next', lease_seconds=60)
    assert m['by_consumer'][item['consumer_id']] == 'A' and port.call('claim_next', lease_seconds=60) is None
    assert port.call('profile', consumer_id=m['consumers']['B']['consumer']) is None and port.call('profile', consumer_id=m['consumers']['C']['consumer']) is None
    # A takeover of A's head conversation stops A too, without touching the others.
    admin.execute("update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp()")
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        conversation = admin.execute('select conversation_id from runtime.nexloop_message_outbox where message_id=%s', (m['consumers']['A']['messages'][0],)).fetchone()[0]
        admin.execute("insert into control.nexloop_takeovers(tenant_id,world,takeover_id,scope_kind,scope_ref,consumer_id,taken_by,start_intent,reason,started_at,expires_at,start_revision) "
                      "values(%s,'real',%s,'conversation',%s,%s,'synthetic-staff','takeover-A','synthetic',clock_timestamp(),clock_timestamp()+interval '1 hour',1)",
                      (tenant, str(uuid.uuid4()), conversation, m['consumers']['A']['consumer']))
    assert port.call('claim_next', lease_seconds=60) is None
    # A consumer enrolled for another relay principal is never claimed by this one.
    m['enroll']('B', relay=m['executor_principal'], intent='enroll-B-other-relay')
    assert port.call('claim_next', lease_seconds=60) is None


def test_withdrawal_between_claim_and_issue_refuses_the_issuance(multi):
    m = multi
    m['enroll']('A')
    owner = m['api'].authenticate(m['owner_token'], world='real')

    class Withdrawing(MessageRelay):
        def _create(self, type_name, message_id, properties, recipe=None):
            if type_name == 'Goal':
                relay_enrollment.withdraw(owner, consumer_id=recipe['consumer_id'], intent_id='withdraw-A-race-0001')
            return super()._create(type_name, message_id, properties, recipe)
    with RunCredentialVault(m['vault_dir']) as vault:
        relay = m['relay'](vault)
        relay.__class__ = Withdrawing
        with pytest.raises(MessageRelayUnavailable):
            relay.run_once()
    assert m['admin'].execute('select count(*) from authz.nexloop_message_run_issuances').fetchone() == (0,)


def test_enrollment_port_rules(multi):
    m = multi
    api, admin, c = m['api'], m['admin'], m['consumers']
    first = m['enroll']('A')
    assert first == {'consumer_id': c['A']['consumer'], 'state': 'active', 'enrollment_revision': 1, 'replay': False}
    assert m['enroll']('A')['replay'] is True and m['enroll']('A', intent='enroll-A-again-0002') == {**first, 'replay': True}
    owner = api.authenticate(m['owner_token'], world='real')
    read = relay_enrollment.read(owner, consumer_id=c['A']['consumer'])
    assert read['control_id'] == c['A']['control'] and read['binding_id'] == c['A']['binding'] and read['role_id'] is None and read['state'] == 'active'
    with pytest.raises(relay_enrollment.RelayEnrollmentUnavailable, match='invalid'):  # B's binding is not A's
        relay_enrollment.enroll(owner, consumer_id=c['A']['consumer'], control_id=c['A']['control'], offering_id=c['B']['offering'], binding_id=c['B']['binding'],
                                relay_principal=m['route_principal'], intent_id='enroll-A-wrong-binding')
    with pytest.raises(relay_enrollment.RelayEnrollmentUnavailable):  # the owner cannot name itself as the relay
        relay_enrollment.enroll(owner, consumer_id=c['B']['consumer'], control_id=c['B']['control'], offering_id=c['B']['offering'], binding_id=c['B']['binding'],
                                relay_principal=owner._session.authentication.subject_principal_id, intent_id='enroll-B-self')
    for token in (m['f']['owner_token'], m['f']['source_token']):  # the relay itself and another service hold no enrollment authority
        with pytest.raises(relay_enrollment.RelayEnrollmentUnavailable):
            relay_enrollment.enroll(api.authenticate(token, world='real'), consumer_id=c['C']['consumer'], control_id=c['C']['control'], offering_id=c['C']['offering'],
                                    binding_id=c['C']['binding'], relay_principal=m['route_principal'], intent_id='enroll-C-foreign')
    with pytest.raises(psycopg.Error):
        admin.execute('update control.nexloop_relay_enrollments set state=state')
    assert admin.execute("select count(*) from control.nexloop_relay_enrollments").fetchone() == (1,)


def test_actual_pi_two_consumers_one_relay_each_run_acts_for_its_own_consumer(multi, tmp_path):
    """Real Host and Pi (deterministic provider), deployment guard shape: A's and B's message Runs, one relay, each Run's only
    effect intent is its own consumer's, and each terminal result carries the Run's tool timing (NX-031 D6)."""
    from test_message_relay import (HostControlConfiguration, RuntimeDispatcher, actual_host, effect_configuration, files, fresh_worker, guard_server,
                                    tool_evidence)
    import secrets as _secrets
    m = multi
    f, admin = m['f'], m['admin']
    for label in 'AB':
        m['enroll'](label)
    with RunCredentialVault(m['vault_dir']) as vault:
        assert [m['relay'](vault).run_once() for _ in range(2)] == ['queued', 'queued']
    routes = admin.execute('select message_id,task_id,command from runtime.nexloop_message_routes order by task_id').fetchall()
    assert sorted(m['by_consumer'][c['consumer_ref'].removeprefix('consumer:')] for _, _, c in routes) == ['A', 'B']
    runtime, key = files(tmp_path)
    guard_key = tmp_path / 'guard-key'
    guard_key.write_text(_secrets.token_hex(32))
    guard_key.chmod(0o600)
    with fresh_worker(f, tmp_path) as worker:
        with guard_server(worker, tmp_path, guard_key) as port:
            with actual_host(runtime, key, effect_configuration(tmp_path, port, guard_key)) as (_, client, headers):
                host_control = HostControlConfiguration(str(client.base_url).rstrip('/'), key, tmp_path / 'host-cert.pem')
                done = [RuntimeDispatcher(worker, host_control, queue='operations', total_timeout=20).run_once() for _ in routes]
                assert all(d['claimed'] and d['status'] == 'succeeded' for d in done)
    for message_id, task_id, command in routes:
        consumer = command['consumer_ref'].removeprefix('consumer:')
        result = admin.execute("select result from runtime.jobs where result->>'run_id'=%s", (command['run_id'],)).fetchone()[0]
        assert result['runtime_outcome'] == 'succeeded' and result['run_id'] == command['run_id']
        # The dispatcher writes the D6 sum for every Run it ends, relay-issued ones included. This harness's guard object does
        # not record (the production _FreshGuard does; counted end to end by test_run_tool_timing_pg), so only the shape here.
        assert set(result['tool_timing']) == {'calls', 'total_ms', 'max_ms'}
        calls, _ = tool_evidence(runtime / command['run_id'] / 'runtime.sqlite')
        assert any(call['name'] == 'nexloop.service.request' for call in calls)
        intents = admin.execute('select distinct i.consumer_id from runtime.nexloop_effect_intents i join runtime.nexloop_message_routes r on r.context_id=i.context_id '
                                'where r.message_id=%s', (message_id,)).fetchall()
        assert intents == [(consumer,)]
    assert admin.execute('select count(distinct consumer_id) from runtime.nexloop_effect_intents').fetchone() == (2,)


def test_cli_run_profile_mode_and_exclusive_modes(multi, tmp_path):
    import subprocess, sys
    m = multi
    m['enroll']('A')
    argv = list(m['relay_plan']['argv'])
    private = m['relay_plan']['private']
    profile = private / 'run-profile-file'
    profile.write_text(json.dumps(m['run_profile']))
    profile.chmod(0o600)
    source = private / 'multi-source-credential'
    source.write_text(m['source_token'])
    source.chmod(0o600)
    argv[argv.index('--source-credential-file') + 1] = str(source)
    both = subprocess.run([sys.executable, '-m', 'nexloop_eios.message_relay_cli', *argv, '--run-profile-file', str(profile)], capture_output=True, text=True, timeout=30)
    assert both.returncode == 2 and 'configuration unavailable' in both.stderr
    i = argv.index('--recipe-file')
    argv[i:i + 2] = ['--run-profile-file', str(profile)]
    queued = subprocess.run([sys.executable, '-m', 'nexloop_eios.message_relay_cli', *argv], capture_output=True, text=True, timeout=60)
    assert queued.returncode == 0 and '"status":"queued"' in queued.stdout, queued.stdout + queued.stderr
    assert [label for label, _ in claims(m)] == ['A']


def test_fallback_launcher_reads_the_enrollment_in_run_profile_mode(multi):
    from nexloop_eios.reply_fallback import FallbackReplyLauncher, FallbackUnavailable
    from test_reply_fallback_pg import TEST_POLICY
    m = multi
    m['enroll']('A')
    api, f = m['api'], m['f']
    def session(token):
        return lambda: api.authenticate(token, world='real')
    launcher = FallbackReplyLauncher(route=session(f['owner_token']), source=session(m['source_token']), planner=session(m['relay_plan']['planner_token']),
                                     executor_token=f['executor_token'], policy=TEST_POLICY, run_profile=m['run_profile'])
    with pytest.raises(FallbackUnavailable, match='not enrolled'):
        launcher.start(message_id=m['consumers']['B']['messages'][0], state={'consumer_id': m['consumers']['B']['consumer']})
    # Enrolled: the profile is found and planning proceeds; issuance then refuses because this reply is not due yet.
    with pytest.raises(Exception) as refused:
        launcher.start(message_id=m['consumers']['A']['messages'][0], state={'consumer_id': m['consumers']['A']['consumer']})
    assert 'not enrolled' not in str(refused.value)
    assert m['admin'].execute("select count(*) from ontology.objects where type_name='Goal' and properties->>'consumer_id'=%s",
                              (m['consumers']['A']['consumer'],)).fetchone()[0] >= 1
    with pytest.raises(Exception):
        FallbackReplyLauncher(route=None, source=None, planner=None, executor_token=None, policy=TEST_POLICY, recipe=m['relay_plan']['recipe'], run_profile=m['run_profile'])


def test_enrollment_throughput_in_process(multi):
    """L4 constraint: in-process, idempotent, minutes for 1000 consumers. Measured on N fresh consumers (enroll calls only)."""
    import time
    m = multi
    api, f = m['api'], m['f']
    owner = api.authenticate(m['owner_token'], world='real')
    keeper = api.authenticate(m['keeper_token'], world='real')
    n = 30
    prepared = []
    for i in range(n):
        consumer = owner.create_object(action_name='Consumer.create', action_version=1, intent_id=f'throughput-consumer-{i:04d}', type_name='Consumer', properties={})['object_id']
        control = owner.create_object(action_name='EffectControl.create', action_version=1, intent_id=f'throughput-control-{i:04d}', type_name='EffectControl',
            properties={'consumer_id': consumer, 'owner_principal': owner._session.authentication.subject_principal_id, 'executor_principal': m['executor_principal_id'],
                        'budget_units': 1, 'allow_effect': True, 'valid_until': m['expiry']})['object_id']
        owner.configure_effect_control(control_id=control, control_revision=1, executor_token=f['executor_token'])
        offered = keeper.create_object(action_name='ServiceOffering.create', action_version=1, intent_id=f'throughput-offering-{i:04d}', type_name='ServiceOffering',
                                       properties=json_export_example(valid_until=m['expiry']))['object_id']
        binding = keeper.create_object(action_name='ConsumerServiceOffering.create', action_version=1, intent_id=f'throughput-binding-{i:04d}', type_name='ConsumerServiceOffering',
            properties={'consumer_id': consumer, 'offering_id': offered, 'offering_revision': 1, 'source_principal': m['source_principal'], 'active': True})['object_id']
        prepared.append((consumer, control, offered, binding))
    owner = api.authenticate(m['owner_token'], world='real')
    started = time.monotonic()
    for i, (consumer, control, offered, binding) in enumerate(prepared):
        relay_enrollment.enroll(owner, consumer_id=consumer, control_id=control, offering_id=offered, binding_id=binding,
                                relay_principal=m['route_principal'], intent_id=f'throughput-enroll-{i:04d}')
    elapsed = time.monotonic() - started
    replay_started = time.monotonic()
    for i, (consumer, control, offered, binding) in enumerate(prepared):
        assert relay_enrollment.enroll(owner, consumer_id=consumer, control_id=control, offering_id=offered, binding_id=binding,
                                       relay_principal=m['route_principal'], intent_id=f'throughput-enroll-{i:04d}')['replay'] is True
    replay = time.monotonic() - replay_started
    print(f'ENROLL n={n} total_s={elapsed:.3f} per_ms={1000 * elapsed / n:.1f} est_1000_s={1000 * elapsed / n:.0f} replay_per_ms={1000 * replay / n:.1f}')
    assert elapsed / n < 0.5  # 1000 consumers well within minutes


def source_reads(m, token, label):
    """The relay's Source reads a consumer's Consumer, offering and binding as Context preparation does (all three or raises)."""
    c = m['consumers'][label]
    source = m['api'].authenticate(token, world='real')
    source.read_object(type_name='Consumer', object_id=c['consumer'])
    source.read_object(type_name='ServiceOffering', object_id=c['offering'], fields=OFFERING_FIELDS)
    return source.read_object(type_name='ConsumerServiceOffering', object_id=c['binding'], fields=BINDING_FIELDS)


def test_source_read_is_derived_only_for_enrolled_consumers_and_ends_at_the_next_read(multi):
    m = multi
    admin, tenant, token, c = m['admin'], m['tenant'], m['source_token'], m['consumers']
    for label in 'AC':
        m['enroll'](label)
    assert source_reads(m, token, 'A')['properties']['source_principal'] == m['source_principal']
    with pytest.raises(Exception):  # B is not enrolled: nothing derived, no static grant exists
        source_reads(m, token, 'B')
    # Only the fixed fields: a property outside the relay Context list is not derived.
    with pytest.raises(Exception):
        m['api'].authenticate(token, world='real').read_object(type_name='ConsumerServiceOffering', object_id=c['A']['binding'], fields=('not_a_field',))
    # Withdrawal ends it at the next read; enrolling again restores it.
    owner = m['api'].authenticate(m['owner_token'], world='real')
    relay_enrollment.withdraw(owner, consumer_id=c['A']['consumer'], intent_id='withdraw-A-read-0001')
    with pytest.raises(Exception):
        source_reads(m, token, 'A')
    m['enroll']('A', intent='enroll-A-again-read-0002')
    source_reads(m, token, 'A')
    # NX-029 erasing ends it at the next read.
    source_reads(m, token, 'C')
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute("insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark) "
                      "values(%s,'real','erase-C-read',%s,'owner','synthetic-owner','synthetic erasure','blocking',1)", (tenant, c['C']['consumer']))
    with pytest.raises(Exception):
        source_reads(m, token, 'C')
    # Another service principal (the fixture's own Source, no rule; the binding names another Source) derives nothing.
    with pytest.raises(Exception):
        source_reads(m, m['f']['source_token'], 'A')
    # Deactivating the rule ends it at the next read.
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{active}','false') where fact_kind='relay_source_read_rule' and tenant_id=%s", (tenant,))
    with pytest.raises(Exception):
        source_reads(m, token, 'A')


def test_source_credential_revocation_run_credentials_and_browser_sessions_get_nothing(multi):
    from eios.authz.operations import Operation as Op
    from nexloop_eios.property_access import property_access_basis
    m = multi
    admin, api, c = m['admin'], m['api'], m['consumers']
    m['enroll']('A')
    target = 'eios:object:ConsumerServiceOffering/' + c['A']['binding']
    source = api.authenticate(m['source_token'], world='real')
    assert property_access_basis(api._pool, source._session, target, 'read')['derivation'] == 'relay-source-v1'
    assert property_access_basis(api._pool, source._session, target, 'edit')['mode'] == 'configured'  # READ only, never EDIT
    # A Run credential issued by this Source derives nothing (run_context).
    run = source.issue_run_credential(action_resources=['eios:action:nexloop.service.request:1'])
    run_services = api.authenticate_run(run.token, world='real', run_id=run.run_id)
    assert property_access_basis(api._pool, run_services._session, target, 'read')['mode'] == 'configured'
    with pytest.raises(Exception):
        run_services.read_object(type_name='ConsumerServiceOffering', object_id=c['A']['binding'], fields=BINDING_FIELDS)
    # A browser Human session (the consumer's own) derives nothing.
    human = authenticate_browser_business(api._pool, c['A']['issued'].session, world='real')
    assert property_access_basis(api._pool, human, target, 'read')['mode'] == 'configured'
    # Revoking the Source credential ends it at the next read.
    source_reads(m, m['source_token'], 'A')
    admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s", (hashlib.sha256(m['source_token'].encode()).hexdigest(),))
    with pytest.raises(Exception):
        source_reads(m, m['source_token'], 'A')


def test_an_agent_with_a_rule_and_a_binding_naming_it_still_derives_nothing(multi):
    from agent_authority_fixture import seed_agent
    from nexloop_eios.property_access import property_access_basis
    m = multi
    admin, api, tenant = m['admin'], m['api'], m['tenant']
    token, _ = seed_agent(admin, tenant)
    agent = api.authenticate(token, world='real')
    agent_principal = agent._session.authentication.subject_principal_id
    owner = api.authenticate(m['owner_token'], world='real')
    keeper = api.authenticate(m['keeper_token'], world='real')
    consumer = owner.create_object(action_name='Consumer.create', action_version=1, intent_id='agent-case-consumer', type_name='Consumer', properties={})['object_id']
    control = owner.create_object(action_name='EffectControl.create', action_version=1, intent_id='agent-case-control', type_name='EffectControl',
        properties={'consumer_id': consumer, 'owner_principal': owner._session.authentication.subject_principal_id, 'executor_principal': m['executor_principal_id'],
                    'budget_units': 1, 'allow_effect': True, 'valid_until': m['expiry']})['object_id']
    owner.configure_effect_control(control_id=control, control_revision=1, executor_token=m['f']['executor_token'])
    offered = keeper.create_object(action_name='ServiceOffering.create', action_version=1, intent_id='agent-case-offering', type_name='ServiceOffering',
                                   properties=json_export_example(valid_until=m['expiry']))['object_id']
    binding = keeper.create_object(action_name='ConsumerServiceOffering.create', action_version=1, intent_id='agent-case-binding', type_name='ConsumerServiceOffering',
        properties={'consumer_id': consumer, 'offering_id': offered, 'offering_revision': 1, 'source_principal': agent_principal, 'active': True})['object_id']
    relay_enrollment.enroll(api.authenticate(m['owner_token'], world='real'), consumer_id=consumer, control_id=control, offering_id=offered, binding_id=binding,
                            relay_principal=m['route_principal'], intent_id='agent-case-enroll')
    rule = RelaySourceReadRule(tenant_id=tenant, principal_id=agent_principal, relay_principal=m['route_principal'], active=True,
                               valid_until=datetime.now(UTC) + timedelta(hours=1))
    admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
                  (tenant, 'relay_source_read_rule', [agent_principal], Jsonb(rule.model_dump(mode='json'))))
    target = 'eios:object:ConsumerServiceOffering/' + binding
    # Everything but the subject kind qualifies (the bare derivation, evaluated as owner, would answer derived) ...
    assert admin.execute("select authz.nexloop_relay_source_derivation(%s,%s,'real',%s,'read')->>'derivation'", (tenant, agent_principal, target)).fetchone() == ('relay-source-v1',)
    # ... yet the agent's own credential gets the configured path, and no read.
    agent = api.authenticate(token, world='real')
    assert property_access_basis(api._pool, agent._session, target, 'read')['mode'] == 'configured'
    with pytest.raises(Exception):
        agent.read_object(type_name='ConsumerServiceOffering', object_id=binding, fields=BINDING_FIELDS)


def test_fallback_issuance_rechecks_the_enrollment_in_sql_no_window(multi):
    """Withdrawal between the launcher's profile read and the fallback issuance: the issuance is refused and rolled back (0162)."""
    from nexloop_eios.reply_fallback import FallbackReplyLauncher
    from test_reply_fallback_pg import TEST_POLICY
    m = multi
    admin, api, f, c = m['admin'], m['api'], m['f'], m['consumers']
    # A relay principal that also holds nexloop.reply.fallback (as message_relay does in service-grants).
    relay_token = seed_multi_uuid(admin, m['tenant'], action_targets(['nexloop.conversation.route', 'nexloop.reply.fallback']), suffix='-fallback-relay')
    relay_principal = api.authenticate(relay_token, world='real')._session.authentication.subject_principal_id
    for label in 'AB':
        m['enroll'](label, relay=relay_principal, intent=f'enroll-{label}-fallback-relay')
    due = admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp()-interval '1 second' where feed='reply-due' returning item_key").fetchall()
    assert {'reply:' + c['A']['messages'][0], 'reply:' + c['B']['messages'][0]} <= {k for (k,) in due}
    owner = api.authenticate(m['owner_token'], world='real')

    def session(token):
        return lambda: api.authenticate(token, world='real')

    class Withdrawing(FallbackReplyLauncher):
        def _create(self, planner, type_name, message_id, properties, recipe=None):
            if type_name == 'Goal' and recipe['consumer_id'] == c['A']['consumer']:
                relay_enrollment.withdraw(owner, consumer_id=recipe['consumer_id'], intent_id='withdraw-A-fallback-0001')
            return super()._create(planner, type_name, message_id, properties, recipe)
    launcher = Withdrawing(route=session(relay_token), source=session(m['source_token']), planner=session(m['relay_plan']['planner_token']),
                           executor_token=f['executor_token'], policy=TEST_POLICY, run_profile=m['run_profile'])
    with pytest.raises(Exception):
        launcher.start(message_id=c['A']['messages'][0], state={'consumer_id': c['A']['consumer']})
    issued = admin.execute('select message_id from authz.nexloop_message_fallback_issuances').fetchall()
    assert (c['A']['messages'][0],) not in issued
    # Control: B stays enrolled, so the same path issues its fallback Run (later steps may need more fixture configuration).
    try:
        launcher.start(message_id=c['B']['messages'][0], state={'consumer_id': c['B']['consumer']})
    except Exception:
        pass
    assert admin.execute('select message_id from authz.nexloop_message_fallback_issuances').fetchall() == [(c['B']['messages'][0],)]


def test_doctor_relay_source_requires_the_fixed_source_principal_and_its_credential(multi, tmp_path):
    """doctor --relay-source (read-only nexloop_metrics role): enrolled consumers need an active rule and an active credential for
    nexloop-service-relay-source-principal; the fixture's own Source principal is reported as another Source."""
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.doctor import RELAY_SOURCE_PRINCIPAL, relay_source
    m = multi
    admin, tenant = m['admin'], m['tenant']
    m['enroll']('A')
    dsn = tmp_path / 'metrics-dsn'
    dsn.write_text(make_conninfo(m['f']['pg'], user='nexloop_metrics'))
    dsn.chmod(0o600)
    report = relay_source(dsn)
    assert report['checks'] == {'metrics_export': True, 'relay_source_rule': False, 'relay_source_credential': False, 'relay_source_principal_fixed': False}
    assert report['details']['relay_source']['tenants'][tenant]['other_sources'] == [m['source_principal']]
    # The deployment shape: the rule and an active credential under the fixed Source principal (the fixture's rule retired).
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{active}','false') where fact_kind='relay_source_read_rule' and tenant_id=%s", (tenant,))
    rule = RelaySourceReadRule(tenant_id=tenant, principal_id=RELAY_SOURCE_PRINCIPAL, relay_principal=m['route_principal'], active=True,
                               valid_until=datetime.now(UTC) + timedelta(hours=1))
    admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
                  (tenant, 'relay_source_read_rule', [RELAY_SOURCE_PRINCIPAL], Jsonb(rule.model_dump(mode='json'))))
    assert relay_source(dsn)['checks']['relay_source_credential'] is False  # rule without a credential under that principal
    binding = admin.execute('select binding from authz.nexloop_service_credentials where token_digest=%s',
                            (hashlib.sha256(m['source_token'].encode()).hexdigest(),)).fetchone()[0]
    binding['subject_principal_id'] = RELAY_SOURCE_PRINCIPAL
    binding['credential_id'] = 'synthetic-relay-source-key'
    admin.execute("insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) "
                  "values(%s,%s,%s,%s,%s,%s,'active',clock_timestamp()+interval '1 hour')",
                  (hashlib.sha256(b'synthetic-relay-source-token').hexdigest(), tenant, 'synthetic-relay-source-key', Jsonb(binding), ['real'], 'nexloop-core'))
    assert all(relay_source(dsn)['checks'].values())
