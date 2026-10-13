#!/usr/bin/env python3
"""NX-031 matrix summary report (design §4, §5.3, §6, D8).

Reads one matrix run directory (contract: docs/implementation/NX-031-results-contract.md), judges the §6.1 thresholds on
the gate cells C4 and C4-low only, and writes report.json and report.md. The output carries metrics, threshold verdicts,
seed, digests and the commit only: no message text, no run or message IDs (D8). Standard library only; read-only.

    python scripts/load/report.py <run_dir> [--out <dir>] [--expected-refusals NXC05,NXC06] [--ci-baseline <json>]

Exit code 0 when the report was written (whatever AT-060 concluded), 2 when the input breaks the contract.
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

CELLS = ('C1', 'C2', 'C4', 'C8', 'C4-low', 'C4-peak', 'C4-quiet', 'C4-ci')
GATES = ('C4', 'C4-low')
IDENTITY = ('run_binding', 'outbound_ownership', 'claim_ownership', 'model_context', 'cross_access_probe', 'duplicate_effects')
DEFAULT_EXPECTED_REFUSALS = ('NXC05', 'NXC06')
GUARD_DEADLINE_MS = 2000
THRESHOLDS = {  # §6.1 (D4); never relaxed by this tool (§7)
    'guard_timeout_rate_5m_max': 0.01, 'guard_p95_ms': 1500, 'dead_letters_delta': 0, 'ack_p95_ms': 500,
    'idle_dispatch_p95_ms': 2000, 'drain_seconds': 600, 'connections_peak': 60, 'critical_alerts': 0,
    'unexpected_refusals': 0, 'identity_violations': 0, 'unresolved_work': 0,
}
# AT-060 calibers (owner 2026-10-13, 4A and 5A, ROADMAP §7). guard_p95_ms: the p95 over all guard calls of the steady window,
# merged from the per-process log-spaced histograms (ratio 1.1, <= +-4.9%; nexloop_eios.observability.HISTOGRAM_*); the worst
# per-process one-minute p95 stays as the observation guard_p95_ms_worst_minute. idle_dispatch_p95_ms: the inbound message
# accepted -> its Run task claimed (NX-031 design §4.6 row 187), split into relay (accepted -> task created) and queue (task
# created -> claimed); run_start_p95_ms (task created -> the Host's first execution authorization, the former caliber) and the
# start segment (claimed -> first authorization) are observations. Results recorded under an earlier caliber are marked
# 'incomparable' (caliber_changed) and never pass a gate.
HISTOGRAM_RATIO = 1.1
HISTOGRAM_LAST = 116
_HISTOGRAM_KEY = re.compile(r'^h_(\d{1,3})$')
_CODE = re.compile(r'^[A-Za-z0-9_.:+,-]{1,200}$')


class ContractError(ValueError):
    pass


# ---- reading -------------------------------------------------------------------------------------------------------
def _json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ContractError(f'{path.name}: unreadable') from error


def _lines(path):
    if not path.exists():
        return None
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError as error:
                raise ContractError(f'{path.name}:{n}: not JSON') from error
    return out


def _time(value):
    if not isinstance(value, str):
        raise ContractError('timestamp expected')
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _number(value, name, *, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ContractError(f'{name}: non-negative number expected')
    return value


def _code(value, name):
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise ContractError(f'{name}: code expected')
    return value


def percentile(values, q):
    """Nearest-rank percentile (same rule as NX-030 GuardTimings)."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))], 1)


# ---- per-cell metrics ----------------------------------------------------------------------------------------------
def _dead_letters(snapshot):
    total = 0
    for group in ('queues', 'feeds'):
        for value in (snapshot.get(group) or {}).values():
            total += _number((value or {}).get('dead_lettered') or 0, f'{group}.dead_lettered')
    return total


def _backlog(snapshot):
    pending, oldest = 0, None
    for group in ('queues', 'feeds'):
        for value in (snapshot.get(group) or {}).values():
            value = value or {}
            # Feeds count only due items when the snapshot splits them (0151): items scheduled for later are not a backlog.
            due = group == 'feeds' and 'due' in value
            pending += _number(value.get('due' if due else 'pending') or 0, f'{group}.pending') + _number(value.get('retry_wait') or 0, f'{group}.retry_wait')
            age = _number(value.get('oldest_due_seconds' if due else 'oldest_pending_seconds'), f'{group}.oldest', allow_none=True)
            oldest = age if oldest is None or (age is not None and age > oldest) else oldest
    extraction = snapshot.get('extraction') or {}
    pending += _number(extraction.get('pending_messages') or 0, 'extraction.pending_messages')
    return pending, oldest


def guard_windows(samples, start, end):
    """Per-minute guard calls/timeouts summed over services, then the worst 5-minute window inside [start, end)."""
    minutes = {}
    p95_max = None
    for row in samples:
        if row.get('kind') != 'guard':
            continue
        at = _time(row.get('window_start'))
        if not start <= at < end:
            continue
        sample = row.get('sample') or {}
        key = at.replace(second=0, microsecond=0)
        calls, timeouts = minutes.get(key, (0, 0))
        minutes[key] = (calls + _number(sample.get('calls', 0), 'guard.calls'), timeouts + _number(sample.get('timeouts', 0), 'guard.timeouts'))
        if sample.get('p95_ms') is not None:
            p = _number(sample['p95_ms'], 'guard.p95_ms')
            p95_max = p if p95_max is None else max(p95_max, p)
    if not minutes:
        return None, None, 0
    worst, keys = 0.0, sorted(minutes)
    for first in keys:
        window = [minutes[k] for k in keys if first <= k < first + timedelta(minutes=5)]
        calls = sum(c for c, _ in window)
        if calls:
            worst = max(worst, sum(t for _, t in window) / calls)
    return round(worst, 4), p95_max, sum(c for c, _ in minutes.values())


def guard_operations(samples, start, end):
    """AT-060: guard calls per operation (kind guard_op, one sample per process, operation and window) inside [start, end):
    calls, timeouts and errors summed; p95_ms merged from the histograms over all the operation's calls (the guard_p95_ms
    caliber); p95_ms_worst_minute and max as observed per sample. Informational, never gated. Absent in older results: {}."""
    out, names = {}, set()
    for row in samples:
        if row.get('kind') != 'guard_op' or not start <= _time(row.get('window_start')) < end:
            continue
        operation = _code(row.get('service'), 'guard_op.service')
        names.add(operation)
        sample = row.get('sample') or {}
        o = out.setdefault(operation, {'calls': 0, 'timeouts': 0, 'errors': 0, 'p95_ms': None, 'p95_ms_worst_minute': None, 'max_ms': None})
        for key in ('calls', 'timeouts', 'errors'):
            o[key] += _number(sample.get(key, 0), f'guard_op.{key}')
        for key, target in (('p95_ms', 'p95_ms_worst_minute'), ('max_ms', 'max_ms')):
            if sample.get(key) is not None:
                value = _number(sample[key], f'guard_op.{key}')
                o[target] = value if o[target] is None else max(o[target], value)
    for operation in names:
        counts, comparable = merged_histogram(samples, 'guard_op', start, end, service=operation)
        out[operation]['p95_ms'] = histogram_quantile(counts, 0.95) if comparable else None
    return dict(sorted(out.items()))


def histogram_quantile(counts, q):
    """counts {bucket index: n}; the quantile at rank ceil(q*n) as its bucket's geometric midpoint (bucket 0: 1 ms; the last,
    open bucket: its lower bound). None when empty."""
    total = sum(counts.values())
    if not total:
        return None
    target, running = max(1, math.ceil(q * total)), 0
    for index in sorted(counts):
        running += counts[index]
        if running >= target:
            if index == 0:
                return 1.0
            if index >= HISTOGRAM_LAST:
                return round(HISTOGRAM_RATIO ** (HISTOGRAM_LAST - 1), 1)
            return round(HISTOGRAM_RATIO ** (index - 0.5), 1)
    return None


RELAY_STEP_ORDER = ('authenticate', 'backlog', 'claim', 'recover', 'lookup', 'goal_create', 'step_create', 'enterprise_step_create', 'assignment_create',
                    'vault_prepare', 'source_proofs', 'issue', 'effect_bind', 'context_prepare', 'message_bind', 'own_route', 'route_authorize',
                    'runtime_accept', 'ack', 'release', 'handoff', 'total')


def relay_steps(samples, start, end):
    """Relay step timings (kind relay_step, dispatcher 2026-10-13) in [start, end): per step the number of observations and
    p50/p95 merged from the relay processes' histograms (the guard_p95_ms caliber). Informational. Absent in older results: {}."""
    names = sorted({_code(r.get('service'), 'relay_step.service') for r in samples if r.get('kind') == 'relay_step' and start <= _time(r.get('window_start')) < end},
                   key=lambda n: RELAY_STEP_ORDER.index(n) if n in RELAY_STEP_ORDER else len(RELAY_STEP_ORDER))
    out = {}
    for name in names:
        counts, comparable = merged_histogram(samples, 'relay_step', start, end, service=name)
        out[name] = {'count': sum(counts.values()), 'p50_ms': histogram_quantile(counts, 0.5) if comparable else None,
                     'p95_ms': histogram_quantile(counts, 0.95) if comparable else None}
    return out


def merged_histogram(rows, kind, start, end, service=None):
    """(counts, comparable): the h_<i> counts of every sample of `kind` in [start, end) summed across processes; comparable is
    False when a sample with latency figures carries no histogram (a result from before 5A)."""
    counts, comparable = {}, True
    for row in rows:
        if row.get('kind') != kind or not start <= _time(row.get('window_start')) < end or (service is not None and row.get('service') != service):
            continue
        sample, found = row.get('sample') or {}, False
        for key, value in sample.items():
            match = _HISTOGRAM_KEY.fullmatch(key)
            if match:
                index = int(match.group(1))
                if index > HISTOGRAM_LAST:
                    raise ContractError(f'{kind}.{key}: bucket out of range')
                counts[index] = counts.get(index, 0) + _number(value, f'{kind}.{key}')
                found = True
        if not found and sample.get('p95_ms') is not None:
            comparable = False
    return counts, comparable


def host_summary(samples, start, end):
    waiting, active, timeouts, seen = None, None, 0, False
    for row in samples:
        if row.get('kind') != 'host' or not start <= _time(row.get('window_start')) < end:
            continue
        sample, seen = row.get('sample') or {}, True
        if sample.get('waiting') is not None:
            waiting = max(waiting or 0, _number(sample['waiting'], 'host.waiting'))
        if sample.get('active_runs') is not None:
            active = max(active or 0, _number(sample['active_runs'], 'host.active_runs'))
        timeouts += _number(sample.get('admission_timeouts', 0), 'host.admission_timeouts')
    return {'available': seen, 'active_runs_max': active, 'waiting_max': waiting, 'admission_timeouts': timeouts if seen else None}


def pool_summary(samples, start, end):
    waiting, errors, seen = 0, 0, False
    for row in samples:
        if row.get('kind') != 'pool' or not start <= _time(row.get('window_start')) < end:
            continue
        sample, seen = row.get('sample') or {}, True
        waiting = max(waiting, _number(sample.get('requests_waiting', 0), 'pool.requests_waiting'))
        errors += _number(sample.get('requests_errors', 0), 'pool.requests_errors')
    return {'available': seen, 'requests_waiting_max': waiting if seen else None, 'requests_errors': errors if seen else None}


def decomposition(runs):
    """AT-060 per Run (dispatcher 2026-10-11): platform = Run duration - model - tool, all over the same span (the Host accepted the
    Run, its first activation → its task ended). A negative platform time is a data anomaly: counted and reported, never clipped to 0, and kept out of the
    platform statistics. End to end (trigger accepted → its reply) is reported on its own and never enters the subtraction. Runs
    without tool or model timing are counted, not guessed."""
    run, model, tool, platform, e2e = [], [], [], [], []
    anomalies, no_tool, no_model, outcomes, failures = 0, 0, 0, {}, {}
    for row in runs:
        outcome = _code(row.get('outcome'), 'runs.outcome')
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if row.get('failure') is not None:  # NX-031: failed Runs by cause; an unanswered guard is not a denial
            failure = _code(row['failure'], 'runs.failure')
            failures[failure] = failures.get(failure, 0) + 1
        if row.get('e2e_ms') is not None:  # no reply yet (in progress, restricted, taken over): no end-to-end time
            e2e.append(_number(row.get('e2e_ms'), 'runs.e2e_ms'))
        duration = _number(row.get('run_ms'), 'runs.run_ms', allow_none=True)
        if duration is None:
            continue
        m = _number(row.get('model_ms'), 'runs.model_ms', allow_none=True)
        t = _number(row.get('tool_ms'), 'runs.tool_ms', allow_none=True)
        run.append(duration)
        if m is not None:
            model.append(m)
        if t is not None:
            tool.append(t)
        if t is None:
            no_tool += 1
            continue
        if m is None:
            no_model += 1
            continue
        rest = duration - m - t
        if rest < 0:
            anomalies += 1
            continue
        platform.append(rest)
    stats = lambda values: {'p50': percentile(values, 0.5), 'p95': percentile(values, 0.95)}
    return {'runs': len(run), 'e2e_ms': stats(e2e), 'run_ms': stats(run), 'model_ms': stats(model), 'tool_ms': stats(tool),
            'platform_ms': stats(platform), 'runs_without_tool_timing': no_tool, 'runs_without_model_timing': no_model,
            'anomalies': anomalies, 'outcomes': dict(sorted(outcomes.items())), 'failures': dict(sorted(failures.items()))}


def cell_metrics(directory, expected_refusals):
    cell = _json(directory / 'cell.json')
    name = _code(cell.get('cell'), 'cell')
    if name not in CELLS:
        raise ContractError(f'unknown cell {name}')
    role = cell.get('role')
    if role not in ('gate', 'observe') or (role == 'gate') != (name in GATES):
        raise ContractError(f'{name}: role must be gate exactly for {"/".join(GATES)} (design §6.1)')
    out = {'cell': name, 'role': role, 'host_concurrency': _number(cell.get('host_concurrency'), 'host_concurrency'),
           'rate_per_s': _number(cell.get('rate_per_s'), 'rate_per_s'), 'background': bool(cell.get('background')),
           'ci_background': bool(cell.get('ci_background')), 'completed': cell.get('completed') is True,
           'aborted': None if cell.get('aborted') is None else _code(cell.get('aborted'), 'aborted'),
           'host_metrics': cell.get('host_metrics') if cell.get('host_metrics') in ('m09', 'approx') else 'missing',
           'beyond_deployment_premise': _number(cell.get('host_concurrency'), 'host_concurrency') > 4}
    window = cell.get('window') or {}
    start, end = _time(window.get('steady_start')), _time(window.get('steady_end'))
    drain = cell.get('drain') or {}
    out['drain'] = {'drained': drain.get('drained') is True, 'seconds': _number(drain.get('seconds'), 'drain.seconds', allow_none=True)}
    if cell.get('ci'):
        ci = cell['ci']
        out['ci'] = {'run_id': _code(ci.get('run_id'), 'ci.run_id'), 'wall_seconds': _number(ci.get('wall_seconds'), 'ci.wall_seconds'),
                     'deadline_gate_failures': _number(ci.get('deadline_gate_failures', 0), 'ci.deadline_gate_failures'),
                     'failed': _number(ci.get('failed', 0), 'ci.failed')}
    metrics = {}
    snapshots = _lines(directory / 'snapshots.jsonl')
    if snapshots:
        first, last = snapshots[0]['snapshot'], snapshots[-1]['snapshot']
        steady = [s['snapshot'] for s in snapshots if s.get('phase') == 'steady'] or [first]
        metrics['dead_letters_start'] = _dead_letters(first)
        metrics['dead_letters_delta'] = max(0, _dead_letters(last) - _dead_letters(first))
        backlog = [_backlog(s) for s in steady]
        metrics['backlog_pending_max'] = max(p for p, _ in backlog)
        ages = [a for _, a in backlog if a is not None]
        metrics['backlog_oldest_seconds_max'] = max(ages) if ages else None
        totals = [_number((s.get('connections') or {}).get('total'), 'connections.total') for s in (x['snapshot'] for x in snapshots)
                  if (s.get('connections') or {}).get('total') is not None]
        metrics['connections_peak'] = max(totals) if totals else None
    samples = _lines(directory / 'process_samples.jsonl')
    if samples is not None:
        rate, worst, calls = guard_windows(samples, start, end)
        counts, comparable = merged_histogram(samples, 'guard', start, end)
        metrics.update(guard_timeout_rate_5m_max=rate, guard_p95_ms_worst_minute=worst, guard_calls=calls,
                       guard_p95_ms=histogram_quantile(counts, 0.95) if comparable else None)
        if not comparable:
            metrics['caliber_incomparable'] = sorted(set(metrics.get('caliber_incomparable', [])) | {'guard_p95_ms'})
        # NX-031: guard calls answered 503 or dropped at the connection bound (unanswered, not timed out); reported, not gated.
        metrics['guard_errors'] = sum(_number((r.get('sample') or {}).get('errors', 0), 'guard.errors') for r in samples
                                      if r.get('kind') == 'guard' and start <= _time(r.get('window_start')) < end)
        out['host'] = host_summary(samples, start, end)
        out['pool'] = pool_summary(samples, start, end)
        out['guard_operations'] = guard_operations(samples, start, end)
        out['relay_steps'] = relay_steps(samples, start, end)
    acks = _lines(directory / 'acks.jsonl')
    if acks is not None:
        ok = [_number(a.get('ack_ms'), 'ack_ms') for a in acks if a.get('status') == 202]
        statuses = {}
        for a in acks:
            key = str(_number(a.get('status'), 'ack.status'))
            statuses[key] = statuses.get(key, 0) + 1
        metrics.update(ack_count=len(ok), ack_p50_ms=percentile(ok, 0.5), ack_p95_ms=percentile(ok, 0.95), ack_p99_ms=percentile(ok, 0.99))
        out['ack_statuses'] = dict(sorted(statuses.items()))
    dispatch = _lines(directory / 'dispatch.jsonl')
    if dispatch is not None:
        if any('queue_ms' not in d for d in dispatch):
            # dispatch.jsonl from before 4A: dispatch_ms was task created -> first execution authorization.
            metrics.update(idle_dispatch_count=len(dispatch), idle_dispatch_p95_ms=None)
            metrics['caliber_incomparable'] = sorted(set(metrics.get('caliber_incomparable', [])) | {'idle_dispatch_p95_ms'})
        else:
            def segment(key):
                return [_number(d[key], f'dispatch.{key}') for d in dispatch if d.get(key) is not None]
            values = segment('dispatch_ms')
            metrics.update(idle_dispatch_count=len(values), idle_dispatch_p95_ms=percentile(values, 0.95),
                           run_start_p95_ms=percentile(segment('run_start_ms'), 0.95))
            out['dispatch_segments'] = {name: {'count': len(v), 'p50_ms': percentile(v, 0.5), 'p95_ms': percentile(v, 0.95)}
                                        for name, v in (('relay', segment('relay_ms')), ('queue', segment('queue_ms')), ('start', segment('start_ms')))}
    refusals = _lines(directory / 'refusals.jsonl')
    if refusals is not None:
        codes = {}
        for r in refusals:
            code = _code(r.get('code'), 'refusal.code')
            codes[code] = codes.get(code, 0) + 1
        out['refusals'] = {'expected': {k: v for k, v in sorted(codes.items()) if k in expected_refusals},
                           'unexpected': {k: v for k, v in sorted(codes.items()) if k not in expected_refusals}}
        metrics['unexpected_refusals'] = sum(out['refusals']['unexpected'].values())
    alerts = _lines(directory / 'alerts.jsonl')
    if alerts is not None:
        firing = [a for a in alerts if a.get('kind') == 'firing']
        by_rule = {}
        for a in firing:
            key = f"{_code(a.get('severity'), 'alert.severity')}:{_code(a.get('rule_id'), 'alert.rule_id')}"
            by_rule[key] = by_rule.get(key, 0) + 1
        out['alerts_firing'] = dict(sorted(by_rule.items()))
        metrics['critical_alerts'] = sum(v for k, v in by_rule.items() if k.startswith('critical:'))
    checks = directory / 'checks.json'
    if checks.exists():
        c = _json(checks)
        identity = {}
        for item in IDENTITY:
            row = c.get(item)
            if not isinstance(row, dict):
                raise ContractError(f'checks.{item} missing')
            identity[item] = {'checked': _number(row.get('checked'), f'{item}.checked'), 'violations': _number(row.get('violations'), f'{item}.violations')}
        work = c.get('unresolved_work') or {}
        out['identity'] = identity
        out['unresolved_work'] = {'checked': _number(work.get('checked'), 'unresolved_work.checked'), 'unresolved': _number(work.get('unresolved'), 'unresolved_work.unresolved')}
        metrics['identity_violations'] = sum(v['violations'] for v in identity.values())
        # AT-060 §4.4 item 4: how many cited IDs were actually compared (collect.py since the v6 message Runs); absent in older
        # results, which then count as missing evidence in a gate cell.
        compared = c['model_context'].get('ids_compared')
        metrics['model_context_ids_compared'] = None if compared is None else _number(compared, 'model_context.ids_compared')
        metrics['unresolved_work'] = out['unresolved_work']['unresolved']
    runs = _lines(directory / 'runs.jsonl')
    out['decomposition'] = decomposition(runs) if runs is not None else None
    out['metrics'] = metrics
    return out


# ---- verdicts ------------------------------------------------------------------------------------------------------
def gate_verdict(cell):
    """§6.1 on a gate cell; a missing required measurement fails, never passes."""
    m, items = cell['metrics'], {}
    def check(name, value, limit, *, op='<='):
        if value is None:
            items[name] = {'value': None, 'limit': limit, 'result': 'missing'}
        else:
            ok = value <= limit if op == '<=' else value == limit
            items[name] = {'value': value, 'limit': limit, 'result': 'pass' if ok else 'fail'}
    check('identity_violations', m.get('identity_violations'), 0, op='==')
    check('unresolved_work', m.get('unresolved_work'), 0, op='==')
    check('guard_timeout_rate_5m_max', m.get('guard_timeout_rate_5m_max'), THRESHOLDS['guard_timeout_rate_5m_max'])
    check('guard_p95_ms', m.get('guard_p95_ms'), THRESHOLDS['guard_p95_ms'])
    check('dead_letters_delta', m.get('dead_letters_delta'), 0, op='==')
    check('ack_p95_ms', m.get('ack_p95_ms'), THRESHOLDS['ack_p95_ms'])
    if cell['cell'] == 'C4-low':
        check('idle_dispatch_p95_ms', m.get('idle_dispatch_p95_ms'), THRESHOLDS['idle_dispatch_p95_ms'])
    drain = cell['drain']
    items['drain_seconds'] = {'value': drain['seconds'], 'limit': THRESHOLDS['drain_seconds'],
                              'result': 'pass' if drain['drained'] and drain['seconds'] is not None and drain['seconds'] <= THRESHOLDS['drain_seconds']
                              else 'missing' if drain['seconds'] is None and drain['drained'] else 'fail'}
    check('connections_peak', m.get('connections_peak'), THRESHOLDS['connections_peak'])
    check('critical_alerts', m.get('critical_alerts'), 0, op='==')
    check('unexpected_refusals', m.get('unexpected_refusals'), 0, op='==')
    for name in m.get('caliber_incomparable') or ():
        if name in items:  # recorded under an earlier caliber: not comparable, never a pass (owner 2026-10-13)
            items[name] = {'value': None, 'limit': items[name]['limit'], 'result': 'incomparable', 'reason': 'caliber_changed'}
    # An identity check that compared nothing is no evidence (dispatcher 2026-10-13): a gate cell whose model request context
    # check compared 0 IDs fails as an empty sample. Observe cells only record the number.
    compared = m.get('model_context_ids_compared')
    items['model_context_sample'] = {'value': compared, 'limit': 1,  # at least one ID compared
                                     'result': 'missing' if compared is None else 'pass' if compared > 0 else 'fail',
                                     **({'reason': 'empty_sample'} if compared == 0 else {})}
    if not cell['completed']:
        result = 'failed'
    elif any(i['result'] != 'pass' for i in items.values()):
        result = 'failed'
    else:
        result = 'passed'
    return {'result': result, 'items': items}


def at060(cells):
    present = {c['cell']: c for c in cells}
    missing = [name for name in CELLS if name not in present]
    reasons = []
    if missing:
        return {'result': 'blocked', 'reasons': ['cells_not_run:' + ','.join(missing)]}
    for c in cells:
        if not c['completed']:
            reasons.append(f"{c['cell']}:not_completed" + (f":{c['aborted']}" if c['aborted'] else ''))
        if not c.get('decomposition') or not c['decomposition']['runs']:
            reasons.append(f"{c['cell']}:no_decomposition")
        elif c['decomposition']['anomalies']:
            reasons.append(f"{c['cell']}:decomposition_anomaly:{c['decomposition']['anomalies']}")
        identity = c['metrics'].get('identity_violations')
        if identity is None:
            reasons.append(f"{c['cell']}:identity_checks_missing")
        elif identity > 0:
            reasons.append(f"{c['cell']}:identity_violations")
        if c['role'] == 'gate' and c['gate']['result'] != 'passed':
            reasons.append(f"{c['cell']}:gate_" + ','.join(k + (f":{v['reason']}" if v.get('reason') else '') for k, v in c['gate']['items'].items()
                                                         if v['result'] != 'pass'))
    return {'result': 'passed' if not reasons else 'failed', 'reasons': reasons}


def ci_interference(cells, baseline):
    """§5.3: C4-ci against C4, and the CI run under load against a baseline full CI (when given)."""
    by = {c['cell']: c for c in cells}
    if 'C4' not in by or 'C4-ci' not in by:
        return None
    keys = ('guard_timeout_rate_5m_max', 'guard_p95_ms', 'guard_p95_ms_worst_minute', 'ack_p95_ms', 'backlog_pending_max', 'backlog_oldest_seconds_max', 'connections_peak')
    base, loaded = by['C4']['metrics'], by['C4-ci']['metrics']
    out = {'load_side': {k: {'C4': base.get(k), 'C4-ci': loaded.get(k)} for k in keys},
           'drain_seconds': {'C4': by['C4']['drain']['seconds'], 'C4-ci': by['C4-ci']['drain']['seconds']},
           'ci_side': None}
    ci = by['C4-ci'].get('ci')
    if ci:
        out['ci_side'] = {'under_load': ci, 'baseline': baseline}
    return out


# ---- output --------------------------------------------------------------------------------------------------------
def _assert_no_free_text(value, path='report'):
    """D8: only numbers, booleans, null and code-like strings leave this tool."""
    if isinstance(value, dict):
        for k, v in value.items():
            _code(k, path)
            _assert_no_free_text(v, f'{path}.{k}')
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_no_free_text(v, f'{path}[{i}]')
    elif isinstance(value, str):
        _code(value, path)


def build(run_dir, *, expected_refusals=DEFAULT_EXPECTED_REFUSALS, ci_baseline=None):
    run_dir = Path(run_dir)
    run = _json(run_dir / 'run.json')
    identity = {'seed': _number(run.get('seed'), 'seed'), 'consumers': _number(run.get('consumers'), 'consumers'),
                'manifest_digest': _code(run.get('manifest_digest'), 'manifest_digest'), 'commit': _code(run.get('commit'), 'commit'),
                'catalog_digest': _code(run.get('catalog_digest'), 'catalog_digest'), 'catalog_head': _code(str(run.get('catalog_head')), 'catalog_head'),
                'matrix_version': _number(run.get('matrix_version'), 'matrix_version'),
                'template_key': _code((run.get('template') or {}).get('key'), 'template.key'),
                'template_build_seconds': _number((run.get('template') or {}).get('build_seconds'), 'template.build_seconds', allow_none=True),
                'started_at': _code(run.get('started_at'), 'started_at'), 'finished_at': _code(run.get('finished_at'), 'finished_at')}
    cells = []
    cells_dir = run_dir / 'cells'
    for directory in sorted(cells_dir.iterdir()) if cells_dir.is_dir() else []:
        if (directory / 'cell.json').exists():
            cells.append(cell_metrics(directory, set(expected_refusals)))
    names = [c['cell'] for c in cells]
    if len(set(names)) != len(names):
        raise ContractError('duplicate cell')
    cells.sort(key=lambda c: CELLS.index(c['cell']))
    for c in cells:
        if c['role'] == 'gate':
            c['gate'] = gate_verdict(c)
    baseline = None
    if ci_baseline:
        b = _json(Path(ci_baseline))
        baseline = {'run_id': _code(b.get('run_id'), 'baseline.run_id'), 'wall_seconds': _number(b.get('wall_seconds'), 'baseline.wall_seconds'),
                    'deadline_gate_failures': _number(b.get('deadline_gate_failures', 0), 'baseline.deadline_gate_failures'),
                    'failed': _number(b.get('failed', 0), 'baseline.failed')}
    report = {'report_version': 1, 'run': identity, 'thresholds': THRESHOLDS, 'expected_refusals': sorted(expected_refusals),
              'cells': cells, 'ci_interference': ci_interference(cells, baseline), 'at060': at060(cells)}
    _assert_no_free_text(report)
    return report


def _fmt(value):
    if value is None:
        return '—'
    if isinstance(value, float):
        return f'{value:g}'
    return str(value)


def markdown(report):
    r, lines = report['run'], []
    lines += ['# NX-031 矩阵汇总报告', '',
              f"- 提交 `{r['commit']}`，catalog `{r['catalog_head']}`（`{r['catalog_digest'][:16]}…`），矩阵版本 {r['matrix_version']}",
              f"- seed {r['seed']}，消费者 {r['consumers']}，清单 digest `{r['manifest_digest'][:16]}…`，模板 `{r['template_key'][:16]}…`",
              f"- 运行时间 {r['started_at']} → {r['finished_at']}",
              f"- 预期内派发拒绝：{', '.join(report['expected_refusals'])}",
              f"- **AT-060：{report['at060']['result']}**" + (f"（{'; '.join(report['at060']['reasons'])}）" if report['at060']['reasons'] else ''), '',
              '只含指标、门槛结论、seed、digest 和提交，不含原文与 ID（D8）。门槛只判 C4、C4-low（§6.1）；C8 超出部署前提，只观察。', '',
              '口径（负责人 2026-10-13，ROADMAP §7 的 4A、5A）：**空闲派发**是来信受理到对应 Run 任务被 claim（设计稿 §4.6），2 s 门槛只适用于它，分为 relay 段与排队段；'
              '启动段（claim → Host 首次执行授权）和 run_start（任务创建 → 首次执行授权）只观察。**guard p95** 是窗口内全部调用的 p95（各进程直方图合并，误差 ≤ ±5%），'
              '最差一分钟 p95 只观察。旧口径的结果标为 incomparable（caliber_changed），不判通过。**端到端**是来信受理到回复（或兜底、升级）落库，'
              '含模型时间（压测中为 D5 注入的确定性时延），见下方分解表。', '',
              '## 各格指标', '',
              '| 格 | 角色 | 并发 | 速率/s | 后台 | 同机 CI | 完成 | 超时率(5m 最差) | guard p95（全部调用） | ACK p95 | 空闲派发 p95（受理→claim） | 端到端 p95 | 积压峰值 | 排空 s | 连接峰值 | critical | 非预期拒绝 | 身份违例 | 未结局 |',
              '|' + '---|' * 19]
    for c in report['cells']:
        m = c['metrics']
        role = c['role'] + ('（超出部署前提）' if c['beyond_deployment_premise'] else '')
        lines.append('| ' + ' | '.join(_fmt(x) for x in (c['cell'], role, c['host_concurrency'], c['rate_per_s'], '开' if c['background'] else '关',
            '是' if c['ci_background'] else '否', '是' if c['completed'] else f"否 {c['aborted'] or ''}", m.get('guard_timeout_rate_5m_max'),
            m.get('guard_p95_ms'), m.get('ack_p95_ms'), m.get('idle_dispatch_p95_ms'),
            (c.get('decomposition') or {}).get('e2e_ms', {}).get('p95'), m.get('backlog_pending_max'),
            c['drain']['seconds'] if c['drain']['drained'] else '未排空', m.get('connections_peak'), m.get('critical_alerts'),
            m.get('unexpected_refusals'), m.get('identity_violations'), m.get('unresolved_work'))) + ' |')
    lines += ['', '## 门槛判定（C4、C4-low）', '']
    for c in report['cells']:
        if c['role'] != 'gate':
            continue
        lines += [f"### {c['cell']}：{c['gate']['result']}", '', '**门槛项**', '', '| 项 | 实测 | 门槛 | 结论 |', '|---|---|---|---|']
        for name, item in c['gate']['items'].items():
            result = item['result'] + (f"（{item['reason']}）" if item.get('reason') else '')
            lines.append(f"| {name} | {_fmt(item['value'])} | {_fmt(item['limit'])} | {result} |")
        m, segments = c['metrics'], c.get('dispatch_segments') or {}
        lines += ['', '**观察项**（只记录，不判定）', '', '| 项 | 实测 |', '|---|---|',
                  f"| guard_p95_ms_worst_minute | {_fmt(m.get('guard_p95_ms_worst_minute'))} |",
                  f"| run_start_p95_ms（任务创建 → Host 首次执行授权） | {_fmt(m.get('run_start_p95_ms'))} |"]
        for name, label in (('relay', 'relay 段（来信受理 → 任务创建）'), ('queue', '排队段（任务创建 → claim）'), ('start', '启动段（claim → Host 首次执行授权）')):
            seg = segments.get(name) or {}
            lines.append(f"| {label} p50 / p95 | {_fmt(seg.get('p50_ms'))} / {_fmt(seg.get('p95_ms'))} |")
        lines.append('')
    lines += ['## 平台 / 模型 / 工具 分解（逐 Run，ms，p50 / p95）', '',
              '- Run 时长：Host 接受 Run（runtime worker 首次为它登记激活，随即提交 Host）→ 该 Run 的任务结束。模型和工具都落在这段时间内，平台 = Run 时长 − 模型 − 工具，包含授权等待；为负即数据异常（计数，不截成 0，不进统计）。',
              '- 模型：Host 报告的 model_timing（生成段时长减去其中的工具调用）；工具：guard 汇总的 tool_timing。',
              '- 端到端：来信受理 → 回复（或兜底、升级）落库，单独报告，不参与相减；空闲派发：受理 → claim，见上表与门槛判定中的分段。',
              '- 失败的 Run 按原因计数：授权不可用（guard 未应答，Host 有界重试后仍不可用）和授权拒绝（guard 明确拒绝）分开，其余记任务结果码。', '',
              '| 格 | Run 数 | 端到端 | Run 时长 | 模型 | 工具 | 平台 | 无工具计时 | 无模型计时 | 数据异常 | 失败（按原因） | Host 样本 |', '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for c in report['cells']:
        d = c.get('decomposition')
        if not d:
            lines.append(f"| {c['cell']} | missing | | | | | | | | | | {c['host_metrics']} |")
            continue
        pair = lambda k: f"{_fmt(d[k]['p50'])} / {_fmt(d[k]['p95'])}"
        lines.append(f"| {c['cell']} | {d['runs']} | {pair('e2e_ms')} | {pair('run_ms')} | {pair('model_ms')} | {pair('tool_ms')} | {pair('platform_ms')} | "
                     f"{d['runs_without_tool_timing']} | {d['runs_without_model_timing']} | {d['anomalies']} | "
                     f"{', '.join(f'{k} {v}' for k, v in d.get('failures', {}).items()) or '0'} | {c['host_metrics']} |")
    lines += ['', '## relay 各步骤（稳态窗口，ms；各 relay 进程直方图合并的 p50 / p95；total 为已入队来信的 claim → release，authenticate 为每个 tick 的凭据重认证）', '',
              '| 格 | 步骤 | 次数 | p50 | p95 |', '|---|---|---|---|---|']
    for c in report['cells']:
        steps = c.get('relay_steps') or {}
        if not steps:
            lines.append(f"| {c['cell']} | missing | | | |")
        for name, v in steps.items():
            lines.append(f"| {c['cell']} | {name} | {v['count']} | {_fmt(v['p50_ms'])} | {_fmt(v['p95_ms'])} |")
    lines += ['', '## 守卫按操作分项（稳态窗口，ms；p95 为该操作全部调用合并后的 p95，最差一分钟为单进程单窗口）', '',
              '| 格 | 操作 | 调用 | 超时 | 错误 | p95 | 最差一分钟 p95 | max |', '|---|---|---|---|---|---|---|---|']
    for c in report['cells']:
        operations = c.get('guard_operations') or {}
        if not operations:
            lines.append(f"| {c['cell']} | missing | | | | | | |")
        for name, o in operations.items():
            lines.append(f"| {c['cell']} | {name} | {o['calls']} | {o['timeouts']} | {o['errors']} | {_fmt(o['p95_ms'])} | "
                         f"{_fmt(o['p95_ms_worst_minute'])} | {_fmt(o['max_ms'])} |")
    ci = report['ci_interference']
    lines += ['', '## CI 同机影响（§5.3）', '']
    if not ci:
        lines.append('C4 或 C4-ci 缺失，无法比较。')
    else:
        lines += ['| 指标 | C4 | C4-ci |', '|---|---|---|']
        for k, v in ci['load_side'].items():
            lines.append(f"| {k} | {_fmt(v['C4'])} | {_fmt(v['C4-ci'])} |")
        lines.append(f"| drain_seconds | {_fmt(ci['drain_seconds']['C4'])} | {_fmt(ci['drain_seconds']['C4-ci'])} |")
        side = ci['ci_side']
        if side:
            u, b = side['under_load'], side['baseline']
            lines += ['', f"负载下的全量 CI（run {u['run_id']}）：墙钟 {u['wall_seconds']} s，时限门槛失败 {u['deadline_gate_failures']}，失败 {u['failed']}。",
                      f"基线全量 CI（run {b['run_id']}）：墙钟 {b['wall_seconds']} s，时限门槛失败 {b['deadline_gate_failures']}，失败 {b['failed']}。" if b
                      else '基线全量 CI：未提供（--ci-baseline）。']
    lines += ['', '## 各格拒绝与告警', '']
    for c in report['cells']:
        refusals = c.get('refusals') or {}
        lines.append(f"- {c['cell']}：预期内拒绝 {json.dumps(refusals.get('expected', {}))}；非预期 {json.dumps(refusals.get('unexpected', {}))}；"
                     f"触发告警 {json.dumps(c.get('alerts_firing', {}))}；ACK 状态 {json.dumps(c.get('ack_statuses', {}))}")
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--out', type=Path, help='output directory (default: <run_dir>)')
    parser.add_argument('--expected-refusals', default=','.join(DEFAULT_EXPECTED_REFUSALS))
    parser.add_argument('--ci-baseline', type=Path, help='JSON of the reference full CI: run_id, wall_seconds, deadline_gate_failures, failed')
    args = parser.parse_args(argv)
    expected = tuple(x for x in args.expected_refusals.split(',') if x)
    try:
        for code in expected:
            _code(code, 'expected refusal')
        report = build(args.run_dir, expected_refusals=expected, ci_baseline=args.ci_baseline)
    except ContractError as error:
        print(json.dumps({'written': False, 'error': 'contract', 'detail': str(error)}))
        return 2
    out = args.out or args.run_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    (out / 'report.md').write_text(markdown(report))
    print(json.dumps({'written': True, 'at060': report['at060']['result'], 'gates': {c['cell']: c['gate']['result'] for c in report['cells'] if c['role'] == 'gate'}}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
