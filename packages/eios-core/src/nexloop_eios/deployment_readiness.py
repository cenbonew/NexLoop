"""Deployment readiness of a service (NX-032 / NX-035): fixed reason codes only, never details.

Ready means: PostgreSQL reachable as the service's own restricted role; the applied migration list equals this release's
catalog exactly; no restore in progress (NX-035: restored or replayed is not ready; verified_readonly is ready for reads and
inbound only, reported as mode read_only); no deletion replay open (NX-029); and the real-dispatch master switch
(REAL_DISPATCH_ENABLED) is a valid value and closed while the restore keeps dispatch closed. The body also reports how many
tenants and tenant Actions have real dispatch open (0220), as counts only. A service adds its own checks
(the API: signer and Artifact directory) as extra (code, passed) pairs.
"""
from eios.adapters.postgres.migrations import MigrationDrift

REASONS = ('database_unavailable', 'database_role_invalid', 'migration_drift', 'restore_in_progress', 'deletion_replay_open',
           'dispatch_switch_invalid', 'dispatch_switch_open_during_restore', 'signer_unavailable', 'artifact_directory_unavailable')


def dispatch_switch(environ):
    value = (environ or {}).get('REAL_DISPATCH_ENABLED')
    if value is None or value == 'false':
        return 'closed'
    if value == 'true':
        return 'open'
    return 'invalid'


def evaluate(connection_factory, *, environ, extra=()):
    """connection_factory() is a context manager yielding a connection as the service role. Returns (status code, body)."""
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.bootstrap import verify
    reasons, mode = [], 'normal'
    switch = dispatch_switch(environ)
    if switch == 'invalid':
        reasons.append('dispatch_switch_invalid')
    try:
        with connection_factory() as connection, connection.transaction():
            connection.execute('set transaction read only')
            try:
                verify_application_role(connection)
            except Exception:
                reasons.append('database_role_invalid')
                raise
            try:
                verify(connection)
            except MigrationDrift:
                reasons.append('migration_drift')
                raise
            facts = connection.execute('select authz.nexloop_service_readiness()').fetchone()[0]
            # 0220: the real dispatch switches, as counts only (reported, never a readiness condition).
            real = connection.execute('select authz.nexloop_real_dispatch_summary()').fetchone()[0]
    except Exception:
        if not set(reasons) & {'database_role_invalid', 'migration_drift'}:
            reasons.append('database_unavailable')
        facts = real = None
    if facts is not None:
        restore = facts.get('restore') or {}
        if restore.get('ready') is not True:
            reasons.append('restore_in_progress')
        elif restore.get('dispatch') is not True:
            mode = 'read_only'
            if switch == 'open':
                reasons.append('dispatch_switch_open_during_restore')
        if (facts.get('erasure') or {}).get('ready') is not True:
            reasons.append('deletion_replay_open')
    for code, passed in extra:
        if code not in REASONS:
            raise ValueError('unknown readiness code')
        if not passed:
            reasons.append(code)
    ready = not reasons
    body = {'ready': ready, 'mode': mode if ready else 'not_ready', 'reasons': sorted(set(reasons)),
            'dispatch_switch': switch if switch != 'invalid' else 'invalid',
            'real_dispatch': {'tenants_open': (real or {}).get('tenants_open'), 'actions_open': (real or {}).get('actions_open')}}
    return (200 if ready else 503), body
