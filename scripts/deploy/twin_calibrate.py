#!/usr/bin/env python3
"""The owner's consumer twin calibration from a private configuration (owner rule: "a configuration file and one command").

One governed owner Action through the workbench actions entry, after the owner's workbench login (logged out at the end):
    calibrate_consumer_twin   nexloop.consumer_twin.calibrate   NX-042 T5: a frozen calibration report of one twin version over 1-3
                                                               non-overlapping calibration windows against real aggregates (D3: 80%
                                                               intervals covering 70%-90% of at least 20 cells, groups of 30+
                                                               Consumers); passed makes the version selectable by NX-043.
It never connects to PostgreSQL and holds no credential beyond the owner's own workbench login for the duration of the run. The
login, private file and transport rules are those of sim_world.py (same directory).

    twin_calibrate.py --config F --dry-run
    security find-generic-password -s <keychain service> -a <account> -w | \
        twin_calibrate.py --config F --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] --username-file F --password-stdin

Configuration (private JSON, 0600, owned by the caller; deploy/examples/twin-calibrate.example.json):
    {"schema": "nexloop-twin-calibrate/1", "twin_id": "trial.service_consumer", "version": 1,
     "windows": [{"split_at": "YYYY-MM-DD", "span_days": 28}, ...]}      1-3 windows, UTC days, span 28 | 56 | 84
Every window must start at or after the version's publication, have ended, and not overlap another (checked by the server).

--dry-run checks the configuration and prints the request; it reads no password and sends nothing. Rerunning the same configuration
replays the request (its Idempotency-Key comes from the configuration): the answer is the governed outcome (outcome_id, replayed) and
no second report is written. Output: one JSON object (HTTP status, the report
reference, conclusion, coverage and the version's calibration status; never the username, password, cookie, CSRF token or a DSN).
Exit: 0 done (whatever the conclusion); 1 refused or unreachable; 2 input refused. Standard library only.
"""
import hashlib
import http.client
import json
import re
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sim_world import HttpsTransport, Refused, Workbench, _Parser, read_private  # noqa: E402

SCHEMA = 'nexloop-twin-calibrate/1'
OPERATION = 'calibrate_consumer_twin'
TWIN = re.compile(r'[a-z][a-z0-9_.-]{0,63}')
DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
# A replayed request answers with the governed outcome (outcome_id, replayed) instead of the report summary.
SUMMARY = ('report_ref', 'twin_id', 'version', 'conclusion', 'coverage', 'calibration', 'created', 'outcome_id', 'replayed')


def load_config(text):
    """The request body: exactly the entry's fields, checked as the entry checks them (workbench_actions_http.OPERATIONS)."""
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA:
        raise Refused(f'configuration schema: {SCHEMA}')
    if set(value) != {'schema', 'twin_id', 'version', 'windows'}:
        raise Refused('fields: twin_id, version, windows')
    if type(value['twin_id']) is not str or not TWIN.fullmatch(value['twin_id']):
        raise Refused(f'twin_id: {TWIN.pattern}')
    if type(value['version']) is not int or not 1 <= value['version'] <= 999999999:
        raise Refused('version: a positive integer')
    windows = value['windows']
    if type(windows) is not list or not 1 <= len(windows) <= 3:
        raise Refused('windows: 1-3 calibration windows')
    for w in windows:
        if type(w) is not dict or set(w) != {'split_at', 'span_days'} or type(w['split_at']) is not str or not DAY.fullmatch(w['split_at']):
            raise Refused('window: {split_at YYYY-MM-DD, span_days}')
        if type(w['span_days']) is not int or w['span_days'] not in (28, 56, 84):
            raise Refused('span_days: 28, 56 or 84')
    return {'twin_id': value['twin_id'], 'version': value['version'], 'windows': windows}


def idempotency_key(body):
    """Stable for the same configuration ([A-Za-z0-9._~-]{16,160})."""
    return 'twincal-' + hashlib.sha256(json.dumps({'operation': OPERATION, **body}, sort_keys=True).encode()).hexdigest()[:48]


def main(argv=None, *, stdin=None, transport=None):
    parser = _Parser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--origin')
    parser.add_argument('--ca-file')
    parser.add_argument('--username-file')
    parser.add_argument('--password-stdin', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True, ensure_ascii=False))
        return code

    try:
        body = load_config(read_private(args.config))
        if args.dry_run:
            return emit({'ok': True, 'dry_run': True, 'operation': OPERATION, 'request': body, 'idempotency_key': idempotency_key(body)}, 0)
        if not args.origin or not args.username_file or not args.password_stdin:
            raise Refused('--origin, --username-file and --password-stdin required (or --dry-run)')
        username = read_private(args.username_file, 1024).strip()
        password = (stdin or sys.stdin).readline().rstrip('\n')
        if not username or not password:
            raise Refused('username and password required')
        workbench = Workbench(transport or HttpsTransport(args.origin, args.ca_file), args.origin)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    try:
        workbench.login(username, password)
        try:
            status, result = workbench._call('POST', '/api/v1/workbench/actions/' + OPERATION, body,
                                             {'X-CSRF-Token': workbench.csrf, 'Idempotency-Key': idempotency_key(body)})
        finally:
            workbench.logout()
    except PermissionError as error:
        return emit({'ok': False, 'detail': str(error)}, 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return emit({'ok': False, 'detail': type(error).__name__}, 1)
    out = {'ok': status == 200, 'dry_run': False, 'operation': OPERATION, 'status': status}
    if status == 200 and isinstance(result, dict):
        out['result'] = {k: result[k] for k in SUMMARY if k in result and (result[k] is None or type(result[k]) in (str, int, float, bool))}
    elif isinstance(result, dict):
        out['code'] = result.get('code')
        reason = (result.get('details') or {}).get('reason')
        if reason:
            out['reason'] = reason
    return emit(out, 0 if status == 200 else 1)


if __name__ == '__main__':
    sys.exit(main())
