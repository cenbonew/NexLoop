#!/usr/bin/env python3
"""The owner ends stale action claims whose result is settled, from a private configuration (dispatcher 2026-10-31; migration
action_claim_settle, docs/release/run-action-claim-settle.md).

Logs in to the workbench as the owner (its own login realm) and calls only the governed owner Action settle_action_claim
(nexloop.action_claim.settle:1), one claim per call; it never connects to PostgreSQL. SQL ends a claim only when it is world-real,
active and more than an hour past its lease, and its category holds now: uncommitted (adapter nexloop.core / target postgres, nothing
written under the intent) or confirmed_not_delivered (the effect intent failed on the owner's 0210 determination over a not_found
observation, no provider acceptance, no delivered reply). Anything else is refused with its code; an unknown effect is never ended here.

    security find-generic-password -s <keychain service> -a <account> -w | \
        action_claim_settle.py --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] --username-file F --password-stdin --config F [--dry-run]

Configuration (private JSON, 0600; deploy/examples/action-claim-settle.example.json):
    {"schema": "nexloop-action-claim-settle/1", "confirmation_ref": "stale-claims:20261031-1", "reason": "...",
     "claims": [{"action_name": "Goal.create", "intent_id": "message-relay-...", "category": "uncommitted"}, ...]}   # 1-50, each once
Rerunning the same configuration replays (the Idempotency-Key is derived from the claim, its category and the confirmation reference).
--dry-run checks the configuration and prints the claims without logging in.

Output: one JSON object (action names, intent ids, settlement ids and refusal codes; never the username, password, cookie, CSRF token or
reason). Exit: 0 every claim settled (or replayed); 1 a claim was refused; 2 input refused. Standard library only.
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

SCHEMA = 'nexloop-action-claim-settle/1'
ACTION = re.compile(r'[A-Za-z][A-Za-z0-9_.]{0,159}')
INTENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
CATEGORIES = ('uncommitted', 'confirmed_not_delivered')


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or set(value) != {'schema', 'confirmation_ref', 'reason', 'claims'}:
        raise Refused('configuration schema')
    if type(value['confirmation_ref']) is not str or not REF.fullmatch(value['confirmation_ref']):
        raise Refused('confirmation_ref: [A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
    _reason(value['reason'])
    if len(value['confirmation_ref']) + 2 + len(value['reason']) > 500:
        raise Refused('confirmation_ref and reason together: at most 498 characters')
    claims = value['claims']
    if (type(claims) is not list or not 1 <= len(claims) <= 50
            or any(type(c) is not dict or set(c) != {'action_name', 'intent_id', 'category'} or type(c['action_name']) is not str
                   or not ACTION.fullmatch(c['action_name']) or type(c['intent_id']) is not str or not INTENT.fullmatch(c['intent_id'])
                   or c['category'] not in CATEGORIES for c in claims)):
        raise Refused('claims: 1-50 [{action_name, intent_id, category uncommitted|confirmed_not_delivered}]')
    if len({(c['action_name'], c['intent_id']) for c in claims}) != len(claims):
        raise Refused('claims: each claim once')
    return value


def idempotency_key(claim, confirmation_ref):
    """Stable per claim, category and confirmation reference: a rerun replays. [A-Za-z0-9._~-]{16,160}."""
    return 'claim-settle-' + hashlib.sha256(json.dumps([claim['action_name'], claim['intent_id'], claim['category'], confirmation_ref]).encode()).hexdigest()[:48]


def settle(workbench, config):
    results, failed = [], False
    for claim in config['claims']:
        status, body = workbench._call('POST', '/api/v1/workbench/actions/settle_action_claim',
            {'claim_action': claim['action_name'], 'intent_id': claim['intent_id'], 'category': claim['category'],
             'reason': config['confirmation_ref'] + ': ' + config['reason']},
            {'X-CSRF-Token': workbench.csrf, 'Idempotency-Key': idempotency_key(claim, config['confirmation_ref'])})
        entry = {'action_name': claim['action_name'], 'intent_id': claim['intent_id'], 'category': claim['category']}
        if status == 200 and isinstance(body, dict):
            entry.update(result='replayed' if body.get('replayed') else 'settled', settlement_id=body.get('settlement_id'),
                         claim_revision=body.get('claim_revision'))
        else:
            failed = True
            entry.update(result='refused', status=status, code=body.get('code') if isinstance(body, dict) else None,
                         reason=(body.get('details') or {}).get('reason') if isinstance(body, dict) else None)
        results.append(entry)
    return results, failed


def main(argv=None, *, stdin=None, transport=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--origin', required=True)
    parser.add_argument('--ca-file')
    parser.add_argument('--username-file', required=True)
    secret = parser.add_mutually_exclusive_group(required=True)
    secret.add_argument('--password-stdin', action='store_true')
    secret.add_argument('--password-file')
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True, ensure_ascii=False))
        return code

    try:
        config = load_config(read_private(args.config, 1 << 16))
        if args.dry_run:
            return emit({'ok': True, 'dry_run': True, 'confirmation_ref': config['confirmation_ref'],
                         'claims': [{k: c[k] for k in ('action_name', 'intent_id', 'category')} for c in config['claims']]}, 0)
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
            results, failed = settle(workbench, config)
        finally:
            workbench.logout()
    except PermissionError as error:
        return emit({'ok': False, 'detail': str(error)}, 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return emit({'ok': False, 'detail': type(error).__name__}, 1)
    return emit({'ok': not failed, 'dry_run': False, 'confirmation_ref': config['confirmation_ref'], 'claims': results}, 1 if failed else 0)


if __name__ == '__main__':
    sys.exit(main())
