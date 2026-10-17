"""NX-037 real driver end to end on the synthetic stack: the actual API (TLS, 127.0.0.1) with the built WebChat, the message
relay, a deterministic Agent Host with its runtime worker and guard, the local JSON delivery and the effect worker, all on the
assembled synthetic tenant (local_message_assembly_fixture). The person who plays the customer in the real trial is an
automated operator here: headless Chromium driving the WebChat (apps/web/test/browser/operator.browser.mjs).

The RealDriver itself sends nothing: it prompts the operator, then watches through the metrics role (0172) for the Run and its
effect, and the executor applies the stop and pause rules. A five interaction plan:
  1 completed; 2 the operator does not act -> paused (interaction_timeout); resume with the owner's confirmation;
  3 completed; 4 a critical alert fires during the interaction (injected) -> stopped; 5 never runs.
Opt-in browser lane (marked real_browser, run with -m real_browser), Node 24, run serially."""
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from nexloop_eios import controlled_interactions as X
from nexloop_eios import interaction_drivers as D
from test_agent_host import files, free_port
from test_local_message_delivery_assembly import process, reaped_host, wait_live
from test_message_runtime_effect_e2e import trusted_host_evidence
from test_runtime_effect_tools import effect_configuration
from test_sse_real_browser import build_web
from test_trusted_configuration_pg import private

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.real_browser
TRIAL_ID = '37c00000-0000-4000-8000-000000000001'
TEXT = 'NX-037 operator interaction {seq}'


class Pump:
    """The background services of the chain, one tick each in turn (as their --once entries), until stopped."""

    def __init__(self, commands, hidden):
        import sys
        self.commands, self.hidden, self.python = commands, hidden, sys.executable
        self.stop, self.failures, self.ticks = threading.Event(), [], 0
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        while not self.stop.is_set():
            for module, args in self.commands:
                run = subprocess.run([self.python, '-m', module, *args, '--once'], capture_output=True, text=True, timeout=120,
                                     env={k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))})
                if any(h in run.stdout + run.stderr for h in self.hidden):
                    self.failures.append(('secret_in_output', module))
                if run.returncode != 0:
                    self.failures.append((module, run.returncode))
            self.ticks += 1
            self.stop.wait(.5)


class AutoOperator:
    """The automated person on the customer side: forwards each prompt to the browser script, which sends the message in the
    WebChat (or not, for the interactions listed in `absent`). `before` runs a test action right after a prompt."""

    def __init__(self, node, absent=(), before=None):
        self.node, self.absent, self.before, self.prompts = node, set(absent), before or {}, []

    def prompt(self, interaction, instruction, deadline_seconds):
        seq = interaction['seq']
        self.prompts.append((seq, interaction['kind']))
        if seq in self.before:
            self.before[seq]()
        self.node.stdin.write(json.dumps({'seq': seq, 'act': seq not in self.absent, 'text': TEXT.format(seq=seq)}) + '\n')
        self.node.stdin.flush()
        answer = json.loads(self.node.stdout.readline())
        assert answer == {'seq': seq, 'sent': seq not in self.absent}, answer


def test_real_driver_with_an_automated_operator_on_the_synthetic_stack(assembled_message, admin, tmp_path):  # noqa: F811
    f = assembled_message
    o = f['original']
    p = o['paths']
    tenant = o['tenant']
    credentials = f['credential_files']
    trusted_host_evidence()
    web = tmp_path / 'web'
    build_web(web)
    common = ['--signing-key-file', str(p['backend_signing']), '--signing-key-id', 'explicit-configuration']
    api_dsn = private(tmp_path, 'api-dsn', make_conninfo(o['pg'], user='nexloop_api'))
    domain_dsn = private(tmp_path, 'domain-dsn', make_conninfo(o['pg'], user='nexloop_domain_worker'))
    effect_dsn = private(tmp_path, 'effect-dsn', make_conninfo(o['pg'], user='nexloop_action_worker'))
    metrics_dsn = private(tmp_path, 'metrics-dsn', make_conninfo(o['pg'], user='nexloop_metrics'))
    api_tls = tmp_path / 'api-tls'
    api_tls.mkdir(mode=0o700)
    files(api_tls)
    port = free_port()
    origin = f'https://127.0.0.1:{port}'
    rate = private(tmp_path, 'rate-key', secrets.token_hex(32))
    api_args = ['--database-url-file', str(api_dsn), *common, '--artifact-root', str(tmp_path / 'api-artifacts'), '--mode', 'test', '--port', str(port),
                '--tls-certificate-file', str(api_tls / 'host-cert.pem'), '--tls-key-file', str(api_tls / 'host-key.pem'),
                '--identity-database-url-file', str(p['identity']), '--browser-rate-key-file', str(rate), '--browser-tenant-id', tenant,
                '--browser-application-id', o['application'], '--browser-origin', origin, '--web-root', str(web)]
    hidden = list(f['tokens'].values()) + [p['password'].read_text()]
    with contextlib.ExitStack() as stack:
        api = stack.enter_context(process('nexloop_eios.http_api', api_args, hidden))
        with httpx.Client(base_url=origin, verify=str(api_tls / 'host-cert.pem'), trust_env=False, timeout=5) as client:
            wait_live(api, client)
            login = client.post('/api/v1/auth/login', headers={'Origin': origin}, json={'username': 'explicit-user', 'password': p['password'].read_text()})
            assert login.status_code == 200
            created = client.post('/api/v1/conversations', headers={'Origin': origin, 'X-CSRF-Token': login.json()['csrf_token'],
                                                                    'Idempotency-Key': 'nx037-real-driver-conversation'}, json={})
            assert created.status_code == 200
            conversation = created.json()['id']
        # The chain: relay (route, source, planner, executor), the deterministic Host with its runtime worker, the JSON delivery
        # provider and the effect worker. The recipe stays valid for the whole run.
        recipe = dict(f['recipe'], valid_until=(datetime.now(UTC) + timedelta(seconds=900)).isoformat())
        recipe_file = private(tmp_path, 'relay-recipe', json.dumps(recipe))
        vault = tmp_path / 'relay-vault'
        vault.mkdir(mode=0o700)
        relay = ['--database-url-file', str(api_dsn), *common, '--artifact-root', str(tmp_path / 'relay-artifacts'), '--vault-root', str(vault),
                 '--recipe-file', str(recipe_file)]
        relay += sum((['--' + name + '-credential-file', str(credentials['assembly-' + label])]
                      for name, label in [('route', 'route'), ('source', 'source'), ('planner', 'planner'), ('executor', 'executor')]), [])
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
        cfg.update(deterministic_message_from_input=True, context_input_protocol='nexloop.context-pack.v2')
        host_config.write_text(json.dumps(cfg))
        _, host_client, _ = stack.enter_context(reaped_host(runtime, host_key, host_config))
        worker = ['--database-url-file', str(domain_dsn), *common, '--service-credential-file', str(credentials['assembly-runtime-worker']),
                  '--artifact-root', str(tmp_path / 'runtime-worker-artifacts'), '--world', 'real', '--queue', 'operations',
                  '--host-origin', str(host_client.base_url).rstrip('/'), '--host-control-key-file', str(host_key), '--host-ca-file', str(host_tls / 'host-cert.pem'),
                  '--guard-port', str(guard_port), '--guard-key-file', str(guard_key), '--guard-certificate-file', str(guard_tls / 'host-cert.pem'),
                  '--guard-tls-key-file', str(guard_tls / 'host-key.pem'), '--total-timeout', '20']
        delivery_tls = tmp_path / 'delivery-tls'
        delivery_tls.mkdir(mode=0o700)
        _, provider_key = files(delivery_tls)
        delivery_origin = f'https://127.0.0.1:{free_port()}'
        delivery_root = tmp_path / 'json-products'
        delivery_root.mkdir(mode=0o700)
        provider_config = private(tmp_path, 'provider-config', json.dumps({'origin': delivery_origin, 'credential_file': str(provider_key),
                                                                            'ca_file': str(delivery_tls / 'host-cert.pem'), 'timeout': 3}))
        delivery = ['--database-url-file', str(effect_dsn), *common, '--service-credential-file', str(credentials['assembly-executor']),
                    '--artifact-root', str(tmp_path / 'delivery-artifacts'), '--delivery-root', str(delivery_root), '--origin', delivery_origin,
                    '--certificate-file', str(delivery_tls / 'host-cert.pem'), '--tls-key-file', str(delivery_tls / 'host-key.pem'),
                    '--provider-ca-file', str(delivery_tls / 'host-cert.pem'), '--provider-credential-file', str(provider_key)]
        stack.enter_context(process('nexloop_eios.local_json_delivery', delivery, hidden + [provider_key.read_text()]))
        executor = ['--database-url-file', str(effect_dsn), *common, '--service-credential-file', str(credentials['assembly-executor']),
                    '--artifact-root', str(tmp_path / 'effect-worker-artifacts'), '--world', 'real', '--provider-config-file', str(provider_config)]
        pump = Pump([('nexloop_eios.message_relay_cli', relay), ('nexloop_eios.runtime_worker', worker), ('nexloop_eios.effect_worker', executor)],
                    hidden + [provider_key.read_text()])
        pump.thread.start()
        stack.callback(lambda: (pump.stop.set(), pump.thread.join(timeout=180)))

        # The automated operator: Chromium on the WebChat, logged in as the synthetic customer.
        session = private(tmp_path, 'operator-session', json.dumps({'origin': origin, 'username': 'explicit-user', 'password_file': str(p['password']),
                                                                    'conversation_id': conversation}))
        node = subprocess.Popen([shutil.which('node'), 'test/browser/operator.browser.mjs'], cwd=ROOT / 'apps/web', text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**{k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))},
                                     'NEXLOOP_OPERATOR_SESSION_FILE': str(session)})
        stack.callback(lambda: (node.stdin.close(), node.wait(timeout=30)))
        assert json.loads(node.stdout.readline()) == {'ready': True}

        def critical_alert():
            """Injected during interaction 4: a critical alert fires (as the evaluator records one)."""
            admin.execute("""insert into control.nexloop_alert_events(tenant_id,world,dedupe_key,rule_id,severity,kind,rules_version)
                values(%s,'real','synthetic-critical','synthetic_critical','critical','firing',1)""", (tenant,))

        connect = lambda path: psycopg.connect(Path(path).read_text().strip(), autocommit=True)  # noqa: E731
        operator = AutoOperator(node, absent={2}, before={4: critical_alert})
        driver = D.RealDriver(D.RealDriverConfig(channel_ref='channel:webchat:synthetic', owner_session_file=private(tmp_path, 'owner-session', 'synthetic')),
                              observer=D.PgObserver(connect, metrics_dsn, tenant), operator=operator, timeout_seconds=40, poll_seconds=.5)

        class Collector:
            def collect(self, run_id):
                with connect(metrics_dsn) as db:
                    return X.PgCollector(db, tenant).collect(run_id)

        config = json.loads((ROOT / 'deploy/configuration/nx037-trial.v1.json').read_text())
        config.update(calibration_interactions=0, plan={**config['plan'], 'total': 5,
                                                        'mix': {'ordinary_reply': 4, 'real_delivery': 1, 'restricted_after_refusal': 0, 'human_takeover': 0, 'unknown_reconciliation': 0}})
        trial = X.validate_plan(config)
        # 10/17 calibration: interaction 3 is a real_delivery whose Run only replies (this stack offers no enterprise delivery).
        order = ['ordinary_reply', 'ordinary_reply', 'real_delivery', 'ordinary_reply', 'ordinary_reply']
        out = tmp_path / 'trial'
        first = X.run(trial, driver, Collector(), out, trial_id=TRIAL_ID, order=order)
        assert first['status'] == 'paused' and first['rules'] == ['interaction_timeout'] and first['at_seq'] == 2, first
        with pytest.raises(X.TrialPaused):
            X.run(trial, driver, Collector(), out, trial_id=TRIAL_ID, order=order)
        second = X.run(trial, driver, Collector(), out, trial_id=TRIAL_ID, order=order, resume='owner-confirmation:nx037-e2e')
        assert second['status'] == 'paused' and second['rules'] == ['effect_evidence_missing'] and second['at_seq'] == 3, second
        third = X.run(trial, driver, Collector(), out, trial_id=TRIAL_ID, order=order, resume='owner-confirmation:nx037-e2e-2')
        assert third['status'] == 'stopped' and third['rules'] == ['critical_alert'] and third['at_seq'] == 4, third
        assert pump.failures == [], pump.failures

    records = X.read_records(out)
    assert [r['seq'] for r in records] == [1, 2, 3, 4] and operator.prompts == [(1, 'ordinary_reply'), (2, 'ordinary_reply'), (3, 'real_delivery'), (4, 'ordinary_reply')]
    # The real_delivery that only replied: never ok, named, paused (its Run itself succeeded).
    assert records[2]['outcome'] == 'error' and records[2]['error_code'] == 'delivery_missing' and records[2]['run_id']
    assert records[2]['decision'] == {'action': 'pause', 'rules': ['effect_evidence_missing']}
    done = [records[0], records[2], records[3]]
    assert all((r['outcome'] == 'ok' or r['seq'] == 3) and r['run_id'] and r['intents'] == {'fulfilled': 1} and len(r['intent_ids']) == 1 for r in done), [(r['seq'], r['outcome'], r['error_code'], bool(r['run_id']), r['intents'], len(r['intent_ids']), r['reconciliation'], r['facts']) for r in done]
    assert len({r['run_id'] for r in done}) == 3 and all(r['reconciliation'] == 'settled' for r in done)
    assert all(r['facts']['unauthorized_sends'] == 0 and r['facts']['wrong_recipient_sends'] == 0 and r['facts']['contacts_after_refusal'] == 0
               and r['facts']['max_executions_per_intent'] == 1 and r['facts']['cross_tenant_reads'] == 0 for r in done)
    assert records[1]['error_code'] == 'interaction_timeout' and records[1]['decision'] == {'action': 'pause', 'rules': ['interaction_timeout']}
    assert records[3]['facts']['critical_alerts'] == 1 and records[3]['decision'] == {'action': 'stop', 'rules': ['critical_alert']}
    resume, again = X.read_resumes(out)
    assert resume['after_seq'] == 2 and resume['confirmation_ref'] == 'owner-confirmation:nx037-e2e' and again['after_seq'] == 3
    assert len(list(delivery_root.glob('*.export.json'))) == 3  # one governed delivery per completed interaction, none for 2
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s', (tenant,)).fetchone() == (3,)
    # 0207: every interaction message names its sender (the conversation's Consumer), so the driver can check the planned account.
    listed = admin.execute("select runtime.nexloop_nx037_interaction_messages(%s,'real','-infinity')", (tenant,)).fetchone()[0]
    consumers = {c for (c,) in admin.execute('select consumer_id from runtime.nexloop_conversations where tenant_id=%s', (tenant,)).fetchall()}
    assert listed and all(m['consumer_id'] in consumers for m in listed)
