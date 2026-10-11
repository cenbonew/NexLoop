"""NX-031 slice 1: one cell's service stack on a clone of the load template (design §3, results contract §4).

Every disposable resource lives under the cell's lane directory: the cloned cluster (its socket directory is short, see
tests/load_template.Clone), private files (0600), Artifact roots and each process's working directory. Services start with
their deployed process counts and pools (deploy/stage/connection-budget.v1.json) and their own credentials from the template
(service-grants manifest). REAL_DISPATCH_ENABLED=false; the model is the deterministic test profile.

Started here: the API (2 processes, loopback HTTPS, browser and workbench realms) and, when the cell has `background`, the ten
background services. The message Run chain (relay, runtime worker with four guard processes, Agent Host, effect worker,
outbound recorder) is not started: the relay serves one consumer per process (recipe consumer_id), which cannot carry 1000
consumers; that is reported to the dispatcher and the cell records `message_runs: "not_started"` (runs.jsonl stays empty, so
the report marks the decomposition missing; nothing is faked).
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
TENANT = 'synthetic-a'
BACKGROUND = ('claim-extraction-scheduler', 'claim-extraction-worker', 'claim-matcher', 'recall-indexer', 'plan-reevaluator', 'reply-guarantor',
              'commitment-keeper', 'commercial-recorder', 'retention-keeper', 'alert-evaluator')
ROLE_OF = {'claim-extraction-scheduler': 'claim_extraction_scheduler', 'claim-extraction-worker': 'claim_extraction_worker', 'claim-matcher': 'claim_matcher',
           'recall-indexer': 'recall_indexer', 'plan-reevaluator': 'plan_reevaluator', 'reply-guarantor': 'reply_guarantor', 'commitment-keeper': 'commitment_keeper',
           'commercial-recorder': 'commercial_recorder', 'retention-keeper': 'retention_keeper', 'alert-evaluator': 'alert_evaluator'}
ENTRY = 'import sys\nfrom nexloop_eios.background_services import main_for\nsys.exit(main_for(sys.argv[1],sys.argv[2:]))'


class StackFailed(RuntimeError):
    pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Stack:
    def __init__(self, clone, template, lane, cell):
        self.clone, self.template, self.lane, self.cell = clone, Path(template), Path(lane), cell
        self.private = self.lane / 'private'
        self.private.mkdir(mode=0o700, parents=True)
        self.children = {}
        self.api_origins = []
        self.ca_file = None

    # -- private files ----------------------------------------------------------------------------------------------
    def file(self, name, content):
        path = self.private / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb' if isinstance(content, bytes) else 'w') as handle:
            handle.write(content)
        return path

    def dsn(self, role):
        parts = dict(item.split('=', 1) for item in self.clone.dsn.split())
        return make_conninfo(host=parts['host'], port=parts['port'], dbname=parts['dbname'], user=role)

    @property
    def admin_dsn(self):
        return self.clone.dsn

    def _spawn(self, name, command, env=None):
        work = self.lane / 'work' / name
        work.mkdir(parents=True)
        log = open(work / 'process.log', 'w')
        self.children[name] = (subprocess.Popen(command, cwd=work, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})}), log)

    # -- API ----------------------------------------------------------------------------------------------------------
    def start_api(self, processes=2):
        signing = json.loads((self.template / 'private/signing.json').read_text())
        key = self.file('api-signing-key', bytes.fromhex(signing['material_hex']))
        cert, tls_key = self.private / 'tls-cert.pem', self.private / 'tls-key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(tls_key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        cert.chmod(0o600)
        tls_key.chmod(0o600)
        self.ca_file = cert
        api_dsn = self.file('api-dsn', self.dsn('nexloop_api'))
        identity_dsn = self.file('identity-dsn', self.dsn('nexloop_identity'))
        rate = self.file('rate-key', secrets.token_hex(32))
        (self.lane / 'api-artifacts').mkdir(mode=0o700)
        for n in range(processes):
            port = free_port()
            origin = f'https://127.0.0.1:{port}'
            self._spawn(f'api-{n + 1}', [sys.executable, '-m', 'nexloop_eios.http_api', '--database-url-file', str(api_dsn), '--signing-key-file', str(key),
                '--signing-key-id', signing['key_id'], '--artifact-root', str(self.lane / 'api-artifacts'), '--mode', 'test', '--port', str(port),
                '--execution-profile', 'deterministic-test', '--tls-certificate-file', str(cert), '--tls-key-file', str(tls_key),
                '--identity-database-url-file', str(identity_dsn), '--browser-rate-key-file', str(rate), '--browser-tenant-id', TENANT,
                '--browser-application-id', 'synthetic-browser-app', '--browser-origin', origin, '--workbench-application-id', 'synthetic-workbench-app'])
            self.api_origins.append(origin)
        self._wait_live()

    def _wait_live(self, timeout=60):
        import httpx
        deadline = time.monotonic() + timeout
        for origin in self.api_origins:
            while True:
                try:
                    if httpx.get(origin + '/health/live', verify=str(self.ca_file), timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline or any(p.poll() is not None for p, _ in self.children.values()):
                    raise StackFailed('api')
                time.sleep(0.25)

    # -- background services ------------------------------------------------------------------------------------------
    def start_background(self):
        tokens = json.loads((self.template / 'private/service-tokens.json').read_text())
        principals = json.loads((self.template / 'private/service-principals.json').read_text())
        signing = self.file('deploy-signing-key', bytes.fromhex((self.template / 'private/deploy-signing-key').read_text().strip()))
        (self.lane / 'artifacts').mkdir(mode=0o700, exist_ok=True)
        for service in BACKGROUND:
            principal = principals[ROLE_OF[service]]
            args = ['--database-url-file', str(self.file(f'{service}-dsn', self.dsn(principal['database_role']))), '--signing-key-file', str(signing),
                    '--signing-key-id', 'load-deploy', '--service-credential-file', str(self.file(f'{service}-credential', tokens[principal['credential_reference']])),
                    '--artifact-root', str(self.lane / 'artifacts'), '--world', 'real', '--tick-seconds', '1']
            args += self._service_options(service, tokens, principals)
            self._spawn(service, [sys.executable, '-c', ENTRY, service, *args])
        self._wait_ready(BACKGROUND)

    def _service_options(self, service, tokens, principals):
        cfg = ROOT / 'deploy/configuration'
        if service == 'claim-extraction-worker':
            # The load lane's rule-based extraction (nexloop_eios.load_rules, dispatcher 2026-10-11): real extraction load shape,
            # synthetic promises only, no model; only in --mode test.
            return ['--model-env-file', str(self.file(f'{service}-model.env', 'MODEL_PROVIDER=test\nMODEL_TEST_VARIANT=load-rules\n')), '--mode', 'test']
        if service == 'claim-matcher':
            return ['--model-env-file', str(self.file(f'{service}-model.env', 'MODEL_PROVIDER=test\n')),
                    '--match-config-file', str(self.file('match-config.json', json.dumps({'edit_actions': {'Consumer': ['Consumer.edit', 1]}, 'create_actions': {}})))]
        if service == 'recall-indexer':
            return ['--types-file', str(self.file('types.json', json.dumps({'Consumer': None})))]
        if service == 'commitment-keeper':
            return ['--settings-file', str(cfg / 'commitments.v1.json')]
        if service == 'commercial-recorder':
            return ['--settings-file', str(cfg / 'commercial.v1.json')]
        if service in ('plan-reevaluator', 'reply-guarantor'):
            # Their launchers start message/Role Runs: without the message Run chain (see module docstring) the launch identities are
            # placeholders that are read but never authenticated while nothing is due; due items are reported, not faked.
            api = self.file(f'{service}-api-dsn', self.dsn('nexloop_api'))
            if service == 'plan-reevaluator':
                options = ['--settings-file', str(cfg / 'plan-reevaluation.v1.json'), '--api-database-url-file', str(api), '--effect-action', 'eios:action:nexloop.service.request:1']
                names = ('source', 'planner', 'queue', 'executor')
            else:
                recipe = {'consumer_id': 'a' * 64, 'control_id': 'b' * 64, 'control_revision': 1, 'consumer_revision': 1, 'valid_until': '2099-01-01T00:00:00+00:00',
                          'role_ref': 'role:load', 'context_manifest_ref': 'artifact:load', 'runtime_profile': 'deterministic-test',
                          'budget': {'maximum_model_turns': 2, 'maximum_tool_calls': 2, 'active_timeout_seconds': 60, 'maximum_cost': '0.20', 'currency': 'USD'},
                          'runtime_owner_epoch': 1, 'queue': 'operations', 'offering_id': 'c' * 64, 'offering_binding_id': 'd' * 64}
                options = ['--policy-file', str(cfg / 'reply-guarantee.v1.json'), '--api-database-url-file', str(api),
                           '--recipe-file', str(self.file('relay-recipe.json', json.dumps(recipe)))]
                names = ('route', 'source', 'planner', 'executor')
            for name in names:
                options += [f'--{name}-credential-file', str(self.file(f'{service}-{name}-credential', 'load-launch-not-started-' + name))]
            return options
        return []

    def _wait_ready(self, names, timeout=60):
        deadline = time.monotonic() + timeout
        for name in names:
            log = self.lane / 'work' / name / 'process.log'
            while ' ready' not in (log.read_text() if log.exists() else ''):
                process, _ = self.children[name]
                if process.poll() is not None or time.monotonic() > deadline:
                    raise StackFailed(name)
                time.sleep(0.2)

    # -- message Run chain (slot reserved; dispatcher 2026-10-11, plan A: the multi-consumer relay is L2's prerequisite) ------
    MESSAGE_CHAIN = {'relay': {'processes': 1, 'pool_max': 2}, 'runtime_worker': {'guard_workers': 4}, 'agent_host': {'processes': 1},
                     'effect_worker': {'processes': 1, 'pool_max': 2}, 'outbound_recorder': {'processes': 1, 'pool_max': 2}}

    def start_message_chain(self, host_concurrency):
        """One multi-consumer relay (pool 2), the runtime worker with four guard processes, one Agent Host with
        maximum_active_runs = the cell's host concurrency (load-lane configuration, never through the doctor for C8), the effect
        worker with the loopback provider and the outbound recorder. Returns the reason it is not started while the
        multi-consumer relay is not delivered; the runner records it in cell.json and writes no Run data."""
        return 'awaiting_multi_consumer_relay'

    def alive(self):
        return {name: process.poll() is None for name, (process, _) in self.children.items()}

    def stop(self, timeout=30):
        for process, _ in self.children.values():
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + timeout
        for process, log in self.children.values():
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()
