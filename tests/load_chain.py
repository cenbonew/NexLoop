"""NX-031 slice 2: the load tenant on the actual message chain (multi-consumer relay, relay-multi-consumer-design.md §10).

Base: the assembled message configuration (local_message_assembly_fixture.assembled_message: trusted configuration, business
setup, the relay's route / planner / executor / runtime-worker identities, the message catalog Actions). On top of it, per
manifest consumer and only through governed entries, with validity long enough for a template that cells reuse for days:
  * the customer's browser identity and conversation grants (trusted identity rows and authority facts, as the multi-consumer
    relay test issues them);
  * Consumer, EffectControl (executor = the chain's executor; configured with its credential) and ownership by a business owner
    service; ServiceOffering and ConsumerServiceOffering by a catalog maintainer service;
  * enrollment with the chain's relay (relay_enrollment.enroll, in process);
and one Source for all consumers whose READ on them is derived from the enrollments (one relay_source_read_rule, 0142).
Conversations and messages are written through each customer's own ConversationMessagePort.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets
import time
import uuid

from argon2 import PasswordHasher
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.identity.models import EncodedPasswordHash, LocalAccount, MembershipKind, Subject, SubjectKind, TenantMembership
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.conversation_messages import CREATE, MESSAGE, OWNERSHIP, READ, ConversationMessagePort, ConversationOwnershipRegistrar
from nexloop_eios.effect_contexts import CONFIGURE
from nexloop_eios.property_access import RelaySourceReadRule
from nexloop_eios import relay_enrollment
from nexloop_eios.service_offerings import json_export_example
from authority_fixture import authority_records
from context_source_declarations import source_declarations
from message_offering_fixture import definitions, message_read_rule_row
from runtime_effect_fixture import seed_multi_uuid
from test_browser_evidence import authenticate
from test_browser_session_creation import create

RUN_PROFILE_KEYS = ('role_ref', 'context_manifest_ref', 'runtime_profile', 'budget', 'runtime_owner_epoch', 'queue')
VALIDITY = timedelta(days=30)


def _targets(names):
    return [('eios:action:' + name + ':1', ResourceType.ACTION, Operation.EXECUTE) for name in names]


def _write_facts(admin, tenant, rows):
    for kind, key, payload in rows:
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) '
                      'on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload', (tenant, kind, key, Jsonb(payload)))


def customer(admin, tenant, identity_dsn, username, index, hasher):
    """A browser Human with the conversation grants (canonical identity rows; facts as the multi-consumer relay test issues)."""
    now = datetime.now(UTC)
    password = secrets.token_urlsafe(24)
    subject = Subject(subject_id=f'load-subject-{index:05d}', kind=SubjectKind.HUMAN, status='active', created_at=now, updated_at=now, revision=1)
    membership = TenantMembership(tenant_id=tenant, principal_id=f'load-principal-{index:05d}', subject_id=subject.subject_id, kind=MembershipKind.HOME,
                                  status='active', valid_from=now, valid_until=None, revision=1)
    account = LocalAccount(local_account_id=f'load-account-{index:05d}', tenant_id=tenant, subject_id=subject.subject_id, username=username,
                           verified_email=f'{username}@example.invalid', password_hash=EncodedPasswordHash(hasher.hash(password)), status='active',
                           failed_attempts=0, lockout_level=0, locked_until=None, must_change_password=False, session_epoch=1, created_at=now, updated_at=now, revision=1)
    admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)', (subject.subject_id, Jsonb(subject.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)', (tenant, membership.principal_id, subject.subject_id, Jsonb(membership.model_dump(mode='json'))))
    payload = account.model_dump(mode='json')
    payload.pop('password_hash')
    payload.pop('password_history')
    admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',
                  (tenant, account.local_account_id, subject.subject_id, account.username, account.password_hash.get_secret_value(), [], Jsonb(payload)))
    rows = {}
    for name in (CREATE, MESSAGE, READ):
        auth, _, records = authority_records(tenant, 'eios:action:' + name + ':1', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION, identity_suffix='-human')

        def convert(value, auth=auth):
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            if value == auth.subject_id:
                return subject.subject_id
            if value == auth.subject_principal_id:
                return membership.principal_id
            return 'human' if value == 'service' else value
        for kind, key, fact in records:
            if kind in ('subject', 'membership', 'authentication', 'application', 'revision'):
                continue  # tenant-wide facts the configuration already holds
            body = convert(fact.model_dump(mode='json'))
            body.pop('snapshot_digest', None)
            if kind == 'scope':  # the business application requests action.execute and function.execute
                body['catalog_scopes'] = body['authorized_scopes'] = ['action.execute', 'function.execute']
                body['catalog_digest'] = F.canonical_authority_digest({'scopes': body['catalog_scopes']})
            rows[(kind, tuple(convert(key)))] = (kind, convert(key), type(fact).model_validate_json(json.dumps(body)).model_dump(mode='json'))
    _write_facts(admin, tenant, rows.values())
    return (identity_dsn, subject, membership, account, password)


class LoadChain:
    """Per-consumer governed setup on the assembled chain; `run_profile` is what the relay and the fallback launcher read."""

    def __init__(self, f, admin, api, uow):
        self.f, self.admin, self.api, self.uow = f, admin, api, uow
        o = f['original']
        self.tenant = o['tenant']
        from psycopg.conninfo import make_conninfo
        self.identity_dsn = make_conninfo(o['pg'], user='nexloop_identity')
        tokens = f['tokens']
        self.route_token, self.planner_token, self.executor_token = tokens['assembly-route'], tokens['assembly-planner'], tokens['assembly-executor']
        self.worker_token = tokens['assembly-runtime-worker']
        self.route_principal = api.authenticate(self.route_token, world='real')._session.authentication.subject_principal_id
        self.executor_principal = api.authenticate(self.executor_token, world='real')._session.authentication.subject_principal_id
        self.owner_token = seed_multi_uuid(admin, self.tenant, _targets(['Consumer.create', 'EffectControl.create', CONFIGURE, OWNERSHIP,
                                                                           'nexloop.relay.enroll', 'nexloop.relay.withdraw']), suffix='-load-business-owner')
        self.keeper_token = seed_multi_uuid(admin, self.tenant, _targets([a['definition']['stable_name'] for a in definitions(self.tenant)]),
                                            suffix='-load-catalog-maintainer')
        self.source_principal = source_declarations(self.tenant, identity_suffix='-load-source')[0].subject_principal_id
        self.run_profile = {k: f['recipe'][k] for k in RUN_PROFILE_KEYS}
        self.expiry = (datetime.now(UTC) + VALIDITY).isoformat()
        self.source_token = None

    def consumer(self, index, username, hasher):
        """Identity, Consumer, EffectControl, catalog, ownership and enrollment of one manifest consumer."""
        api, key = self.api, f'load-{index:05d}'
        identity = customer(self.admin, self.tenant, self.identity_dsn, username, index, hasher)
        owner = api.authenticate(self.owner_token, world='real')
        keeper = api.authenticate(self.keeper_token, world='real')
        consumer = owner.create_object(action_name='Consumer.create', action_version=1, intent_id=key + '-consumer', type_name='Consumer', properties={})['object_id']
        control = owner.create_object(action_name='EffectControl.create', action_version=1, intent_id=key + '-control', type_name='EffectControl',
            properties={'consumer_id': consumer, 'owner_principal': owner._session.authentication.subject_principal_id, 'executor_principal': self.executor_principal,
                        'budget_units': 1000, 'allow_effect': True, 'valid_until': self.expiry})['object_id']
        owner.configure_effect_control(control_id=control, control_revision=1, executor_token=self.executor_token)
        offering = keeper.create_object(action_name='ServiceOffering.create', action_version=1, intent_id=key + '-offering', type_name='ServiceOffering',
                                        properties=json_export_example(valid_until=self.expiry))['object_id']
        binding = keeper.create_object(action_name='ConsumerServiceOffering.create', action_version=1, intent_id=key + '-binding', type_name='ConsumerServiceOffering',
            properties={'consumer_id': consumer, 'offering_id': offering, 'offering_revision': 1, 'source_principal': self.source_principal, 'active': True})['object_id']
        owner = api.authenticate(self.owner_token, world='real')
        with api._lock:
            ConversationOwnershipRegistrar(api._pool, owner._session, api._signer).register_consumer_owner(
                consumer_id=consumer, principal_id=identity[2].principal_id, idempotency_key=key + '-ownership-key')
        enrolled = relay_enrollment.enroll(api.authenticate(self.owner_token, world='real'), consumer_id=consumer, control_id=control, offering_id=offering,
                                           binding_id=binding, relay_principal=self.route_principal, intent_id=key + '-enroll')
        return {'identity': identity, 'consumer': consumer, 'control': control, 'offering': offering, 'binding': binding, 'enrollment': enrolled['state']}

    def declare_source(self):
        """One Source for every consumer; its READ on them is derived from the enrollments (relay_source_read_rule, 0142)."""
        declared, rows = source_declarations(self.tenant, identity_suffix='-load-source')
        rows.append(message_read_rule_row(self.tenant, self.source_principal, self.expiry))
        rule = RelaySourceReadRule(tenant_id=self.tenant, principal_id=self.source_principal, relay_principal=self.route_principal, active=True,
                                   valid_until=datetime.now(UTC) + VALIDITY)
        rows.append({'kind': 'relay_source_read_rule', 'key': [self.source_principal], 'payload': rule.model_dump(mode='json')})
        self.source_token = secrets.token_urlsafe(48)
        end = max(next(r['payload']['expires_at'] for r in rows if r['kind'] == 'authentication'), (datetime.now(UTC) + VALIDITY).isoformat())
        self.admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) '
                           'values(%s,%s,%s,%s,%s,%s,%s,%s)', (hashlib.sha256(self.source_token.encode()).hexdigest(), self.tenant, declared.credential_id,
                           Jsonb(declared.model_dump(mode='json')), ['real'], 'nexloop-core', 'active', end))
        _write_facts(self.admin, self.tenant, [(r['kind'], r['key'], r['payload']) for r in rows])

    def messages(self, identity, record):
        issued = create(self.uow, identity, authenticate(self.uow, identity).evidence)
        port = ConversationMessagePort(self.api._pool, authenticate_browser_business(self.api._pool, issued.session, world='real'), self.api._signer)
        written = 0
        for conversation in record['conversations']:
            created = port.create_conversation(idempotency_key=f'load-{conversation["key"]}-conversation')
            for m, message in enumerate(conversation['messages'], 1):
                port.accept_message(conversation_id=created['id'], idempotency_key=f'load-{conversation["key"]}-{m:04d}', body=message['body'])
                written += 1
        return written

    def load(self, records, *, progress=None):
        """All manifest consumers: setup and enrollment first, the Source rule once, then each customer's messages."""
        hasher, started, people = PasswordHasher(), time.monotonic(), []
        for record in records:
            people.append((record, self.consumer(record['index'], record['username'], hasher)))
        setup_seconds = time.monotonic() - started
        self.declare_source()
        started, messages = time.monotonic(), 0
        for n, (record, person) in enumerate(people, 1):
            messages += self.messages(person['identity'], record)
            if progress and n % 50 == 0:
                progress(n, time.monotonic() - started)
        return {'consumers': len(people), 'enrolled': sum(p['enrollment'] == 'active' for _, p in people), 'messages': messages,
                'seconds': {'setup_enroll': round(setup_seconds, 1), 'messages': round(time.monotonic() - started, 1)},
                'people': people}


# -- workbench of the load tenant (tenant-parameterised versions of the slice-1 helpers) -------------------------------------
WORKBENCH_APP, CALLER = 'synthetic-workbench-app', 'eios:application:synthetic-workbench'
GOAL_ACTIONS = {'Control.set': 'goals.control.set', 'Contact.release': 'goals.contact.release',
                # Active plans (history option B, dispatcher 2026-10-11): the owner approves the plan goal's metric and publishes it.
                'Metric.approve': 'goals.metric.approve', 'Goal.publish': 'goals.version.publish'}


def publish_workbench_actions(admin, tenant):
    """The workbench write entry's tenant Actions: human requests and takeovers (business manifest) and the goal controls
    derived from the compiled request Action, as tests/test_workbench_actions_http.py publishes them."""
    from commitment_fixture import TAKEOVER_ACTIONS, publish_request_actions
    publish_request_actions(admin, tenant=tenant)
    publish_request_actions(admin, tenant=tenant, names=TAKEOVER_ACTIONS)
    # NX-032 A1 T4: the goal Actions as the catalog builds them (goals.* 1.0.0 snapshots), typed on this tenant's Consumer v1
    # (the reference of the compiled request Action).
    from system_catalog_fixture import publish_catalog_actions_like
    publish_catalog_actions_like(admin, tenant, tuple(GOAL_ACTIONS), 'eios:action:nexloop.plan.request_reevaluation:1')


def workbench_members(admin, pg, tenant, workdir, identities=None):
    """The workbench application and one owner and two operators by trusted configuration (configurator), roles compiled from
    deploy/authorization/workbench-roles. Returns {key: {username, password, principal_id}}."""
    from pathlib import Path
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.workbench_roles import apply_members, compile_members, load as load_roles
    roles = load_roles(Path(__file__).resolve().parents[1] / 'deploy/authorization/workbench-roles.v1.json')
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true) on conflict do nothing', (tenant, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s) on conflict do nothing', (tenant, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    return _members(admin, pg, tenant, workdir, roles, apply_members, compile_members, make_conninfo, identities)


def _members(admin, pg, tenant, workdir, roles, apply_members, compile_members, make_conninfo, identities=None):
    hasher, people = PasswordHasher(), {}
    for n, key in enumerate(('owner', 'operator-1', 'operator-2'), 1):
        now = datetime.now(UTC)
        password = secrets.token_urlsafe(24)
        subject = Subject(subject_id=f'load-staff-{key}', kind=SubjectKind.HUMAN, status='active', created_at=now, updated_at=now, revision=1)
        membership = TenantMembership(tenant_id=tenant, principal_id=f'load-staff-{key}-principal', subject_id=subject.subject_id, kind=MembershipKind.HOME,
                                      status='active', valid_from=now, valid_until=None, revision=1)
        account = LocalAccount(local_account_id=f'load-staff-{key}-account', tenant_id=tenant, subject_id=subject.subject_id, username=f'staff-{key}',
                               verified_email=f'staff-{key}@example.invalid', password_hash=EncodedPasswordHash(hasher.hash(password)), status='active',
                               failed_attempts=0, lockout_level=0, locked_until=None, must_change_password=False, session_epoch=1, created_at=now, updated_at=now, revision=1)
        admin.execute('insert into control.nexloop_browser_subjects values(%s,%s)', (subject.subject_id, Jsonb(subject.model_dump(mode='json'))))
        admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)', (tenant, membership.principal_id, subject.subject_id, Jsonb(membership.model_dump(mode='json'))))
        payload = account.model_dump(mode='json')
        payload.pop('password_hash')
        payload.pop('password_history')
        admin.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',
                      (tenant, account.local_account_id, subject.subject_id, account.username, account.password_hash.get_secret_value(), [], Jsonb(payload)))
        people[key] = {'username': account.username, 'password': password, 'principal_id': membership.principal_id, 'subject_id': subject.subject_id}
        if identities is not None:  # in-process sessions (test_browser_evidence.authenticate shape); never written to a file
            identities[key] = (None, subject, membership, account, password)
    members = {'schema_version': 'nexloop-workbench-members/1', 'tenant_id': tenant, 'application_id': WORKBENCH_APP, 'caller_application_id': CALLER,
               'members': [{'principal_id': p['principal_id'], 'subject_id': p['subject_id'], 'role': 'owner' if k == 'owner' else 'operator'} for k, p in people.items()]}
    admin.execute('alter role nexloop_configurator login')
    dsn = workdir / 'configurator-dsn'
    dsn.write_text(make_conninfo(pg, user='nexloop_configurator'))
    dsn.chmod(0o600)
    apply_members(roles, members, database_url_file=dsn)
    dsn.unlink()
    facts = []
    for item in compile_members(roles, members):
        kind, key, payload = (item['kind'], item['key'], item['payload']) if isinstance(item, dict) else (item[0], item[1], item[2].model_dump(mode='json'))
        facts.append((kind, key, payload))
    _write_facts(admin, tenant, facts)
    return {k: {'username': p['username'], 'password': p['password'], 'principal_id': p['principal_id']} for k, p in people.items()}


# -- active plans (dispatcher 2026-10-11, option B: plan data only) ---------------------------------------------------------
PLAN_GOAL = 'load-retention'


def load_plans(admin, api, worker, identity_pool, tenant, owner_identity, plan_token, people, *, now=None):
    """Active plans of the manifest's `active_plan` consumers, only through governed entries: the workbench owner (in-process
    workbench session) approves the goal's metric, publishes the goal (GoalGovernedActions) and the built-in context strategy
    (StrategyPublisher); the deployed plan-reevaluator identity establishes each plan through its port (PlanPort.establish).
    Plan data only: every step has reassess_at null and the cells do not run the plan reevaluator, so no plan Run starts
    (reevaluation and fallback Run load are not covered; docs/implementation/NX-031-implementation.md). The recipe carries the
    consumer's enrolled control, offering and binding; role, link and step are synthetic ids (no role chain is loaded).
    Returns counts and seconds."""
    from pathlib import Path
    from datetime import timedelta as _td
    from nexloop_eios import business_actions
    from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
    from nexloop_eios.context_engine import strategy as S
    from nexloop_eios.goal_controls import GoalGovernedActions
    from nexloop_eios.plan_reevaluation import PlanPort, load_settings, plan_settings
    root = Path(__file__).resolve().parents[1]
    started, now = time.monotonic(), now or datetime.now(UTC).replace(microsecond=0)
    # The strategy publication Action from the versioned business manifest (as tests/test_context_engine_pg.py), and the owner's grant on it.
    schema = S.context_strategy_object_type()
    admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s) on conflict do nothing',
                  (tenant, schema.type_name, 1, Jsonb(schema.model_dump(mode='json'))))
    from system_catalog_fixture import business_capabilities
    for compiled in business_actions.compile_actions(business_actions.load(root / 'deploy/configuration/business-actions.v1.json'), tenant=tenant, created_by='load-owner',
            created_at=now, object_types=[schema.model_dump(mode='json')], select=(S.PUBLISH_ACTION,),
            capabilities=business_capabilities((S.PUBLISH_CAPABILITY,))):
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s) on conflict do nothing',
                      (tenant, 'real', f"eios:action:{compiled['definition']['stable_name']}:1", Jsonb(compiled['definition']), Jsonb(compiled['capability'])))
    # The owner's grant on it, compiled like every workbench member grant (nexloop_eios.workbench_roles) from the owner role (which
    # carries it since workbench roles v11); written as the trusted configuration writes compiled facts.
    import copy
    from nexloop_eios.workbench_roles import compile_members, load as load_roles
    roles = copy.deepcopy(load_roles(root / 'deploy/authorization/workbench-roles.v1.json'))
    assert f'eios:action:{S.PUBLISH_ACTION}:1' in roles['roles']['owner']  # workbench roles v11: the owner publishes strategies
    members = {'schema_version': 'nexloop-workbench-members/1', 'tenant_id': tenant, 'application_id': WORKBENCH_APP, 'caller_application_id': CALLER,
               'members': [{'principal_id': owner_identity[2].principal_id, 'subject_id': owner_identity[1].subject_id, 'role': 'owner'}]}
    facts = []
    for item in compile_members(roles, members):
        kind, key, payload = (item['kind'], item['key'], item['payload']) if isinstance(item, dict) else (item[0], item[1], item[2].model_dump(mode='json'))
        if f'eios:action:{S.PUBLISH_ACTION}:1' in key or kind == 'application':  # the added Action's facts and the application's catalog
            facts.append((kind, key, payload))
    _write_facts(admin, tenant, facts)
    # The owner's workbench session (the application's own realm).
    uow = PostgresBrowserSessionUnitOfWork(identity_pool, tenant_id=tenant, application_id=WORKBENCH_APP)
    session = create(uow, owner_identity, authenticate(uow, owner_identity).evidence).session
    human = authenticate_browser_business(api._pool, session, world='real')
    owner = GoalGovernedActions(api._pool, human, api._signer)
    owner.approve_metric(action_name='Metric.approve', action_version=1, request_id='load-metric-retention-1', metric_id='retention-rate', version=1,
                         name='复购率', aggregation='ratio_of_sums', unit='ratio', maturity_seconds=0, refund_rule='not_applicable', cohort_rule='synthetic')
    period = (now - _td(days=30), now + _td(days=335))
    owner.publish_goal(action_name='Goal.publish', action_version=1, request_id='load-goal-retention-1', goal_id=PLAN_GOAL, goal_kind='long_term',
                       expected_current_version=0, parent=None, objective='合成数据：维持复购，不为指标硬发消息', period_start=period[0], period_end=period[1],
                       priority=3, budget=None, constraints=[], change_summary='load template', role_ref=None,
                       key_results=[dict(kr_key='retention', metric_id='retention-rate', metric_version=1, target='0.60', direction='at_least',
                                         window_start=period[0], window_end=period[1])])
    builtin = next(x for x in S.load_builtins(root / 'deploy/configuration/context-strategies.v1.json') if x['strategy_id'] == 'recent_plus_required')
    S.StrategyPublisher(api._pool, authenticate_browser_business(api._pool, session, world='real'), api._signer).publish(builtin, request_id='load-strategy-recent-plus-required-1')
    # The workbench application's catalog as the configured roles compile it.
    original = load_roles(root / 'deploy/authorization/workbench-roles.v1.json')
    _write_facts(admin, tenant, [(item['kind'], item['key'], item['payload']) for item in compile_members(original, members) if item['kind'] == 'application'])
    goals_seconds = time.monotonic() - started
    settings = plan_settings(load_settings(root / 'deploy/configuration/plan-reevaluation.v1.json'))
    established, started = 0, time.monotonic()
    for record, person in people:
        if not record['active_plan']:
            continue
        synthetic = lambda label: hashlib.sha256(f'load-plan:{record["index"]}:{label}'.encode()).hexdigest()
        plan = {'plan_id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'nexloop-load-plan:{record["index"]:05d}')), 'consumer_id': person['consumer'],
                'goal_version_ref': f'goal:{PLAN_GOAL}@1', 'strategy': {'content': '先确认需求，再给出服务选项；不催促'},
                'context_strategy_ref': S.strategy_ref(builtin),
                'recipe': {'role_id': synthetic('role'), 'link_id': synthetic('link'), 'step_id': synthetic('step'), 'offering_id': person['offering'],
                           'binding_id': person['binding'], 'control_id': person['control']},
                'settings': settings,
                'steps': [{'step_key': 'confirm-need', 'step_object_id': None, 'prerequisites': ['客户最近来信已回复'], 'expected_result': '确认需求，不催促',
                           'stop_if': [], 'reassess_at': None, 'budget': {'maximum_model_turns': 6, 'maximum_tool_calls': 20, 'active_timeout_seconds': 120}, 'intent_ref': None}]}
        service = worker.authenticate(plan_token, world='real')
        PlanPort(service._backend._pool, service._session, service._backend._signer).establish(plan)
        established += 1
    return {'established': established, 'seconds': {'goal_strategy': round(goals_seconds, 1), 'establish': round(time.monotonic() - started, 1)}}
