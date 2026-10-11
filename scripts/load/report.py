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
    'guard_timeout_rate_5m_max': 0.01, 'guard_p95_ms_max': 1500, 'dead_letters_delta': 0, 'ack_p95_ms': 500,
    'idle_dispatch_p95_ms': 2000, 'drain_seconds': 600, 'connections_peak': 60, 'critical_alerts': 0,
    'unexpected_refusals': 0, 'identity_violations': 0, 'unresolved_work': 0,
}
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
            pending += _number(value.get('pending') or 0, f'{group}.pending') + _number(value.get('retry_wait') or 0, f'{group}.retry_wait')
            age = _number(value.get('oldest_pending_seconds'), f'{group}.oldest', allow_none=True)
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
    """AT-060 platform / model / tool per Run: platform = e2e - model - tool (D6). Runs without tool timing are counted, not guessed."""
    model, tool, platform, e2e, negative, no_tool = [], [], [], [], 0, 0
    outcomes = {}
    for row in runs:
        total = _number(row.get('e2e_ms'), 'runs.e2e_ms')
        m = _number(row.get('model_ms') or 0, 'runs.model_ms')
        t = _number(row.get('tool_ms'), 'runs.tool_ms', allow_none=True)
        outcome = _code(row.get('outcome'), 'runs.outcome')
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        e2e.append(total), model.append(m)
        if t is None:  # no tool timing: tool and platform unknown for this Run, counted but never guessed as 0
            no_tool += 1
            continue
        rest = total - m - t
        if rest < 0:
            negative += 1
            rest = 0
        tool.append(t), platform.append(rest)
    stats = lambda values: {'p50': percentile(values, 0.5), 'p95': percentile(values, 0.95)}
    return {'runs': len(e2e), 'e2e_ms': stats(e2e), 'model_ms': stats(model), 'tool_ms': stats(tool), 'platform_ms': stats(platform),
            'runs_without_tool_timing': no_tool, 'inconsistent_runs': negative, 'outcomes': dict(sorted(outcomes.items()))}


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
        rate, p95, calls = guard_windows(samples, start, end)
        metrics.update(guard_timeout_rate_5m_max=rate, guard_p95_ms_max=p95, guard_calls=calls)
        out['host'] = host_summary(samples, start, end)
        out['pool'] = pool_summary(samples, start, end)
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
        values = [_number(d.get('dispatch_ms'), 'dispatch_ms') for d in dispatch]
        metrics.update(idle_dispatch_count=len(values), idle_dispatch_p95_ms=percentile(values, 0.95))
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
    check('guard_p95_ms_max', m.get('guard_p95_ms_max'), THRESHOLDS['guard_p95_ms_max'])
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
        identity = c['metrics'].get('identity_violations')
        if identity is None:
            reasons.append(f"{c['cell']}:identity_checks_missing")
        elif identity > 0:
            reasons.append(f"{c['cell']}:identity_violations")
        if c['role'] == 'gate' and c['gate']['result'] != 'passed':
            reasons.append(f"{c['cell']}:gate_" + ','.join(k for k, v in c['gate']['items'].items() if v['result'] != 'pass'))
    return {'result': 'passed' if not reasons else 'failed', 'reasons': reasons}


def ci_interference(cells, baseline):
    """§5.3: C4-ci against C4, and the CI run under load against a baseline full CI (when given)."""
    by = {c['cell']: c for c in cells}
    if 'C4' not in by or 'C4-ci' not in by:
        return None
    keys = ('guard_timeout_rate_5m_max', 'guard_p95_ms_max', 'ack_p95_ms', 'backlog_pending_max', 'backlog_oldest_seconds_max', 'connections_peak')
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
              '两个时延口径分开给出：**派发**是 Run 任务入队到 Host 接受 Run，不含模型时间，2 s 限值只适用于它；**端到端**是来信受理到回复（或兜底、升级）落库，'
              '含模型时间（压测中为 D5 注入的确定性时延），见下方分解表。', '',
              '## 各格指标', '',
              '| 格 | 角色 | 并发 | 速率/s | 后台 | 同机 CI | 完成 | 超时率(5m 最差) | guard p95 max | ACK p95 | 派发 p95（入队→Host 接受） | 端到端 p95 | 积压峰值 | 排空 s | 连接峰值 | critical | 非预期拒绝 | 身份违例 | 未结局 |',
              '|' + '---|' * 19]
    for c in report['cells']:
        m = c['metrics']
        role = c['role'] + ('（超出部署前提）' if c['beyond_deployment_premise'] else '')
        lines.append('| ' + ' | '.join(_fmt(x) for x in (c['cell'], role, c['host_concurrency'], c['rate_per_s'], '开' if c['background'] else '关',
            '是' if c['ci_background'] else '否', '是' if c['completed'] else f"否 {c['aborted'] or ''}", m.get('guard_timeout_rate_5m_max'),
            m.get('guard_p95_ms_max'), m.get('ack_p95_ms'), m.get('idle_dispatch_p95_ms'),
            (c.get('decomposition') or {}).get('e2e_ms', {}).get('p95'), m.get('backlog_pending_max'),
            c['drain']['seconds'] if c['drain']['drained'] else '未排空', m.get('connections_peak'), m.get('critical_alerts'),
            m.get('unexpected_refusals'), m.get('identity_violations'), m.get('unresolved_work'))) + ' |')
    lines += ['', '## 门槛判定（C4、C4-low）', '']
    for c in report['cells']:
        if c['role'] != 'gate':
            continue
        lines += [f"### {c['cell']}：{c['gate']['result']}", '', '| 项 | 实测 | 门槛 | 结论 |', '|---|---|---|---|']
        for name, item in c['gate']['items'].items():
            lines.append(f"| {name} | {_fmt(item['value'])} | {_fmt(item['limit'])} | {item['result']} |")
        lines.append('')
    lines += ['## 平台 / 模型 / 工具 分解（逐 Run，ms，p50 / p95）', '',
              '| 格 | Run 数 | 端到端 | 模型 | 工具 | 平台 | 无工具计时 | 不一致 | Host 样本 |', '|---|---|---|---|---|---|---|---|---|']
    for c in report['cells']:
        d = c.get('decomposition')
        if not d:
            lines.append(f"| {c['cell']} | missing | | | | | | | {c['host_metrics']} |")
            continue
        pair = lambda k: f"{_fmt(d[k]['p50'])} / {_fmt(d[k]['p95'])}"
        lines.append(f"| {c['cell']} | {d['runs']} | {pair('e2e_ms')} | {pair('model_ms')} | {pair('tool_ms')} | {pair('platform_ms')} | "
                     f"{d['runs_without_tool_timing']} | {d['inconsistent_runs']} | {c['host_metrics']} |")
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
