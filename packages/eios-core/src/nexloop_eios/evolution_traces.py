"""NX-038 E2: trajectory read for diagnosis (docs/implementation/NX-038-design.md §11.2, §12.1, §12.5; D10-D12, D14).

Two routes, one output (D10): every sample is a `trajectory-sample` (packages/contracts/trajectory-sample.schema.json): ids, codes,
counts and times only; never a message body, a Claim value, a prompt or a quote.

- ``DatabaseTraceSource`` (formal diagnosis): the read-only role nexloop_evo_reader calls authz.nexloop_evo_trace_runs with a
  service credential read from a private file; the tenant comes from that credential (SQL), the world is real, the window and the
  persona list are the caller's. Runs of an erasing or erased Consumer are excluded by SQL and counted.
- ``EvidencePackTraceSource`` (design time and deterministic regression, D10): an NX-037 evidence pack's
  ``interactions/records.json``. Test-stack and non-real-driver records are dropped and counted (§11.3); voided interactions are
  not in the pack's records at all.

Both label every sample with its persona (D12, ADR-034) and filter by the caller's persona list; ``snapshot_digest`` is the
frozen input digest of a sample list (§2.3: the same samples give the same digest). Nothing here writes or holds a model, channel
or effect credential.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = '1.0'
PERSONAS = ('real', 'simulated_agent', 'synthetic')
DEFAULT_PERSONAS = ('real', 'simulated_agent')  # D12: simulated agents are diagnosed (labelled); synthetic test customers are not
_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class TraceUnavailable(ValueError):
    pass


def _counts(value):
    return {k: int(v) for k, v in sorted((value or {}).items()) if type(k) is str and _CODE.fullmatch(k) and type(v) is int}


def _failures(value):
    out = []
    for f in value or []:
        item = {k: f[k] for k in ('code', 'failure_code', 'failure_detail', 'failure_tool') if isinstance(f, dict) and isinstance(f.get(k), str)}
        if item:
            out.append(item)
    return out


def _recovered(value):
    return [{'tool': e['tool'], 'code': e['code']} for e in value or [] if isinstance(e, dict) and isinstance(e.get('tool'), str) and isinstance(e.get('code'), str)]


def sample_from_run(run):
    """One database Run (authz.nexloop_evo_trace_runs) as a trajectory sample."""
    e = run.get('evidence') or {}
    failure = e.get('failure')
    return {'schema_version': SCHEMA, 'source': 'database', 'run_id': run['run_id'], 'interaction_id': None, 'run_kind': run['kind'], 'scenario': None,
            'issued_at': run['issued_at'], 'consumer_id': run.get('consumer_id'), 'persona': run.get('persona'), 'test_stack': False,
            # Owner 2026-10-22 (NX-038 failure basis A): a message Run whose message got the fallback reply Run did not complete its reply.
            'outcome': 'reply_fallback' if run.get('reply_fallback') is True else None, 'error_code': None,
            'model': {k: int((e.get('model') or {}).get(k, 0)) for k in ('calls', 'failed', 'unknown')},
            'tool_calls': int((e.get('tool') or {}).get('calls', 0)), 'intents': _counts(e.get('intents')),
            'intent_ids': list(e.get('intent_ids') or []), 'refusals': _counts(e.get('refusals')),
            'failures': _failures([failure] if failure else []), 'recovered_tool_errors': _recovered(e.get('recovered_tool_errors')),
            'receipts': [], 'reconciliation': None}


def sample_from_record(record):
    """One NX-037 evidence-pack interaction record as a trajectory sample (the run entry's own record shape)."""
    return {'schema_version': SCHEMA, 'source': 'evidence_pack', 'run_id': record.get('run_id'), 'interaction_id': record.get('interaction_id'),
            'run_kind': None, 'scenario': record.get('kind'), 'issued_at': record.get('started_at'), 'consumer_id': None,
            'persona': record.get('persona'), 'test_stack': record.get('test_stack') is True,
            'outcome': record.get('outcome'), 'error_code': record.get('error_code'),
            'model': {k: int((record.get('model') or {}).get(k, 0)) for k in ('calls', 'failed', 'unknown')},
            'tool_calls': int(record.get('tool_calls') or 0), 'intents': _counts(record.get('intents')),
            'intent_ids': list(record.get('intent_ids') or []), 'refusals': _counts(record.get('refusals')),
            'failures': _failures(record.get('failures')), 'recovered_tool_errors': _recovered(record.get('recovered_tool_errors')),
            'receipts': sorted(r for r in record.get('receipts') or [] if isinstance(r, str)), 'reconciliation': record.get('reconciliation')}


def snapshot_digest(samples):
    """§2.3 frozen input digest: sha256 of the canonical, ordered sample list."""
    ordered = sorted(samples, key=lambda s: (s['source'], s['issued_at'] or '', s['run_id'] or '', s['interaction_id'] or ''))
    return hashlib.sha256(json.dumps(ordered, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _personas(personas):
    personas = tuple(personas)
    if not personas or any(p not in PERSONAS for p in personas) or len(set(personas)) != len(personas):
        raise TraceUnavailable('persona list')
    return personas


@dataclass
class TraceRead:
    samples: list
    excluded: dict = field(default_factory=dict)

    @property
    def digest(self):
        return snapshot_digest(self.samples)


class DatabaseTraceSource:
    """The database route (§11.2): authz.nexloop_evo_trace_runs as nexloop_evo_reader, paged until the window is read."""

    def __init__(self, connect, credential_file, *, page=500):
        from nexloop_eios.private_configuration import read_private_text
        self.connect, self.page = connect, page
        self._digest = lambda: hashlib.sha256(read_private_text(credential_file, maximum=16384).strip().encode()).hexdigest()

    def read(self, since, until, *, personas=DEFAULT_PERSONAS, maximum=2000):
        personas = _personas(personas)
        samples, after, excluded = [], None, {}
        with self.connect() as db:
            if db.execute('select current_user').fetchone()[0] != 'nexloop_evo_reader':
                raise TraceUnavailable('read-only evolution reader role required')
            while True:
                value = db.execute('select authz.nexloop_evo_trace_runs(%s,%s,%s,%s,%s,%s)',
                                   (self._digest(), since, until, after, self.page, list(personas))).fetchone()[0]
                excluded = value['excluded']
                samples += [sample_from_run(r) for r in value['runs']]
                after = value['next_after']
                if after is None:
                    break
                if len(samples) >= maximum:  # D20: at most 2000 Runs a diagnosis
                    excluded = {**excluded, 'over_limit': value['total'] - len(samples)}
                    break
        return TraceRead(samples[:maximum], excluded)


class EvidencePackTraceSource:
    """The evidence-pack route (§11.2): an NX-037 pack's interaction records, structure only (D14)."""

    def __init__(self, pack):
        self.pack = Path(pack)

    def read(self, *, personas=DEFAULT_PERSONAS):
        personas = _personas(personas)
        path = self.pack / 'interactions' / 'records.json'
        try:
            records = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise TraceUnavailable('evidence pack records unavailable') from None
        if type(records) is not list:
            raise TraceUnavailable('evidence pack records shape')
        samples, excluded = [], {'test_stack': 0, 'driver': 0, 'persona': 0}
        for record in records:
            if not isinstance(record, dict):
                raise TraceUnavailable('evidence pack records shape')
            if record.get('test_stack') is True:
                excluded['test_stack'] += 1
                continue
            if record.get('driver') not in (None, 'real'):
                excluded['driver'] += 1
                continue
            sample = sample_from_record(record)
            if sample['persona'] is not None and sample['persona'] not in personas:
                excluded['persona'] += 1
                continue
            samples.append(sample)
        return TraceRead(samples, excluded)
