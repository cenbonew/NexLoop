"""NX-037 readiness gaps §9 step 2: the five interaction kinds driven through the run entry (scripts/nx037/run_interactions.py,
main(..., test_stack=True)) on the local synthetic stack, on clean catalog PostgreSQL. No model, no real credential, no server.

The stack: the actual API (TLS, 127.0.0.1), the message relay with an enterprise slot (ADR-030), the deterministic Agent Host
(deterministic-test, CNY, its synthetic enterprise delivery protocol) with its runtime worker and guard, the local JSON delivery
for replies, the reference enterprise endpoint (apps/reference-endpoint, in process, test CA and test tokens) for
nexloop.enterprise.deliver:1, and the effect worker with one provider route per Action. The three real dispatch layers are
open in this disposable database only (REAL_DISPATCH_ENABLED for the effect worker and the run entry, the tenant switch, the
Action switch of the enterprise delivery). The tenant is the synthetic TENANT_A.

The person playing the customer is an automated operator over the same HTTPS API as the WebChat; for the takeover it also
plays the staff (the 0126/0127 handlers as the governed entry calls them), and for the unknown result it switches the
endpoint's local drill ("the next answer comes after NexLoop's deadline"). The run entry's RealDriver only observes (0172,
0189 and 0190 here): one interaction of each kind, in the order ordinary reply, enterprise delivery, unknown result,
refusal, takeover (the restricted customer writes again); the trial pauses after the three calibration interactions and resumes with the owner's confirmation.

Run serially (Node 24, the built Host)."""
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from nexloop_eios import controlled_interactions as X
from support.tenants import TENANT_A
from test_agent_host import files, free_port
from test_local_message_delivery_assembly import process, reaped_host, wait_live
from test_message_runtime_effect_e2e import trusted_host_evidence
from test_outbound_messages_pg import publish_recorder
from test_real_dispatch_switch_pg import set_switch
from test_reference_endpoint import endpoint_module
from test_runtime_effect_tools import effect_configuration
from test_trusted_configuration_pg import private

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import evidence_pack as E  # noqa: E402
import trial_guards  # noqa: E402
import run_interactions as R  # noqa: E402

pytestmark = pytest.mark.nx037_test_stack
TRIAL_ID = '37d00000-0000-4000-8000-000000000005'
DELIVER = 'eios:action:nexloop.enterprise.deliver:1'
# The refusal comes before the takeover: the restricted customer writes again and gets its bound reply (ADR-023 rule 6, 0190).
# The enterprise deliveries come first: a contact restriction stops them at dispatch (customer_contact, effect categories v3).
ORDER = ['ordinary_reply', 'real_delivery', 'unknown_reconciliation', 'restricted_after_refusal', 'human_takeover']
MARKER = '企业交付：'  # the deterministic Host's synthetic enterprise delivery protocol (deterministic_enterprise_deliver)
TEXT = {'ordinary_reply': '你好，想咨询一下合成服务的营业时间', 'real_delivery': MARKER + '合成工单：上门检查一次',
        'human_takeover': '我想找人工客服聊聊合成订单', 'unknown_reconciliation': MARKER + '合成工单：结果暂时未知的一次',
        'restricted_after_refusal': '不要再联系我'}
STAFF = 'synthetic-staff-principal'


class Pump:
    """The background services, one --once tick each in turn, until stopped. While `quiet` is set (a takeover holds the
    conversation) the relay's fixed refusal exit (1) is expected and not a failure."""

    def __init__(self, commands, hidden, environ):
        self.commands, self.hidden, self.environ = commands, hidden, environ
        self.stop, self.quiet, self.failures = threading.Event(), threading.Event(), []
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        while not self.stop.is_set():
            for module, args in self.commands:
                run = subprocess.run([sys.executable, '-m', module, *args, '--once'], capture_output=True, text=True, timeout=120, env=self.environ(module))
                if any(h in run.stdout + run.stderr for h in self.hidden):
                    self.failures.append(('secret_in_output', module))
                if run.returncode != 0 and not (self.quiet.is_set() and module == 'nexloop_eios.message_relay_cli' and run.returncode == 1):
                    self.failures.append((module, run.returncode, run.stderr[-300:] if not any(h in run.stderr for h in self.hidden) else 'hidden'))
            self.stop.wait(.3)


class Operator:
    """The automated person on the customer side (and the staff during the takeover)."""

    def __init__(self, client, headers, conversation, admin, tenant, drill, pump):
        self.client, self.headers, self.conversation, self.admin, self.tenant, self.drill, self.pump = client, headers, conversation, admin, tenant, drill, pump
        self.prompts, self.sent, self.takeover, self.errors = [], {}, {}, []

    def send(self, text, key):
        response = self.client.post(f'/api/v1/conversations/{self.conversation}/messages', headers={**self.headers, 'Idempotency-Key': key}, json={'body': text})
        assert response.status_code in (200, 202), response.status_code
        return response.json()['message']['id']

    def messages(self):
        return self.client.get(f'/api/v1/conversations/{self.conversation}/messages').json()['items']

    def wait_reply(self, inbound, deadline=60):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            replies = [m for m in self.messages() if m.get('direction') == 'outbound' and m.get('reply_to_message_id') == inbound]
            if replies:
                return replies
            time.sleep(.3)
        raise AssertionError(('no reply to the operator message', inbound, [{k: m.get(k) for k in m if k != 'body'} for m in self.messages()], self.pump.failures))

    def prompt(self, interaction, instruction, deadline_seconds):
        try:
            self.act(interaction)
        except Exception:
            import traceback
            self.errors.append(traceback.format_exc())  # the run entry reports only the exception type
            raise

    def act(self, interaction):
        seq, kind = interaction['seq'], interaction['kind']
        self.prompts.append((seq, kind))
        if kind == 'unknown_reconciliation':
            # L5 drill: the endpoint persists the next new POST and answers only after 5 s, beyond the worker's 3 s deadline.
            assert self.drill(['drill', '--data-dir', str(self.drill.data), '--delay-next-posts', '1', '--delay-seconds', '5']) == 0
        inbound = self.send(TEXT[kind], f'nx037-five-message-key-{seq}')
        self.sent[kind] = inbound
        if kind == 'human_takeover':
            self.wait_reply(inbound)  # the Agent answered the first message; then a person takes the conversation over
            self.pump.quiet.set()
            started = self.admin.execute('select control.nexloop_governed_takeover(%s,%s,%s,%s,%s,%s)', (self.tenant, 'real', STAFF, 'human', 'nx037-five-takeover',
                Jsonb({'operation': 'take_over_conversation', 'scope_kind': 'conversation', 'scope_ref': self.conversation, 'reason': '合成数据：人工接手'}))).fetchone()[0]
            during = self.send('人工客服在吗？', f'nx037-five-message-key-{seq}-during')
            time.sleep(1.5)  # relay ticks pass: no Run for a message of a taken-over conversation
            with self.admin.transaction():
                self.admin.execute("select set_config('eios.tenant_id',%s,true)", (self.tenant,))
                staff = self.admin.execute('select runtime.nexloop_governed_staff_reply(%s,%s,%s,%s,%s,%s)', (self.tenant, 'real', STAFF, 'human', 'nx037-five-staff',
                    Jsonb({'operation': 'send_staff_reply', 'conversation_id': self.conversation, 'reply_to': during, 'text': '我是人工客服，合成订单已经帮你看过了。'}))).fetchone()[0]['message']
            ended = self.admin.execute('select control.nexloop_takeover_end(%s,%s,%s,%s,%s,%s)', (self.tenant, 'real', started['takeover_id'], 'handback', STAFF,
                                                                                               'nx037-five-handback')).fetchone()[0]
            self.admin.execute('update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp() where tenant_id=%s', (self.tenant,))
            self.pump.quiet.clear()
            self.takeover = {'during': during, 'staff': staff['id'], 'ended': ended}


def drill_command(data):
    def run(argv):
        return endpoint_module.main(argv)
    run.data = data
    return run


@contextlib.contextmanager
def reference_endpoint(tmp_path):
    """The enterprise's stand-in, in process so the test sees every request it receives (method, intent id)."""
    tls = tmp_path / 'endpoint-tls'
    tls.mkdir(mode=0o700)
    files(tls)
    service, staff = private(tmp_path, 'endpoint-service-token', secrets.token_urlsafe(32)), private(tmp_path, 'endpoint-staff-token', secrets.token_urlsafe(32))
    data = tmp_path / 'endpoint-data'
    port = free_port()
    server = endpoint_module.make_server(data_dir=data, listen=f'127.0.0.1:{port}', service_token=service.read_text(), staff_token=staff.read_text(),
                                         tls_cert_file=tls / 'host-cert.pem', tls_key_file=tls / 'host-key.pem', tenant_label='合成企业')
    seen = []
    handler = server.RequestHandlerClass
    post, get = handler.post_effect, handler.get_effect

    def logged_post(self):
        seen.append(('POST', self.headers.get('Idempotency-Key')))
        return post(self)

    def logged_get(self):
        seen.append(('GET', self.path.rsplit('/', 1)[-1]))
        return get(self)
    handler.post_effect, handler.get_effect = logged_post, logged_get
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield dict(origin=f'https://127.0.0.1:{port}', ca=tls / 'host-cert.pem', token=service, data=data, seen=seen, store=server.store)
    finally:
        server.shutdown()
        server.server_close()


def enrolled_customer(f, admin, api, tmp_path):
    """One customer on the multi-consumer relay chain (tests/load_chain.py, the deployed enrollment path), with the owner's
    enterprise slot (tenant assembly §6): its own EffectControl configured for the enterprise Action, the enterprise offering and
    the consumer's binding to it, enrolled with the relay. The Source's READ is derived from the enrollment (0142) and it holds
    the enterprise Action next to the reply. Synthetic data."""
    from argon2 import PasswordHasher
    from load_chain import LoadChain, _write_facts
    from message_offering_fixture import message_read_rule_row
    from context_source_declarations import source_declarations
    from nexloop_eios import relay_enrollment
    from nexloop_eios.property_access import RelaySourceReadRule
    from nexloop_eios.service_offerings import enterprise_delivery_example
    chain = LoadChain(f, admin, api, None)
    person = chain.consumer(1, 'nx037-five-customer', PasswordHasher())
    owner = api.authenticate(chain.owner_token, world='real')
    control = owner.create_object(action_name='EffectControl.create', action_version=1, intent_id='nx037-five-enterprise-control', type_name='EffectControl',
        properties={'consumer_id': person['consumer'], 'owner_principal': owner._session.authentication.subject_principal_id,
                    'executor_principal': chain.executor_principal, 'budget_units': 3, 'allow_effect': True, 'valid_until': chain.expiry})['object_id']
    api.authenticate(chain.owner_token, world='real').configure_effect_control(control_id=control, control_revision=1, executor_token=chain.executor_token,
                                                                               action='nexloop.enterprise.deliver')
    keeper = api.authenticate(chain.keeper_token, world='real')
    offering = keeper.create_object(action_name='ServiceOffering.create', action_version=1, intent_id='nx037-five-enterprise-offering', type_name='ServiceOffering',
        properties=enterprise_delivery_example(service_code='enterprise.synthetic-visit', title='合成上门检查', price_amount='12.50', valid_until=chain.expiry))['object_id']
    binding = api.authenticate(chain.keeper_token, world='real').create_object(action_name='ConsumerServiceOffering.create', action_version=1,
        intent_id='nx037-five-enterprise-binding', type_name='ConsumerServiceOffering',
        properties={'consumer_id': person['consumer'], 'offering_id': offering, 'offering_revision': 1, 'source_principal': chain.source_principal, 'active': True})['object_id']
    enrolled = relay_enrollment.enroll(api.authenticate(chain.owner_token, world='real'), consumer_id=person['consumer'], control_id=person['control'],
                                       offering_id=person['offering'], binding_id=person['binding'], relay_principal=chain.route_principal,
                                       intent_id='nx037-five-enroll-enterprise', enterprise={'control_id': control, 'offering_id': offering, 'binding_id': binding})
    assert enrolled['state'] == 'active'
    # The Source (load chain identity) with the enterprise Action; READ derived from the enrollment.
    declared, rows = source_declarations(chain.tenant, identity_suffix='-load-source', extra_actions=[DELIVER])
    assert declared.subject_principal_id == chain.source_principal
    rows.append(message_read_rule_row(chain.tenant, chain.source_principal, chain.expiry))
    rule = RelaySourceReadRule(tenant_id=chain.tenant, principal_id=chain.source_principal, relay_principal=chain.route_principal, active=True,
                               valid_until=datetime.now(UTC) + timedelta(days=1))
    rows.append({'kind': 'relay_source_read_rule', 'key': [chain.source_principal], 'payload': rule.model_dump(mode='json')})
    token = secrets.token_urlsafe(48)
    end = next(r['payload']['expires_at'] for r in rows if r['kind'] == 'authentication')
    admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) '
                  'values(%s,%s,%s,%s,%s,%s,%s,%s)', (hashlib.sha256(token.encode()).hexdigest(), chain.tenant, declared.credential_id,
                                                      Jsonb(declared.model_dump(mode='json')), ['real'], 'nexloop-core', 'active', end))
    _write_facts(admin, chain.tenant, [(r['kind'], r['key'], r['payload']) for r in rows])
    return {'chain': chain, 'person': person, 'source_token': token, 'enterprise': {'control_id': control, 'offering_id': offering, 'binding_id': binding}}


def tenant_readiness(o, admin, tenant, trial, period):
    """Real tenant init steps on the test database: effect categories v3 for the published Actions and alert rules v4 through
    trusted configuration (the configurator role); the D-g model budget (100 CNY over the period) as a test seed."""
    from nexloop_eios import effect_categories, observability
    configurator = private(o['paths']['dsn'].parent, 'five-configurator-dsn', make_conninfo(o['pg'], user='nexloop_configurator'))
    manifest = effect_categories.load(R.ROOT / 'deploy/configuration' / json.loads(trial.read_text())['effect_categories'])
    published = {r[0] for r in admin.execute("select definition->>'stable_name' from control.nexloop_action_definitions where tenant_id=%s", (tenant,)).fetchall()}
    declared = [a for a in manifest['actions'] if a['stable_name'] in published]
    assert {a['stable_name'] for a in declared} == {'nexloop.service.request', 'nexloop.enterprise.deliver'}
    effect_categories.apply({**manifest, 'actions': declared}, tenant, database_url_file=configurator)
    rules = json.loads((R.ROOT / 'deploy/configuration' / json.loads(trial.read_text())['alert_rules']).read_text())
    observability.apply_rules(rules, tenant, database_url_file=configurator)
    # The glue (merge) configuration step of real_tenant_init (verify: merge_configuration_active), on the calibrated recall profile.
    from nexloop_eios import merge_configuration
    from test_nx037_tenant_init_pg import activate_calibrated_recall_profile
    activate_calibrated_recall_profile(admin, tenant)
    merge_configuration.apply(merge_configuration.load(R.ROOT / 'deploy/ontology/merge-config.v1.json'), tenant, database_url_file=configurator)
    admin.execute("insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision) "
                  "values(%s,'real','model','CNY',100,%s,%s,1)", (tenant, *period))


def wait_ready(check, seconds):
    """Poll `check()` (refusal codes) until it holds or `seconds` pass; the last refusal fails the test."""
    end = time.monotonic() + seconds
    while True:
        failed = check()
        if not failed:
            return
        assert time.monotonic() < end, failed
        time.sleep(0.5)


def refused_codes(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1]).get('refused', [])


def test_five_interaction_kinds_through_the_run_entry(assembled_message, admin, tmp_path, capsys):  # noqa: F811
    f = assembled_message
    o = f['original']
    p = o['paths']
    tenant = o['tenant']
    assert tenant == TENANT_A
    credentials = f['credential_files']
    trusted_host_evidence()
    common = ['--signing-key-file', str(p['backend_signing']), '--signing-key-id', 'explicit-configuration']
    api_dsn = private(tmp_path, 'api-dsn', make_conninfo(o['pg'], user='nexloop_api'))
    domain_dsn = private(tmp_path, 'domain-dsn', make_conninfo(o['pg'], user='nexloop_domain_worker'))
    effect_dsn = private(tmp_path, 'effect-dsn', make_conninfo(o['pg'], user='nexloop_action_worker'))
    metrics_dsn = private(tmp_path, 'metrics-dsn', make_conninfo(o['pg'], user='nexloop_metrics'))
    now = datetime.now(UTC)
    period = (now - timedelta(hours=1), now + timedelta(days=2))

    # The trial: v2 (CNY, caps 100/2/300, alert rules v4, effect categories v3) with one interaction of each kind.
    trial_config = json.loads(R.TRIAL.read_text())
    trial_config['plan'] = {**trial_config['plan'], 'total': 5, 'mix': {k: 1 for k in X.KINDS}}
    trial_file = private(tmp_path, 'trial.json', json.dumps(trial_config))
    plan = json.loads(R.PLAN.read_text())
    plan['order'] = ORDER
    plan_file = private(tmp_path, 'plan.json', json.dumps(plan))
    assert plan['real_dispatch'] == {'deployment': True, 'tenant': True, 'data_mode': 'real', 'actions': [DELIVER]}
    tenant_readiness(o, admin, tenant, R.TRIAL, period)

    api_tls = tmp_path / 'api-tls'
    api_tls.mkdir(mode=0o700)
    files(api_tls)
    port = free_port()
    origin = f'https://127.0.0.1:{port}'
    rate = private(tmp_path, 'rate-key', secrets.token_hex(32))
    api_args = ['--database-url-file', str(api_dsn), *common, '--artifact-root', str(tmp_path / 'api-artifacts'), '--mode', 'test', '--port', str(port),
                '--tls-certificate-file', str(api_tls / 'host-cert.pem'), '--tls-key-file', str(api_tls / 'host-key.pem'),
                '--identity-database-url-file', str(p['identity']), '--browser-rate-key-file', str(rate), '--browser-tenant-id', tenant,
                '--browser-application-id', o['application'], '--browser-origin', origin]
    hidden = list(f['tokens'].values()) + [p['password'].read_text()]
    clean = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_', 'REAL_DISPATCH_'))}
    with contextlib.ExitStack() as stack:
        endpoint = stack.enter_context(reference_endpoint(tmp_path))
        hidden.append(endpoint['token'].read_text())
        api = stack.enter_context(process('nexloop_eios.http_api', api_args, hidden))
        client = stack.enter_context(httpx.Client(base_url=origin, verify=str(api_tls / 'host-cert.pem'), trust_env=False, timeout=10))
        wait_live(api, client)
        from nexloop_eios.backend import open_backend
        backend = stack.enter_context(open_backend(database_url=make_conninfo(o['pg'], user='nexloop_api'), artifact_root=tmp_path / 'chain-backend',
                                                   signing_key_file=p['backend_signing'], signing_key_id='explicit-configuration'))
        customer = enrolled_customer(f, admin, backend, tmp_path)
        chain, person = customer['chain'], customer['person']
        hidden += [customer['source_token'], chain.owner_token, chain.keeper_token, person['identity'][4]]
        login = client.post('/api/v1/auth/login', headers={'Origin': origin}, json={'username': 'nx037-five-customer', 'password': person['identity'][4]})
        assert login.status_code == 200
        headers = {'Origin': origin, 'X-CSRF-Token': login.json()['csrf_token']}
        created = client.post('/api/v1/conversations', headers={**headers, 'Idempotency-Key': 'nx037-five-conversation'}, json={})
        assert created.status_code == 200
        conversation = created.json()['id']

        # The relay in run profile mode (every enrolled consumer, here one with the enterprise slot); the Run budget in CNY.
        run_profile = private(tmp_path, 'run-profile', json.dumps({**chain.run_profile, 'budget': {**chain.run_profile['budget'], 'currency': 'CNY'}}))
        source_file = private(tmp_path, 'chain-source-credential', customer['source_token'])
        vault = tmp_path / 'relay-vault'
        vault.mkdir(mode=0o700)
        relay = ['--database-url-file', str(api_dsn), *common, '--artifact-root', str(tmp_path / 'relay-artifacts'), '--vault-root', str(vault),
                 '--run-profile-file', str(run_profile), '--source-credential-file', str(source_file)]
        relay += sum((['--' + name + '-credential-file', str(credentials['assembly-' + label])]
                      for name, label in [('route', 'route'), ('planner', 'planner'), ('executor', 'executor')]), [])

        # The deterministic Host: message from input on context pack v2, CNY, the synthetic enterprise delivery protocol.
        host_tls = tmp_path / 'host-tls'
        host_tls.mkdir(mode=0o700)
        runtime, host_key = files(host_tls)
        guard_port = free_port()
        guard_key = private(tmp_path, 'guard-key', secrets.token_hex(32))
        guard_tls = tmp_path / 'guard-tls'
        guard_tls.mkdir(mode=0o700)
        files(guard_tls)
        host_config = effect_configuration(guard_tls, guard_port, guard_key)
        cfg = json.loads(host_config.read_text())
        cfg.pop('deterministic_effect_message')
        cfg.update(deterministic_message_from_input=True, context_input_protocol='nexloop.context-pack.v2', cost_currency='CNY', deterministic_enterprise_deliver=True)
        host_config.write_text(json.dumps(cfg))
        _, host_client, _ = stack.enter_context(reaped_host(runtime, host_key, host_config))
        worker = ['--database-url-file', str(domain_dsn), *common, '--service-credential-file', str(credentials['assembly-runtime-worker']),
                  '--artifact-root', str(tmp_path / 'runtime-worker-artifacts'), '--world', 'real', '--queue', 'operations',
                  '--host-origin', str(host_client.base_url).rstrip('/'), '--host-control-key-file', str(host_key), '--host-ca-file', str(host_tls / 'host-cert.pem'),
                  '--guard-port', str(guard_port), '--guard-key-file', str(guard_key), '--guard-certificate-file', str(guard_tls / 'host-cert.pem'),
                  '--guard-tls-key-file', str(guard_tls / 'host-key.pem'), '--total-timeout', '20']

        # Replies: the local JSON delivery. Enterprise deliveries: the reference endpoint. One route per Action, no default.
        delivery_tls = tmp_path / 'delivery-tls'
        delivery_tls.mkdir(mode=0o700)
        _, provider_key = files(delivery_tls)
        delivery_origin = f'https://127.0.0.1:{free_port()}'
        delivery_root = tmp_path / 'json-products'
        delivery_root.mkdir(mode=0o700)
        webchat_route = private(tmp_path, 'webchat-provider', json.dumps({'origin': delivery_origin, 'credential_file': str(provider_key),
                                                                         'ca_file': str(delivery_tls / 'host-cert.pem'), 'timeout': 3}))
        enterprise_route = private(tmp_path, 'enterprise-provider', json.dumps({'origin': endpoint['origin'], 'credential_file': str(endpoint['token']),
                                                                               'ca_file': str(endpoint['ca']), 'timeout': 3}))
        routes = private(tmp_path, 'provider-routes', json.dumps({'schema': 'nexloop-effect-provider-routes/1',
                                                                  'routes': {'nexloop.service.request': str(webchat_route), 'nexloop.enterprise.deliver': str(enterprise_route)}}))
        delivery = ['--database-url-file', str(effect_dsn), *common, '--service-credential-file', str(credentials['assembly-executor']),
                    '--artifact-root', str(tmp_path / 'delivery-artifacts'), '--delivery-root', str(delivery_root), '--origin', delivery_origin,
                    '--certificate-file', str(delivery_tls / 'host-cert.pem'), '--tls-key-file', str(delivery_tls / 'host-key.pem'),
                    '--provider-ca-file', str(delivery_tls / 'host-cert.pem'), '--provider-credential-file', str(provider_key)]
        stack.enter_context(process('nexloop_eios.local_json_delivery', delivery, hidden + [provider_key.read_text()]))
        # The delivery service just started is the one the start guard checks: wait (bounded) until it answers its health
        # route. Its start can take longer than the steps before the first trial start on a loaded host (CI 2026-10-14:
        # webchat_delivery_unavailable at the first start); the guard itself stays a single strict check.
        wait_ready(lambda: trial_guards.webchat_delivery(webchat_route), 120)
        executor = ['--database-url-file', str(effect_dsn), *common, '--service-credential-file', str(credentials['assembly-executor']),
                    '--artifact-root', str(tmp_path / 'effect-worker-artifacts'), '--world', 'real', '--provider-routes-file', str(routes)]

        # The three real dispatch layers, open in this test database only.
        set_switch(admin, tenant, 'tenant', '*', True, 'real')
        set_switch(admin, tenant, 'action_type', DELIVER, True)
        opened = {'REAL_DISPATCH_ENABLED': 'true'}
        # The governed recorder writes each delivered reply into the conversation stream (NX-047, Message.agent_create).
        recorder_token = publish_recorder(f, admin)
        hidden.append(recorder_token)
        recorder = ['--database-url-file', str(api_dsn), *common, '--service-credential-file', str(private(tmp_path, 'recorder-credential', recorder_token)),
                    '--artifact-root', str(tmp_path / 'recorder-artifacts'), '--world', 'real']
        pump = Pump([('nexloop_eios.message_relay_cli', relay), ('nexloop_eios.runtime_worker', worker), ('nexloop_eios.effect_worker', executor),
                     ('nexloop_eios.outbound_messages', recorder)],
                    hidden + [provider_key.read_text()], lambda module: {**clean, **opened} if module == 'nexloop_eios.effect_worker' else clean)
        pump.thread.start()
        stack.callback(lambda: (pump.stop.set(), pump.thread.join(timeout=180)))

        operator = Operator(client, headers, conversation, admin, tenant, drill_command(endpoint['data']), pump)
        catalog = private(tmp_path, 'value-catalog.json', json.dumps({'offerings': ['enterprise.synthetic-visit']}))
        owner_session = private(tmp_path, 'owner-session', 'synthetic-owner-session')
        out = tmp_path / 'trial'
        # Start guards (trial_guards): the customer's D3 consent (a private synthetic file, registered by sha256 in the trial
        # configuration, the consumer listed in the plan), the trial window (the run entry's clock: 12:00 local), the enterprise
        # endpoint's conformance and the WebChat delivery through the effect worker's own provider files, the recorder backlog.
        # Owner 2026-10-14: the customer is an AI agent the owner operates (here the automated operator); the owner's one-time
        # authorization (a private file, by sha256) and the agent participant.
        consumer_ref = 'consumer:' + person['consumer']
        authorization = private(tmp_path, 'operator-authorization', 'synthetic owner authorization for the trial agents')
        trial_config['operator_authorization'] = {'authorization_sha256': hashlib.sha256(authorization.read_bytes()).hexdigest()}
        trial_config['participants'] = [{'kind': 'agent', 'consumer_ref': consumer_ref, 'agent': 'synthetic test operator'}]
        trial_file.write_text(json.dumps(trial_config))
        plan['consumers'] = [consumer_ref]
        plan_file.write_text(json.dumps(plan))
        local_noon = datetime.now(UTC).replace(hour=4, minute=0, second=0, microsecond=0)  # 12:00 Asia/Shanghai
        argv = ['--driver', 'real', '--trial-id', TRIAL_ID, '--out', str(out), '--trial-config', str(trial_file), '--plan-file', str(plan_file),
                '--tenant', tenant, '--owner-principal', 'owner-principal-synthetic', '--value-catalog', str(catalog), '--channel-ref', 'channel:webchat:synthetic',
                '--period-start', period[0].isoformat(), '--period-end', (period[1] - timedelta(hours=1)).isoformat(), '--metrics-dsn-file', str(metrics_dsn),
                '--owner-session-file', str(owner_session), '--host-config', str(host_config), '--interaction-timeout', '40', '--poll-seconds', '0.5',
                '--operator-authorization-file', str(authorization), '--enterprise-provider-file', str(enterprise_route), '--webchat-provider-file', str(webchat_route)]

        def connect(path):
            return psycopg.connect(Path(path).read_text().strip(), autocommit=True)
        # Without the test stack the deterministic Host is refused (host_not_real_model): the escape is a keyword, never the CLI.
        assert R.main(argv, environ=opened, connect=connect, operator=operator, clock=lambda: local_noon) == 3 and not (out / 'state.json').exists()
        # Counter-examples at the run entry: no operator authorization, or outside the window, refuses before anything runs.
        without = [a for i, a in enumerate(argv) if a != '--operator-authorization-file' and (i == 0 or argv[i - 1] != '--operator-authorization-file')]
        for args, at, code in ((without, local_noon, 'authorization_missing'), (argv, local_noon + timedelta(hours=8), 'outside_trial_window')):
            assert R.main(args, environ=opened, connect=connect, operator=operator, test_stack=True, provider_routes_file=routes, clock=lambda at=at: at) == 3
            assert not (out / 'state.json').exists() and code in refused_codes(capsys)
        # --preflight (calibration 2026-10-17): every start check against the live stack, then exit; nothing written, nothing sent,
        # no prompt (the operator is never called), the enterprise conformance not run (it would create effects on the endpoint).
        prompts = len(operator.prompts)
        pre_out = tmp_path / 'preflight-trial'
        pre = [*argv, '--preflight']
        pre[pre.index('--out') + 1] = str(pre_out)
        assert R.main(pre, environ=opened, connect=connect, operator=operator, test_stack=True, provider_routes_file=routes,
                      clock=lambda: local_noon) == 0
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert line == {'ok': True, 'preflight': True, 'refused': [], 'not_run': ['enterprise_endpoint_conformance'], 'trial_id': TRIAL_ID}
        assert not pre_out.exists() and len(operator.prompts) == prompts
        # The same preflight sees a delivery it cannot reach and an enterprise provider file it cannot read (not private).
        loose = tmp_path / 'enterprise-provider-loose'
        loose.write_text(enterprise_route.read_text())
        loose.chmod(0o644)
        dead = private(tmp_path, 'webchat-provider-dead', json.dumps({**json.loads(webchat_route.read_text()), 'origin': f'https://127.0.0.1:{free_port()}'}))
        bad = list(pre)
        bad[bad.index('--enterprise-provider-file') + 1], bad[bad.index('--webchat-provider-file') + 1] = str(loose), str(dead)
        assert R.main(bad, environ=opened, connect=connect, operator=operator, test_stack=True, provider_routes_file=routes, clock=lambda: local_noon) == 3
        assert {'enterprise_provider_unreadable', 'webchat_delivery_unavailable'} <= set(refused_codes(capsys)) and not pre_out.exists()
        first = R.main(argv, environ=opened, connect=connect, operator=operator, test_stack=True, provider_routes_file=routes, clock=lambda: local_noon)
        assert operator.errors == [], operator.errors[-1]
        state = json.loads((out / 'state.json').read_text())
        assert operator.errors == [], operator.errors
        assert first == 1 and state['status'] == 'paused' and state['rules'] == ['calibration'] and state['at_seq'] == 3, (state, pump.failures)
        second = R.main([*argv, '--resume', 'owner-confirmation:nx037-five'], environ=opened, connect=connect, operator=operator, test_stack=True,
                        provider_routes_file=routes, clock=lambda: local_noon)
        state = json.loads((out / 'state.json').read_text())
        assert second == 0 and state['status'] == 'completed' and state['recorded'] == 5, (state, pump.failures)
        assert pump.failures == [], pump.failures
        # The customer's view: the recorder writes a delivered reply into the stream after the driver saw its intent settle.
        for kind in ORDER:
            operator.wait_reply(operator.sent[kind], deadline=30)
        staff_view = operator.messages()
        assert pump.failures == [], pump.failures

    records = {r['kind']: r for r in X.read_records(out)}
    assert [r['seq'] for r in X.read_records(out)] == [1, 2, 3, 4, 5] and operator.prompts == list(enumerate(ORDER, 1))
    detail = {k: (r['outcome'], r['error_code'], r['intents'], r['reconciliation'], r['facts'], r['refusals']) for k, r in records.items()}
    # Every kind: completed by the Run, settled, no stop rule fact, test stack, cost in CNY within the per-Run cap.
    for kind, r in records.items():
        assert r['outcome'] == 'ok' and r['run_id'] and r['test_stack'] is True, detail
        assert r['decision']['action'] in ('continue', 'pause') and r['cost']['currency'] == 'CNY' and r['cost'].get('other_currencies') is None, detail
        assert all(r['facts'][fact] == 0 for fact in ('unauthorized_sends', 'wrong_recipient_sends', 'contacts_after_refusal', 'cross_tenant_reads')), detail
        assert r['facts']['max_executions_per_intent'] == 1, detail
    # Reply arrives: one governed reply per customer message of the trial.
    replies = {kind: [m for m in staff_view if m.get('direction') == 'outbound' and m.get('reply_to_message_id') == operator.sent[kind]] for kind in ORDER}
    assert all(len(v) == 1 and v[0]['sender_kind'] == 'agent' for v in replies.values()), replies
    for kind in ('ordinary_reply', 'human_takeover', 'restricted_after_refusal'):
        assert records[kind]['intents'] == {'fulfilled': 1} and records[kind]['reconciliation'] == 'settled', detail
    # Enterprise delivery: two fulfilled intents (reply and delivery); the ticket is stored at the endpoint, POSTed once.
    tickets = {row[0]: row for row in endpoint['store'].listing()}
    for kind in ('real_delivery', 'unknown_reconciliation'):
        assert records[kind]['intents'] == {'fulfilled': 2} and records[kind]['reconciliation'] == 'settled', detail
        [deliver] = admin.execute("select intent_id::text from runtime.nexloop_effect_intents where tenant_id=%s and action_name='nexloop.enterprise.deliver' "
                                  "and intent_id::text = any(%s)", (tenant, records[kind]['intent_ids'])).fetchall()
        assert deliver[0] in tickets and json.loads(tickets[deliver[0]][2])['consumer_ref'] == 'consumer:' + person['consumer']
        assert [m for m, i in endpoint['seen'] if i == deliver[0]].count('POST') == 1, endpoint['seen']
        records[kind]['deliver'] = deliver[0]
    # The start guard's conformance checks also created marked effects on the endpoint (their documented behaviour); the trial's
    # own tickets are the two of this consumer.
    assert sum(json.loads(row[2]).get('consumer_ref') == 'consumer:' + person['consumer'] for row in tickets.values()) == 2
    assert (out / 'preflight/endpoint-conformance.json').exists()
    # The unknown result: the worker recorded unknown, then queried (GET) and reconciled, never re-sent.
    unknown = records['unknown_reconciliation']['deliver']
    assert [m for m, i in endpoint['seen'] if i == unknown] == ['POST', 'GET'], endpoint['seen']
    assert [m for m, i in endpoint['seen'] if i == records['real_delivery']['deliver']] == ['POST'], endpoint['seen']
    history = [s for (s,) in admin.execute("select event_kind from runtime.nexloop_effect_events where intent_id::text=%s order by created_at", (unknown,)).fetchall()]
    assert 'unknown' in history and history[-1] == 'fulfilled', history
    # Takeover: no Agent reply while a person held the conversation; the staff reply is the answer; handed back.
    # Multi-turn observation (0196): both customer messages of the takeover belong to it; the one sent during the takeover has
    # no Run and settled on its staff reply.
    assert records['human_takeover']['messages'] == 2 and len(records['human_takeover']['run_ids']) == 1
    assert all(r['messages'] == 1 for k, r in records.items() if k != 'human_takeover')
    during = [m for m in staff_view if m.get('reply_to_message_id') == operator.takeover['during']]
    assert [(m['sender_kind'], m['id']) for m in during] == [('human_takeover', operator.takeover['staff'])], during
    # Refusal: the consumer is restricted by the refusing message itself; its one bound reply still went out (ADR-023 §2.6).
    assert admin.execute('select active,message_id from control.nexloop_contact_restrictions where tenant_id=%s', (tenant,)).fetchone() == (True, operator.sent['restricted_after_refusal'])
    # Budget and currency: the D-g budget is CNY; every record and the trial state count CNY.
    assert admin.execute("select unit,limit_amount from control.nexloop_budget_limits where tenant_id=%s and budget_kind='model'", (tenant,)).fetchone() == ('CNY', 100)
    assert state['currency'] == 'CNY' and state['test_stack'] is True
    # Evidence pack: generated from the trial, marked test stack, never real evidence.
    assert E.main(['--out', str(tmp_path / 'evidence'), '--trial', str(out)]) == 0
    index = json.loads((tmp_path / 'evidence' / 'index.json').read_text())
    assert index['test_stack'] is True and index['real_evidence'] is False and index['items']['interactions']['records'] == 5
    summary = json.loads((tmp_path / 'evidence' / 'interactions' / 'summary.json').read_text())
    assert summary['by_kind'] == {k: 1 for k in X.KINDS} and summary['cost']['currency'] == 'CNY' and summary['status'] == 'completed'
    assert E.check(tmp_path / 'evidence') == []
    # D3: the evidence names the participant by consumer id and consent sha256 only.
    assert json.loads((tmp_path / 'evidence/interactions/participants.json').read_text()) == [
        {'consumer_id': person['consumer'], 'kind': 'agent', 'persona': 'simulated_agent'}]
    # Persona: every record, the state and the evidence summary say simulated_agent (the customers were agents; the system real).
    assert all(r['persona'] == 'simulated_agent' for r in records.values()) and state['persona'] == 'simulated_agent' and summary['persona'] == 'simulated_agent'
    # Counted cost: every record keeps the trigger Run's cost and the tenant's actual increase over the interaction.
    assert all({'period_increase', 'counted'} <= set(r['cost']) for r in records.values())
