"""NX-062 M2a (docs/implementation/NX-062-design.md §3.4, §10 C1-C3): the reviewed constants of the machine access channel.

NEVER_GRANT_MARKERS is the never-grant list of migration nx062_machine_access (control.nexloop_machine_never_grant_markers()): a machine
capability whose name contains any marker is refused by the allowlist trigger. tests/test_machine_access_pg.py locks the two lists
together and checks that it covers service_grants.FORBIDDEN_MARKERS, so the list changes only with a new migration and this file.
The allowlist itself is SQL (control.nexloop_machine_capabilities); nothing here grants anything.
"""
NEVER_GRANT_MARKERS = ('approval', 'machine_client', 'machine_key', 'service_grant', 'grant', 'credential', 'member', 'role', 'owner',
                       'publish', 'schema', 'policy', 'rule', 'relation_type', 'erase', 'erasure', 'retention', 'legal_hold',
                       'real_dispatch', 'channel', 'remediation', 'external_effect', 'nexloop.service.request',
                       'nexloop.enterprise.deliver', 'nexloop.service.query', 'nexloop.service.receipt_reconcile',
                       'nexloop.receipt.reconcile', 'schema.review')

# The machine identity of a key, as authz.nexloop_machine_identity_snapshot returns it.
CREDENTIAL_KIND = 'machine_key'
PRINCIPAL_PREFIX = 'nexloop-machine:'
SUBJECT_PREFIX = 'nexloop-machine-subject:'


def forbidden(capability):
    """The Python form of control.nexloop_machine_capability_forbidden (tests assert they agree)."""
    return not isinstance(capability, str) or any(m in capability.lower() for m in NEVER_GRANT_MARKERS)



# ---- NX-062 M3: the machine rate policy (L6 C4) -------------------------------------------------------------------------------
RATE_SCHEMA = 'nexloop-machine-rate-limits/1'
RATE_KEYS = {'schema_version', 'manifest_version', 'per_key_per_minute', 'per_tenant_per_minute'}


class RatePolicyRejected(ValueError):
    pass


def validate_rate_policy(value):
    """deploy/configuration/machine-rate-limits.v<N>.json; the same rules as control.nexloop_publish_machine_rate_policy."""
    if type(value) is not dict or set(value) != RATE_KEYS or value['schema_version'] != RATE_SCHEMA:
        raise RatePolicyRejected('shape')
    for name, high in (('manifest_version', 10 ** 6), ('per_key_per_minute', 10000), ('per_tenant_per_minute', 100000)):
        if type(value[name]) is not int or not 1 <= value[name] <= high:
            raise RatePolicyRejected(name)
    if value['per_tenant_per_minute'] < value['per_key_per_minute']:
        raise RatePolicyRejected('per_tenant_per_minute')
    return value


def publish_rate_policy(path, *, database_url_file):
    """Publish the policy through the configurator (trusted configuration); an unchanged file writes nothing."""
    import json
    from pathlib import Path

    import psycopg

    from nexloop_eios.private_configuration import read_private_text
    policy = validate_rate_policy(json.loads(Path(path).read_text(encoding='utf-8')))
    with psycopg.connect(read_private_text(database_url_file, maximum=16384), autocommit=True) as db:
        return db.execute('select control.nexloop_publish_machine_rate_policy(%s::jsonb)', (json.dumps(policy, sort_keys=True),)).fetchone()[0]


# ---- NX-062 M5c: the machine note caps (W3) ----------------------------------------------------------------------------------------
NOTE_SCHEMA = 'nexloop-machine-note-limits/1'
NOTE_KEYS = {'schema_version', 'manifest_version', 'notes_per_client_per_day', 'notes_per_object_per_day'}


def validate_note_limits(value):
    """deploy/configuration/machine-note-limits.v<N>.json; the same rules as control.nexloop_publish_machine_note_limits."""
    if type(value) is not dict or set(value) != NOTE_KEYS or value['schema_version'] != NOTE_SCHEMA:
        raise RatePolicyRejected('shape')
    for name, high in (('manifest_version', 10 ** 6), ('notes_per_client_per_day', 100000), ('notes_per_object_per_day', 10000)):
        if type(value[name]) is not int or not 1 <= value[name] <= high:
            raise RatePolicyRejected(name)
    return value


def publish_note_limits(path, *, database_url_file):
    import json
    from pathlib import Path

    import psycopg

    from nexloop_eios.private_configuration import read_private_text
    policy = validate_note_limits(json.loads(Path(path).read_text(encoding='utf-8')))
    with psycopg.connect(read_private_text(database_url_file, maximum=16384), autocommit=True) as db:
        return db.execute('select control.nexloop_publish_machine_note_limits(%s::jsonb)', (json.dumps(policy, sort_keys=True),)).fetchone()[0]


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    p = argparse.ArgumentParser(prog='python -m nexloop_eios.machine_access')
    sub = p.add_subparsers(dest='command', required=True)
    pub = sub.add_parser('publish-rate-policy', help='NX-062 M3: publish deploy/configuration/machine-rate-limits.v<N>.json (configurator DSN file)')
    pub.add_argument('--policy', type=Path, required=True)
    pub.add_argument('--database-url-file', type=Path, required=True)
    notes = sub.add_parser('publish-note-limits', help='NX-062 M5c: publish deploy/configuration/machine-note-limits.v<N>.json (configurator DSN file)')
    notes.add_argument('--policy', type=Path, required=True)
    notes.add_argument('--database-url-file', type=Path, required=True)
    a = p.parse_args(argv)
    publish = publish_note_limits if a.command == 'publish-note-limits' else publish_rate_policy
    try:
        print(json.dumps(publish(a.policy, database_url_file=a.database_url_file)))
    except (RatePolicyRejected, ValueError, OSError):
        print(json.dumps({'error': 'policy_rejected_or_unavailable'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
