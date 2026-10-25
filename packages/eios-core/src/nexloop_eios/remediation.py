"""NX-057 R1: remediation intents after a final effect failure, suggestions only (docs/design/remediation-intents.md v4; migration
remediation_intents).

The rules are trusted configuration (deploy/configuration/remediation-rules.v1.json), published by the configurator into
control.nexloop_remediation_rules; ``validate_rules`` applies the same checks as SQL before connecting (SQL re-checks the source
against the declared effect categories). The planner runs on the plan reevaluator's tick, at most once a minute per process, through
the signed service port authz.nexloop_remediation_plan_command (eios:action:nexloop.remediation.plan:1): SQL decides everything (which
failure, which rule, the limits and the switches); this module only signs the call. The deployment switch is REMEDIATION_ENABLED of
the planner's own process: only the exact value 'true' opens it.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import psycopg

from nexloop_eios.plan_reevaluation import PlanUnavailable, _SignedPort
from nexloop_eios.postgres_artifacts import canonical_payload

RULES_SCHEMA = 'nexloop-remediation-rules/1'
PLAN_ACTION = 'eios:action:nexloop.remediation.plan:1'
SETTING = 'nexloop.remediation_enabled'
FAILURE_KINDS = ('origin_expired', 'confirmed_not_delivered')
# R2 opens reevaluate; governed_action comes with R3 (SQL refuses it too).
OPEN_KINDS = ('escalate', 'reevaluate')
ROUND_SECONDS = 60
ROUND_LIMIT = 50
_RULE_ID = re.compile(r'[a-z][a-z0-9_]{0,63}')
_STABLE_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_.]{0,159}')
_RULE_KEYS = {'rule_id', 'source', 'failure_kinds', 'kind', 'mode', 'due_after_seconds', 'purpose'}


class RemediationRulesRejected(ValueError):
    pass


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def validate_rules(value):
    """The checks of control.nexloop_configure_remediation_rules that need no database."""
    if type(value) is not dict or set(value) != {'schema_version', 'manifest_version', 'decision', 'limits', 'rules'} \
            or value['schema_version'] != RULES_SCHEMA or not _int(value['manifest_version'], 1, 999999999) \
            or type(value['decision']) is not str or not 1 <= len(value['decision']) <= 2000:
        raise RemediationRulesRejected('manifest')
    limits = value['limits']
    if type(limits) is not dict or set(limits) != {'tenant_per_hour', 'consumer_per_day', 'plan_wakes_per_week', 'breaker_per_day'} \
            or not _int(limits['tenant_per_hour'], 1, 1000) or not _int(limits['consumer_per_day'], 1, 99) \
            or not _int(limits['plan_wakes_per_week'], 1, 9) or not _int(limits['breaker_per_day'], 1, 99):
        raise RemediationRulesRejected('limits')
    rules = value['rules']
    if type(rules) is not list or len(rules) > 100:
        raise RemediationRulesRejected('rules')
    ids, keys = set(), set()
    for r in rules:
        if type(r) is not dict or not _RULE_KEYS <= set(r) or set(r) - _RULE_KEYS - {'exclude_bound_replies'}:
            raise RemediationRulesRejected('rule shape')
        source, kinds = r['source'], r['failure_kinds']
        if (type(r['rule_id']) is not str or not _RULE_ID.fullmatch(r['rule_id']) or r['rule_id'] in ids
                or type(source) is not dict or set(source) != {'stable_name', 'version'}
                or type(source['stable_name']) is not str or not _STABLE_NAME.fullmatch(source['stable_name'])
                or not _int(source['version'], 1, 999999999)
                or type(kinds) is not list or not 1 <= len(kinds) <= 2 or len(set(kinds)) != len(kinds) or not set(kinds) <= set(FAILURE_KINDS)
                or r['mode'] not in ('automatic', 'manual') or not _int(r['due_after_seconds'], 3600, 2592000)
                or type(r['purpose']) is not str or not 1 <= len(r['purpose']) <= 500
                or ('exclude_bound_replies' in r and type(r['exclude_bound_replies']) is not bool)):
            raise RemediationRulesRejected('rule ' + str(r.get('rule_id')))
        if r['kind'] not in OPEN_KINDS:
            raise RemediationRulesRejected('rule ' + r['rule_id'] + ': kind ' + str(r['kind']) + ' is not available yet')
        if source['stable_name'] == 'nexloop.service.request' and r.get('exclude_bound_replies') is not True:
            raise RemediationRulesRejected('rule ' + r['rule_id'] + ': bound replies must be excluded')
        for kind in kinds:
            key = (source['stable_name'], source['version'], kind, r['kind'])
            if key in keys:
                raise RemediationRulesRejected('rule ' + r['rule_id'] + ': duplicate')
            keys.add(key)
        ids.add(r['rule_id'])
    return value


def load_rules(path):
    return validate_rules(json.loads(Path(path).read_text(encoding='utf-8')))


def publish_rules(text, tenant, *, world='real', expected_revision, database_url_file):
    """Technical configurator only (SQL refuses any other session). The file's exact text: SQL records its sha256 (equal to sha256sum
    of the file) and the expected revision (the tenant's current rules version, 0 before the first), G1 (migration
    remediation_rule_publications)."""
    from nexloop_eios.trusted_configuration import configurator_connection
    validate_rules(json.loads(text))
    if type(expected_revision) is not int or expected_revision < 0:
        raise RemediationRulesRejected('expected revision')
    db = configurator_connection(database_url_file)
    with db, db.transaction():
        db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
        return db.execute('select control.nexloop_publish_remediation_rules(%s,%s,%s,%s,%s)',
                          (tenant, world, text, hashlib.sha256(text.encode('utf-8')).hexdigest(), expected_revision)).fetchone()[0]


def apply_rules(manifest, tenant, *, world='real', expected_revision, database_url_file):
    """publish_rules for a manifest held in memory (its canonical text)."""
    return publish_rules(canonical_payload(validate_rules(manifest)), tenant, world=world, expected_revision=expected_revision,
                         database_url_file=database_url_file)


def deployment_open(environ=None):
    return (os.environ if environ is None else environ).get('REMEDIATION_ENABLED') == 'true'


class RemediationPort(_SignedPort):
    """The planner's service port (eios:action:nexloop.remediation.plan:1)."""
    PROTOCOL = 'nexloop-remediation-plan-v1'; ACTION = PLAN_ACTION; FUNCTION = 'nexloop_remediation_plan_command'

    def __init__(self, pool, session, signer, *, environ=None):
        super().__init__(pool, session, signer)
        self.environ = environ

    def _prepare(self, db):
        # The 5 s bound of the round (design §6.1): set before the call, so it bounds the call itself (L6 R1 review 1).
        db.execute("set local statement_timeout='5000ms'")
        db.execute('select set_config(%s,%s,true)', (SETTING, 'true' if deployment_open(self.environ) else 'false'))

    def round(self, limit=ROUND_LIMIT):
        return self._signed({'verb': 'round', 'limit': limit})


# The last round per tenant in this process: the reevaluator ticks every few seconds, the planner once a minute (design §6.1).
_LAST_ROUND = {}


class RemediationPlanner:
    def __init__(self, pool, session, signer, *, interval_seconds=ROUND_SECONDS, environ=None, clock=time.monotonic, rounds=None):
        # rounds: the last round per (tenant, world). The process keeps one (the worker builds a planner per tick); a caller with
        # its own clock passes its own, so no earlier planner of the process decides whether this one is due.
        self.rounds = _LAST_ROUND if rounds is None else rounds
        self.port = RemediationPort(pool, session, signer, environ=environ)
        self.key = (session.authentication.tenant_id, session.world)
        self.interval_seconds, self.clock = interval_seconds, clock

    def run_once(self):
        now = self.clock()
        last = self.rounds.get(self.key)
        if last is not None and now - last < self.interval_seconds:
            return {'status': 'not_due'}
        self.rounds[self.key] = now
        try:
            return self.port.round()
        except PlanUnavailable:
            # Without its grant (service grants not yet applied) the planner is simply off; the reevaluator keeps working.
            return {'status': 'unavailable'}
        except psycopg.Error as error:
            # A timeout, a deadlock or any other database error ends this round only (rolled back); the reevaluator's own work of
            # the tick stays reported (L6 R1 review 5). The next round starts again from the same failure rows.
            return {'status': 'error', 'code': error.sqlstate or 'unknown'}


def main(argv=None):
    p = argparse.ArgumentParser(description='NexLoop remediation rules (trusted configuration)')
    sub = p.add_subparsers(dest='command', required=True)
    rules = sub.add_parser('rules')
    rules.add_argument('--manifest', type=Path, required=True)
    rules.add_argument('--tenant')
    rules.add_argument('--world', default='real')
    rules.add_argument('--database-url-file', type=Path)
    rules.add_argument('--expected-revision', type=int, help='the tenant\'s current rules version (0 before the first); required with --apply')
    mode = rules.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    a = p.parse_args(argv)
    try:
        text = a.manifest.read_text(encoding='utf-8')
        manifest = validate_rules(json.loads(text))
        if a.check:
            print(json.dumps({'valid': True, 'manifest_version': manifest['manifest_version'], 'rules': len(manifest['rules']),
                              'manifest_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}))
            return 0
        if a.tenant is None or a.database_url_file is None or a.expected_revision is None:
            raise RemediationRulesRejected('tenant, database url file and expected revision required')
        print(json.dumps(publish_rules(text, a.tenant, world=a.world, expected_revision=a.expected_revision, database_url_file=a.database_url_file)))
        return 0
    except Exception as error:
        print(json.dumps({'valid': False, 'reason': str(error) if isinstance(error, RemediationRulesRejected) else 'unavailable'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
