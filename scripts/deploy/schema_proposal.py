#!/usr/bin/env python3
"""The owner's NX-040 Schema proposal review and publication from a private configuration (owner rule: "a configuration file and one
command"). Through the workbench Schema publication routes, i.e. the existing SchemaPublicationPort for the member's own workbench
session (ontology.schema.review: owner and reviewer; the operator gets 403), after the workbench login (logged out at the end).

    schema_proposal.py <basis|check|publish|hide|migrate-batch> --config F --dry-run
    security find-generic-password -s <keychain service> -a <account> -w | \
        schema_proposal.py <basis|check|publish|hide|migrate-batch> --config F --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] \
        --username-file F --password-stdin [--verify-reject]

    basis          the publication basis of the proposal (proposal, Parent version, Actions, object count) - summarized
    check          what a publication would be judged now: the F1 report (allowed, class, codes), the next version and the published
                   Definitions it may affect (NX-061 A1: impact - names, versions, changed properties; no data), by kind (object
                   type, properties, Object Sets, views, Functions, Actions) with a verdict: affected, no_impact (无影响: nothing
                   listed, truncated, unreadable or malformed), incomplete, or not_reported (no impact in the answer, whatever its
                   shape: exit 1); a listed impact that is not complete says so (complete false); writes nothing
    publish        the publication: SQL re-checks the F4 release permit (an accepted nx040-gates-1 report: G9-G13), F1, the successor
                   Actions and the permission proof; refused codes come back and nothing is written
    hide           the rollback of a published optional property: the next Schema version with it hidden
    migrate-batch  the next 10 000 objects of a batched publication (configuration: publication_id)
    --verify-reject  with check or publish: only the check runs (never a publication); exit 0 when the change is refused (and, when the
                   configuration lists expect_codes, with those codes), 1 when it would be allowed

Configuration (private JSON, 0600): {"schema": "nexloop-schema-proposal/1", "proposal_id": "<uuid>", "rationale": "...", "batch": false,
"expect_codes": [...], "publication_id": "<uuid>"} - only the fields the subcommand uses are required
(deploy/examples/schema-proposal.example.json). The login, private file and transport rules are those of real_dispatch_switch.py.
--dry-run checks the configuration and prints the request; it reads no password and sends nothing. The Idempotency-Key of a write
comes from the subcommand and the configuration, so a rerun replays. Output: one JSON line (status, codes, versions, counts; never the
username, password, cookie, CSRF token, DSN or the proposal's definitions). Exit: 0 done; 1 refused or unreachable; 2 input refused.
"""
import hashlib
import http.client
import json
import re
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from real_dispatch_switch import HttpsTransport, Refused, Workbench, read_private  # noqa: E402
import argparse  # noqa: E402


class _Parser(argparse.ArgumentParser):
    """A usage error never echoes the arguments (a password typed on the command line by mistake stays out of the output)."""

    def error(self, message):
        self.exit(2, f'{self.prog}: usage error (see --help); arguments are not echoed\n')

SCHEMA = 'nexloop-schema-proposal/1'
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')
BASE = '/api/v1/workbench'
NEEDS = {'basis': ('proposal_id',), 'check': ('proposal_id',), 'publish': ('proposal_id', 'rationale'), 'hide': ('proposal_id', 'rationale'),
         'migrate-batch': ('publication_id',)}
SUMMARY = ('published', 'codes', 'source', 'publication_id', 'version_after', 'permit', 'objects_migrated', 'objects_remaining', 'migrated',
           'remaining', 'batches', 'replay')


def load_config(command, text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA:
        raise Refused(f'configuration schema: {SCHEMA}')
    unknown = set(value) - {'schema', 'proposal_id', 'publication_id', 'rationale', 'batch', 'expect_codes'}
    if unknown:
        raise Refused(f'unknown fields {sorted(unknown)}')
    for field in NEEDS[command]:
        if field not in value:
            raise Refused(f'{command} needs {field}')
    for field in ('proposal_id', 'publication_id'):
        if field in value and (type(value[field]) is not str or not UUID.fullmatch(value[field])):
            raise Refused(f'{field}: a uuid')
    if 'rationale' in value and (type(value['rationale']) is not str or not 1 <= len(value['rationale'].strip()) <= 2000):
        raise Refused('rationale: 1-2000 characters')
    if 'batch' in value and type(value['batch']) is not bool:
        raise Refused('batch: true or false')
    if 'expect_codes' in value and (type(value['expect_codes']) is not list or not all(type(c) is str and CODE.fullmatch(c) for c in value['expect_codes'])):
        raise Refused('expect_codes: a list of refusal codes')
    return value


def request_of(command, config, verify_reject):
    """(method, path, body or None) of the one request the subcommand sends."""
    if verify_reject or command in ('basis', 'check'):
        return 'GET', f"{BASE}/schema-proposals/{config['proposal_id']}/{'basis' if command == 'basis' and not verify_reject else 'check'}", None
    if command == 'migrate-batch':
        return 'POST', f"{BASE}/schema-publications/{config['publication_id']}/migrate-batch", None
    body = {'rationale': config['rationale'], **({'batch': config['batch']} if command == 'publish' and config.get('batch') else {})}
    return 'POST', f"{BASE}/schema-proposals/{config['proposal_id']}/{command}", body


def idempotency_key(command, config):
    payload = {'command': command, **{k: config.get(k) for k in ('proposal_id', 'publication_id', 'rationale', 'batch')}}
    return 'schema-' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:48]


def summarize(command, value):
    out = _summary(command, value)
    if command == 'check' and not isinstance(out.get('impact'), dict):  # L6 F1: whatever the answer's shape, the impact or not_reported
        out['impact'] = impact_of(value.get('impact') if isinstance(value, dict) else None)
    return out


def _summary(command, value):
    if not isinstance(value, dict):
        return {}
    if command == 'basis':
        return {'operation': value.get('operation'), 'type_name': value.get('type_name'), 'status': value.get('status'),
                'parent_version': (value.get('schema') or {}).get('version'), 'actions': len(value.get('actions') or []), 'object_count': value.get('object_count')}
    if 'compatibility' in value:
        c = value['compatibility'] or {}
        return {'operation': value.get('operation'), 'type_name': value.get('type_name'), 'parent_version': value.get('parent_version'),
                'next_version': value.get('next_version'), 'successor_actions': value.get('successor_actions'),
                'allowed': c.get('allowed'), 'change_class': c.get('change_class'), 'codes': c.get('codes') or [],
                'impact': impact_of(value.get('impact'))}
    return {k: value[k] for k in SUMMARY if k in value}


IMPACT_ENTRY = ('definition_type', 'stable_name', 'version', 'pinned_type_version', 'changed_properties')
# The listed Definitions by kind, as /check names them (eios.ontology.definitions.DefinitionType; 0322 reads the first four).
IMPACT_KINDS = (('object_set', 'object_sets'), ('object_view', 'views'), ('function', 'functions'), ('action', 'actions'))
VERDICTS = ('affected', 'no_impact', 'incomplete')  # a check whose impact verdict is none of these is not ok (not_reported, or no verdict)
VERDICT_TEXT = {'affected': '有影响', 'no_impact': '无影响', 'incomplete': '影响不完整：有定义解析失败、被截断或回答格式不对，需另看 unreadable / truncated',
                'not_reported': '接口未返回影响清单（不是无影响）'}
PARTIAL = '（不完整：有一类被截断、有定义解析失败或回答格式不对，affected 是至少这么多条）'


def impact_of(value):
    """NX-061 A1 (owner 2026-10-26: shown, not a release gate): the Definitions the /check answer lists, those depending on a changed
    property first - names, versions and property names only, as /check returns them (schema_impact.impact), and the same list by
    kind: the object type, the changed properties, the Object Sets, views, Functions and Actions. An empty list is never silent:
    verdict no_impact only when the list is complete - nothing truncated, nothing unreadable, dependents a list of entries - and
    empty (incomplete otherwise); a listed impact that is not complete is affected with complete false and its text says so (L6
    S1, S2); an answer without impact is not_reported, not "no impact"."""
    if not isinstance(value, dict):
        return {'verdict': 'not_reported', 'verdict_text': VERDICT_TEXT['not_reported']}
    raw = value.get('dependents')
    malformed = not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw)
    listed = [{k: e.get(k) for k in IMPACT_ENTRY} for e in (raw if isinstance(raw, list) else ()) if isinstance(e, dict)]
    known = {kind for kind, _ in IMPACT_KINDS}
    by_kind = {name: [f"{e['stable_name']}@{e['version']}" for e in listed if e['definition_type'] == kind] for kind, name in IMPACT_KINDS}
    by_kind['other'] = [f"{e['definition_type']}:{e['stable_name']}@{e['version']}" for e in listed if e['definition_type'] not in known]
    truncated, unreadable = bool(value.get('truncated')), value.get('unreadable') or []
    complete = not (truncated or unreadable or malformed)
    verdict = 'affected' if listed else 'no_impact' if complete else 'incomplete'
    return {'verdict': verdict, 'verdict_text': VERDICT_TEXT[verdict] + ('' if complete or not listed else PARTIAL), 'complete': complete,
            'type_name': value.get('type_name'),
            'object_types': [value['type_name']] if listed and value.get('type_name') else [],
            'changed_properties': value.get('changed_properties') or [], 'affected': len(listed),
            'affected_by_changed_properties': value.get('affected_by_changed_properties', 0), 'dependents': listed, **by_kind,
            'truncated': truncated, 'unreadable': unreadable}


def main(argv=None, *, stdin=None, transport=None):
    parser = _Parser(description=__doc__.split('\n\n')[0])
    parser.add_argument('command', choices=sorted(NEEDS))
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--verify-reject', action='store_true')
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
        if args.verify_reject and args.command not in ('check', 'publish'):
            raise Refused('--verify-reject goes with check or publish')
        config = load_config(args.command, read_private(args.config))
        method, path, body = request_of(args.command, config, args.verify_reject)
        planned = {'command': args.command, 'verify_reject': args.verify_reject, 'method': method, 'path': path, 'body': body}
        if args.dry_run:
            return emit({'ok': True, 'dry_run': True, **planned}, 0)
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
            extra = {'X-CSRF-Token': workbench.csrf, 'Idempotency-Key': idempotency_key(args.command, config)} if method == 'POST' else None
            status, result = workbench._call(method, path, body if method == 'POST' else None, extra)
        finally:
            workbench.logout()
    except PermissionError as error:
        return emit({'ok': False, 'detail': str(error)}, 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return emit({'ok': False, 'detail': type(error).__name__}, 1)
    out = {'command': args.command, 'verify_reject': args.verify_reject, 'dry_run': False, 'status': status}
    if status != 200:
        out.update(ok=False, code=result.get('code') if isinstance(result, dict) else None)
        return emit(out, 1)
    out['result'] = summarize('check' if args.verify_reject else args.command, result)
    if args.verify_reject:
        codes, expected = out['result'].get('codes') or [], config.get('expect_codes') or []
        rejected = out['result'].get('allowed') is False and set(expected) <= set(codes)
        out.update(ok=rejected, rejected=out['result'].get('allowed') is False, expected_codes=expected)
        return emit(out, 0 if rejected else 1)
    ok = not (args.command in ('publish', 'hide') and out['result'].get('published') is False)
    impact = out['result'].get('impact')
    verdict = impact.get('verdict') if isinstance(impact, dict) else None
    ok = ok and not (args.command == 'check' and verdict not in VERDICTS)  # never a silent pass (L6 F1)
    out['ok'] = ok
    return emit(out, 0 if ok else 1)


if __name__ == '__main__':
    sys.exit(main())
