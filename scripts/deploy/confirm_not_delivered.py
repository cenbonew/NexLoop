#!/usr/bin/env python3
"""The owner confirms unknown effects did not happen, from a private configuration (0210; owner 2026-10-18, no browser console).

Logs in to the workbench as the owner (its own login realm), reads each intent's precheck (GET
/api/v1/workbench/effect-determination/<intent>: one code, the state, the latest provider observation and the earliest time a
determination is allowed; read only), and, unless --dry-run, calls the governed owner Action confirm_effect_not_delivered
(POST /api/v1/workbench/actions/confirm_effect_not_delivered, Action nexloop.effect.confirm_not_delivered:1, owner only) for
every intent whose precheck is eligible; nothing else. It never connects to PostgreSQL and sends nothing to a provider; the
intent and its outbox become failed and the reply's inbound message is escalated for the owner's follow-up (default) or left
for its one fallback reply (reply_followup fallback_reply). The owner's credentials live only for the run (logged out at the end).

    confirm_not_delivered.py --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] --username-file F \
        (--password-stdin | --password-file F) --config F [--dry-run]

    security find-generic-password -s <keychain service> -a <account> -w | confirm_not_delivered.py ... --password-stdin

Configuration (private JSON, 0600, owned by the caller; deploy/examples/confirm-not-delivered.example.json):
    {"schema": "nexloop-confirm-not-delivered/1", "confirmation_ref": "owner-confirm:20261018-1",
     "reason": "...", "provider_retention_checked": true,
     "intents": [{"intent_id": "<uuid>", "reply_followup": "owner_follow_up"}]}
provider_retention_checked must be true: the owner has checked that the provider keeps its records long enough for not_found to
mean "never received" (design Q5). Each intent's Idempotency-Key is derived from the intent, the confirmation reference and the
follow-up, so rerunning the same configuration replays instead of repeating.

Output: one JSON object (codes, states and times; never the username, password, cookie, CSRF token or the reason).
Exit: 0 every intent determined (or dry run with every intent eligible or already determined); 1 an intent was not eligible or
a request was refused; 2 input refused. Standard library only.
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

SCHEMA = 'nexloop-confirm-not-delivered/1'
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
FOLLOWUPS = ('owner_follow_up', 'fallback_reply')
PRECHECK_FIELDS = ('code', 'state', 'outbox_state', 'outbound_state', 'first_unknown_at', 'latest_observation', 'wait_until', 'reply_message_escalated')


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or set(value) != {'schema', 'confirmation_ref', 'reason', 'provider_retention_checked', 'intents'}:
        raise Refused('configuration schema')
    if type(value['confirmation_ref']) is not str or not REF.fullmatch(value['confirmation_ref']):
        raise Refused('confirmation_ref: [A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
    if value['provider_retention_checked'] is not True:
        raise Refused('provider_retention_checked must be true (check the provider record retention first)')
    intents = value['intents']
    if (type(intents) is not list or not 1 <= len(intents) <= 16
            or any(type(i) is not dict or set(i) - {'intent_id', 'reply_followup'} or type(i.get('intent_id')) is not str or not UUID.fullmatch(i['intent_id'])
                   or i.get('reply_followup', 'owner_follow_up') not in FOLLOWUPS for i in intents)):
        raise Refused('intents: 1-16 [{intent_id uuid, reply_followup owner_follow_up|fallback_reply}]')
    if len({i['intent_id'] for i in intents}) != len(intents):
        raise Refused('intents: each intent once')
    return {'confirmation_ref': value['confirmation_ref'], 'reason': _reason(value['reason']),
            'intents': [{'intent_id': i['intent_id'], 'reply_followup': i.get('reply_followup', 'owner_follow_up')} for i in intents]}


def idempotency_key(intent, confirmation_ref, followup):
    """Stable per intent, confirmation reference and follow-up: a rerun replays. [A-Za-z0-9._~-]{16,160}."""
    return 'confirm-nd-' + hashlib.sha256(json.dumps([intent, confirmation_ref, followup]).encode()).hexdigest()[:48]


def precheck(workbench, intent):
    status, body = workbench._call('GET', '/api/v1/workbench/effect-determination/' + intent)
    if status != 200 or not isinstance(body, dict):
        raise PermissionError(f'precheck unreadable ({status})')
    return {k: body.get(k) for k in PRECHECK_FIELDS}


def confirm(workbench, item, config):
    request = {'intent_id': item['intent_id'], 'confirmation_ref': config['confirmation_ref'], 'reason': config['reason'],
               'provider_retention_checked': True, 'reply_followup': item['reply_followup']}
    return workbench._call('POST', '/api/v1/workbench/actions/confirm_effect_not_delivered', request,
                           {'X-CSRF-Token': workbench.csrf, 'Idempotency-Key': idempotency_key(item['intent_id'], config['confirmation_ref'], item['reply_followup'])})


def run(workbench, username, password, config, *, dry_run=False):
    workbench.login(username, password)
    try:
        results, failed = [], False
        for item in config['intents']:
            entry = {'intent_id': item['intent_id'], 'reply_followup': item['reply_followup'], 'before': precheck(workbench, item['intent_id'])}
            code = entry['before']['code']
            if code == 'already_determined':
                entry['result'] = 'already_determined'
            elif code != 'eligible':
                entry['result'] = 'not_eligible';failed = True
            elif dry_run:
                entry['result'] = 'eligible'
            else:
                status, body = confirm(workbench, item, config)
                if status == 200 and isinstance(body, dict):
                    entry['result'] = 'replayed' if body.get('replayed') else 'determined'
                    entry.update({k: body[k] for k in ('determination_id', 'reply_followup', 'outbound_state') if k in body})
                else:
                    entry.update(result='refused', status=status, code=body.get('code') if isinstance(body, dict) else None,
                                 reason=(body.get('details') or {}).get('reason') if isinstance(body, dict) else None)
                    failed = True
                entry['after'] = precheck(workbench, item['intent_id'])
            results.append(entry)
        return {'dry_run': dry_run, 'confirmation_ref': config['confirmation_ref'], 'intents': results}, 1 if failed else 0
    finally:
        workbench.logout()


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
        config = load_config(read_private(args.config))
        username = read_private(args.username_file, 1024).strip()
        password = (stdin or sys.stdin).readline().rstrip('\n') if args.password_stdin else read_private(args.password_file, 1024).rstrip('\n')
        if not username or not password:
            raise Refused('username and password required')
        workbench = Workbench(transport or HttpsTransport(args.origin, args.ca_file), args.origin)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    try:
        out, code = run(workbench, username, password, config, dry_run=args.dry_run)
    except PermissionError as error:
        return emit({'ok': False, 'detail': str(error)}, 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return emit({'ok': False, 'detail': type(error).__name__}, 1)
    return emit({'ok': code == 0, **out}, code)


if __name__ == '__main__':
    sys.exit(main())
