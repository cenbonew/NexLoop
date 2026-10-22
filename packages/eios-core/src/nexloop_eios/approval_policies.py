"""NX-056 A5 (docs/design/unified-approvals.md v5 §3): the approval policies as trusted configuration.

deploy/configuration/approval-policies.v<N>.json is checked here before connecting (the same constraints SQL enforces again:
control.nexloop_configure_approval_policies and the 0321 policy guard) and applied by the technical configurator only. A policy is
append-only: a changed policy is a new version of it; the current version given again is a replay.
"""
import json
import re
from pathlib import Path

from nexloop_eios.postgres_artifacts import canonical_payload

SCHEMA = 'nexloop-approval-policies/1'
ROLES = ('owner', 'operator', 'reviewer')
_CAPABILITY = re.compile(r'[a-z][a-z0-9_]{0,63}(\.[a-z][a-z0-9_]{0,63}){1,3}')
_FIELD = re.compile(r'[a-z][a-z0-9_]{0,63}')
_KEYS = {'capability', 'version', 'action_version', 'condition', 'quorum', 'approver_roles', 'expires_after_seconds', 'summary_fields'}


class ApprovalPoliciesRejected(ValueError):
    pass


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def validate_policies(value):
    if type(value) is not dict or value.get('schema_version') != SCHEMA or not _int(value.get('version'), 1, 999999999) \
            or set(value) - {'schema_version', 'version', 'decision', 'policies'}:
        raise ApprovalPoliciesRejected('manifest')
    policies = value.get('policies')
    if type(policies) is not list or not 1 <= len(policies) <= 100:
        raise ApprovalPoliciesRejected('policies')
    seen = set()
    for p in policies:
        if type(p) is not dict or not _KEYS <= set(p) or set(p) - _KEYS - {'purpose'}:
            raise ApprovalPoliciesRejected('policy shape')
        cap, condition = p['capability'], p['condition']
        if (type(cap) is not str or not _CAPABILITY.fullmatch(cap) or cap.startswith('approvals.') or cap in seen
                or not _int(p['version'], 1, 999999999) or not _int(p['action_version'], 1, 999999999) or not _int(p['quorum'], 1, 5)
                or not _int(p['expires_after_seconds'], 900, 604800)
                or type(p['approver_roles']) is not list or not p['approver_roles'] or not set(p['approver_roles']) <= set(ROLES)
                or len(set(p['approver_roles'])) != len(p['approver_roles'])
                or type(p['summary_fields']) is not list or any(type(f) is not str or not _FIELD.fullmatch(f) for f in p['summary_fields'])
                or (condition is not None and (type(condition) is not dict or set(condition) != {'field', 'equals'}
                    or type(condition['field']) is not str or not _FIELD.fullmatch(condition['field'])
                    or type(condition['equals']) not in (str, int, bool)))
                or ('purpose' in p and (type(p['purpose']) is not str or not 1 <= len(p['purpose']) <= 500))):
            raise ApprovalPoliciesRejected('policy ' + str(cap))
        seen.add(cap)
    return value


def load_policies(path):
    return validate_policies(json.loads(Path(path).read_text(encoding='utf-8')))


def apply_policies(manifest, *, database_url_file):
    """Technical configurator only (SQL refuses any other session)."""
    from nexloop_eios.trusted_configuration import configurator_connection
    validate_policies(manifest)
    db = configurator_connection(database_url_file)
    with db, db.transaction():
        db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
        return db.execute('select control.nexloop_configure_approval_policies(%s::jsonb)', (canonical_payload(manifest),)).fetchone()[0]


__all__ = ['ApprovalPoliciesRejected', 'apply_policies', 'load_policies', 'validate_policies']
