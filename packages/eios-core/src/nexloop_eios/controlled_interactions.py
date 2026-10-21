"""NX-037 A-class prep: the controlled interaction executor (design docs/implementation/NX-037-design.md §2–§3.1, ROADMAP D-g/D-i).

The executor runs the D-i plan (30 interactions, five scenario kinds) one interaction at a time through a driver, collects the
evidence of each (model, tool and platform time and cost, delivery receipts, reconciliation outcome) and applies the D-i rules
before the next one:

- stop at the first occurrence of: an unauthorized send, a send to the wrong recipient, a contact after a refusal, a duplicate
  external effect (design §3.1 adds: a cross-tenant read, a Run over the per-Run cost cap, a critical alert, the owner's stop);
- pause when: more than 10% errors within the last 10 interactions, cumulative cost at 80% of the cumulative cap (or the
  tenant's month at 80% of the monthly cap), an unknown effect still unresolved at the end of its reconciliation window.

A driver is the only thing that touches the system: `interact(interaction)` returns the observable facts of one interaction
(ids, counts, times; never message text). In this prep only the synthetic driver exists (deterministic provider and loopback
channel, tests/support/nx037_synthetic.py); a driver for the real trial is NX-037 B-class work. The executor never opens a
dispatch switch and holds no credential.

Every record is written (fsync) before the next interaction starts, so a stop or a crash leaves the evidence of everything that
happened. Records hold only ids, digests, numbers, codes and times (checked by `scripts/nx037/evidence_pack.py`).
"""
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

KINDS = ('ordinary_reply', 'real_delivery', 'restricted_after_refusal', 'human_takeover', 'unknown_reconciliation')
STOP_RULES = ('unauthorized_send', 'wrong_recipient', 'contact_after_refusal', 'duplicate_external_effect', 'cross_tenant_read',
              'per_run_cost_exceeded', 'critical_alert', 'owner_stop')
PAUSE_RULES = ('error_rate', 'cumulative_cost', 'monthly_cost', 'unresolved_unknown_effect', 'calibration', 'interaction_timeout', 'effect_evidence_missing')
_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
NAMESPACE = uuid.UUID('6f1d3c2e-37a0-4c37-9b37-000000000037')
SCHEMA = 'nexloop.nx037-trial.v1'
# v2 (NX-037 gap B): only effect_categories changes (action-effect-categories.v3.json, nexloop.enterprise.deliver:1).
# v3 (owner 2026-10-14): 36 interactions with variants and agent-played customers; caps and rules unchanged.
# v5 (owner 2026-10-20, release 25): v4 with alert rules v6 (data health).
# v6 (owner 2026-10-21, release 25 with batches 3 and 4): v5 with alert rules v8 (evolution regression, outreach unsubscribe and approvals).
# v7 (owner 2026-10-21, release 26 with batches 5 and 6): v6 with alert rules v9 (NX-040 F4 schema release regression).
# v8 (L6 audit 2026-10-21, release 27 with batch 7): v7 with alert rules v10 (clock skew).
# v9 (L6 review 2026-10-21, NX-043 X5 wiring): v8 with alert rules v11 (experiment_auto_stopped).
SCHEMAS = ('nexloop.nx037-trial.v1', 'nexloop.nx037-trial.v2', 'nexloop.nx037-trial.v3', 'nexloop.nx037-trial.v4', 'nexloop.nx037-trial.v5',
           'nexloop.nx037-trial.v6', 'nexloop.nx037-trial.v7',
           'nexloop.nx037-trial.v8', 'nexloop.nx037-trial.v9')
_VARIANT = re.compile(r'[a-z][a-z0-9_]{0,47}')


def latest_trial(config_dir):
    """The newest trial configuration in `config_dir` (nx037-trial.v<N>.json with the highest N): versions are only added, so the
    init and the driver follow the newest without a code change."""
    found = [(int(m.group(1)), p) for p in Path(config_dir).glob('nx037-trial.v*.json') if (m := re.fullmatch(r'nx037-trial\.v([1-9][0-9]*)\.json', p.name))]
    if not found:
        raise PlanInvalid('no trial configuration')
    return max(found)[1]


class PlanInvalid(ValueError):
    pass


class TrialPaused(RuntimeError):
    """A paused trial continues only with an explicit resume carrying the owner's confirmation reference."""


def _money(value, name):
    try:
        amount = Decimal(str(value))
    except Exception as error:  # noqa: BLE001 - any parse failure is an invalid plan
        raise PlanInvalid(name) from error
    if not amount.is_finite() or amount <= 0:
        raise PlanInvalid(name)
    return amount


def validate_plan(value, *, cost_caps=None):
    """The versioned trial configuration (deploy/configuration/nx037-trial.v1.json); `cost_caps` overrides the three D-g values."""
    if type(value) is not dict or value.get('schema') not in SCHEMAS:
        raise PlanInvalid('schema')
    caps = {**value.get('cost_caps', {}), **(cost_caps or {})}
    if set(caps) != {'cumulative', 'per_run', 'tenant_monthly'}:
        raise PlanInvalid('cost_caps')
    caps = {k: _money(v, k) for k, v in caps.items()}
    if not caps['per_run'] <= caps['cumulative'] <= caps['tenant_monthly']:
        raise PlanInvalid('cost_caps order')
    plan = value.get('plan') or {}
    mix = plan.get('mix') or {}
    if tuple(sorted(mix)) != tuple(sorted(KINDS)) or any(type(n) is not int or n < 0 for n in mix.values()) or sum(mix.values()) != plan.get('total'):
        raise PlanInvalid('mix')
    pause = value.get('pause') or {}
    if type(pause.get('error_window')) is not int or pause['error_window'] < 1:
        raise PlanInvalid('pause')
    if value.get('currency') is None or len(value['currency']) != 3 or not value['currency'].isupper():
        raise PlanInvalid('currency')
    if tuple(value.get('stop', ())) != STOP_RULES:
        raise PlanInvalid('stop')
    return {'currency': value['currency'], 'cost_caps': caps, 'mix': dict(mix), 'total': plan['total'],
            'calibration': int(value.get('calibration_interactions', 0)),
            'error_window': pause['error_window'], 'error_rate_above': Decimal(str(pause['error_rate_above'])),
            'cost_ratio': Decimal(str(pause['cost_ratio_at_least']))}


def load_plan(path, *, cost_caps=None):
    return validate_plan(json.loads(Path(path).read_text(encoding='utf-8')), cost_caps=cost_caps)


def kind_of(item):
    """An order item: a kind (plan v1/v2) or {kind, variant, consumer} (plan v3)."""
    return item['kind'] if isinstance(item, dict) else item


def validate_order(plan, order):
    """An owner-edited order (kinds, or plan v3 items {kind, variant, consumer}) must hold exactly the plan's mix (counted by
    kind)."""
    if type(order) is not list or len(order) != plan['total']:
        raise PlanInvalid('order does not match the mix')
    for item in order:
        if isinstance(item, dict) and (set(item) != {'kind', 'variant', 'consumer'} or not _VARIANT.fullmatch(str(item['variant']))
                                       or type(item['consumer']) is not str or not 1 <= len(item['consumer']) <= 200):
            raise PlanInvalid('order item')
    kinds = [kind_of(i) for i in order]
    if any(k not in KINDS for k in kinds) or {k: kinds.count(k) for k in KINDS} != plan['mix']:
        raise PlanInvalid('order does not match the mix')
    return order


def order_digest(order):
    return hashlib.sha256(json.dumps(order, separators=(',', ':')).encode()).hexdigest()


def schedule(plan, *, trial_id, order=None):
    """The interactions in order: the owner's order when given (validated against the mix), otherwise kinds interleaved evenly
    (largest remainder), so every kind appears early. Deterministic for a trial id; each interaction has a stable id that
    survives retries and restarts."""
    if order is not None:
        return [{'seq': i + 1, 'kind': kind_of(k), 'interaction_id': str(uuid.uuid5(NAMESPACE, f'{trial_id}:{i + 1}')),
                 **({'variant': k['variant'], 'consumer': k['consumer']} if isinstance(k, dict) else {})}
                for i, k in enumerate(validate_order(plan, order))]
    remaining = dict(plan['mix'])
    total = plan['total']
    order = []
    for step in range(total):
        # the kind furthest behind its share at this step
        kind = max(KINDS, key=lambda k: (plan['mix'][k] * (step + 1) / total - (plan['mix'][k] - remaining[k]) if remaining[k] else -1, -KINDS.index(k)))
        remaining[kind] -= 1
        order.append(kind)
    return [{'seq': i + 1, 'kind': k, 'interaction_id': str(uuid.uuid5(NAMESPACE, f'{trial_id}:{i + 1}'))} for i, k in enumerate(order)]


def _decimal(value):
    return Decimal(str(value)) if value is not None else Decimal(0)


def evaluate(record, history, plan, *, cumulative, monthly, owner_stop=False):
    """The rules after one interaction. Returns ('stop'|'pause'|None, [rule ids]). Stops win over pauses."""
    facts = record['facts']
    stops = []
    if facts.get('unauthorized_sends', 0) > 0:
        stops.append('unauthorized_send')
    if facts.get('wrong_recipient_sends', 0) > 0:
        stops.append('wrong_recipient')
    if facts.get('contacts_after_refusal', 0) > 0:
        stops.append('contact_after_refusal')
    if facts.get('max_executions_per_intent', 0) > 1:
        stops.append('duplicate_external_effect')
    if facts.get('cross_tenant_reads', 0) > 0:
        stops.append('cross_tenant_read')
    if _decimal(record['cost'].get('max_run', record['cost']['total'])) > plan['cost_caps']['per_run']:
        stops.append('per_run_cost_exceeded')
    if record['cost'].get('other_currencies'):
        stops.append('currency_mismatch')  # D-g option 2: no conversion; a cost outside the trial currency is not counted
    if facts.get('critical_alerts', 0) > 0:
        stops.append('critical_alert')
    if owner_stop:
        stops.append('owner_stop')
    if stops:
        return 'stop', stops
    pauses = []
    # The error window restarts after a pause: the owner reviewed the errors before resuming.
    since = max((i + 1 for i, r in enumerate(history) if r['decision']['action'] == 'pause'), default=0)
    window = (history[since:] + [record])[-plan['error_window']:]
    errors = sum(1 for r in window if r['outcome'] != 'ok')
    if Decimal(errors) / Decimal(plan['error_window']) > plan['error_rate_above']:
        pauses.append('error_rate')
    if cumulative >= plan['cost_caps']['cumulative'] * plan['cost_ratio']:
        pauses.append('cumulative_cost')
    if monthly >= plan['cost_caps']['tenant_monthly'] * plan['cost_ratio']:
        pauses.append('monthly_cost')
    if facts.get('unresolved_unknown_effects', 0) > 0:
        pauses.append('unresolved_unknown_effect')
    if facts.get('interaction_timeout', 0) > 0:
        pauses.append('interaction_timeout')  # the operator did not complete the interaction in time
    if facts.get('effect_evidence_missing', 0) > 0:
        pauses.append('effect_evidence_missing')  # the kind's required effect (e.g. the enterprise delivery) is not in the records
    return ('pause', pauses) if pauses else (None, [])


def _iso(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _append(path, row):
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def read_records(directory):
    """The trial's records in order, without the ones the owner voided (voids.jsonl: interaction id and end time)."""
    path = Path(directory) / 'interactions.jsonl'
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    voided = {(v['interaction_id'], v['ended_at']) for v in read_voids(directory)}
    return [r for r in records if (r['interaction_id'], r['ended_at']) not in voided]


def read_voids(directory):
    path = Path(directory) / 'voids.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def merge_evidence(items):
    """One interaction's evidence over all its Runs (a long conversation has one Run per customer message): model and tool
    counts and times summed, costs per kind summed with their currencies, intents and refusals counted together."""
    if len(items) == 1:
        return items[0]
    out = {'model': {}, 'tool': {'calls': 0, 'ms': 0}, 'costs': {}, 'intents': {}, 'intent_ids': [], 'refusals': {}}
    for item in items:
        for key, value in (item.get('model') or {}).items():
            if isinstance(value, (int, float)):
                out['model'][key] = out['model'].get(key, 0) + value
            elif key == 'cost':
                out['model'][key] = str(_decimal(out['model'].get(key)) + _decimal(value))
        for key in ('calls', 'ms'):
            out['tool'][key] += (item.get('tool') or {}).get(key, 0)
        for kind, value in (item.get('costs') or {}).items():
            held = out['costs'].setdefault(kind, {'amount': '0', 'currencies': []})
            held['amount'] = str(_decimal(held['amount']) + _decimal(value.get('amount')))
            held['currencies'] = sorted(set(held['currencies']) | set(value.get('currencies') or []))
        for field in ('intents', 'refusals'):
            for key, n in (item.get(field) or {}).items():
                out[field][key] = out[field].get(key, 0) + n
        out['intent_ids'] += list(item.get('intent_ids') or [])
        if item.get('failure'):
            out.setdefault('failures', []).append(item['failure'])
        if item.get('recovered_tool_errors'):
            out.setdefault('recovered_tool_errors', []).extend(item['recovered_tool_errors'])
    return out


def counted_cost(record):
    """The cost a record counts toward the cumulative and monthly caps (cost.counted when the period increase was measured)."""
    return _decimal(record['cost'].get('counted', record['cost']['total']))


def run(plan, driver, collector, directory, *, trial_id, clock=None, owner_stop=None, month_cost_before=Decimal(0), order=None,
        resume=None, calibration_pause=False, test_stack=False, month_cost=None, persona=None, void=None, driver_mode=None):
    """Runs (or resumes after a pause) the trial into `directory`: interactions.jsonl (one record per interaction, written
    before the next starts) and state.json (the outcome). An interaction already recorded is never run again: resuming
    continues after the last record (the error window restarts after a pause). A stopped trial does not resume (the owner
    reviews and starts a new trial id, a uuid). A paused trial raises TrialPaused unless `resume` is the owner's confirmation
    reference, which is appended to resumes.jsonl (fsync) before anything runs; the interaction order may not change across
    resumes. With `calibration_pause` the trial pauses after the plan's calibration interactions (rule 'calibration') so the
    owner confirms the cost per interaction before the rest. `test_stack` (the local synthetic stack, never a real trial) marks
    every record and the state test_stack: true; a trial keeps that mark across resumes.

    `driver_mode` (the runner's --driver: real or synthetic) is written into every record and the state, a mark only (nothing is
    judged on it), so evidence can tell a real trial from a synthetic one.

    `void` = (seq, owner confirmation reference) withdraws the last recorded interaction, which must have that seq, from the
    evidence (10/17 calibration: a record judged before its effect evidence was required): voids.jsonl gets one fsynced line
    naming that record (interaction id and end time); interactions.jsonl is never edited; read_records leaves the record out;
    and the interaction runs again now. On a paused trial the owner's resume reference is required as well.

    `month_cost()` (real mode: the tenant's model cost this month in the trial currency, 0185) is read before and after each
    interaction. The record keeps the trigger Run's own cost (cost.total, judged by the per-Run cap) and the actual increase over
    the interaction (cost.period_increase: fallback, plan reevaluation and role Runs, extraction and matching, anything else the
    tenant spent meanwhile); the larger of the two (cost.counted) is what the cumulative and monthly caps count, so neither is
    underestimated. Without it the counted cost is the trigger Run's. `persona` (real mode: simulated_agent when every customer is
    an AI agent the owner operates, owner 2026-10-14) is written into every record and the state, so later analyses can exclude
    those accounts."""
    if str(uuid.UUID(trial_id)) != trial_id:
        raise ValueError('trial id must be a lowercase uuid')
    clock = clock or (lambda: datetime.now(UTC))
    owner_stop = owner_stop or (lambda: False)
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = root / 'state.json'
    previous = json.loads(state_path.read_text()) if state_path.exists() else None
    if previous and previous['status'] == 'stopped':
        raise RuntimeError('trial stopped')
    interactions = schedule(plan, trial_id=trial_id, order=order)
    digest = order_digest([{k: i[k] for k in ('kind', 'variant', 'consumer')} if 'variant' in i else i['kind'] for i in interactions])
    if previous and previous.get('order_digest', digest) != digest:
        raise PlanInvalid('the interaction order changed')
    if previous and previous.get('test_stack', False) is not bool(test_stack):
        raise PlanInvalid('the trial stack changed')
    if void is not None:
        # Checked before anything is appended (a refused void leaves no resume line either).
        seq, reference = void
        last = (read_records(root) or [None])[-1]
        if not _REF.fullmatch(str(reference)) or last is None or last['seq'] != seq:
            raise ValueError('only the last recorded interaction can be voided, with an owner confirmation reference')
    if previous and previous['status'] == 'paused':
        if resume is None:
            raise TrialPaused(','.join(previous['rules']))
        if not _REF.fullmatch(str(resume)):
            raise ValueError('owner confirmation must be an opaque reference')
        _append(root / 'resumes.jsonl', {'after_seq': previous['at_seq'], 'rules': previous['rules'], 'confirmation_ref': resume,
                                         'at': _iso(clock())})
    elif resume is not None:
        raise ValueError('the trial is not paused')
    if void is not None:
        at = clock()
        _append(root / 'voids.jsonl', {'seq': seq, 'interaction_id': last['interaction_id'], 'ended_at': last['ended_at'],
                                       'confirmation_ref': reference, 'at': _iso(at)})
        # 10/18: the voided attempt's operator done marker is retired with it (renamed, never deleted), so the redo is not
        # taken as completed by a marker left from before.
        marker = root / 'operator-done' / str(seq)
        if marker.exists():
            marker.rename(marker.with_name(f"{seq}.voided-{at.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"))
    history = read_records(root)
    cumulative = sum((counted_cost(r) for r in history), Decimal(0))
    done = {r['interaction_id'] for r in history}
    status, rules, at_seq = 'completed', [], None
    for interaction in interactions:
        if interaction['interaction_id'] in done:
            continue
        if owner_stop():
            status, rules, at_seq = 'stopped', ['owner_stop'], interaction['seq']
            break
        started = clock()
        month_before = month_cost() if month_cost is not None else None
        observed = driver.interact(interaction)
        ended = clock()
        run_ids = observed.get('run_ids') or ([observed['run_id']] if observed.get('run_id') else [])
        collected = [collector.collect(r) for r in run_ids]
        evidence = merge_evidence(collected) if collected else None
        # The per-Run cap applies to each Run of the interaction (a long conversation has several), in the trial currency.
        run_totals = [sum((_decimal(v['amount']) for v in (e.get('costs') or {}).values()
                           if sorted(v.get('currencies') or []) in ([], [plan['currency']])), Decimal(0)) for e in collected]
        end_to_end = int(round((ended - started).total_seconds() * 1000))
        model_ms = int(evidence['model']['ms']) if evidence else 0
        tool_ms = int(evidence['tool']['ms']) if evidence else 0
        costs = (evidence or {}).get('costs', {})
        # D-g option 2: costs are kept per currency, never converted. Only amounts in the trial currency count toward the caps;
        # an amount recorded in another currency (or mixed) is kept apart and stops the trial (currency_mismatch).
        total, foreign = Decimal(0), {}
        for kind, value in costs.items():
            currencies = sorted(value.get('currencies') or [])
            if currencies in ([], [plan['currency']]):
                total += _decimal(value['amount'])
            elif _decimal(value['amount']) != 0:
                foreign[kind] = {'amount': str(value['amount']), 'currencies': currencies}
        cost = {'total': str(total), **({'max_run': str(max(run_totals))} if len(run_totals) > 1 else {}), 'by_kind': {k: v['amount'] for k, v in sorted(costs.items()) if k not in foreign}, 'currency': plan['currency'],
                **({'other_currencies': dict(sorted(foreign.items()))} if foreign else {})}
        counted = total
        if month_before is not None:
            increase = max(_decimal(month_cost()) - _decimal(month_before), Decimal(0))
            counted = max(total, increase)
            cost.update(period_increase=str(increase), counted=str(counted))
        record = {
            'seq': interaction['seq'], 'interaction_id': interaction['interaction_id'], 'kind': interaction['kind'],
            'run_id': observed.get('run_id'), **({'run_ids': run_ids, 'messages': observed['messages']} if 'messages' in observed else {}),
            **({'variant': interaction['variant']} if 'variant' in interaction else {}), 'started_at': _iso(started), 'ended_at': _iso(ended),
            'outcome': observed['outcome'], 'error_code': observed.get('error_code'),
            'time_ms': {'end_to_end': end_to_end, 'model': model_ms, 'tool': tool_ms, 'platform': max(end_to_end - model_ms - tool_ms, 0)},
            'model': {k: (evidence or {}).get('model', {}).get(k, 0) for k in ('calls', 'failed', 'unknown')},
            'tool_calls': (evidence or {}).get('tool', {}).get('calls', 0),
            'cost': cost,
            'intents': (evidence or {}).get('intents', {}), 'intent_ids': (evidence or {}).get('intent_ids', []),
            'refusals': (evidence or {}).get('refusals', {}),
            # 10/18: why a Run of this interaction failed (codes only, 0204): which tool, when a tool failed.
            **({'failures': (evidence or {}).get('failures') or ([evidence['failure']] if (evidence or {}).get('failure') else [])}
               if (evidence or {}).get('failure') or (evidence or {}).get('failures') else {}),
            # Owner 2026-10-18 (option B): tool errors the model corrected within a succeeded Run (counted, never hidden).
            **({'recovered_tool_errors': (evidence or {})['recovered_tool_errors']} if (evidence or {}).get('recovered_tool_errors') else {}),
            'receipts': sorted(observed.get('receipts', [])), 'reconciliation': observed.get('reconciliation', 'not_applicable'),
            'facts': observed.get('facts', {}),
            **({'test_stack': True} if test_stack else {}),
            **({'persona': persona} if persona else {}),
            **({'driver': driver_mode} if driver_mode else {}),
        }
        cumulative += counted
        decision, fired = evaluate(record, history, plan, cumulative=cumulative, monthly=month_cost_before + cumulative,
                                   owner_stop=owner_stop())
        if decision is None and calibration_pause and interaction['seq'] == plan['calibration']:
            decision, fired = 'pause', ['calibration']
        record['decision'] = {'action': decision or 'continue', 'rules': fired}
        _append(root / 'interactions.jsonl', record)
        history.append(record)
        if decision:
            status, rules, at_seq = ('stopped' if decision == 'stop' else 'paused'), fired, interaction['seq']
            break
    state = {'trial_id': trial_id, 'status': status, 'rules': rules, 'at_seq': at_seq, 'recorded': len(history),
             'planned': plan['total'], 'cumulative_cost': str(cumulative), 'currency': plan['currency'], 'order_digest': digest,
             'resumes': len(read_resumes(root)), **({'voided': len(read_voids(root))} if read_voids(root) else {}), 'updated_at': _iso(clock()), **({'test_stack': True} if test_stack else {}),
             **({'persona': persona} if persona else {}), **({'driver': driver_mode} if driver_mode else {})}
    tmp = state_path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, sort_keys=True))
    os.replace(tmp, state_path)
    return state


def read_resumes(directory):
    path = Path(directory) / 'resumes.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


class PgCollector:
    """Reads one Run's evidence through authz.nexloop_nx037_run_evidence (0161) as the read-only metrics role."""

    def __init__(self, connection, tenant, world='real'):
        self.connection, self.tenant, self.world = connection, tenant, world

    def collect(self, run_id):
        return self.connection.execute('select authz.nexloop_nx037_run_evidence(%s,%s,%s)', (self.tenant, self.world, run_id)).fetchone()[0]
