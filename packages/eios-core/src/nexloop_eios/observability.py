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
        with self.pool.connection() as db, db.transaction():
            verify_application_role(db)
            result = db.execute('select authz.nexloop_alert_evaluate(%s,%s,%s,%s,%s)', (self.session.token_digest, self.session.world, text, signature, body)).fetchone()[0]
        record_pool_sample(self.pool, 'alert-evaluator')
        return {k: v for k, v in result.items() if type(v) in (int, str, bool)}


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


class GuardTimings:
    """Per-process guard call latency; a call reaching the 2 s deadline counts as a timeout (the caller has given up)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._elapsed = []
        self._errors = 0
        self._started = time.time()

    def observe(self, elapsed_ms, *, error=False):
        with self._lock:
            self._elapsed.append(float(elapsed_ms))
            self._errors += 1 if error else 0

    def drain(self):
        with self._lock:
            values, errors, started = sorted(self._elapsed), self._errors, self._started
            self._elapsed, self._errors, self._started = [], 0, time.time()
        if not values:
            return None, started
        def rank(q):
            return round(values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))], 1)
        return {'calls': len(values), 'timeouts': sum(1 for v in values if v >= GUARD_DEADLINE_MS), 'errors': errors,
                'p50_ms': rank(0.50), 'p95_ms': rank(0.95), 'max_ms': round(values[-1], 1)}, started


GUARD = GuardTimings()


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
    sample, started = GUARD.drain()
    if sample is not None:
        record_sample(pool, 'guard', service, sample, window_start=datetime.fromtimestamp(started, UTC), window_seconds=max(1, min(3600, round(time.time() - started))))
    record_pool_sample(pool, service)


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
MAPS = frozenset({'last_5m', 'last_hour', 'by_state', 'escalations_last_hour', 'exceptions', 'exceptions_last_hour', 'queues', 'feeds', 'by_service', 'budgets'})


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
        for section in ('refusals', 'effects', 'replies', 'commitments', 'queues', 'feeds', 'extraction', 'guard', 'host', 'pool', 'connections', 'cost', 'evaluator'):
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
