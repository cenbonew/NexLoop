"""NX-031 slice 1: one cell's service stack on a clone of the load template (design §3, results contract §4).

Every disposable resource lives under the cell's lane directory: the cloned cluster (its socket directory is short, see
tests/load_template.Clone), private files (0600), Artifact roots and each process's working directory. Services start with
their deployed process counts and pools (deploy/stage/connection-budget.v1.json) and their own credentials from the template
(service-grants manifest). REAL_DISPATCH_ENABLED=false; the model is the deterministic test profile.

Started here: the API (2 processes, loopback HTTPS, browser and workbench realms); when the cell has `background`, the background
services except the plan reevaluator (the load plans are data only, dispatcher 2026-10-11 option B: no plan Run); and the message
Run chain (one multi-consumer relay, the runtime worker with four guard processes and one dispatch loop per Host slot, the Agent
Host with the D5 injected model latency, the local JSON delivery provider, the effect worker and the outbound recorder).
"""
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
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
        self.chain = json.loads((self.template / 'private/chain.json').read_text())
        self.tenant = self.chain['tenant']

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

    def _spawn(self, name, command, env=None, inherit=True):
        work = self.lane / 'work' / name
        work.mkdir(parents=True)
        log = open(work / 'process.log', 'w')
        self.children[name] = (subprocess.Popen(command, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                                env={**(os.environ if inherit else {}), **(env or {})}), log)

    # -- API ----------------------------------------------------------------------------------------------------------
    def tls(self, name):
        """A one-day self-signed loopback certificate and key in a private directory (cert host-cert.pem, key host-key.pem)."""
        directory = self.private / name
        directory.mkdir(mode=0o700)
        cert, key = directory / 'host-cert.pem', directory / 'host-key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        cert.chmod(0o600)
        key.chmod(0o600)
        return cert, key

    @property
    def signing(self):
        if not (self.private / 'chain-signing-key').exists():
            self.file('chain-signing-key', (self.template / 'private/backend-signing-key').read_bytes())
        return self.private / 'chain-signing-key', self.chain['signing_key_id']

    def start_api(self, processes=2):
        key, key_id = self.signing
        cert, tls_key = self.tls('api-tls')
        self.ca_file = cert
        api_dsn = self.file('api-dsn', self.dsn('nexloop_api'))
        identity_dsn = self.file('identity-dsn', self.dsn('nexloop_identity'))
        rate = self.file('rate-key', secrets.token_hex(32))
        (self.lane / 'api-artifacts').mkdir(mode=0o700)
        for n in range(processes):
            port = free_port()
            origin = f'https://127.0.0.1:{port}'
            self._spawn(f'api-{n + 1}', [sys.executable, '-m', 'nexloop_eios.http_api', '--database-url-file', str(api_dsn), '--signing-key-file', str(key),
                '--signing-key-id', key_id, '--artifact-root', str(self.lane / 'api-artifacts'), '--mode', 'test', '--port', str(port),
                '--execution-profile', 'deterministic-test', '--tls-certificate-file', str(cert), '--tls-key-file', str(tls_key),
                '--identity-database-url-file', str(identity_dsn), '--browser-rate-key-file', str(rate), '--browser-tenant-id', self.tenant,
                '--browser-application-id', self.chain['browser_application'], '--browser-origin', origin, '--workbench-application-id', 'synthetic-workbench-app'])
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
    # The load plans are data only (dispatcher 2026-10-11, option B): no plan reevaluator, so no plan Run starts in a cell.
    CELL_BACKGROUND = tuple(s for s in BACKGROUND if s != 'plan-reevaluator')

    def start_background(self, services=CELL_BACKGROUND):
        tokens = json.loads((self.template / 'private/service-tokens.json').read_text())
        principals = json.loads((self.template / 'private/service-principals.json').read_text())
        signing = self.file('deploy-signing-key', bytes.fromhex((self.template / 'private/deploy-signing-key').read_text().strip()))
        (self.lane / 'artifacts').mkdir(mode=0o700, exist_ok=True)
        for service in services:
            principal = principals[ROLE_OF[service]]
            args = ['--database-url-file', str(self.file(f'{service}-dsn', self.dsn(principal['database_role']))), '--signing-key-file', str(signing),
                    '--signing-key-id', 'load-deploy', '--service-credential-file', str(self.file(f'{service}-credential', tokens[principal['credential_reference']])),
                    '--artifact-root', str(self.lane / 'artifacts'), '--world', 'real', '--tick-seconds', '1']
            args += self._service_options(service, tokens, principals)
            self._spawn(service, [sys.executable, '-c', ENTRY, service, *args])
        self._wait_ready(services)

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
            # Their launchers run as the chain's relay identities on the API role (multi-consumer relay: run-profile mode).
            api = self.file(f'{service}-api-dsn', self.dsn('nexloop_api'))
            tokens = self.chain['tokens']
            if service == 'plan-reevaluator':
                options = ['--settings-file', str(cfg / 'plan-reevaluation.v1.json'), '--api-database-url-file', str(api), '--effect-action', 'eios:action:nexloop.service.request:1']
                names = {'source': 'source', 'planner': 'planner', 'queue': 'route', 'executor': 'executor'}
            else:
                # The deployed reply policy with the fallback Run on the lane's deterministic Host (the deployed file names the real model
                # profile; the published reply window and start time are unchanged).
                policy = json.loads((cfg / 'reply-guarantee.v1.json').read_text())
                policy['fallback']['runtime_profile'] = 'deterministic-test'
                options = ['--policy-file', str(self.file('reply-guarantee.lane.json', json.dumps(policy))), '--api-database-url-file', str(api),
                           '--run-profile-file', str(self.run_profile_file())]
                names = {'route': 'route', 'source': 'source', 'planner': 'planner', 'executor': 'executor'}
            for name, token in names.items():
                options += [f'--{name}-credential-file', str(self.file(f'{service}-{name}-credential', tokens[token]))]
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
    MESSAGE_CHAIN = {'relay': {'processes': 1, 'pool_max': 2}, 'runtime_worker': {'guard_workers': 4, 'dispatch_loops': 'host_concurrency'}, 'agent_host': {'processes': 1},
                     'effect_worker': {'processes': 1, 'pool_max': 2}, 'outbound_recorder': {'processes': 1, 'pool_max': 2}}

    def run_profile_file(self):
        path = self.private / 'run-profile.json'
        if not path.exists():
            self.file('run-profile.json', json.dumps(self.chain['run_profile']))
        return path

    def start_message_chain(self, host_concurrency, latency=None):
        """One multi-consumer relay (pool 2), the runtime worker with four guard processes (pool 4 each), one Agent Host with
        maximum_active_runs = the cell's host concurrency (load-lane configuration, never through the doctor; C8 is 8), the
        local JSON delivery provider, the effect worker and the outbound recorder. Returns None once all are up. `latency`
        ({median_ms, p95_ms, seed}) is the Host's D5 injected model latency (deterministic-test only); None injects none."""
        key, key_id = self.signing
        tokens = self.chain['tokens']
        token = {name: self.file(f'chain-{name}-credential', value) for name, value in tokens.items()}
        recorder = token['recorder']  # published with the recorder's Action (the service-grants apply keeps that credential)
        common = ['--signing-key-file', str(key), '--signing-key-id', key_id]
        for name in ('host-runtime', 'runtime-artifacts', 'relay-artifacts', 'relay-vault', 'delivery-artifacts', 'delivery-root', 'effect-artifacts', 'recorder-artifacts'):
            (self.lane / name).mkdir(mode=0o700)
        # Agent Host (deterministic profile with the D5 injected model latency of the matrix).
        host_cert, host_tls_key = self.tls('host-tls')
        host_key = self.file('host-internal-key', secrets.token_hex(32))
        guard_cert, guard_tls_key = self.tls('guard-tls')
        guard_key = self.file('guard-key', secrets.token_hex(32))
        guard_port, host_port = free_port(), free_port()
        config = self.file('host-runtime-config.json', json.dumps({'guard_url': f'https://127.0.0.1:{guard_port}/internal/v1/runtime/authorize',
            'guard_key_file': str(guard_key), 'guard_ca_file': str(guard_cert), 'runtime_profile': 'deterministic-test', 'effect_tools': True,
            # One Host for every Run kind (NX-031 slice 3): message Runs (v2), fallback replies and plan reevaluations (v6), each by the
            # protocol its own start input declares.
            'deterministic_protocols': True, 'plan_outcome_tool': True, 'maximum_active_runs': host_concurrency,
            **({'deterministic_latency': latency} if latency else {})}))
        node = shutil.which('node')
        if node is None or not (ROOT / 'apps/agent-host/dist/main.js').is_file():
            raise StackFailed('agent-host')
        host_env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))}
        self._spawn('agent-host', [sys.executable, str(ROOT / 'scripts/agent_host.py'), '--node', node, '--runtime-root', str(self.lane / 'host-runtime'),
            '--internal-key-file', str(host_key), '--port', str(host_port), '--tls-certificate-file', str(host_cert), '--tls-key-file', str(host_tls_key),
            '--runtime-config-file', str(config)], env=host_env, inherit=False)
        self._wait_https(f'https://127.0.0.1:{host_port}/health/live', host_cert, 'agent-host')
        # Runtime worker: four guard processes (ADR-024 deployment shape) and one dispatch loop per Host slot (NX-031 --dispatch-loops).
        runtime_dsn = self.file('runtime-dsn', self.dsn('nexloop_domain_worker'))
        self._spawn('runtime-worker', [sys.executable, '-m', 'nexloop_eios.runtime_worker', '--database-url-file', str(runtime_dsn),
            *common, '--service-credential-file', str(token['runtime_worker']), '--artifact-root', str(self.lane / 'runtime-artifacts'), '--world', 'real',
            '--queue', self.chain['run_profile']['queue'], '--host-origin', f'https://127.0.0.1:{host_port}', '--host-control-key-file', str(host_key),
            '--host-ca-file', str(host_cert), '--guard-port', str(guard_port), '--guard-key-file', str(guard_key), '--guard-certificate-file', str(guard_cert),
            '--guard-tls-key-file', str(guard_tls_key), '--guard-workers', '4', '--dispatch-loops', str(host_concurrency)], env={'NEX_EIOS_DB_POOL_MAX': '4'})
        # Local JSON delivery provider and the effect worker that dispatches to it.
        delivery_cert, delivery_key = self.tls('delivery-tls')
        provider_key = self.file('provider-key', secrets.token_hex(32))
        origin = f'https://127.0.0.1:{free_port()}'
        action_dsn = self.file('action-dsn', self.dsn('nexloop_action_worker'))
        self._spawn('delivery', [sys.executable, '-m', 'nexloop_eios.local_json_delivery', '--database-url-file', str(action_dsn), *common,
            '--service-credential-file', str(token['executor']), '--artifact-root', str(self.lane / 'delivery-artifacts'), '--delivery-root', str(self.lane / 'delivery-root'),
            '--certificate-file', str(delivery_cert), '--tls-key-file', str(delivery_key), '--provider-ca-file', str(delivery_cert),
            '--provider-credential-file', str(provider_key), '--origin', origin])
        # Provider timeout 10 s, the most the effect worker's 30 s lease allows (lease / 3): under a loaded host the local JSON
        # delivery's fsync can pass 3 s, and an unknown result then waits for an explicit query (by design, never a blind resend).
        provider = self.file('provider-config.json', json.dumps({'origin': origin, 'credential_file': str(provider_key), 'ca_file': str(delivery_cert), 'timeout': 10}))
        self._spawn('effect-worker', [sys.executable, '-m', 'nexloop_eios.effect_worker', '--database-url-file', str(action_dsn), *common,
            '--service-credential-file', str(token['executor']), '--artifact-root', str(self.lane / 'effect-artifacts'), '--world', 'real',
            '--provider-config-file', str(provider)])
        # One multi-consumer relay (run profile) and the outbound recorder.
        api_dsn = self.file('chain-api-dsn', self.dsn('nexloop_api'))
        self._spawn('relay', [sys.executable, '-m', 'nexloop_eios.message_relay_cli', '--database-url-file', str(api_dsn), *common,
            '--artifact-root', str(self.lane / 'relay-artifacts'), '--vault-root', str(self.lane / 'relay-vault'), '--run-profile-file', str(self.run_profile_file()),
            *sum(([f'--{name}-credential-file', str(token[name])] for name in ('route', 'source', 'planner', 'executor')), [])])
        self._spawn('outbound-recorder', [sys.executable, '-m', 'nexloop_eios.outbound_messages', '--database-url-file', str(api_dsn), *common,
            '--service-credential-file', str(recorder), '--artifact-root', str(self.lane / 'recorder-artifacts'), '--world', 'real'])
        time.sleep(3)
        dead = [name for name in ('runtime-worker', 'delivery', 'effect-worker', 'relay', 'outbound-recorder') if self.children[name][0].poll() is not None]
        if dead:
            raise StackFailed(','.join(dead))
        return None

    def _wait_https(self, url, ca, name, timeout=30):
        import httpx
        deadline = time.monotonic() + timeout
        while True:
            try:
                if httpx.get(url, verify=str(ca), timeout=2, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if self.children[name][0].poll() is not None or time.monotonic() > deadline:
                raise StackFailed(name)
            time.sleep(0.2)

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
