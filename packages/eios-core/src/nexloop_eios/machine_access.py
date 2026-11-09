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

