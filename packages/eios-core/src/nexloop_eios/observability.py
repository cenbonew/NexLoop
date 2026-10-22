"""NX-030 audit metrics and alerts (docs/implementation/NX-030-design.md, D1–D8 settled; migration 0151).

PostgreSQL computes every metric (runtime.nexloop_metrics_snapshot) and every alert (authz.nexloop_alert_evaluate); this module
only signs calls, records per-process counters and renders the optional loopback text export. No new dependency: the export is
the standard library's HTTP server, the format is the Prometheus text exposition written by hand (D1).

Nothing here logs or exports raw text: metrics are codes, counts, ages and ratios; process samples are numbers only (SQL refuses
anything else).
"""
import argparse
import hashlib
import hmac
import json
import math
import re
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from nexloop_eios.postgres_artifacts import canonical_payload

RULES_SCHEMA = 'nexloop-alert-rules/1'
EVALUATE_ACTION = 'nexloop.alert.evaluate'
EVALUATE_PROTOCOL = 'nexloop-alert-evaluate-v1'
# NX-043 X5: the experiment guard runs on the same tick and grant, under its own protocol (0296).
GUARD_PROTOCOL = 'nexloop-experiment-guard-v1'
GUARD_DEADLINE_MS = 2000  # the Agent Host / provider guard deadline (runtime-host.ts, effect_provider.py)
_RULE_ID = re.compile(r'[a-z][a-z0-9_]{0,63}')
_METRIC = re.compile(r'[a-z_0-9]+(\.([a-z_0-9]+|\*))*')
_SELECTOR = re.compile(r'\*|[A-Za-z0-9_.:-]{1,64}')


class AlertRulesRejected(ValueError):
    pass


# ---- alert rules (trusted configuration, D3) -------------------------------------------------------------------------------
def validate_rules(value):
    """Same constraints as control.nexloop_configure_alert_rules, checked before connecting."""
    if type(value) is not dict or value.get('schema_version') != RULES_SCHEMA or type(value.get('version')) is not int or value['version'] < 1:
        raise AlertRulesRejected('manifest')
    rules = value.get('rules')
    if type(rules) is not list or not 1 <= len(rules) <= 200:
        raise AlertRulesRejected('rules')
    seen = set()
    for r in rules:
        keys = {'rule_id', 'metric', 'condition', 'threshold', 'for_seconds', 'severity', 'purpose'}
        if type(r) is not dict or not keys <= set(r) or set(r) - keys - {'selector'}:
            raise AlertRulesRejected('rule shape')
        metric = r['metric']
        if (type(r['rule_id']) is not str or not _RULE_ID.fullmatch(r['rule_id']) or r['rule_id'] in seen
                or type(metric) is not str or not _METRIC.fullmatch(metric) or metric.count('*') > 1 or ('*' in metric and '.*.' not in metric)
                or ('*' in metric and 'selector' in r) or r['condition'] not in ('gt', 'ge')
                or type(r['threshold']) not in (int, float) or isinstance(r['threshold'], bool) or not math.isfinite(r['threshold'])
                or type(r['for_seconds']) is not int or not 0 <= r['for_seconds'] <= 86400 or r['severity'] not in ('warning', 'critical')
                or ('selector' in r and (type(r['selector']) is not str or not _SELECTOR.fullmatch(r['selector'])))
                or type(r['purpose']) is not str or not 1 <= len(r['purpose']) <= 500):
            raise AlertRulesRejected('rule ' + str(r.get('rule_id')))
        seen.add(r['rule_id'])
    return value


def load_rules(path):
    return validate_rules(json.loads(Path(path).read_text(encoding='utf-8')))


def apply_rules(manifest, tenant, *, database_url_file):
    """Technical configurator only (0151 refuses any other session)."""
    from nexloop_eios.trusted_configuration import configurator_connection
    validate_rules(manifest)
    db = configurator_connection(database_url_file)
    with db, db.transaction():
        db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
        return db.execute('select control.nexloop_configure_alert_rules(%s,%s::jsonb)', (tenant, canonical_payload(manifest))).fetchone()[0]


# ---- evaluator (service, nexloop.alert.evaluate:1) -------------------------------------------------------------------------
class AlertEvaluator:
    def __init__(self, pool, session, signer):
        self.pool, self.session, self.signer = pool, session, signer

    def run_once(self):
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.context_engine.authority import action_claims
        body = canonical_payload({'evaluate': True})
        claims = {**action_claims(self.pool, self.session, EVALUATE_ACTION), 'protocol': EVALUATE_PROTOCOL, 'key_id': self.signer.key_id,
                  'parameters_digest': hashlib.sha256(body.encode()).hexdigest()}
        text = canonical_payload(claims)
        signature = hmac.new(self.signer.material, (EVALUATE_PROTOCOL + ':' + text).encode(), 'sha256').hexdigest()
        # The skew between this host's clock and the database clock is sampled first, so this evaluation already sees it (rule clock_skew).
        record_clock_sample(self.pool, 'alert-evaluator')
        # NX-043 X5: guardrails stop running experiments first, so this evaluation already sees an automatic stop (experiment_auto_stopped).
        # Review 2.3: a failing guard never costs the tenant its alerts (backup, clock, ...); it is counted in the result.
        try:
            guard = self.guard_experiments()
        except Exception:  # noqa: BLE001 - any failure of the guard pass; the evaluation below goes on
            guard = {'checked': 0, 'stopped': 0, 'failed': 1, 'guard_error': True}
        with self.pool.connection() as db, db.transaction():
            verify_application_role(db)
            result = db.execute('select authz.nexloop_alert_evaluate(%s,%s,%s,%s,%s)', (self.session.token_digest, self.session.world, text, signature, body)).fetchone()[0]
        result = {**result, 'experiments_checked': guard.get('checked', 0), 'experiments_stopped': guard.get('stopped', 0),
                  'experiments_guard_failed': guard.get('failed', 0),
                  'experiments_guard_sqlstates': ','.join(guard.get('failed_sqlstates') or ([] if not guard.get('guard_error') else ['guard_call']))}
        record_pool_sample(self.pool, 'alert-evaluator')
        return {k: v for k, v in result.items() if type(v) in (int, str, bool)}


    def guard_experiments(self):
        """NX-043 X5: one guard pass over the tenant's started experiments (idempotent: a stopped experiment is not stopped again)."""
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.context_engine.authority import action_claims
        body = canonical_payload({'guard': True})
        claims = {**action_claims(self.pool, self.session, EVALUATE_ACTION), 'protocol': GUARD_PROTOCOL, 'key_id': self.signer.key_id,
                  'parameters_digest': hashlib.sha256(body.encode()).hexdigest()}
        text = canonical_payload(claims)
        signature = hmac.new(self.signer.material, (GUARD_PROTOCOL + ':' + text).encode(), 'sha256').hexdigest()
        with self.pool.connection() as db, db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_experiment_guard_tick(%s,%s,%s,%s,%s)', (self.session.token_digest, self.session.world, text, signature, body)).fetchone()[0]


# ---- process samples (M08–M10) ---------------------------------------------------------------------------------------------
def record_sample(pool, kind, service, sample, *, window_start=None, window_seconds=60, instance=None):
    """Numbers only; SQL rejects any other key or value type."""
    from nexloop_eios.assembly import verify_application_role
    from psycopg.types.json import Jsonb
    with pool.connection() as db, db.transaction():
        verify_application_role(db)
        db.execute('select authz.nexloop_record_process_sample(%s,%s,%s,%s,%s,%s)',
            (kind, service, instance or socket.gethostname()[:100] + ':' + str(_pid()), window_start or datetime.now(UTC), window_seconds, Jsonb(sample)))


def _pid():
    import os
    return os.getpid()


def pool_sample(pool):
    """psycopg_pool statistics (since the previous sample) as counters; None when the pool exposes none."""
    target = getattr(pool, '_pool', pool)  # RequestConnectionPool wraps the psycopg_pool ConnectionPool
    if not hasattr(target, 'pop_stats'):
        return None
    stats = target.pop_stats()
    return {'pool_size': stats.get('pool_size', 0), 'pool_available': stats.get('pool_available', 0), 'requests_waiting': stats.get('requests_waiting', 0),
            'requests_num': stats.get('requests_num', 0), 'requests_errors': stats.get('requests_errors', 0),
            'requests_wait_ms': stats.get('requests_wait_ms', 0), 'usage_ms': stats.get('usage_ms', 0), 'max_size': getattr(target, 'max_size', 0)}


def record_pool_sample(pool, service):
    """Best effort: an unavailable sample never breaks the process it observes."""
    try:
        sample = pool_sample(pool)
        if sample is not None:
            record_sample(pool, 'pool', service, sample)
    except Exception:
        pass


# The host clock the skew sample reads (a test moves it; production keeps time.time).
CLOCK = time.time


def clock_skew(pool, samples=3, clock=None):
    """The database clock minus this host's clock, in seconds, at the midpoint of the shortest of `samples` round trips (as
    scripts/deploy/post_deploy_check.py); returns (skew, round_trip)."""
    clock = clock or CLOCK
    best = None
    with pool.connection() as db:
        for _ in range(samples):
            sent = clock()
            database = db.execute('select extract(epoch from clock_timestamp())::float8').fetchone()[0]
            received = clock()
            measured = (database - (sent + received) / 2, received - sent)
            if best is None or measured[1] < best[1]:
                best = measured
    return best


def clock_sample(skew, round_trip):
    return {'skew_ms': round(skew * 1000), 'skew_abs_ms': round(abs(skew) * 1000), 'round_trip_ms': round(round_trip * 1000)}


def record_clock_sample(pool, service, clock=None):
    """Best effort, as the pool sample: the skew metric never breaks the process that measures it."""
    try:
        record_sample(pool, 'clock', service, clock_sample(*clock_skew(pool, clock=clock)))
    except Exception:
        pass


class GuardTimings:
    """Per-process guard call latency; a call reaching the 2 s deadline counts as a timeout (the caller has given up)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._elapsed = []
        self._errors = 0
        self._dropped = 0
        self._started = time.time()
        self._operations = {}  # AT-060: operation -> ([elapsed_ms], errors); numbers only, labels from GUARD_OPERATIONS

    def drop(self):
        """NX-031: a connection dropped at the connection bound: a call and an error, with no latency sample."""
        with self._lock:
            self._dropped += 1

    def observe(self, elapsed_ms, *, error=False, operation=None):
        """`operation` (AT-060) only labels the call for the per-operation sample; the overall sample is unchanged."""
        with self._lock:
            self._elapsed.append(float(elapsed_ms))
            self._errors += 1 if error else 0
            label = operation if operation in GUARD_OPERATIONS else 'other'
            values, errors = self._operations.get(label, ([], 0))
            values.append(float(elapsed_ms))
            self._operations[label] = (values, errors + (1 if error else 0))

    def drain(self):
        sample, started, _ = self.drain_with_operations()
        return sample, started

    def drain_with_operations(self):
        """The overall sample (as before), its window start, and {operation: sample} for the same window."""
        with self._lock:
            values, errors, dropped, started = sorted(self._elapsed), self._errors, self._dropped, self._started
            operations = self._operations
            self._elapsed, self._errors, self._dropped, self._started, self._operations = [], 0, 0, time.time(), {}
        by_operation = {label: _latency_sample(sorted(v), e) for label, (v, e) in sorted(operations.items()) if v}
        if not values:
            if dropped:
                return {'calls': dropped, 'timeouts': 0, 'errors': dropped}, started, by_operation
            return None, started, by_operation
        sample = _latency_sample(values, errors)
        sample.update(calls=len(values) + dropped, errors=errors + dropped)
        return sample, started, by_operation


# AT-060: the guard's operations as the Host names them (runtime_activation._OPERATIONS, model results and the tool endpoints).
GUARD_OPERATIONS = ('start', 'resume', 'inspect', 'cancel', 'model', 'model_result', 'tool', 'effect_submit', 'effect_find', 'outcome', 'incentive', 'other')
# AT-060 5A (owner 2026-10-13, ROADMAP §7): a log-spaced latency histogram so that the guard p95 over all calls of a window can be
# merged across processes. Bucket 0 holds calls <= 1 ms; bucket i (1..HISTOGRAM_LAST-1) holds (1.1^(i-1), 1.1^i] ms; the last
# bucket holds everything above 1.1^(HISTOGRAM_LAST-1) ms (about 60 s). Estimating a quantile by its bucket's geometric
# midpoint is off by at most sqrt(1.1)-1 = 4.9% (relative). Only non-empty buckets are written, as h_<i>.
HISTOGRAM_RATIO = 1.1
HISTOGRAM_LAST = 116


def histogram_bucket(ms):
    if ms <= 1:
        return 0
    return min(HISTOGRAM_LAST, max(1, math.ceil(math.log(ms) / math.log(HISTOGRAM_RATIO) - 1e-9)))


def _latency_sample(values, errors, *, buckets=False):
    """values sorted ascending, non-empty; the histogram (h_<i> counts) is part of every sample."""
    def rank(q):
        return round(values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))], 1)
    sample = {'calls': len(values), 'timeouts': sum(1 for v in values if v >= GUARD_DEADLINE_MS), 'errors': errors,
              'p50_ms': rank(0.50), 'p95_ms': rank(0.95), 'max_ms': round(values[-1], 1)}
    histogram = {}
    for v in values:
        key = f'h_{histogram_bucket(v)}'
        histogram[key] = histogram.get(key, 0) + 1
    sample.update(sorted(histogram.items(), key=lambda kv: int(kv[0][2:])))
    return sample


GUARD = GuardTimings()


# Relay step timings (dispatcher 2026-10-13): per inbound message, the time of each step of MessageRelay._run_once
# (message_relay.py), numbers only. `total` is claim -> release of a message the relay queued; `authenticate` is the CLI's
# per-tick credential re-authentication (message_relay_cli.py), measured on every tick.
RELAY_STEPS = ('backlog', 'claim', 'recover', 'lookup', 'goal_create', 'step_create', 'enterprise_step_create', 'assignment_create',
               'vault_prepare', 'source_proofs', 'issue', 'effect_bind', 'context_prepare', 'message_bind', 'own_route', 'route_authorize',
               'runtime_accept', 'ack', 'release', 'handoff', 'total', 'authenticate')


class StepTimings:
    """Per-process latency per named step (labels from a fixed list), drained as one histogram sample per step."""

    def __init__(self, steps):
        self.steps = tuple(steps)
        self._lock = threading.Lock()
        self._values = {}
        self._started = time.time()

    def observe(self, step, elapsed_ms):
        if step not in self.steps:
            return
        with self._lock:
            self._values.setdefault(step, []).append(float(elapsed_ms))

    def drain(self):
        with self._lock:
            values, started = self._values, self._started
            self._values, self._started = {}, time.time()
        return {step: _latency_sample(sorted(v), 0) for step, v in sorted(values.items()) if v}, started


RELAY = StepTimings(RELAY_STEPS)


class StepClock:
    """Marks the steps of one relay attempt; nothing is recorded until commit() (an idle tick records nothing)."""

    def __init__(self, timings=None, clock=time.monotonic):
        self.timings, self.clock = RELAY if timings is None else timings, clock
        self.started = self.last = clock()
        self.marks = []

    def mark(self, step):
        now = self.clock()
        self.marks.append((step, (now - self.last) * 1000))
        self.last = now

    def commit(self, *, total=False):
        for step, ms in self.marks:
            self.timings.observe(step, ms)
        if total:
            self.timings.observe('total', (self.last - self.started) * 1000)
        self.marks = []


def flush_relay(pool, timings=None):
    samples, started = (RELAY if timings is None else timings).drain()
    window = dict(window_start=datetime.fromtimestamp(started, UTC), window_seconds=max(1, min(3600, round(time.time() - started))))
    for step, value in samples.items():
        try:
            record_sample(pool, 'relay_step', step, value, **window)
        except Exception:
            pass
    record_pool_sample(pool, 'message-relay')


def start_sampler(stop, interval, flush):
    """A daemon thread calling flush() every interval seconds until stop is set; failures are swallowed (best effort)."""
    def loop():
        while not stop.wait(interval):
            try:
                flush()
            except Exception:
                pass
    thread = threading.Thread(target=loop, name='nexloop-observability-sampler', daemon=True)
    thread.start()
    return thread


def flush_guard(pool, service='runtime-guard'):
    sample, started, by_operation = GUARD.drain_with_operations()
    window = dict(window_start=datetime.fromtimestamp(started, UTC), window_seconds=max(1, min(3600, round(time.time() - started))))
    if sample is not None:
        record_sample(pool, 'guard', service, sample, **window)
    # AT-060: one sample per operation of the same window (kind guard_op, service = operation; 0186). Best effort per item.
    for operation, value in by_operation.items():
        try:
            record_sample(pool, 'guard_op', operation, value, **window)
        except Exception:
            pass
    record_pool_sample(pool, service)


# EIOS gap E4 (NX-061): API requests per class (read = GET/HEAD, write = any other method), per process and minute: calls, errors
# (5xx), slow (>= 500 ms, the PRD 8 read target), p50/p95/max and the guard histogram (kind 'api', 0320). Per-route detail is the
# vendored eios.api.metrics registry of the same process (optional loopback export).
API_SLOW_MS = 500
API_CLASSES = ('read', 'write')


class ApiTimings:
    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.time()
        self._values = {c: [] for c in API_CLASSES}
        self._errors = dict.fromkeys(API_CLASSES, 0)
        self._slow = dict.fromkeys(API_CLASSES, 0)

    @staticmethod
    def request_class(method):
        return 'read' if method in ('GET', 'HEAD') else 'write'

    def observe(self, method, status, elapsed_ms):
        c = self.request_class(method)
        with self._lock:
            self._values[c].append(float(elapsed_ms))
            self._errors[c] += 1 if status >= 500 else 0
            self._slow[c] += 1 if elapsed_ms >= API_SLOW_MS else 0

    def drain(self):
        """({class: sample} for the classes with calls, window start); the counters restart."""
        with self._lock:
            values, errors, slow, started = self._values, self._errors, self._slow, self._started
            self._values, self._errors, self._slow = {c: [] for c in API_CLASSES}, dict.fromkeys(API_CLASSES, 0), dict.fromkeys(API_CLASSES, 0)
            self._started = time.time()
        out = {}
        for c in API_CLASSES:
            if values[c]:
                sample = _latency_sample(sorted(values[c]), errors[c])
                sample.pop('timeouts')  # the guard deadline means nothing here; slow is the API's own threshold
                sample['slow'] = slow[c]
                out[c] = sample
        return out, started


API = ApiTimings()


def flush_api(pool, timings=None):
    samples, started = (API if timings is None else timings).drain()
    window = dict(window_start=datetime.fromtimestamp(started, UTC), window_seconds=max(1, min(3600, round(time.time() - started))))
    for request_class, value in samples.items():
        try:
            record_sample(pool, 'api', request_class, value, **window)
        except Exception:
            pass
    record_pool_sample(pool, 'api')


class HostCounters:
    """Agent Host counters → one window's sample: gauges as read, cumulative counters as the increase since the last
    read (a Host restart resets them: the current value is the increase)."""
    CUMULATIVE = ('runs_started', 'admission_timeouts')

    def __init__(self):
        self.previous = None

    def sample(self, host):
        if host is None or not host.get('available'):
            self.previous = None
            return None
        out = {k: host[k] for k in ('active_runs', 'waiting', 'max_active_runs') if k in host}
        for k in self.CUMULATIVE:
            now = host.get(k, 0)
            before = (self.previous or {}).get(k)
            out[k] = now if before is None or now < before else now - before
        if self.previous is None:
            out = {k: v for k, v in out.items() if k not in self.CUMULATIVE}  # the first read only sets the baseline
        self.previous = dict(host)
        out['available'] = True
        return out


def flush_host(pool, config, counters, service='agent-host'):
    from nexloop_eios.host_control import read_host_metrics
    sample = counters.sample(read_host_metrics(config))
    if sample is not None:
        record_sample(pool, 'host', service, sample)


# ---- optional loopback text export (D1, off by default; read-only role D4) -------------------------------------------------
# Snapshot levels whose keys are data (codes, states, reasons, queues, feeds, services, budget kinds): rendered as a label.
MAPS = frozenset({'last_5m', 'last_hour', 'by_state', 'escalations_last_hour', 'exceptions', 'exceptions_last_hour', 'queues', 'feeds', 'by_service', 'budgets', 'classes'})


def _flatten(prefix, value, labels, out, *, keyed=False):
    if type(value) is dict and keyed:
        for key, item in sorted(value.items()):
            _flatten(prefix, item, {**labels, 'key': key}, out)
        return
    if type(value) is bool:
        out.append((prefix, labels, 1 if value else 0))
    elif type(value) in (int, float):
        out.append((prefix, labels, value))
    elif type(value) is str and re.fullmatch(r'-?\d+(\.\d+)?', value):
        out.append((prefix, labels, float(value)))
    elif type(value) is dict:
        for key, item in sorted(value.items()):
            if re.fullmatch(r'[a-z][a-z0-9_]*', key):
                _flatten(prefix + '_' + key, item, labels, out, keyed=key in MAPS)


def render_prometheus(export):
    """Snapshot list → Prometheus text exposition (gauges). Only numbers; tenant/world/key labels; no free text."""
    lines, typed = [], set()
    for snapshot in export:
        labels = {'tenant': snapshot['tenant_id'], 'world': snapshot['world']}
        rows = []
        for section in ('refusals', 'effects', 'replies', 'commitments', 'queues', 'feeds', 'extraction', 'guard', 'host', 'pool', 'connections', 'cost', 'evaluator', 'api'):
            _flatten('nexloop_' + section, snapshot.get(section), labels, rows)
        for name, row_labels, number in rows:
            if name not in typed:
                lines.append(f'# TYPE {name} gauge')
                typed.add(name)
            rendered = ','.join(f'{k}="{_escape(v)}"' for k, v in sorted(row_labels.items()))
            lines.append(f'{name}{{{rendered}}} {number}')
    return '\n'.join(lines) + '\n'


def _escape(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


def read_export(database_url_file):
    """As nexloop_metrics only (D4): the role can execute the export and nothing else."""
    import psycopg
    from nexloop_eios.private_configuration import read_private_text
    with psycopg.connect(read_private_text(database_url_file, maximum=16384), connect_timeout=5) as db:
        if db.execute('select current_user').fetchone()[0] != 'nexloop_metrics':
            raise ValueError('read-only metrics role required')
        return db.execute('select authz.nexloop_metrics_export()').fetchone()[0]


def serve_export(database_url_file, port, *, ready=None):
    """GET /internal/metrics on 127.0.0.1 only. Not started by any default profile (D1)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('unprivileged loopback port required')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != '/internal/metrics' or self.client_address[0] != '127.0.0.1':
                self.send_response(404)
                self.end_headers()
                return
            try:
                body = render_prometheus(read_export(database_url_file)).encode()
                status = 200
            except Exception:
                body, status = b'# metrics unavailable\n', 503
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    if ready is not None:
        ready(server)
    return server


def main(argv=None):
    p = argparse.ArgumentParser(description='NexLoop metrics: alert rules (trusted configuration) and the optional loopback export')
    sub = p.add_subparsers(dest='command', required=True)
    rules = sub.add_parser('rules')
    rules.add_argument('--manifest', type=Path, required=True)
    rules.add_argument('--tenant')
    rules.add_argument('--database-url-file', type=Path)
    mode = rules.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    export = sub.add_parser('export')
    export.add_argument('--database-url-file', type=Path, required=True)
    export.add_argument('--port', type=int, required=True)
    from nexloop_eios.structured_log import configure as _structured_logging
    _structured_logging('metrics')
    a = p.parse_args(argv)
    try:
        if a.command == 'rules':
            manifest = load_rules(a.manifest)
            if a.check:
                print(json.dumps({'valid': True, 'version': manifest['version'], 'rules': len(manifest['rules'])}))
                return 0
            if a.tenant is None or a.database_url_file is None:
                raise AlertRulesRejected('tenant and database url file required')
            print(json.dumps(apply_rules(manifest, a.tenant, database_url_file=a.database_url_file)))
            return 0
        server = serve_export(a.database_url_file, a.port)
        print('metrics export ready', flush=True)
        server.serve_forever()
        return 0
    except Exception as error:
        print(json.dumps({'valid': False, 'reason': str(error) if isinstance(error, AlertRulesRejected) else 'unavailable'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
