#!/usr/bin/env python3
"""The owner settles inbound messages no relay will serve, from a private configuration (S42 option A, owner decision 2026-10-30;
docs/design/relay-backlog-disposition.md, migration relay_backlog_settle).

Logs in to the workbench as the owner (its own login realm) and calls only the governed owner Action settle_relay_backlog
(nexloop.relay.backlog_settle:1), in batches of at most 50; it never connects to PostgreSQL. SQL settles a message only when its
Consumer is erased or has no active and serviceable relay enrollment now, and refuses the whole batch otherwise (code in the output).

    security find-generic-password -s <keychain service> -a <account> -w | \
        relay_backlog_settle.py --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] --username-file F --password-stdin --config F [--dry-run]

Configuration (private JSON, 0600; deploy/examples/relay-backlog-settle.example.json):
    {"schema": "nexloop-relay-backlog-settle/1", "confirmation_ref": "s42-settle:20261030-1", "reason": "...",
     "message_ids": ["<64 hex>", ...]}            # 1-500, each once
Rerunning the same configuration replays (the Idempotency-Key is derived from the batch and the confirmation reference). --dry-run
checks the configuration and prints the batches without logging in.

Output: one JSON object (message IDs, reasons, counts and refusal codes; never the username, password, cookie, CSRF token or reason).
Exit: 0 every batch settled (or replayed); 1 a batch was refused; 2 input refused. Standard library only.
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

SCHEMA = 'nexloop-relay-backlog-settle/1'
HEX = re.compile(r'[a-f0-9]{64}')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
BATCH = 50


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or set(value) != {'schema', 'confirmation_ref', 'reason', 'message_ids'}:
        raise Refused('configuration schema')
    if type(value['confirmation_ref']) is not str or not REF.fullmatch(value['confirmation_ref']):
        raise Refused('confirmation_ref: [A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
    _reason(value['reason'])
    if len(value['confirmation_ref']) + 2 + len(value['reason']) > 500:
        raise Refused('confirmation_ref and reason together: at most 498 characters')
    ids = value['message_ids']
    if type(ids) is not list or not 1 <= len(ids) <= 500 or any(type(i) is not str or not HEX.fullmatch(i) for i in ids):
        raise Refused('message_ids: 1-500 message IDs (64 hex)')
    if len(set(ids)) != len(ids):
        raise Refused('message_ids: each message once')
    return value


def batches(ids):
    return [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]


def idempotency_key(batch, confirmation_ref):
    """Stable per batch and confirmation reference: a rerun replays. [A-Za-z0-9._~-]{16,160}."""
    return 'relay-backlog-' + hashlib.sha256(json.dumps([sorted(batch), confirmation_ref]).encode()).hexdigest()[:48]


def settle(workbench, config):
    results, failed = [], False
    for batch in batches(config['message_ids']):
        status, body = workbench._call('POST', '/api/v1/workbench/actions/settle_relay_backlog',
            {'message_ids': batch, 'reason': config['confirmation_ref'] + ': ' + config['reason']},
            {'X-CSRF-Token': workbench.csrf, 'Idempotency-Key': idempotency_key(batch, config['confirmation_ref'])})
        if status == 200 and isinstance(body, dict):
            results.append({'result': 'replayed' if body.get('replayed') else 'settled', 'count': body.get('count'),
                            'settled': [{'message_id': s.get('message_id'), 'reason': s.get('reason')} for s in body.get('settled') or []]})
        else:
            failed = True
            results.append({'result': 'refused', 'status': status, 'message_ids': batch,
                            'code': body.get('code') if isinstance(body, dict) else None,
                            'reason': (body.get('details') or {}).get('reason') if isinstance(body, dict) else None})
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
            return emit({'ok': True, 'dry_run': True, 'confirmation_ref': config['confirmation_ref'], 'messages': len(config['message_ids']),
                         'batches': [len(b) for b in batches(config['message_ids'])]}, 0)
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
    return emit({'ok': not failed, 'dry_run': False, 'confirmation_ref': config['confirmation_ref'], 'batches': results}, 1 if failed else 0)


if __name__ == '__main__':
    sys.exit(main())
