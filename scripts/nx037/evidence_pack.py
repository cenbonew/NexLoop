"""NX-037 A-class prep: the release evidence directory (14 §7, NX-037 design §3.2). Only ids, hashes, numbers, codes and times.

    python scripts/nx037/evidence_pack.py --out DIR [--item NAME=PATH ...] [--trial DIR]

Every source is optional; an item not given is recorded as `missing`, never invented. For each given file the pack records its
sha256 and size only: their content may name hosts, paths or people, so nothing else is copied. The one exception is the
release manifest, from which fixed fields are extracted (commit, tree, release id, CI result digest, catalog, contracts digest,
lock hashes, image digests and archive hashes). The controlled interaction records (`--trial`, the executor's directory) are
summarized: counts by kind and outcome, time decomposition percentiles, cost, the stop/pause outcome and the intent ids with
their reconciliation states. A trial run on the local synthetic stack (state or any record test_stack: true) is test evidence:
the pack says test_stack: true and real_evidence: false, and it never counts as evidence of the real trial.

Allowed values: uuids, hex hashes and commits, times and dates, decimal numbers, booleans, null, and codes from the fixed
vocabulary below (scenario kinds, rule ids, statuses). Any other string is refused when the pack is written and by `check`.

`check(DIR)` re-validates a pack; the command refuses to write into an existing directory.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ITEMS = ('inventory_before', 'inventory_after', 'release_manifest', 'ci_result', 'restore_drill', 'effect_reconciliation',
         'load_report', 'public_preflight', 'third_party_notices', 'known_risks')
_PATTERNS = [re.compile(p) for p in (
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',      # uuid
    r'(sha256:)?[0-9a-f]{7,128}',                                          # commit / digest
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})',  # time
    r'\d{4}-\d{2}-\d{2}',                                                  # date
    r'-?\d{1,18}(\.\d{1,12})?',                                            # decimal string
    r'\d{8}T\d{6}Z-[0-9a-f]{7,40}(-\d{1,4})?',                              # release id
)]
_KEY = re.compile(r'[a-z0-9_][a-z0-9_.:-]{0,63}')


def _vocabulary():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
    from nexloop_eios.controlled_interactions import KINDS, PAUSE_RULES, STOP_RULES
    return frozenset((*KINDS, *STOP_RULES, *PAUSE_RULES, *ITEMS, 'present', 'missing', 'completed', 'paused', 'stopped', 'continue',
                      'stop', 'pause', 'ok', 'error', 'settled', 'unresolved', 'not_applicable', 'accepted', 'dispatching', 'observed_fulfilled', 'unknown',
                      'fulfilled', 'failed', 'confirmed', 'synthetic_error', 'interaction_timeout', 'run_failed', 'delivery_missing', 'reconciliation_missing', 'unexpected_delivery', 'effect_evidence_unavailable', 'nexloop.nx037-evidence.v1', 'CNY', 'USD',
                      'agent', 'human', 'simulated_agent', 'mixed',
                      # 10/18: Run failure codes and the effect tools (0204 failure evidence)
                      'runtime_failed', 'runtime_tool_error', 'runtime_task_failed', 'runtime_model_unavailable', 'runtime_effect_unavailable',
                      'runtime_tool_unavailable', 'invalid_effect_request', 'tool_error', 'intent_payload_conflict', 'outside_catalog_terms',
                      'nexloop.service.request', 'nexloop.service.find', 'nexloop.enterprise.deliver', 'nexloop.enterprise.find', 'unregistered_tool',
                      'offplan_sender',  # 10/18: a message from an account outside the plan (0207)
                      'effect_intent_unavailable', 'request_scope_wrong_service'))  # 10/18: corrected tool errors (option B, 0208)


VOCABULARY = _vocabulary()


def allowed(value):
    if value is None or type(value) in (bool, int, float):
        return True
    return type(value) is str and (value in VOCABULARY or value.startswith('NX') and re.fullmatch(r'NX[A-Z]\d{2}', value) is not None
                                   or any(p.fullmatch(value) for p in _PATTERNS))


def project(value, dropped):
    """The allowed part of a JSON value; `dropped` counts what was removed."""
    if type(value) is dict:
        out = {}
        for k, v in value.items():
            if type(k) is not str or not _KEY.fullmatch(k):
                dropped[0] += 1
                continue
            kept = project(v, dropped)
            if kept is not _DROP:
                out[k] = kept
        return out
    if type(value) is list:
        return [x for x in (project(v, dropped) for v in value) if x is not _DROP]
    if allowed(value):
        return value
    dropped[0] += 1
    return _DROP


_DROP = object()


def violations(value, path='$'):
    if type(value) is dict:
        return [f for k, v in value.items() for f in (([path + '.<key>'] if not (type(k) is str and _KEY.fullmatch(k)) else []) + violations(v, path + '.' + str(k)))]
    if type(value) is list:
        return [f for i, v in enumerate(value) for f in violations(v, f'{path}[{i}]')]
    return [] if allowed(value) else [path]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _write(path, value):
    bad = violations(value)
    if bad:
        raise ValueError('evidence value not allowed at ' + bad[0])
    path.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=1) + '\n')
    path.chmod(0o600)


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def release_fields(manifest):
    """Fixed fields of an NX-034 release manifest; a field that is not an allowed value is left out (None)."""
    def keep(value):
        return value if allowed(value) else None
    catalog = manifest.get('catalog') or {}
    return {'release_id': keep(manifest.get('release_id')), 'commit': keep(manifest.get('commit')), 'tree': keep(manifest.get('tree')),
            'ci_result_digest': keep(manifest.get('ci_result_digest')), 'contracts_digest': keep(manifest.get('contracts_digest')),
            'catalog': {k: keep(v) for k, v in sorted(catalog.items()) if _KEY.fullmatch(k) and type(v) is not dict and type(v) is not list},
            'locks': [keep(v) for _, v in sorted((manifest.get('locks') or {}).items())],
            'images': [{'digest': keep(i.get('digest')), 'archive_sha256': keep(i.get('archive_sha256'))} for i in manifest.get('images') or []]}


def summarize_trial(directory):
    from decimal import Decimal
    root = Path(directory)
    from nexloop_eios.controlled_interactions import read_records
    records = read_records(root)  # an interaction the owner voided (voids.jsonl) is not evidence
    state = json.loads((root / 'state.json').read_text(encoding='utf-8'))
    kinds, outcomes = {}, {}
    for r in records:
        kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
        outcomes[r['outcome']] = outcomes.get(r['outcome'], 0) + 1
    times = {part: {f'p{int(q * 100)}': _percentile([r['time_ms'][part] for r in records], q) for q in (0.5, 0.95)}
             for part in ('end_to_end', 'model', 'tool', 'platform')}
    intents = {}
    for r in records:
        states = {s for s, n in r['intents'].items() if n}
        for intent in r['intent_ids']:
            intents[intent] = {'seq': r['seq'], 'reconciliation': r['reconciliation'], 'states': sorted(states)}
    return {
        'trial_id': state['trial_id'], 'status': state['status'], 'rules': state['rules'], 'at_seq': state['at_seq'],
        'planned': state['planned'], 'recorded': len(records), 'by_kind': kinds, 'by_outcome': outcomes, 'time_ms': times,
        'cost': {'total': str(sum((Decimal(r['cost']['total']) for r in records), Decimal(0))),
                 'counted': str(sum((Decimal(r['cost'].get('counted', r['cost']['total'])) for r in records), Decimal(0))), 'currency': state['currency'],
                 'max_run': max((r['cost']['total'] for r in records), key=Decimal, default='0')},
        'model_calls': sum(r['model']['calls'] for r in records), 'tool_calls': sum(r['tool_calls'] for r in records),
        'receipts': sum(len(r['receipts']) for r in records), 'first_at': records[0]['started_at'] if records else None,
        'last_at': records[-1]['ended_at'] if records else None,
        'test_stack': state.get('test_stack') is True or any(r.get('test_stack') is True for r in records),
        # Owner 2026-10-14: simulated_agent when the customers were AI agents the owner operated (the system itself was real).
        'persona': state.get('persona'),
    }, intents, records


def build(out, items, trial=None):
    root = Path(out)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    index = {'schema': 'nexloop.nx037-evidence.v1', 'items': {}}
    unknown = set(items) - set(ITEMS)
    if unknown:
        raise ValueError('unknown evidence item')
    for name in ITEMS:
        source = items.get(name)
        if source is None:
            index['items'][name] = {'status': 'missing'}
            continue
        path = Path(source)
        index['items'][name] = {'status': 'present', 'sha256': _sha256(path), 'bytes': path.stat().st_size}
        if name == 'release_manifest':
            _write(root / 'release.json', release_fields(json.loads(path.read_text(encoding='utf-8'))))
    if trial is not None:
        summary, intents, records = summarize_trial(trial)
        (root / 'interactions').mkdir(mode=0o700)
        _write(root / 'interactions' / 'summary.json', summary)
        _write(root / 'interactions' / 'reconciliation.json', intents)
        dropped = [0]
        _write(root / 'interactions' / 'records.json', project(records, dropped))
        consent = Path(trial) / 'consent.json'
        if consent.exists():  # D3: consumer ids and consent sha256 only
            participants = json.loads(consent.read_text(encoding='utf-8'))['participants']
            _write(root / 'interactions' / 'participants.json', [{k: p[k] for k in ('consumer_id', 'kind', 'persona', 'consent_sha256') if k in p}
                                                                 for p in participants])
        index['items']['interactions'] = {'status': 'present', 'records': len(records), 'dropped_values': dropped[0],
                                          'sha256': _sha256(Path(trial) / 'interactions.jsonl')}
        if summary['test_stack']:
            index['test_stack'], index['real_evidence'] = True, False
    else:
        index['items']['interactions'] = {'status': 'missing'}
    _write(root / 'index.json', index)
    return index


def check(out):
    """Every file of a pack is JSON holding only allowed values; returns the offending files (empty when clean)."""
    bad = []
    for path in sorted(Path(out).rglob('*')):
        if path.is_file():
            try:
                if violations(json.loads(path.read_text(encoding='utf-8'))):
                    bad.append(str(path.relative_to(out)))
            except ValueError:
                bad.append(str(path.relative_to(out)))
    return bad


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', required=True)
    parser.add_argument('--item', action='append', default=[], metavar='NAME=PATH')
    parser.add_argument('--trial')
    args = parser.parse_args(argv)
    items = {}
    for pair in args.item:
        name, _, path = pair.partition('=')
        items[name] = path
    if os.path.exists(args.out):
        print(json.dumps({'ok': False, 'detail': 'output directory exists'}))
        return 2
    try:
        index = build(args.out, items, args.trial)
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'detail': type(error).__name__}))
        return 1
    print(json.dumps({'ok': True, 'present': sorted(k for k, v in index['items'].items() if v['status'] == 'present'),
                      'missing': sorted(k for k, v in index['items'].items() if v['status'] == 'missing')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
