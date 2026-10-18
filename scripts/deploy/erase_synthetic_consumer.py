#!/usr/bin/env python3
"""The owner marks test personas and erases a synthetic Consumer, from a private configuration (0217; AT-050 second restore drill,
scheduler 2026-10-18; ADR-034 first step).

Logs in to the workbench as the owner (its own login realm) and uses only governed workbench entries; it never connects to
PostgreSQL. The owner's credentials live only for the run (logged out at the end).

    erase_synthetic_consumer.py mark  --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] --username-file F \
        (--password-stdin | --password-file F) --config F --recipe F [--dry-run]
    erase_synthetic_consumer.py erase --origin ... --username-file F (--password-stdin | --password-file F) --config F [--dry-run]

    security find-generic-password -s <keychain service> -a <account> -w | erase_synthetic_consumer.py erase ... --password-stdin

mark: sets each listed Consumer's persona through the owner Action set_consumer_persona (nexloop.consumer.set_persona:1). Only a
Consumer whose principal is in the owner's private webchat-consumers recipe (schema nexloop-webchat-consumers/1, the same file
webchat_consumers.py applies) with persona simulated_agent is accepted; the persona written is the recipe's. Configuration
(private JSON, 0600; deploy/examples/consumer-persona.example.json):
    {"schema": "nexloop-consumer-persona/1", "confirmation_ref": "persona-mark:20261018-1", "reason": "...",
     "consumers": [{"consumer_id": "<64 hex>", "principal_id": "<the recipe's principal_id>"}]}

erase: reads each Consumer's erasure precheck (GET /api/v1/workbench/erasure-precheck/<consumer>: persona, erasure state, what is
still held; counts only), and unless --dry-run calls the owner Action erase_synthetic_consumer (nexloop.consumer.erase_synthetic:1):
SQL refuses a Consumer whose persona is real, then runs the NX-029 erasure (blocking at once; the keeper purges afterwards).
A Consumer the precheck shows as real is not sent at all. Configuration (deploy/examples/erase-synthetic-consumer.example.json):
    {"schema": "nexloop-erase-synthetic-consumer/1", "confirmation_ref": "at050-erase:20261018-1", "reason": "...",
     "consumers": ["<64 hex>"]}
Rerunning the same configuration replays (the Idempotency-Key is derived from the Consumer and the confirmation reference); after
the erasure, --dry-run is the post-check (erasure state, Claims not yet erased, messages still carrying a body, data exports).

Output: one JSON object (IDs, personas, states and counts; never a name, the username, password, cookie, CSRF token or the reason).
Exit: 0 done (or dry run with every Consumer acceptable); 1 a Consumer was refused or not acceptable; 2 input refused.
Standard library only.
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import ssl
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from real_dispatch_switch import HttpsTransport, Refused, Workbench, _reason, read_private  # noqa: E402

MARK_SCHEMA = 'nexloop-consumer-persona/1'
ERASE_SCHEMA = 'nexloop-erase-synthetic-consumer/1'
RECIPE_SCHEMA = 'nexloop-webchat-consumers/1'
HEX = re.compile(r'[a-f0-9]{64}')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:@-]{0,190}')
SYNTHETIC = ('simulated_agent', 'synthetic')
HELD = ('conversations', 'messages', 'messages_with_body', 'claims', 'claims_not_erased', 'data_exports')


def _json(text, schema, keys):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != schema or set(value) != keys:
        raise Refused('configuration schema')
    if type(value['confirmation_ref']) is not str or not REF.fullmatch(value['confirmation_ref']):
        raise Refused('confirmation_ref: [A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
    _reason(value['reason'])
    if len(value['confirmation_ref']) + 2 + len(value['reason']) > 500:
        raise Refused('confirmation_ref and reason together: at most 498 characters')
    return value


def load_mark_config(text):
    value = _json(text, MARK_SCHEMA, {'schema', 'confirmation_ref', 'reason', 'consumers'})
    items = value['consumers']
    if (type(items) is not list or not 1 <= len(items) <= 16
            or any(type(i) is not dict or set(i) != {'consumer_id', 'principal_id'} or type(i['consumer_id']) is not str or not HEX.fullmatch(i['consumer_id'])
                   or type(i['principal_id']) is not str or not NAME.fullmatch(i['principal_id']) for i in items)):
        raise Refused('consumers: 1-16 [{consumer_id 64 hex, principal_id}]')
    if len({i['consumer_id'] for i in items}) != len(items):
        raise Refused('consumers: each Consumer once')
    return value


def load_recipe(text):
    """The owner's private webchat-consumers recipe: principal_id -> persona (only the fields this tool needs are checked)."""
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('recipe is not JSON') from None
    if type(value) is not dict or value.get('schema_version') != RECIPE_SCHEMA or type(value.get('consumers')) is not list:
        raise Refused('recipe schema')
    personas = {}
    for c in value['consumers']:
        if type(c) is not dict or type(c.get('principal_id')) is not str or type(c.get('persona')) is not str:
            raise Refused('recipe consumers')
        if not c.get('revoke'):
            personas[c['principal_id']] = c['persona']
    return personas


def load_erase_config(text):
    value = _json(text, ERASE_SCHEMA, {'schema', 'confirmation_ref', 'reason', 'consumers'})
    items = value['consumers']
    if type(items) is not list or not 1 <= len(items) <= 16 or any(type(i) is not str or not HEX.fullmatch(i) for i in items):
        raise Refused('consumers: 1-16 Consumer IDs (64 hex)')
    if len(set(items)) != len(items):
        raise Refused('consumers: each Consumer once')
    return value


def idempotency_key(kind, consumer, confirmation_ref, extra=''):
    """Stable per operation, Consumer and confirmation reference: a rerun replays. [A-Za-z0-9._~-]{16,160}."""
    return kind + '-' + hashlib.sha256(json.dumps([kind, consumer, confirmation_ref, extra]).encode()).hexdigest()[:48]


def precheck(workbench, consumer):
    status, body = workbench._call('GET', '/api/v1/workbench/erasure-precheck/' + consumer)
    if status != 200 or not isinstance(body, dict):
        raise PermissionError(f'precheck unreadable ({status})')
    erasure = body.get('erasure')
    return {'consumer_object': body.get('consumer_object'), 'persona': body.get('persona'), 'persona_revision': body.get('persona_revision'),
            'erasure': None if not isinstance(erasure, dict) else {k: erasure.get(k) for k in ('request_id', 'source', 'state', 'phase', 'counts', 'completed_at')},
            'held': {k: (body.get('held') or {}).get(k) for k in HELD}}


def _refusal(status, body):
    return {'result': 'refused', 'status': status, 'code': body.get('code') if isinstance(body, dict) else None,
            'reason': (body.get('details') or {}).get('reason') if isinstance(body, dict) else None}


def mark(workbench, config, personas, *, dry_run=False):
    results, failed = [], False
    for item in config['consumers']:
        consumer = item['consumer_id']
        entry = {'consumer_id': consumer}
        wanted = personas.get(item['principal_id'])
        if wanted != 'simulated_agent':
            # Only the recipe's simulated_agent principals; the principal itself is never printed.
            entry['result'] = 'not_in_recipe' if wanted is None else 'recipe_persona_' + re.sub(r'[^a-z_]', '', wanted)[:40]
            failed = True
            results.append(entry)
            continue
        before = precheck(workbench, consumer)
        entry.update(persona_before=before['persona'], persona_revision=before['persona_revision'], target=wanted)
        if before['persona'] == wanted:
            entry['result'] = 'already_marked'
        elif dry_run:
            entry['result'] = 'would_mark'
        else:
            status, body = workbench._call('POST', '/api/v1/workbench/actions/set_consumer_persona',
                {'consumer_id': consumer, 'persona': wanted, 'reason': config['confirmation_ref'] + ': ' + config['reason']},
                {'X-CSRF-Token': workbench.csrf, 'Idempotency-Key': idempotency_key('persona', consumer, config['confirmation_ref'], wanted)})
            if status == 200 and isinstance(body, dict):
                entry.update(result='marked' if body.get('changed') else 'unchanged', persona=body.get('persona'), revision=body.get('revision'))
            else:
                entry.update(_refusal(status, body))
                failed = True
        results.append(entry)
    return results, failed


def erase(workbench, config, *, dry_run=False):
    results, failed = [], False
    for consumer in config['consumers']:
        entry = {'consumer_id': consumer, 'before': precheck(workbench, consumer)}
        before = entry['before']
        if before['erasure'] and before['erasure']['state'] != 'pending_confirmation':
            entry['result'] = 'already_erasing' if before['erasure']['state'] not in ('completed', 'completed_with_holds') else 'already_erased'
        elif before['persona'] not in SYNTHETIC:
            entry['result'] = 'persona_real'  # never sent: SQL would refuse it too
            failed = True
        elif not before['consumer_object']:
            entry['result'] = 'consumer_unavailable'
            failed = True
        elif dry_run:
            entry['result'] = 'would_erase'
        else:
            status, body = workbench._call('POST', '/api/v1/workbench/actions/erase_synthetic_consumer',
                {'consumer_id': consumer, 'reason': config['confirmation_ref'] + ': ' + config['reason']},
                {'X-CSRF-Token': workbench.csrf, 'Idempotency-Key': idempotency_key('erase-synthetic', consumer, config['confirmation_ref'])})
            if status == 200 and isinstance(body, dict):
                entry.update(result='replayed' if body.get('replayed') else 'erasing', request_id=body.get('request_id'), state=body.get('state'))
            else:
                entry.update(_refusal(status, body))
                failed = True
            entry['after'] = precheck(workbench, consumer)
        results.append(entry)
    return results, failed


def main(argv=None, *, stdin=None, transport=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('command', choices=('mark', 'erase'))
    parser.add_argument('--origin', required=True)
    parser.add_argument('--ca-file')
    parser.add_argument('--username-file', required=True)
    secret = parser.add_mutually_exclusive_group(required=True)
    secret.add_argument('--password-stdin', action='store_true')
    secret.add_argument('--password-file')
    parser.add_argument('--config', required=True)
    parser.add_argument('--recipe')
    parser.add_argument('--dry-run', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True, ensure_ascii=False))
        return code

    try:
        if args.command == 'mark':
            if not args.recipe:
                raise Refused('mark needs --recipe (the private webchat-consumers recipe)')
            config, personas = load_mark_config(read_private(args.config)), load_recipe(read_private(args.recipe, 1 << 20))
        else:
            if args.recipe:
                raise Refused('erase takes no --recipe')
            config, personas = load_erase_config(read_private(args.config)), None
        username = read_private(args.username_file, 1024).strip()
        password = (stdin or sys.stdin).readline().rstrip('\n') if args.password_stdin else read_private(args.password_file, 1024).rstrip('\n')
        if not username or not password:
            raise Refused('username and password required')
        workbench = Workbench(transport or HttpsTransport(args.origin, args.ca_file), args.origin)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    try:
        workbench.login(username, password)
        try:
            results, failed = (mark(workbench, config, personas, dry_run=args.dry_run) if args.command == 'mark'
                               else erase(workbench, config, dry_run=args.dry_run))
        finally:
            workbench.logout()
    except PermissionError as error:
        return emit({'ok': False, 'detail': str(error)}, 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return emit({'ok': False, 'detail': type(error).__name__}, 1)
    return emit({'ok': not failed, 'command': args.command, 'dry_run': args.dry_run, 'confirmation_ref': config['confirmation_ref'],
                 'consumers': results}, 1 if failed else 0)


if __name__ == '__main__':
    sys.exit(main())
