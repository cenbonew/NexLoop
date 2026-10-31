#!/usr/bin/env python3
"""The owner cancels open commitments from a private configuration, one command (I36, owner 2026-10-31; docs/release/run-commitment-cancel.md).

Logs in to the workbench as the owner (its own login realm) and calls only the governed human Action Commitment.cancel (operation
cancel_commitment), one commitment per call; it never connects to PostgreSQL. SQL accepts a cancel only for a conditional, open or
in_progress commitment and records cancel_requested; the commitment keeper then moves it to cancelled on its ledger (0111). A fulfilled,
breached or cancelled commitment is refused with its code.

    security find-generic-password -s <keychain service> -a <account> -w | \
        commitment_cancel.py --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] --username-file F --password-stdin --config F [--dry-run]

Configuration (private JSON, 0600; deploy/examples/commitment-cancel.example.json):
    {"schema": "nexloop-commitment-cancel/1", "confirmation_ref": "i36:20261031-1", "reason": "...",
     "commitments": ["<64 hex>", ...]}   # 1-500, each once
Rerunning the same configuration replays (the Idempotency-Key is derived from the commitment and the confirmation reference).
--dry-run checks the configuration and prints the commitments without logging in.

Output: one JSON object (commitment ids, results cancel_requested / replayed / refused, outcome ids and refusal codes; never the username, password, cookie, CSRF token or reason).
Exit: 0 every cancel request accepted (or replayed); 1 a commitment was refused; 2 input refused. Standard library only.
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

SCHEMA = 'nexloop-commitment-cancel/1'
COMMITMENT = re.compile(r'[0-9a-f]{64}')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or set(value) != {'schema', 'confirmation_ref', 'reason', 'commitments'}:
        raise Refused('configuration schema')
    if type(value['confirmation_ref']) is not str or not REF.fullmatch(value['confirmation_ref']):
        raise Refused('confirmation_ref: [A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
    _reason(value['reason'])
    if len(value['confirmation_ref']) + 2 + len(value['reason']) > 500:
        raise Refused('confirmation_ref and reason together: at most 498 characters')
    items = value['commitments']
    if type(items) is not list or not 1 <= len(items) <= 500 or any(type(c) is not str or not COMMITMENT.fullmatch(c) for c in items):
        raise Refused('commitments: 1-500 commitment ids (64 lowercase hex)')
    if len(set(items)) != len(items):
        raise Refused('commitments: each commitment once')
    return value


def idempotency_key(commitment_id, confirmation_ref):
    """Stable per commitment and confirmation reference: a rerun replays. [A-Za-z0-9._~-]{16,160}."""
    return 'commitment-cancel-' + hashlib.sha256(json.dumps([commitment_id, confirmation_ref]).encode()).hexdigest()[:48]


def cancel(workbench, config):
    results, failed = [], False
    for commitment_id in config['commitments']:
        status, body = workbench._call('POST', '/api/v1/workbench/actions/cancel_commitment',
            {'commitment_id': commitment_id, 'reason': config['confirmation_ref'] + ': ' + config['reason']},
            {'X-CSRF-Token': workbench.csrf, 'Idempotency-Key': idempotency_key(commitment_id, config['confirmation_ref'])})
        entry = {'commitment_id': commitment_id}
        if status == 200 and isinstance(body, dict):
            entry.update(result='replayed' if body.get('replayed') else 'cancel_requested', outcome_id=body.get('outcome_id'))
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
                         'commitments': config['commitments']}, 0)
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
            results, failed = cancel(workbench, config)
        finally:
            workbench.logout()
    except PermissionError as error:
        return emit({'ok': False, 'detail': str(error)}, 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return emit({'ok': False, 'detail': type(error).__name__}, 1)
    return emit({'ok': not failed, 'dry_run': False, 'confirmation_ref': config['confirmation_ref'], 'commitments': results}, 1 if failed else 0)


if __name__ == '__main__':
    sys.exit(main())
