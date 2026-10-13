"""NX-031 slice 1: the matrix runner (design §3–§5, results contract docs/implementation/NX-031-results-contract.md).

    uv run --frozen python scripts/load/matrix.py --template <run_dir>/template --matrix deploy/load/matrix.v1.json \\  (or matrix.v2.json)
        --out <run_dir> --lane-root <run_dir>/lane [--cells C4,C4-low] [--timing-scale 0.05]

For each cell: clone the template under --lane-root, start the stack (stack.py), log the customers in (warm-up), drive the
Poisson load for the warm-up and the steady window while sampling the NX-030 snapshot every 30 s, stop the inbound load and wait
for the drain, collect the window's samples, refusals, alerts and the §4.4 checks, write cells/<cell>/, then stop every process
and remove the clone. A failing cell is written with `completed: false` and an `aborted` code; the runner exits non-zero if any
cell aborted. C4-ci starts $LOAD_CI_COMMAND inside the lock the lane script already holds (it takes no lock itself).
--timing-scale shortens every duration (local small-scale checks only; the lane always runs 1.0).
"""
import argparse
import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / 'tests'), str(ROOT / 'packages/eios-core/src')]
import collect  # noqa: E402
from driver import Driver  # noqa: E402
from stack import Stack, StackFailed  # noqa: E402
from load_template import Clone  # noqa: E402

CELLS = ('C1', 'C2', 'C4', 'C8', 'C4-low', 'C4-peak', 'C4-quiet', 'C4-ci')
GATES = ('C4', 'C4-low')
# AT-060 §4.4 item 4 (dispatcher 2026-10-13, A + B1): message Runs in the cells use Context v6 with the deployed built-in
# strategy (context-strategies.v1.json; the template publishes it), so every message Run records its model requests.
MESSAGE_CONTEXT_STRATEGY = 'recent_plus_required'
# Dispatcher 2026-10-13: before the steady window, the template's residual due feed items (created before the cell started)
# are waited out while the warm-up traffic continues, up to this cap (scaled with the timing); then the cell goes on regardless
# and records warm_up_not_drained. The residue at the warm-up's end, the wait and the per-poll counts go into cell.json.
RESIDUE_WAIT_SECONDS = 300
RESIDUE_POLL_SECONDS = 5


def now():
    return datetime.now(UTC)


def validate_matrix(matrix):
    cells = [c['cell'] for c in matrix['cells']]
    if matrix.get('schema') not in ('nexloop.load-matrix.v1', 'nexloop.load-matrix.v2') or sorted(cells) != sorted(CELLS):
        raise SystemExit('matrix: the eight design cells are required')
    for c in matrix['cells']:
        if (c['role'] == 'gate') != (c['cell'] in GATES):
            raise SystemExit('matrix: only C4 and C4-low are gate cells (design §6.1)')


def run_json(args, template, matrix):
    meta = json.loads((Path(args.template) / 'template.json').read_text())
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    catalog = (ROOT / 'packages/eios-core/src/eios/migrations/catalog.json').read_bytes()
    return {'run_id': now().strftime('%Y%m%dT%H%M%SZ') + '-' + commit[:12], 'seed': meta['seed'], 'consumers': meta['manifest_totals']['consumers'],
            'manifest_digest': meta['manifest_digest'], 'commit': commit, 'catalog_digest': hashlib.sha256(catalog).hexdigest(),
            'catalog_head': meta.get('catalog_revision'), 'matrix_version': matrix['version'],
            'template': {'key': hashlib.sha256((Path(args.template) / 'template.json').read_bytes()).hexdigest(), 'build_seconds': meta['seconds'].get('total_load')},
            'started_at': now().isoformat(), 'finished_at': None, 'host': {'cpus': os.cpu_count(), 'mem_gib': _mem_gib()}}


def _mem_gib():
    try:
        return round(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 2 ** 30)
    except (ValueError, OSError):
        return None


def accounts(template):
    credentials = json.loads((Path(template) / 'private/credentials.json').read_text())
    restricted = set()
    for line in (Path(template) / 'manifest.jsonl').read_text().splitlines():
        record = json.loads(line)
        if not record.get('trailer') and record['restricted']:
            restricted.add(record['username'])
    return credentials['customers'], restricted, credentials['workbench']


def login_policy(dsn, tenant):
    with psycopg.connect(dsn, autocommit=True) as db:
        row = db.execute("select maximum_attempts,window_seconds,active from control.nexloop_browser_rate_policies "
                         "where tenant_id=%s and action='local_login' and operator_principal_id='nexloop_identity'", (tenant,)).fetchone()
    return {'maximum_attempts': row[0], 'window_seconds': row[1], 'active': row[2]} if row else None


def describe_error(error):
    """Why a cell aborted, without any message text, value or ID (D8): the exception types along the cause chain, a SQLSTATE
    where PostgreSQL gave one, and the traceback as file name, line and function only."""
    import traceback
    chain, frames, seen, current = [], [], set(), error
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        chain.append({'type': type(current).__module__ + '.' + type(current).__qualname__,
                      **({'sqlstate': current.sqlstate} if isinstance(getattr(current, 'sqlstate', None), str) else {})})
        # Each link's own frames (outermost first): the root cause's frames show where it actually failed.
        frames += [f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in traceback.extract_tb(current.__traceback__)][-8:]
        current = current.__cause__ or current.__context__
    return {'chain': chain, 'frames': frames[-24:]}


class Cell:
    def __init__(self, spec, matrix, args, run):
        self.spec, self.matrix, self.args, self.run = spec, matrix, args, run
        self.dir = Path(args.out) / 'cells' / spec['cell']
        self.lane = Path(args.lane_root) / spec['cell']
        scale = args.timing_scale
        t = matrix['timing']
        self.warmup, self.steady, self.drain_max = t['warmup_seconds'] * scale, t['steady_seconds'] * scale, t['drain_max_seconds'] * scale
        self.interval = max(1.0, t['snapshot_interval_seconds'] * scale)
        self.result = {'cell': spec['cell'], 'role': spec['role'], 'host_concurrency': spec['host_concurrency'], 'rate_per_s': spec['rate_per_s'],
                       'background': spec['background'], 'ci_background': spec['ci_background'], 'completed': False, 'aborted': None,
                       'window': {'steady_start': None, 'steady_end': None}, 'drain': {'drained': False, 'seconds': None},
                       'host_metrics': 'approx', 'ci': None,
                       # Beyond the contract, informational: what this stack did not start (stack.py).
                       'message_runs': None, 'message_context_strategy': MESSAGE_CONTEXT_STRATEGY, 'timing_scale': scale}

    def execute(self):
        self.started_at = now()
        self.dir.mkdir(parents=True)
        self.lane.mkdir(parents=True)
        clone = stack = None
        try:
            try:
                clone = Clone(self.args.template, self.lane / 'pg')
            except Exception as error:
                return self.abort('template_clone_failed', error)
            stack = Stack(clone, self.args.template, self.lane, self.spec)
            collect.TENANT = stack.tenant
            # The login rate limit is a security control: the cell runs under the policy the template recorded from its trusted
            # configuration, never a relaxed one (dispatcher 2026-10-11).
            self.result['login_rate_policy'] = login_policy(clone.dsn, stack.tenant)
            if self.result['login_rate_policy'] != json.loads((Path(self.args.template) / 'template.json').read_text()).get('login_rate_policy') \
                    or not self.result['login_rate_policy']:
                return self.abort('login_policy_mismatch')
            try:
                stack.start_api()
                if self.spec['background']:
                    stack.start_background()
                latency = self.matrix['model_latency']
                pending = stack.start_message_chain(self.spec['host_concurrency'], latency={'median_ms': latency['median_ms'], 'p95_ms': latency['p95_ms'],
                                                                                            'seed': self.run['seed']},
                                                     context_strategy=MESSAGE_CONTEXT_STRATEGY)
                self.result['message_runs'] = 'started' if pending is None else pending
            except (StackFailed, OSError, subprocess.SubprocessError) as error:
                return self.abort('service_start_failed', error)
            asyncio.run(self.drive(stack))
            if not all(stack.alive().values()):
                return self.abort('service_exited')
            self.result['completed'] = True
            self._record_fk_state()
            return self.result
        except Exception as error:
            return self.abort(self.result['aborted'] or 'runner_error', error)
        finally:
            if stack is not None:
                stack.stop()
            if clone is not None:
                try:
                    clone.stop()
                except Exception:
                    pass
            # The cell's private files (service tokens, DSNs) do not outlive the cell; logs and artifacts stay for the lane summary.
            import shutil
            shutil.rmtree(self.lane / 'private', ignore_errors=True)
            (self.dir / 'cell.json').write_text(json.dumps(self.result, ensure_ascii=False, indent=2, default=str) + '\n')

    def abort(self, code, error=None):
        self.result.update(completed=False, aborted=code)
        if error is not None:
            self.result['error'] = describe_error(error)
        self._record_fk_state()
        return self.result

    def _record_fk_state(self):
        """NX-052 N state: what the cell's foreign key drop did (batches, count, missing constraint identifiers), if it ran."""
        try:
            self.result['tenant_fk_state'] = json.loads((self.lane / 'tenant-fk-state.json').read_text())
        except (OSError, ValueError):
            pass

    async def drive(self, stack):
        customers, restricted, workbench = accounts(self.args.template)
        driver = Driver(origins=stack.api_origins, ca=str(stack.ca_file), accounts=customers, restricted=restricted,
                        workbench=workbench, matrix=self.matrix, seed=self.run['seed'])
        acks, events = [], []
        try:
            self.result['login_failures'] = await driver.login_all()
            await driver.run_phase(self.warmup, self.spec['rate_per_s'], lambda r: None)  # warm-up: not measured
            def query():
                with psycopg.connect(stack.admin_dsn, autocommit=True) as db:
                    return collect.residue(db, self.started_at)
            self.result['warm_up_residue'] = await self.settle_residue(lambda: asyncio.to_thread(query),
                                                                       lambda seconds: driver.run_phase(seconds, self.spec['rate_per_s'], lambda r: None))
            ci = self.start_ci() if self.spec['ci_background'] else None
            start = now()
            self.result['window']['steady_start'] = start.isoformat()
            sampler = asyncio.create_task(self.sample(stack, 'steady'))
            await asyncio.gather(driver.run_phase(self.steady, self.spec['rate_per_s'], lambda r: acks.append({'ack_ms': r[0], 'status': r[1]})),
                                 driver.workbench_activity(self.steady, events))
            end = now()
            sampler.cancel()
            self.result['window']['steady_end'] = end.isoformat()
            self.result['sent'] = driver.sent
            collect.jsonl(self.dir / 'acks.jsonl', acks)
            self.result['workbench_events'] = _count(events)
            await self.drain(stack)
            cross = await driver.cross_access_probe(self.matrix['cross_access_probe_pairs'])
            with psycopg.connect(stack.admin_dsn, autocommit=True) as db:
                collect.jsonl(self.dir / 'process_samples.jsonl', collect.process_samples(db, start, now()))
                collect.jsonl(self.dir / 'refusals.jsonl', collect.refusals(db, start, now()))
                collect.jsonl(self.dir / 'alerts.jsonl', collect.alerts(db, start, now()))
                collect.jsonl(self.dir / 'runs.jsonl', collect.runs(db, start, end))
                collect.jsonl(self.dir / 'dispatch.jsonl', collect.dispatch(db, start, end))
                (self.dir / 'checks.json').write_text(json.dumps(collect.checks(db, start, end, cross), indent=2) + '\n')
                self.result['jobs'] = collect.jobs_summary(db)
                self.result['run_durations'] = collect.run_durations(db)
            if ci is not None:
                self.result['ci'] = self.finish_ci(ci)
        finally:
            await driver.close()

    async def settle_residue(self, query, warm, clock=time.monotonic):
        """Wait (warm-up traffic going on, unmeasured) until no residual due feed item of the template remains, or the cap.
        `query` returns {feed: due count}; `warm(seconds)` drives the warm-up for that long. Never aborts the cell."""
        cap = RESIDUE_WAIT_SECONDS * self.args.timing_scale
        started = clock()
        first = await query()
        polls, current = [], first
        while True:
            elapsed = clock() - started
            polls.append({'seconds': round(elapsed, 1), 'due': sum(current.values())})
            if not current or not sum(current.values()):
                return {'at_start': sum(first.values()), 'by_feed': first, 'drained': True, 'seconds': round(elapsed, 1), 'code': None, 'polls': polls}
            if elapsed >= cap:
                return {'at_start': sum(first.values()), 'by_feed': first, 'drained': False, 'seconds': round(elapsed, 1), 'code': 'warm_up_not_drained',
                        'remaining': current, 'polls': polls}
            await warm(min(RESIDUE_POLL_SECONDS, max(0.1, cap - elapsed)))
            current = await query()

    async def sample(self, stack, phase):
        while True:
            def one():
                with psycopg.connect(stack.admin_dsn, autocommit=True) as db:
                    collect.jsonl(self.dir / 'snapshots.jsonl', [{'at': now().isoformat(), 'phase': phase, 'snapshot': collect.snapshot(db)}])
            try:
                await asyncio.to_thread(one)
            except psycopg.Error:
                pass
            await asyncio.sleep(self.interval)

    async def drain(self, stack):
        started = time.monotonic()
        sampler = asyncio.create_task(self.sample(stack, 'drain'))
        try:
            while time.monotonic() - started < self.drain_max:
                def pending():
                    with psycopg.connect(stack.admin_dsn, autocommit=True) as db:
                        return collect.drained(db)
                if await asyncio.to_thread(pending) == 0:
                    self.result['drain'] = {'drained': True, 'seconds': round(time.monotonic() - started)}
                    return
                await asyncio.sleep(min(5.0, self.interval))
            self.result['drain'] = {'drained': False, 'seconds': round(time.monotonic() - started)}
        finally:
            sampler.cancel()

    def start_ci(self):
        command = os.environ.get('LOAD_CI_COMMAND')
        if not command:
            self.result['aborted'] = 'ci_command_missing'
            raise RuntimeError('LOAD_CI_COMMAND required for C4-ci')
        import threading
        log = open(self.lane / 'ci.log', 'w')
        state = {'started': time.monotonic(), 'started_wall': time.time(), 'ended': None}
        process = subprocess.Popen(shlex.split(command), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)

        def wait():
            process.wait()
            state['ended'] = time.monotonic()  # the CI's own wall time, not when the cell collects it
        threading.Thread(target=wait, daemon=True).start()
        return process, state, log

    def finish_ci(self, ci):
        process, state, log = ci
        process.wait()
        while state['ended'] is None:
            time.sleep(0.05)
        log.close()
        # Only reports written by this CI run (older .ci-results files never count).
        reports = [p for p in (ROOT / '.ci-results').glob('*.xml') if p.stat().st_mtime >= state['started_wall']] if (ROOT / '.ci-results').exists() else []
        gate = [p for p in reports if p.name == 'python-deadline-gate.xml']
        counts = [_junit_failures(p) for p in reports]
        return {'run_id': self.run['run_id'] + '-ci', 'wall_seconds': round(state['ended'] - state['started']), 'exit': process.returncode,
                'deadline_gate_failures': _junit_failures(gate[0]) if gate else None,
                'failed': sum(c for c in counts if c is not None) if reports and None not in counts else None}


def _junit_failures(path):
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == 'testsuite' else list(root)
        return sum(int(s.get('failures', 0)) + int(s.get('errors', 0)) for s in suites)
    except (ET.ParseError, OSError, ValueError):
        return None


def _count(events):
    summary = {}
    for event in events:
        for key, value in event.items():
            label = f'{key}:{json.dumps(value, sort_keys=True)}' if key != 'takeover_cycle' else f'{key}:{json.dumps(value, sort_keys=True)}'
            summary[label] = summary.get(label, 0) + 1
    return summary


def resume_run(out, fresh):
    """The interrupted run's run.json, kept: same template and commit are required (results must belong to one commit)."""
    path = out / 'run.json'
    if not path.is_file():
        raise SystemExit('resume: no run.json in --out')
    run = json.loads(path.read_text())
    if run['template']['key'] != fresh['template']['key'] or run['commit'] != fresh['commit'] or run['manifest_digest'] != fresh['manifest_digest']:
        raise SystemExit('resume: template, manifest or commit differs from the interrupted run')
    run.setdefault('resumed_at', []).append(now().isoformat())
    run['finished_at'] = None
    return run


def prepare_resume(out, run, cell, lane_root=None):
    """False when the cell completed earlier (kept as is). Otherwise an existing incomplete directory is moved to attempts/ and
    recorded in run.json, and the cell runs again."""
    directory = out / 'cells' / cell
    lane = Path(lane_root) / cell if lane_root else None
    if not directory.exists():
        if lane is not None and lane.exists():  # a cell interrupted before writing its result: keep its lane logs aside
            attempts = Path(lane_root) / 'attempts'
            attempts.mkdir(exist_ok=True)
            lane.rename(attempts / f'{cell}.{1 + sum(1 for p in attempts.iterdir() if p.name.startswith(cell + "."))}')
        return True
    result = json.loads((directory / 'cell.json').read_text()) if (directory / 'cell.json').is_file() else {}
    if result.get('completed') is True:
        return False
    attempts = out / 'attempts'
    attempts.mkdir(exist_ok=True)
    n = 1 + sum(1 for p in attempts.iterdir() if p.name.startswith(cell + '.'))
    target = attempts / f'{cell}.{n}'
    directory.rename(target)
    if lane is not None and lane.exists():  # its lane (stopped cluster copy, process logs) goes beside it
        (Path(lane_root) / 'attempts').mkdir(exist_ok=True)
        lane.rename(Path(lane_root) / 'attempts' / f'{cell}.{n}')
    run.setdefault('attempts', []).append({'cell': cell, 'attempt': n, 'aborted': result.get('aborted'), 'kept_in': f'attempts/{cell}.{n}'})
    (out / 'run.json').write_text(json.dumps(run, indent=2) + '\n')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--template', required=True)
    parser.add_argument('--matrix', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--lane-root', required=True)
    parser.add_argument('--cells', default=','.join(CELLS))
    parser.add_argument('--timing-scale', type=float, default=1.0)
    parser.add_argument('--resume', action='store_true',
                        help='continue an interrupted run in --out: completed cells are kept, an incomplete cell directory is moved to '
                             'attempts/<cell>.<n> (kept, never overwritten) and the cell runs again')
    args = parser.parse_args(argv)
    if os.environ.get('REAL_DISPATCH_ENABLED', 'false') != 'false':
        raise SystemExit('REAL_DISPATCH_ENABLED must be false')
    if not 0 < args.timing_scale <= 1:
        raise SystemExit('--timing-scale must be in (0, 1]')
    matrix = json.loads(Path(args.matrix).read_text())
    validate_matrix(matrix)
    wanted = [c for c in args.cells.split(',') if c]
    if set(wanted) - set(CELLS):
        raise SystemExit('unknown cell')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    Path(args.lane_root).mkdir(parents=True, exist_ok=True)
    run = run_json(args, args.template, matrix)
    if args.resume:
        run = resume_run(out, run)
    (out / 'run.json').write_text(json.dumps(run, indent=2) + '\n')
    aborted = 0
    for spec in matrix['cells']:
        if spec['cell'] not in wanted:
            continue
        if args.resume and not prepare_resume(out, run, spec['cell'], args.lane_root):
            print(f'cell {spec["cell"]}: kept (completed earlier)', flush=True)
            continue
        print(f'cell {spec["cell"]}: start', flush=True)
        result = Cell(spec, matrix, args, run).execute()
        aborted += not result['completed']
        print(f'cell {spec["cell"]}: completed={result["completed"]} aborted={result["aborted"]}', flush=True)
        if result.get('error'):
            print(f'cell {spec["cell"]}: error {json.dumps(result["error"], separators=(",", ":"))}', flush=True)
    run['finished_at'] = now().isoformat()
    (out / 'run.json').write_text(json.dumps(run, indent=2) + '\n')
    return 1 if aborted else 0


if __name__ == '__main__':
    sys.exit(main())
