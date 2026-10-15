#!/usr/bin/env python3
"""Offline shadow of the decision-evaluation layer (design §6): ask each item's question set and report distributions.

Input JSON: {"items": [{"id": "<label>", "set": "claim_review" | "handoff", "state": <text or object>,
             ["extracted_kind": "<kind>"]}, ...]} — synthetic data or an export of test-persona conversations only.

    nx_decision_shadow.py --input ITEMS.json [--questions deploy/configuration/decision-questions.v1.json] [--out REPORT.json]
                          (default, --dry-run) | --live --endpoint https://… --credential-file F --model ID

- Default is a dry run: no network, no credential read. It prints, per item, what would be sent: question-set version, question
  ids, state digest and size. Never the state text.
- --live calls the endpoint once per item (DecisionEvalClient: beta header, timeouts, backoff, `uncertain` on any failure) and
  reports, per question set and question: probability (or choice / score) distributions, the share of yes / no / between
  (to a human) by the thresholds, decisions, latency percentiles and the failure rate. The report holds numbers and digests only.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
from nexloop_eios import decision_eval as de  # noqa: E402

DEFAULT_QUESTIONS = ROOT / 'deploy/configuration/decision-questions.v1.json'


class InputInvalid(ValueError):
    pass


def load_items(path, questions):
    body = json.loads(Path(path).read_text())
    items = body.get('items') if type(body) is dict else None
    if type(items) is not list or not 1 <= len(items) <= 10000:
        raise InputInvalid('items')
    for item in items:
        if type(item) is not dict or not set(item) <= {'id', 'set', 'state', 'extracted_kind'} or type(item.get('id')) is not str \
                or item.get('set') not in questions['sets'] or 'state' not in item:
            raise InputInvalid('item shape')
    return items


def _bucket(p):
    return f'{min(int(p * 10), 9) / 10:.1f}'


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def plan(items, questions, model='<model>'):
    out = []
    for item in items:
        qset = questions['sets'][item['set']]
        body = de.canonical(de.request_body(qset, item['state'], model)).encode()
        out.append({'id': item['id'], 'set': qset['version'], 'questions': sorted(qset['questions']),
                    'state_digest': de.state_digest(item['state']), 'request_bytes': len(body)})
    return out


def report(items, questions, results):
    sets = {}
    for item, result in zip(items, results):
        name = item['set']
        qset = questions['sets'][name]
        s = sets.setdefault(name, {'version': qset['version'], 'items': 0, 'failures': {}, 'decisions': {}, 'latency_ms': [],
                                   'questions': {qid: {'distribution': {}, 'verdicts': {}} for qid in qset['questions']}})
        s['items'] += 1
        s['latency_ms'].append(result.latency_ms)
        if result.status != 'ok':
            s['failures'][result.reason] = s['failures'].get(result.reason, 0) + 1
        decision, _ = de.decide(name, qset, result, extracted_kind=item.get('extracted_kind'))
        s['decisions'][decision] = s['decisions'].get(decision, 0) + 1
        for qid, verdict in de.verdicts(qset, result).items():
            q = s['questions'][qid]
            q['verdicts'][str(verdict)] = q['verdicts'].get(str(verdict), 0) + 1
            if result.status == 'ok':
                a = result.answers[qid]
                key = _bucket(a['probability']) if a['type'] == 'noul' else str(a['choice'] if a['type'] == 'choice' else a['score'])
                q['distribution'][key] = q['distribution'].get(key, 0) + 1
    for s in sets.values():
        latencies = s.pop('latency_ms')
        s['latency_ms'] = {'p50': _percentile(latencies, 0.5), 'p95': _percentile(latencies, 0.95),
                           'mean': round(statistics.fmean(latencies), 1) if latencies else None}
        s['failure_rate'] = round(sum(s['failures'].values()) / s['items'], 4) if s['items'] else None
        for q in s['questions'].values():
            total = sum(q['verdicts'].values())
            q['shares'] = {k: round(v / total, 4) for k, v in sorted(q['verdicts'].items())} if total else {}
    return {'schema': 'nexloop-decision-shadow/1', 'items': len(items), 'sets': sets}


def main(argv=None, *, client_factory=None):
    parser = argparse.ArgumentParser(description='Offline shadow of the decision-evaluation layer')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--questions', type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument('--out', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', default=True)
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--endpoint')
    parser.add_argument('--credential-file', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--timeout', type=float, default=5.0)
    args = parser.parse_args(argv)
    try:
        questions = de.load_questions(args.questions)
        items = load_items(args.input, questions)
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 2
    if not args.live:
        print(json.dumps({'ok': True, 'mode': 'dry-run', 'network': False, 'requests': plan(items, questions, args.model or '<model>')},
                         ensure_ascii=False))
        return 0
    if not (args.endpoint and args.credential_file and args.model):
        print(json.dumps({'ok': False, 'error': 'live needs --endpoint, --credential-file and --model'}))
        return 2
    try:
        client = (client_factory or de.DecisionEvalClient)(endpoint=args.endpoint, credential_file=args.credential_file, model=args.model,
                                                           timeout=args.timeout)
    except ValueError as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 2
    results = [client.evaluate(questions['sets'][item['set']], item['state']) for item in items]
    out = report(items, questions, results)
    if args.out:
        if args.out.exists():
            print(json.dumps({'ok': False, 'error': 'report exists'}))
            return 2
        args.out.write_text(json.dumps(out, indent=2, sort_keys=True) + '\n')
        args.out.chmod(0o600)
    print(json.dumps({'ok': True, 'mode': 'live', **out}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
