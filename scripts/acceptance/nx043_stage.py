#!/usr/bin/env python3
"""NX-043 X7 on stage: the screening and shadow parts of the strategy experiments (design §2-§9, D1-D9; the acceptance list is
docs/implementation/NX-043-X7-acceptance.md). Same configuration, result and redaction formats as the S6 and AT-046 stage scripts
(scripts/acceptance/s6_common.py; result schema nexloop-s6-stage-result/1, checks `nx043-<phase>`).

Every write goes through the owner's governed workbench Actions over HTTPS (password from stdin) or, for the shadow decisions, through
the simulation worker's own entry with the shadow world's credential; the probe is read only (the bootstrap DSN with
default_transaction_read_only=on, the dispatcher's). The identifiers the phases share (worlds, experiments, strategies) are kept in a
private state file in results_dir (0600), never in the output, which holds codes, counts and digests only.

    nx043_stage.py prepare --config PRIVATE.json [--dry-run]       owner: create_sim_world (shadow), publish_strategy x2, register_experiment
                                                                     (shadow; and screening when inputs.screening names a sim world and twin)
    nx043_stage.py run --config PRIVATE.json [--dry-run]           owner: start_experiment (screening refused twin_not_calibrated, D9; shadow
                                                                     started), set_shadow_copy on
    # real inbound traffic of the tenant while the copy is on (the dispatcher's synthetic webchat Consumers), then:
    nx043_stage.py probe-before --config DATA.json                 probe: world-real digests, the real dispatch switches (after that traffic)
    nx043_stage.py decide --config DATA+WORKER.json                sim worker + probe in one process: the candidate's shadow decisions on the
                                                                     copied events (record only)
    nx043_stage.py report --config PRIVATE.json                    owner: read_experiment_report (shadow difference report, experiment
                                                                     result) aggregates only; set_shadow_copy off
    nx043_stage.py probe-after --config DATA.json --from BEFORE    probe: the guard stopped the shadow experiment at its end (D4), nothing
                                                                     sent from the shadow world, the switches and every world-real row unchanged
    nx043_stage.py gaps --config ANY.json                          no connection: what the stage cannot show yet (blocked, never a pass)

inputs: shadow_purpose, candidate_levers, control_levers (D1 levers), strategy_prefix, experiment_prefix, ends_in_minutes (shadow
    experiment end; the guard stops it there), screening {world, twin {twin_id, version}} (optional), decisions {kind, limit},
    guard_timeout_seconds (probe-after waits for the guard stop, default 900)
roles: probe {dsn_file}; sim_worker {dsn_file, token_file} (the shadow world's worker credential; optional); owner {origin, ca_file,
    username_file}.
Exit: 0 passed, 1 failed or blocked, 2 input refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
from at046_stage import Ports, real_digest  # noqa: E402
from s6_common import RESULT_SCHEMA, Blocked, Redactor, Refused, Result, connect_from, read_private  # noqa: E402

IDS = re.compile(r'[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
ACCEPTANCE = {'prepare': ['NX-043 D1', 'NX-043 D3'], 'probe-before': ['AT-046'], 'run': ['NX-043 D3', 'NX-043 D7', 'NX-043 D9'],
              'decide': ['NX-043 D7', 'AT-046'],
              'probe-after': ['AT-046', 'NX-043 D4', 'NX-043 §9'], 'report': ['NX-043 D6', 'NX-043 §6', 'NX-043 D7'],
              'gaps': ['NX-043 D2', 'NX-043 D5', 'NX-043 online']}


# --- private state (ids between phases, never in the output) ---------------------------------------------------------------------

def _state_path(config):
    return Path(config['results_dir']) / 'nx043-state.json'


def load_state(config):
    try:
        return json.loads(read_private(_state_path(config), 65536))
    except Refused:
        raise Blocked('no private state: run prepare first') from None


def save_state(config, state):
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(state, f)


def _ok(status):
    return status == 200


def _reason(out):
    return out.get('reason') if isinstance(out, dict) else None


# --- phases ----------------------------------------------------------------------------------------------------------------------

def strategy_definition(strategy_id, levers):
    return {'schema': 'nexloop-strategy-version/1', 'strategy_id': strategy_id, 'levers': levers}


def registration(experiment_id, stage, world, candidate, control, twin, ends_at):
    return {'schema': 'nexloop-experiment-registration/1', 'experiment_id': experiment_id, 'stage': stage, 'world': world,
            # No code like NX-043 in the text: the registration's real-content guard reads letters-dash-digits as a member number.
            'hypothesis': 'Stage acceptance (synthetic): the candidate follow-up timing is no worse than the control',
            'candidate': candidate, 'control': control, 'twin': twin, 'primary_metric': {'key': 'payment', 'direction': 'increase'},
            'guardrails': {'refusal_or_unsubscribe_rate_delta': 0.02}, 'sample_size': {'candidate': 20, 'control': 20},
            'maturity': {'window_days': 14}, 'budget': {'model_cny': 1, 'incentive_cny': 0}, 'ends_at': ends_at,
            'allocation_seed': int(hashlib.sha256(experiment_id.encode()).hexdigest()[:7], 16)}  # at most 9 digits


def prepare(ports, config, r):
    inputs = config.get('inputs') or {}
    # Letters only: ids land in the registration, whose real-content guard refuses 7+ digits in a row.
    run = ''.join(chr(97 + int(c, 16)) for c in uuid.uuid4().hex[:10])
    key = 'nx043-' + run
    status, out = ports.act('create_sim_world', {'kind': 'shadow', 'purpose': inputs.get('shadow_purpose') or 'NX-043 X7 stage acceptance (synthetic)',
                                                 'expires_in_days': 7}, key + '-w')
    world = out.get('world') if isinstance(out, dict) else None
    r.step('shadow_world_created', 'a shadow:<uuid> world (D7)', {'status': status}, _ok(status) and isinstance(world, str) and world.startswith('shadow:'))
    if not world:
        return
    prefix = inputs.get('strategy_prefix') or 'x7'
    strategies = {}
    for arm, levers in (('candidate', inputs.get('candidate_levers') or {'follow_up_delay_hours': 48}),
                        ('control', inputs.get('control_levers') or {'follow_up_delay_hours': 24})):
        sid = f'{prefix}.{arm}.{run}'
        status, out = ports.act('publish_strategy', {'definition': strategy_definition(sid, levers)}, f'{key}-s-{arm}')
        strategies[arm] = {'strategy_id': sid, 'version': out.get('version') if isinstance(out, dict) else None}
        r.step(f'{arm}_strategy_published', 'a published strategy version with D1 levers only', {'status': status, 'reason': _reason(out)},
               _ok(status) and bool(strategies[arm]['version']))
    if not all(s['version'] for s in strategies.values()):
        return
    ends = (datetime.now(UTC) + timedelta(minutes=int(inputs.get('ends_in_minutes') or 30))).strftime('%Y-%m-%dT%H:%M:%SZ')
    experiments = {}
    eprefix = inputs.get('experiment_prefix') or 'x7'
    shadow_id = f'{eprefix}.shadow.{run}'
    status, out = ports.act('register_experiment', {'registration': registration(shadow_id, 'shadow', world, strategies['candidate'], strategies['control'],
                                                                                 None, ends)}, key + '-e-shadow')
    r.step('shadow_experiment_registered', 'a frozen preregistration (D3) in the shadow world', {'status': status, 'reason': _reason(out)},
           _ok(status) and isinstance(out, dict) and bool(out.get('registration_digest')))
    if _ok(status):
        experiments['shadow'] = shadow_id
    screening = inputs.get('screening') or {}
    if screening.get('world') and (screening.get('twin') or {}).get('twin_id'):
        sid = f'{eprefix}.screening.{run}'
        status, out = ports.act('register_experiment', {'registration': registration(sid, 'screening', screening['world'], strategies['candidate'],
                                                                                     strategies['control'], screening['twin'], ends)}, key + '-e-screening')
        r.step('screening_experiment_registered', 'a screening registration with its twin (calibration checked at start, D9)',
               {'status': status, 'reason': _reason(out)}, _ok(status))
        if _ok(status):
            experiments['screening'] = sid
    else:
        r.block('screening_experiment_registered', 'inputs.screening (a sim world and a twin version) not configured')
    save_state(config, {'shadow_world': world, 'strategies': strategies, 'experiments': experiments, 'run': run, 'ends_at': ends})


def probe_before(ports, config, r, prior=None):
    tenant = config['tenant_id']
    digests = real_digest(ports, tenant)
    switches = ports.read("select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from control.nexloop_real_dispatch_switches x")[0][0]
    r.step('real_digest_before', 'the world-real digests and the real dispatch switches taken', {'tables': len(digests)}, bool(digests),
           digests=digests, switches=switches)


def run_phase(ports, config, r, prior=None):
    state = load_state(config)
    exps = state['experiments']
    if 'screening' in exps:
        status, out = ports.act('start_experiment', {'experiment_id': exps['screening']}, 'nx043-' + state['run'] + '-start-screening')
        r.step('screening_start_needs_a_calibrated_twin', 'refused twin_not_calibrated (D9: no calibrated twin on stage)',
               {'status': status, 'reason': _reason(out)}, status == 409 and _reason(out) == 'twin_not_calibrated')
    else:
        r.block('screening_start_needs_a_calibrated_twin', 'no screening experiment registered (inputs.screening)')
    if 'shadow' not in exps:
        r.step('shadow_experiment_started', 'started', {'registered': False}, False)
        return
    status, out = ports.act('start_experiment', {'experiment_id': exps['shadow']}, 'nx043-' + state['run'] + '-start-shadow')
    r.step('shadow_experiment_started', 'started (the start rechecks the registration; it opens no dispatch switch, D3)',
           {'status': status, 'reason': _reason(out), 'started': out.get('started') if isinstance(out, dict) else None}, _ok(status))
    status, out = ports.act('set_shadow_copy', {'world': state['shadow_world'], 'enabled': True, 'reason': 'NX-043 X7 stage acceptance (synthetic)'},
                            'nx043-' + state['run'] + '-copy-on')
    r.step('shadow_copy_on', 'the tenant switch on for the shadow world (D7, default off)', {'status': status, 'reason': _reason(out)}, _ok(status))


def decide(ports, config, r, prior=None):
    """After real inbound traffic while the copy is on: the candidate strategy's decisions on the copied events, recorded only."""
    state = load_state(config)
    inputs = config.get('inputs') or {}
    d = inputs.get('decisions') or {}
    if ports.decide is None or ports.probe is None:
        r.block('shadow_decisions_recorded', 'the simulation worker of the shadow world and the probe in one process (roles.sim_worker, roles.probe)')
        return
    keys = [k for (k,) in ports.read("""select event_key from runtime.nexloop_shadow_events where tenant_id=%s and world=%s order by copied_hour desc,event_key
        limit %s""", (config['tenant_id'], state['shadow_world'], int(d.get('limit') or 20)))]
    if not keys:
        r.block('shadow_decisions_recorded', 'no inbound real event copied into the shadow world yet (it needs real inbound traffic while on)')
        return
    outs = [ports.decide(state['shadow_world'], k, state['strategies']['candidate'], d.get('kind') or 'reply') for k in keys]
    r.step('shadow_decisions_recorded', 'recorded only, never sent (D7)',
           {'decisions': len(outs), 'sent': sum(1 for o in outs if o.get('sent'))}, bool(outs) and not any(o.get('sent') for o in outs))


def probe_after(ports, config, r, prior):
    state = load_state(config)
    tenant, world = config['tenant_id'], state['shadow_world']
    inputs = config.get('inputs') or {}
    if 'shadow' in state['experiments']:
        deadline = time.monotonic() + int(inputs.get('guard_timeout_seconds') or 900)
        stop = None
        while True:
            rows = ports.read("""select recorded_by,reason from control.nexloop_experiment_events where tenant_id=%s and experiment_id=%s and status='stopped'""",
                              (tenant, state['experiments']['shadow']))
            stop = rows[0] if rows else None
            if stop or time.monotonic() >= deadline:
                break
            time.sleep(10)
        r.step('guard_stops_at_the_end', 'the experiment guard (alert evaluator tick) stopped the shadow experiment at its end (D4)',
               {'stopped_by_guard': bool(stop) and stop[0] == 'nexloop:experiment-guard', 'reason': stop[1] if stop else None},
               bool(stop) and stop[0] == 'nexloop:experiment-guard' and stop[1] == 'guardrail: ended')
    sent = ports.read("""select (select count(*) from runtime.nexloop_outbound_messages where tenant_id=%s and world=%s)
          +(select count(*) from runtime.nexloop_effect_intents where tenant_id=%s and world=%s)""", (tenant, world, tenant, world))[0][0]
    simulated = ports.read('select count(*) from runtime.nexloop_sim_effect_intents where tenant_id=%s and world=%s', (tenant, world))[0][0]
    r.step('shadow_sends_nothing', 'no outbound message and no real effect intent in the shadow world (§9: zero provider requests)',
           {'real_sends': sent, 'stand_in_intents': 'some' if simulated else 'none'}, sent == 0)
    copied = ports.read('select count(*) from runtime.nexloop_shadow_events where tenant_id=%s and world=%s', (tenant, world))[0][0]
    r.step('shadow_copy_counts', 'redacted copies of real inbound events (counts only)', {'copied': 'some' if copied else 'none'}, True)
    before = next((s.get('evidence') or {} for s in (prior or []) if s.get('name') == 'real_digest_before'), {})
    after = real_digest(ports, tenant)
    switches = ports.read("select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from control.nexloop_real_dispatch_switches x")[0][0]
    changed = sorted(t for t in set(before.get('digests') or {}) | set(after) if (before.get('digests') or {}).get(t) != after.get(t))
    r.step('real_dispatch_switches_unchanged', 'the experiment opened no real dispatch switch (§9, D3)', {'unchanged': switches == before.get('switches')},
           bool(before) and switches == before.get('switches'))
    r.step('real_side_unchanged', 'every world-real row of the tenant unchanged (AT-046 digests)', {'tables': len(after), 'changed': changed},
           bool(before.get('digests')) and not changed)


def report(ports, config, r, prior=None):
    state = load_state(config)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    status, out = ports.act('read_experiment_report', {'kind': 'shadow', 'world': state['shadow_world'], 'strategy_id': state['strategies']['candidate']['strategy_id'],
                                                       'strategy_version': state['strategies']['candidate']['version'], 'since': today.isoformat(),
                                                       'until': (today + timedelta(days=1)).isoformat()}, 'nx043-' + state['run'] + '-report-shadow')
    # Individual identifiers (a Consumer, message or conversation): none. The report's own world, id, digest and strategy are its frame.
    body = {k: v for k, v in out.items() if k not in ('world', 'report_id', 'report_digest', 'strategy')} if isinstance(out, dict) else {}
    individual = bool(IDS.search(json.dumps(body, sort_keys=True)))
    r.step('shadow_report_aggregates_only', 'the frozen difference report: cells over 30 Consumers only, none/some side counts, no individual identifier',
           {'status': status, 'cells': len(out.get('cells') or []) if isinstance(out, dict) else None,
            'suppressed': out.get('suppressed_cells') if isinstance(out, dict) else None, 'identifiers': individual},
           _ok(status) and isinstance(out, dict) and out.get('suppressed_cells') in ('none', 'some') and not individual)
    if 'shadow' in state['experiments']:
        status, out = ports.act('read_experiment_report', {'kind': 'experiment', 'experiment_id': state['experiments']['shadow'], 'as_of': today.isoformat()},
                                'nx043-' + state['run'] + '-report-experiment')
        r.step('experiment_result_reports_nothing_below_the_minimum', 'not_reported (no matured allocations; D6 minimum 30 per group), no rate',
               {'status': status, 'conclusion': out.get('conclusion') if isinstance(out, dict) else None, 'reason': _reason(out) or (out.get('reason') if isinstance(out, dict) else None)},
               _ok(status) and isinstance(out, dict) and out.get('conclusion') == 'not_reported' and out.get('difference') is None)
    status, out = ports.act('set_shadow_copy', {'world': state['shadow_world'], 'enabled': False, 'reason': 'NX-043 X7 stage acceptance done'},
                            'nx043-' + state['run'] + '-copy-off')
    r.step('shadow_copy_off', 'the switch off again', {'status': status}, _ok(status))


def gaps(ports, config, r, prior=None):
    """What the stage cannot show yet: blocked (passed null), with the slice that closes each gap."""
    r.block('allocation', 'X4 allocation is not wired to any service entry (runtime.nexloop_experiment_assign is owner-only); local: tests/test_nx043_allocation_pg.py')
    r.block('experiment_budget', 'needs simulation Runs of the experiment world (the AT-046 run chain); local: tests/test_nx043_guard_wiring_pg.py')
    r.block('online_stage', 'online_not_available until a calibrated twin (NX-042 T5) and the X4 wiring; follow-up')


PHASES = {'prepare': prepare, 'probe-before': probe_before, 'run': run_phase, 'decide': decide, 'probe-after': probe_after, 'report': report, 'gaps': gaps}
NEEDS = {'probe-after': ('probe-before',)}
PLAN = {'prepare': ['owner: create_sim_world (shadow)', 'owner: publish_strategy (candidate, control)', 'owner: register_experiment (shadow; screening when configured)'],
        'probe-before': ['probe (read only): world-real digests, real dispatch switches'],
        'run': ['owner: start_experiment (screening: refused twin_not_calibrated; shadow: started)', 'owner: set_shadow_copy on'],
        'decide': ['probe (read only): the copied events', 'sim worker (shadow world credential): the candidate decision on each, recorded only'],
        'probe-after': ['probe (read only): guard stop at the end, nothing sent from the shadow world, switches and world-real digests unchanged'],
        'report': ['owner: read_experiment_report (shadow, experiment)', 'owner: set_shadow_copy off'],
        'gaps': ['no connection: the blocked items (allocation wiring, experiment budget, online stage)']}
OWNER_PHASES = ('prepare', 'run', 'report')


def load_prior(paths):
    steps = []
    for path in paths or []:
        try:
            doc = json.loads(Path(path).read_text().strip().splitlines()[-1])
        except (OSError, ValueError, IndexError):
            raise Refused('--from: a result document of this script') from None
        if doc.get('schema') != RESULT_SCHEMA or not str(doc.get('check', '')).startswith('nx043-') or doc.get('dry_run'):
            raise Refused('--from: a result document of this script (not a dry run)')
        steps += doc.get('steps') or []
    return steps


def decide_from(roles):
    s = roles.get('sim_worker') or {}
    if not (s.get('dsn_file') and s.get('token_file')):
        return None
    connect = connect_from(s['dsn_file'])
    token = read_private(s['token_file'], 4096).strip()

    def decide(world, event_key, candidate, kind):
        from psycopg.types.json import Jsonb
        with connect() as db:
            return db.execute('select authz.nexloop_shadow_decision_record(%s,%s,%s)', (hashlib.sha256(token.encode()).hexdigest(), world, Jsonb(
                {'event_key': event_key, 'strategy_id': candidate['strategy_id'], 'strategy_version': candidate['version'], 'decision': kind}))).fetchone()[0]
    return decide


def ports_from(config, password):
    roles = config.get('roles') or {}
    owner = None
    o = config.get('owner') or {}
    if o.get('origin') and o.get('username_file') and password:
        from real_dispatch_switch import HttpsTransport, Workbench
        owner = Workbench(HttpsTransport(o['origin'], o.get('ca_file')), o['origin'])
        owner.login(read_private(o['username_file'], 1024).strip(), password)
    probe = (roles.get('probe') or {}).get('dsn_file')
    ports = Ports(owner=owner, probe=connect_from(probe) if probe else None)
    ports.decide = decide_from(roles)
    return ports


def main(argv=None, *, ports=None, stdin=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))  # never echo the arguments
    parser.add_argument('phase', choices=sorted(PHASES))
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--from', dest='prior', action='append', default=[])
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    config = {}
    try:
        from s6_common import load_config
        config = load_config(args.config)
        result = Result('nx043-' + args.phase, ACCEPTANCE[args.phase], config, dry_run=args.dry_run, world='simulation')
        if args.phase in NEEDS and len(args.prior) != len(NEEDS[args.phase]) and not args.dry_run:
            raise Refused(f'{args.phase}: --from the result of ' + ', '.join(NEEDS[args.phase]))
        prior = load_prior(args.prior) if not args.dry_run else []
        if args.dry_run:
            result.step('plan', 'the entries this phase calls', {'calls': PLAN[args.phase]}, True)
            return result.write(out)
        owner = None
        if ports is None and args.phase != 'gaps':
            password = (stdin or sys.stdin).readline().rstrip('\n') if args.phase in OWNER_PHASES and (config.get('owner') or {}).get('origin') else None
            ports = ports_from(config, password)
            owner = ports.owner
        try:
            PHASES[args.phase](ports, config, result, prior) if args.phase != 'prepare' else prepare(ports, config, result)
        except Blocked as error:
            result.block(args.phase, str(error))
        except Refused:
            raise
        except Exception as error:  # noqa: BLE001 - by type only
            result.step('unexpected_error', 'no error', {'error': type(error).__name__}, False)
        finally:
            if owner is not None:
                owner.logout()
        return result.write(out)
    except Refused as error:
        print(json.dumps({'ok': False, 'refused': Redactor(config)(str(error))}), file=out)
        return 2


if __name__ == '__main__':
    sys.exit(main())
