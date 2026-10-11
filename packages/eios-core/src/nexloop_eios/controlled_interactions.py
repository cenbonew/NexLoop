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
PAUSE_RULES = ('error_rate', 'cumulative_cost', 'monthly_cost', 'unresolved_unknown_effect', 'calibration')
_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
NAMESPACE = uuid.UUID('6f1d3c2e-37a0-4c37-9b37-000000000037')
SCHEMA = 'nexloop.nx037-trial.v1'


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
    if type(value) is not dict or value.get('schema') != SCHEMA:
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


def validate_order(plan, order):
    """An owner-edited order (a list of kinds) must hold exactly the plan's mix."""
    if type(order) is not list or any(k not in KINDS for k in order) or len(order) != plan['total'] \
            or {k: order.count(k) for k in KINDS} != plan['mix']:
        raise PlanInvalid('order does not match the mix')
    return order


def order_digest(order):
    return hashlib.sha256(json.dumps(order, separators=(',', ':')).encode()).hexdigest()


def schedule(plan, *, trial_id, order=None):
    """The interactions in order: the owner's order when given (validated against the mix), otherwise kinds interleaved evenly
    (largest remainder), so every kind appears early. Deterministic for a trial id; each interaction has a stable id that
    survives retries and restarts."""
    if order is not None:
        return [{'seq': i + 1, 'kind': k, 'interaction_id': str(uuid.uuid5(NAMESPACE, f'{trial_id}:{i + 1}'))}
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
    if _decimal(record['cost']['total']) > plan['cost_caps']['per_run']:
        stops.append('per_run_cost_exceeded')
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
    return ('pause', pauses) if pauses else (None, [])


def _iso(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _append(path, row):
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def read_records(directory):
    path = Path(directory) / 'interactions.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def run(plan, driver, collector, directory, *, trial_id, clock=None, owner_stop=None, month_cost_before=Decimal(0), order=None,
        resume=None, calibration_pause=False):
    """Runs (or resumes after a pause) the trial into `directory`: interactions.jsonl (one record per interaction, written
    before the next starts) and state.json (the outcome). An interaction already recorded is never run again: resuming
    continues after the last record (the error window restarts after a pause). A stopped trial does not resume (the owner
    reviews and starts a new trial id, a uuid). A paused trial raises TrialPaused unless `resume` is the owner's confirmation
    reference, which is appended to resumes.jsonl (fsync) before anything runs; the interaction order may not change across
    resumes. With `calibration_pause` the trial pauses after the plan's calibration interactions (rule 'calibration') so the
    owner confirms the cost per interaction before the rest."""
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
    digest = order_digest([i['kind'] for i in interactions])
    if previous and previous.get('order_digest', digest) != digest:
        raise PlanInvalid('the interaction order changed')
    if previous and previous['status'] == 'paused':
        if resume is None:
            raise TrialPaused(','.join(previous['rules']))
        if not _REF.fullmatch(str(resume)):
            raise ValueError('owner confirmation must be an opaque reference')
        _append(root / 'resumes.jsonl', {'after_seq': previous['at_seq'], 'rules': previous['rules'], 'confirmation_ref': resume,
                                         'at': _iso(clock())})
    elif resume is not None:
        raise ValueError('the trial is not paused')
    history = read_records(root)
    cumulative = sum((_decimal(r['cost']['total']) for r in history), Decimal(0))
    done = {r['interaction_id'] for r in history}
    status, rules, at_seq = 'completed', [], None
    for interaction in interactions:
        if interaction['interaction_id'] in done:
            continue
        if owner_stop():
            status, rules, at_seq = 'stopped', ['owner_stop'], interaction['seq']
            break
        started = clock()
        observed = driver.interact(interaction)
        ended = clock()
        evidence = collector.collect(observed['run_id']) if observed.get('run_id') else None
        end_to_end = int(round((ended - started).total_seconds() * 1000))
        model_ms = int(evidence['model']['ms']) if evidence else 0
        tool_ms = int(evidence['tool']['ms']) if evidence else 0
        costs = (evidence or {}).get('costs', {})
        total = sum((_decimal(v['amount']) for v in costs.values()), Decimal(0))
        record = {
            'seq': interaction['seq'], 'interaction_id': interaction['interaction_id'], 'kind': interaction['kind'],
            'run_id': observed.get('run_id'), 'started_at': _iso(started), 'ended_at': _iso(ended),
            'outcome': observed['outcome'], 'error_code': observed.get('error_code'),
            'time_ms': {'end_to_end': end_to_end, 'model': model_ms, 'tool': tool_ms, 'platform': max(end_to_end - model_ms - tool_ms, 0)},
            'model': {k: (evidence or {}).get('model', {}).get(k, 0) for k in ('calls', 'failed', 'unknown')},
            'tool_calls': (evidence or {}).get('tool', {}).get('calls', 0),
            'cost': {'total': str(total), 'by_kind': {k: v['amount'] for k, v in sorted(costs.items())}, 'currency': plan['currency']},
            'intents': (evidence or {}).get('intents', {}), 'intent_ids': (evidence or {}).get('intent_ids', []),
            'refusals': (evidence or {}).get('refusals', {}),
            'receipts': sorted(observed.get('receipts', [])), 'reconciliation': observed.get('reconciliation', 'not_applicable'),
            'facts': observed.get('facts', {}),
        }
        cumulative += total
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
             'resumes': len(read_resumes(root)), 'updated_at': _iso(clock())}
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
