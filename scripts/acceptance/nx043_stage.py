#!/usr/bin/env python3
"""NX-043 X7 on stage: the screening and shadow parts of the strategy experiments (design §2-§9, D1-D9; the acceptance list is
docs/implementation/NX-043-X7-acceptance.md). Same configuration, result and redaction formats as the S6 and AT-046 stage scripts
(scripts/acceptance/s6_common.py; result schema nexloop-s6-stage-result/1, checks `nx043-<phase>`).

Every write goes through the owner's governed workbench Actions over HTTPS (password from stdin) or, for the shadow decisions, through
the simulation worker's own entry with the shadow world's credential; the probe is read only (the bootstrap DSN with
default_transaction_read_only=on, the dispatcher's). The identifiers the phases share (worlds, experiments, strategies) are kept in a
private state file in results_dir (0600), never in the output, which holds codes, counts and digests only.

    nx043_stage.py readiness --config INSPECT.json                 inspector (read only): is the stage ready for this run - sim world,
                                                                     strategies, shadow copy, real inbound trigger, real dispatch, conflicts
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
    nx043_stage.py online --config PRIVATE.json                    owner: register_experiment (online, world real, no twin) and start it
                                                                     (owner 2026-10-21: online open in the test stage, D9 waived online)
    # real inbound traffic of the tenant (the dispatcher's synthetic webchat Consumers), then:
    nx043_stage.py online-probe --config DATA.json                 probe: allocations by arm (first sight on inbound), strategy_applied
                                                                     audit of the candidate group's accepted outreach sends (counts only)
    nx043_stage.py online-stop --config PRIVATE.json               owner: stop_experiment (everyone back to the current policy)
    nx043_stage.py online-drain --config DATA.json                 probe: the real outreach sends and effects since the online start reach
                                                                     a final state (real dispatch is open: nothing left in flight)
    nx043_stage.py gaps --config ANY.json                          no connection: what the stage cannot show yet (blocked, never a pass)

inputs: deployment_real_dispatch (true/false: the dispatching workers' REAL_DISPATCH_ENABLED as observed by the operator; optional),
    shadow_purpose, candidate_levers, control_levers (D1 levers), strategy_prefix, experiment_prefix, ends_in_minutes (shadow
    experiment end; the guard stops it there), screening {world, twin {twin_id, version}} (optional), decisions {kind, limit},
    guard_timeout_seconds (probe-after waits for the guard stop, default 900)
roles: inspector {dsn_file} (read only; readiness, falls back to probe); probe {dsn_file}; sim_worker {dsn_file, token_file} (the shadow world's worker credential; optional); owner {origin, ca_file,
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
from at046_stage import EXCLUDED, FILTERED, Ports, real_digest  # noqa: E402
from s6_common import Blocked, Redactor, Refused, Result, connect_from, load_chain, read_private  # noqa: E402
from s6_common import connect_read_only as s6_connect_read_only  # noqa: E402

IDS = re.compile(r'[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
ACCEPTANCE = {'prepare': ['NX-043 D1', 'NX-043 D3'], 'probe-before': ['AT-046'], 'run': ['NX-043 D3', 'NX-043 D7', 'NX-043 D9'],
              'shadow-keys': ['NX-043 D7'], 'decide': ['NX-043 D7', 'AT-046'], 'merge': ['NX-043 D7', 'AT-046', 'NX-043 D4', 'NX-043 §9'],
              'probe-after': ['AT-046', 'NX-043 D4', 'NX-043 §9'], 'report': ['NX-043 D6', 'NX-043 §6', 'NX-043 D7'],
              'online': ['NX-043 online', 'NX-043 D3'], 'online-probe': ['NX-043 online', 'NX-043 D2'], 'online-stop': ['NX-043 online', 'NX-043 D4'],
              'online-drain': ['NX-043 online', 'NX-043 §9'], 'gaps': ['NX-043 D5'], 'readiness': ['NX-043 D3', 'NX-043 D7', 'NX-043 online']}


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
    """The refusal code: top-level reason, else the workbench error body's details.reason (stage X7 S2, release 37: a 409 is
    {code, message, trace_id, retryable, details: {reason}})."""
    if not isinstance(out, dict):
        return None
    details = out.get('details')
    return out.get('reason') or (details.get('reason') if isinstance(details, dict) else None)


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


UNFINISHED_CLAIMS = """select action_name,count(*) from runtime.nexloop_action_claims where tenant_id=%s and world='real' and claim->>'state'='active'
    and (claim->>'lease_expires_at')::timestamptz>clock_timestamp() group by 1 order by 1"""


def _quiet_real(ports, tenant, inputs):
    """L6 X7 review 3.2: the injected real inbound traffic makes real replies (relay, Run, reply guarantor). The probe waits until the
    real side is quiet - no unfinished real job of the tenant, no live Action claim of world real (third round S8, release 38: the base
    was taken while the Runs of the S3 traffic still held Goal/PlanStep claims, which finished after it), and the world-real digests
    unchanged over quiet_seconds - so the replies to that traffic are in the baseline, not in the difference. An active claim whose
    lease has run out no longer moves; it does not hold the probe (the orphan query in the X7 document finds those)."""
    quiet = int(inputs.get('quiet_seconds') or 60)
    deadline = time.monotonic() + int(inputs.get('quiet_timeout_seconds') or 900)
    last = real_digest(ports, tenant)
    while True:
        time.sleep(quiet)
        busy = ports.read("""select count(*) from runtime.jobs where tenant_id=%s and world='real' and status in ('pending','running','retry_wait')""", (tenant,))[0][0]
        claims = dict(ports.read(UNFINISHED_CLAIMS, (tenant,)))
        now = real_digest(ports, tenant)
        if not busy and not claims and now == last:
            return now, True, {}
        if time.monotonic() >= deadline:
            return now, False, {'unfinished_jobs': busy, 'unfinished_claims': claims}
        last = now


def probe_before(ports, config, r, prior=None):
    tenant = config['tenant_id']
    digests, quiet, unfinished = _quiet_real(ports, tenant, config.get('inputs') or {})
    if not quiet:  # the real side never settled: no base is taken, the Actions still running are named (counts only)
        r.block('real_side_quiet', 'the real side did not settle within quiet_timeout_seconds: ' + json.dumps(unfinished, sort_keys=True))
        return
    r.step('real_side_quiet', 'no unfinished real job, no live Action claim of world real and the world-real digests unchanged over the quiet period '
           '(the replies to the injected traffic settled)', {'quiet': quiet}, quiet)
    switches = ports.read("select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from control.nexloop_real_dispatch_switches x")[0][0]
    taken_at = ports.read('select clock_timestamp()')[0][0].isoformat()
    r.step('real_digest_before', 'the world-real digests and the real dispatch switches taken', {'tables': len(digests)}, bool(digests),
           digests=digests, switches=switches, taken_at=taken_at)


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


def _keys_path(config):
    return Path(config['results_dir']) / 'nx043-shadow-keys.json'


def shadow_keys(ports, config, r, prior=None):
    """DATA_HOST, the read-only probe (L6 X7 review 2.1): the copied events' keys (hashes, no personal data) into a private file of
    results_dir (0600) for the APP_HOST decide part; the output holds only the count."""
    state = load_state(config)
    d = (config.get('inputs') or {}).get('decisions') or {}
    keys = [k for (k,) in ports.read("""select event_key from runtime.nexloop_shadow_events where tenant_id=%s and world=%s order by copied_hour desc,event_key
        limit %s""", (config['tenant_id'], state['shadow_world'], int(d.get('limit') or 20)))]
    path = _keys_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'run_nonce': state['run'], 'keys': keys}, f)
    r.step('shadow_events_copied', 'real inbound events copied into the shadow world while the switch was on (D7)', {'copied': len(keys)}, bool(keys))


def decide(ports, config, r, prior=None):
    """APP_HOST, the simulation worker of the shadow world (hostssl): the candidate's decision on each copied event, recorded only."""
    state = load_state(config)
    d = (config.get('inputs') or {}).get('decisions') or {}
    if ports.decide is None:
        raise Blocked('roles.sim_worker (the shadow world worker credential) not configured')
    try:
        keys_doc = json.loads(read_private(_keys_path(config), 1048576))
    except Refused:
        raise Blocked('no private key file: run shadow-keys on DATA_HOST and copy it here') from None
    if keys_doc.get('run_nonce') != state['run']:
        raise Refused('the key file of another run')
    if not keys_doc.get('keys'):
        raise Blocked('no inbound real event copied into the shadow world (it needs real inbound traffic while on)')
    outs = [ports.decide(state['shadow_world'], k, state['strategies']['candidate'], d.get('kind') or 'reply') for k in keys_doc['keys']]
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
    # (L6 X7 review 2.2: the zero above is guaranteed by the effect tables' CHECK; what shows the decisions stayed simulated is below.)
    decided = ports.read('''select count(*),count(stand_in_intent) from runtime.nexloop_shadow_decisions where tenant_id=%s and world=%s and strategy_id=%s
        and strategy_version=%s''', (tenant, world, state['strategies']['candidate']['strategy_id'], state['strategies']['candidate']['version']))[0]
    expected = next(((s.get('observed') or {}).get('decisions') for s in (prior or []) if s.get('name') == 'shadow_decisions_recorded'), None)
    r.step('shadow_decisions_landed', 'the decisions of the decide part are in the shadow world, any effect only as a stand-in intent',
           {'decisions': 'some' if decided[0] else 'none', 'matches_decide': expected is not None and decided[0] >= expected,
            'stand_in_only': decided[1] <= simulated}, expected is not None and decided[0] >= expected and decided[1] <= simulated)
    before = next((s.get('evidence') or {} for s in (prior or []) if s.get('name') == 'real_digest_before'), {})
    after = real_digest(ports, tenant)
    switches = ports.read("select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from control.nexloop_real_dispatch_switches x")[0][0]
    changed = sorted(t for t in set(before.get('digests') or {}) | set(after) if (before.get('digests') or {}).get(t) != after.get(t))
    r.step('real_dispatch_switches_unchanged', 'the experiment opened no real dispatch switch (§9, D3)', {'unchanged': switches == before.get('switches')},
           bool(before) and switches == before.get('switches'))
    # The alert events left the comparison by name; which rules fired or resolved since the base is evidence (rule, kind, count only).
    alerts = [{'rule_id': rule, 'kind': kind, 'events': n} for rule, kind, n in ports.read(
        '''select rule_id,kind,count(*) from control.nexloop_alert_events where tenant_id=%s and world='real' and recorded_at>=%s::timestamptz
           group by 1,2 order by 1,2''', (tenant, before.get('taken_at') or 'infinity'))]
    r.step('real_alerts_in_window', 'the alert rules that fired or resolved between the base and now (alert events are excluded from the digest)',
           {'rules': alerts}, before.get('taken_at') is not None)
    r.step('real_side_unchanged', 'every world-real row of the tenant unchanged (AT-046 digests; periodic bookkeeping and the owner\'s own governance '
           'claims left out, by name and with the reason)', {'tables': len(after), 'changed': changed, 'excluded': sorted(EXCLUDED),
                                                             'filtered': {t: f[2] for t, f in FILTERED.items()}},
           bool(before.get('digests')) and not changed)


def report(ports, config, r, prior=None):
    state = load_state(config)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    # L6 X7 review 4.1: a finished day (yesterday), never today's open window - a report is frozen on its first read.
    yesterday = today - timedelta(days=1)
    status, out = ports.act('read_experiment_report', {'kind': 'shadow', 'world': state['shadow_world'], 'strategy_id': state['strategies']['candidate']['strategy_id'],
                                                       'strategy_version': state['strategies']['candidate']['version'], 'since': yesterday.isoformat(),
                                                       'until': today.isoformat()}, 'nx043-' + state['run'] + '-report-shadow')
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


def online(ports, config, r, prior=None):
    """Owner 2026-10-21: online is open in the test stage (D9 calibration waived online). An online experiment in world real, no twin,
    with the strategies of prepare; the start keeps every other check (purposes, strategies, persona)."""
    state = load_state(config)
    inputs = config.get('inputs') or {}
    eid = f"{inputs.get('experiment_prefix') or 'x7'}.online.{state['run']}"
    ends = (datetime.now(UTC) + timedelta(minutes=int(inputs.get('online_ends_in_minutes') or 60))).strftime('%Y-%m-%dT%H:%M:%SZ')
    status, out = ports.act('register_experiment', {'registration': registration(eid, 'online', 'real', state['strategies']['candidate'],
                                                                                 state['strategies']['control'], None, ends)}, 'nx043-' + state['run'] + '-e-online')
    r.step('online_experiment_registered', 'a frozen online preregistration in world real (D3)', {'status': status, 'reason': _reason(out)}, _ok(status))
    if not _ok(status):
        return
    status, out = ports.act('start_experiment', {'experiment_id': eid}, 'nx043-' + state['run'] + '-start-online')
    r.step('online_experiment_started', 'started (online open; no dispatch switch written)',
           {'status': status, 'reason': _reason(out), 'started': out.get('started') if isinstance(out, dict) else None}, _ok(status))
    if _ok(status):
        save_state(config, {**state, 'experiments': {**state['experiments'], 'online': eid}, 'online_started_at': datetime.now(UTC).isoformat()})


def online_probe(ports, config, r, prior=None):
    state = load_state(config)
    eid = state['experiments'].get('online')
    if not eid:
        r.step('online_allocated', 'an online experiment started (phase online)', {'started': False}, False)
        return
    tenant = config['tenant_id']
    arms = dict(ports.read("""select coalesce(arm,'excluded'),count(*) from control.nexloop_experiment_allocations
        where tenant_id=%s and experiment_id=%s group by 1""", (tenant, eid)))
    r.step('online_allocated', 'the real inbound traffic allocated Consumers on first sight; the candidate group within its cap (D2)',
           {'candidate': arms.get('candidate', 0), 'control': arms.get('control', 0), 'excluded': arms.get('excluded', 0)},
           arms.get('candidate', 0) + arms.get('control', 0) > 0 and arms.get('candidate', 0) <= 20)
    applied = ports.read("""select count(*) from runtime.audit_events where tenant_id=%s and action='nexloop.experiment.strategy_applied'
        and payload->>'experiment_id'=%s""", (tenant, eid))[0][0]
    r.step('online_strategy_audited', 'each accepted outreach send of the candidate group carries its strategy version in audit (counted)',
           {'strategy_applied': applied}, True)


def online_stop(ports, config, r, prior=None):
    state = load_state(config)
    eid = state['experiments'].get('online')
    status, out = ports.act('stop_experiment', {'experiment_id': eid, 'reason': 'NX-043 X7 stage acceptance done'} if eid else {},
                            'nx043-' + state['run'] + '-stop-online')
    r.step('online_experiment_stopped', 'stopped: everyone back to the current policy', {'status': status, 'reason': _reason(out)}, bool(eid) and _ok(status))


DISPATCH = {'outreach.send': 'eios:action:nexloop.outreach.send:1', 'enterprise.deliver': 'eios:action:nexloop.enterprise.deliver:1'}


def _ready(r, name, expected, ready, reason, **observed):
    r.step(name, expected, {'status': 'ready' if ready else 'blocked', 'reason': reason, **observed}, ready)


def dispatch_state(ports, tenant, deployment=None):
    """The three real dispatch layers without control.nexloop_real_dispatch_closed_layer (stage finding, release 35): its first layer
    is the transaction GUC nexloop.real_dispatch_enabled that only a dispatching worker sets, so an inspector session always read
    'deployment', and the function's row lock fails in a read-only transaction anyway. Here: the tenant row and each DISPATCH Action
    row of control.nexloop_real_dispatch_switches, read only under the tenant (the same rules as the function: tenant enabled with
    data_mode real, then the Action enabled). The deployment layer (REAL_DISPATCH_ENABLED of the dispatching workers) is not visible
    in the database: inputs.deployment_real_dispatch (true/false, the operator's observation of the containers' env) decides it when
    given; otherwise it is reported not_observable_from_db."""
    if ports.probe is None:
        raise Blocked('read-only probe DSN not configured')
    with ports.probe() as db, db.transaction():
        db.execute('set transaction read only')
        db.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        rows = db.execute("""select scope_kind,scope_ref,enabled,data_mode from control.nexloop_real_dispatch_switches
            where tenant_id=%s and world='real' and ((scope_kind='tenant' and scope_ref='*') or (scope_kind='action_type' and scope_ref=any(%s)))""",
                          (tenant, list(DISPATCH.values()))).fetchall()
    found = {(kind, ref): (enabled, mode) for kind, ref, enabled, mode in rows}
    t = found.get(('tenant', '*'), (None, None))
    tenant_open = t[0] is True and t[1] == 'real'
    layers = {name: ('tenant' if not tenant_open else 'action' if found.get(('action_type', res), (None,))[0] is not True else 'open')
              for name, res in DISPATCH.items()}
    layers['deployment'] = {True: 'open', False: 'closed'}.get(deployment, 'not_observable_from_db')
    if any(v != 'open' for k, v in layers.items() if k != 'deployment') or deployment is False:
        reason = 'closed_sends_refused'
    elif deployment is True:
        reason = 'open_sends_execute'
    else:
        reason = 'open_in_database_deployment_not_observed'
    return {'layers': layers, 'reason': reason}


def readiness(ports, config, r, prior=None):
    """Read only (the inspector DSN): whether this stage can run the acceptance now. Each item ready or blocked, with its reason; counts
    and states only, no identifier in the output."""
    tenant = config['tenant_id']
    inputs = config.get('inputs') or {}
    world = (inputs.get('screening') or {}).get('world')
    if world:
        row = ports.read("""select status,expires_at>clock_timestamp() from control.nexloop_sim_worlds where tenant_id=%s and world=%s""", (tenant, world))
        active = bool(row) and row[0][0] == 'active' and row[0][1]
        _ready(r, 'sim_world_active', 'the configured screening sim world exists, is active and not expired', active,
               'ok' if active else ('not_found' if not row else 'not_active_or_expired'), exists=bool(row))
    else:
        r.block('sim_world_active', 'inputs.screening.world not configured (the screening part is skipped; prepare creates the shadow world)')
    twin = (inputs.get('screening') or {}).get('twin') or {}
    if twin.get('twin_id'):
        row = ports.read('select runtime.nexloop_consumer_twin_selectable(%s,%s,%s)', (tenant, twin['twin_id'], int(twin.get('version') or 0)))
        calibrated = bool(row) and row[0][0] is True
        _ready(r, 'screening_twin_uncalibrated', 'the screening twin is not calibrated, as run expects (screening start refused twin_not_calibrated, D9)',
               not calibrated, 'ok' if not calibrated else 'twin_calibrated_run_expects_refusal')
    for arm, levers in (('candidate', inputs.get('candidate_levers') or {'follow_up_delay_hours': 48}),
                        ('control', inputs.get('control_levers') or {'follow_up_delay_hours': 24})):
        sid = f"{inputs.get('strategy_prefix') or 'x7'}.{arm}.readiness"
        problem = ports.read('select runtime.nexloop_strategy_problem(%s,%s::jsonb)', (tenant, json.dumps(strategy_definition(sid, levers))))[0][0]
        _ready(r, f'{arm}_strategy_publishable', 'the configured levers make a publishable strategy version (D1)', problem is None, problem or 'ok')
    target = ports.read('select runtime.nexloop_shadow_copy_target(%s)', (tenant,))[0][0]
    _ready(r, 'shadow_copy_off', 'the shadow copy switch is off before run turns it on for the new shadow world (D7)', target is None,
           'ok' if target is None else 'on_for_another_world', on=target is not None)
    trigger = ports.read("""select tgenabled from pg_trigger where tgname='nx043_experiment_allocate'
        and tgrelid='runtime.nexloop_conversation_messages'::regclass""")
    enabled = bool(trigger) and trigger[0][0] in ('O', 'A')
    _ready(r, 'real_inbound_allocation', 'the real inbound allocation trigger (X4 wiring) is installed and enabled for the online part', enabled,
           'ok' if enabled else ('missing' if not trigger else 'disabled'))
    state = dispatch_state(ports, tenant, inputs.get('deployment_real_dispatch'))
    _ready(r, 'real_dispatch_state', 'the real dispatch layers read (open: the online candidate group really sends; closed: refused by real_dispatch)',
           True, state['reason'], **state['layers'])
    running = ports.read("""select e.registration->>'stage',e.registration->>'world'='real',
          e.registration->'candidate'->>'strategy_id' like %s from control.nexloop_experiments e
        where e.tenant_id=%s and runtime.nexloop_experiment_status(e.tenant_id,e.experiment_id)='started'
          and (e.registration->>'ends_at')::timestamptz>clock_timestamp()""", ((inputs.get('strategy_prefix') or 'x7') + '.%', tenant))
    online = sum(1 for stage, real, _ in running if stage == 'online' and real)
    same = sum(1 for _, _, mine in running if mine)
    _ready(r, 'no_conflicting_experiment', 'no running online experiment in world real (its members are in_other_experiment) and none of this prefix (in_same_comparison)',
           online == 0 and same == 0, 'ok' if online == 0 and same == 0 else ('online_running' if online else 'same_prefix_running'),
           running=len(running), online_real=online, same_prefix=same)


# L6 review 1.3: only the experiment's members (allocated with an arm), so other real outreach of the tenant does not hold the drain.
MEMBERS = """with m as (select consumer_id from control.nexloop_experiment_allocations where tenant_id=%s and experiment_id=%s and arm is not null) """
OUTREACH_IN_FLIGHT = ('pending_approval', 'accepted', 'dispatching', 'unknown')
EFFECT_IN_FLIGHT = ('accepted', 'dispatching', 'unknown', 'observed_fulfilled')


def online_drain(ports, config, r, prior=None):
    """After online-stop: with real dispatch open the candidate group's sends really ran; wait (drain_timeout_seconds, default 900) until
    every real outreach send and effect of the experiment's members since the online start is final. Counts only."""
    state = load_state(config)
    since, eid = state.get('online_started_at'), state['experiments'].get('online')
    if not since or not eid:
        r.step('online_nothing_in_flight', 'an online experiment started (phase online)', {'started': False}, False)
        return
    tenant, inputs = config['tenant_id'], config.get('inputs') or {}
    deadline = time.monotonic() + int(inputs.get('drain_timeout_seconds') or 900)
    while True:
        sends, effects = ports.read(MEMBERS + """select (select count(*) from runtime.nexloop_outreach_sends s join m on m.consumer_id=s.consumer_id
              where s.tenant_id=%s and s.world='real' and s.created_at>=%s and s.state = any(%s)),
            (select count(*) from runtime.nexloop_effect_intents i join m on m.consumer_id=i.consumer_id
              where i.tenant_id=%s and i.world='real' and i.created_at>=%s and i.state = any(%s))""",
                                    (tenant, eid, tenant, since, list(OUTREACH_IN_FLIGHT), tenant, since, list(EFFECT_IN_FLIGHT)))[0]
        if (sends == 0 and effects == 0) or time.monotonic() >= deadline:
            break
        time.sleep(int(inputs.get('drain_poll_seconds') or 15))
    total = ports.read(MEMBERS + """select count(*) from runtime.nexloop_outreach_sends s join m on m.consumer_id=s.consumer_id
        where s.tenant_id=%s and s.world='real' and s.created_at>=%s""", (tenant, eid, tenant, since))[0][0]
    r.step('online_nothing_in_flight', 'every real outreach send and effect of the experiment members since the online start is final (none accepted, dispatching or unknown)',
           {'outreach_sends': total, 'outreach_in_flight': sends, 'effects_in_flight': effects}, sends == 0 and effects == 0)


def gaps(ports, config, r, prior=None):
    """What the stage cannot show yet: blocked (passed null), with the slice that closes each gap."""
    r.block('experiment_budget', 'needs simulation Runs of the experiment world (the AT-046 run chain); local: tests/test_nx043_guard_wiring_pg.py')


def merge(ports, config, r, prior=None):
    """No connection: the shadow parts of one run (probe-before, shadow-keys, decide, probe-after) as one result; load_chain checked them."""


PHASES = {'prepare': prepare, 'probe-before': probe_before, 'run': run_phase, 'shadow-keys': shadow_keys, 'decide': decide, 'probe-after': probe_after,
          'report': report, 'merge': merge, 'readiness': readiness, 'online': online, 'online-probe': online_probe, 'online-stop': online_stop, 'online-drain': online_drain, 'gaps': gaps}
CHAIN = ['nx043-probe-before', 'nx043-shadow-keys', 'nx043-decide', 'nx043-probe-after']
NEEDS = {'probe-after': CHAIN[:3], 'merge': CHAIN}
PLAN = {'prepare': ['owner: create_sim_world (shadow)', 'owner: publish_strategy (candidate, control)', 'owner: register_experiment (shadow; screening when configured)'],
        'probe-before': ['probe (read only): wait for the real side to be quiet, then world-real digests, real dispatch switches'],
        'run': ['owner: start_experiment (screening: refused twin_not_calibrated; shadow: started)', 'owner: set_shadow_copy on'],
        'shadow-keys': ['probe (read only, DATA_HOST): the copied events\' keys into a private file'],
        'decide': ['sim worker (shadow world credential, APP_HOST): the candidate decision on each key of the private file, recorded only'],
        'merge': ['no connection: the four shadow parts of one run (run_nonce, each once, in order) as nx043-shadow'],
        'probe-after': ['probe (read only): guard stop at the end, nothing sent from the shadow world, switches and world-real digests unchanged'],
        'report': ['owner: read_experiment_report (shadow, experiment)', 'owner: set_shadow_copy off'],
        'online': ['owner: register_experiment (online, world real, no twin)', 'owner: start_experiment (online)'],
        'online-probe': ['probe (read only): allocations by arm, strategy_applied audit count'],
        'online-stop': ['owner: stop_experiment (online)'],
        'online-drain': ['probe (read only): real outreach sends and effects since the online start, until none is in flight'],
        'gaps': ['no connection: the blocked item (experiment budget)'],
        'readiness': ['inspector (read only): sim world, strategy problems, shadow copy target, inbound trigger, real dispatch layers, running experiments']}
OWNER_PHASES = ('prepare', 'run', 'report', 'online', 'online-stop')


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


def connect_read_only(path):
    """L6 review 1.1: a read-only role is read only by the script, not only by its DSN file - a factory over s6_common's
    connect_read_only (set default_transaction_read_only=on, check it with show, refuse otherwise)."""
    return lambda: s6_connect_read_only(path, who='the read-only role')


def ports_from(config, password, phase=None):
    roles = config.get('roles') or {}
    owner = None
    o = config.get('owner') or {}
    if o.get('origin') and o.get('username_file') and password:
        from real_dispatch_switch import HttpsTransport, Workbench
        owner = Workbench(HttpsTransport(o['origin'], o.get('ca_file')), o['origin'])
        owner.login(read_private(o['username_file'], 1024).strip(), password)
    probe = (roles.get('inspector') or roles.get('probe') or {}).get('dsn_file') if phase == 'readiness' else (roles.get('probe') or {}).get('dsn_file')
    ports = Ports(owner=owner, probe=connect_read_only(probe) if probe else None)
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
        check = 'nx043-shadow' if args.phase == 'merge' else 'nx043-' + args.phase
        result = Result(check, ACCEPTANCE[args.phase], config, dry_run=args.dry_run, world='simulation')
        if args.dry_run:
            result.step('plan', 'the entries this phase calls', {'calls': PLAN[args.phase]}, True)
            return result.write(out)
        if args.phase in NEEDS and len(args.prior) != len(NEEDS[args.phase]):
            raise Refused(f'{args.phase}: --from the results of ' + ', '.join(NEEDS[args.phase]))
        if args.phase not in ('prepare', 'gaps', 'readiness'):
            try:
                result.nonce = load_state(config)['run']  # the run nonce (L6 X7 review 3.1)
            except Blocked as error:
                result.block(args.phase, str(error))
                return result.write(out)
        prior, prior_blocked, _ = load_chain(args.prior, prefix='nx043-', order=list(NEEDS[args.phase]), nonce=result.nonce) if args.phase in NEEDS else ([], [], None)
        if args.phase == 'merge':
            result.steps, result.blocked = list(prior), list(prior_blocked)
            return result.write(out)
        owner = None
        if ports is None and args.phase != 'gaps':
            password = (stdin or sys.stdin).readline().rstrip('\n') if args.phase in OWNER_PHASES and (config.get('owner') or {}).get('origin') else None
            ports = ports_from(config, password, args.phase)
            owner = ports.owner
        try:
            PHASES[args.phase](ports, config, result, prior) if args.phase != 'prepare' else prepare(ports, config, result)
            if args.phase == 'prepare':
                try:
                    result.nonce = load_state(config)['run']
                except Blocked:
                    pass
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
