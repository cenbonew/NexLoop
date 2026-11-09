"""NX-062 M5b: machine writes - prepare, commit and the intent query (docs/implementation/NX-062-M5-design.md §4, §9 R4, R5).

prepare and the intent query are single SQL calls. commit is where the API signs: in one transaction it reads the key's identity, the
capability's Action resource and the six machine authority facts (M5a), signs a command binding client, intent, prepare digest and world
(R5) with the authority signing key, and calls authz.nexloop_machine_commit, which re-runs every check in SQL. The signing key stays in
the API process (never in a runtime); the command lives 20 s on the database clock. Nothing here decides authority."""
import hmac
from datetime import timedelta

from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.signing import SIGNED_COMMAND_TTL

PROTOCOL = 'nexloop-machine-governed-v1'
FACT_KINDS = ('actor', 'grants', 'machine_authentication', 'policies', 'scope', 'subject')


def _call(connection, sql, args):
    return connection.execute(sql, args).fetchone()[0]


def prepare(pool, *, prefix, digest, capability, intent_id, params):
    with pool.connection() as c, c.transaction():
        verify_application_role(c)
        return _call(c, 'select authz.nexloop_machine_prepare(%s,%s,%s,%s,%s::jsonb)', (prefix, digest, capability, intent_id, canonical_payload(params)))


def intent(pool, *, prefix, digest, intent_id):
    with pool.connection() as c, c.transaction():
        verify_application_role(c)
        return _call(c, 'select authz.nexloop_machine_intent(%s,%s,%s)', (prefix, digest, intent_id))


def signed_command(connection, signer, *, prefix, digest, capability, intent_id, prepare_digest):
    """The command text and its signature, read and signed inside the caller's transaction."""
    ident = _call(connection, 'select authz.nexloop_machine_identity_snapshot(%s,%s)', (prefix, digest))
    binding = ident['binding']
    resource = _call(connection, 'select authz.nexloop_machine_write_action(%s,%s,%s)', (prefix, digest, capability))
    keys = {'subject': [binding['subject_id']], 'actor': [binding['subject_principal_id']], 'machine_authentication': [binding['credential_id']],
            'grants': [binding['subject_principal_id'], capability], 'scope': [binding['credential_id'], capability], 'policies': [capability]}
    facts = []
    for kind in FACT_KINDS:
        snap = _call(connection, 'select authz.nexloop_machine_authority_fact_snapshot(%s,%s,%s,%s)', (prefix, digest, kind, keys[kind]))
        facts.append({'kind': kind, 'key': keys[kind], 'record_hash': None if snap is None else snap['record_hash']})
    now = _call(connection, 'select clock_timestamp()', ())
    claims = {'protocol': PROTOCOL, 'key_id': signer.key_id, 'tenant_id': binding['tenant_id'], 'client_id': ident['client_id'],
              'credential_id': binding['credential_id'], 'principal_id': binding['subject_principal_id'], 'world': ident['world'],
              'intent_id': intent_id, 'prepare_digest': prepare_digest, 'capability': capability, 'resource_id': resource, 'operation': 'execute',
              'expires_at': (now + SIGNED_COMMAND_TTL).isoformat(), 'facts': facts}
    text = canonical_payload(claims)
    return text, hmac.new(signer.material, (PROTOCOL + ':' + text).encode(), 'sha256').hexdigest()


def commit(pool, signer, *, prefix, digest, capability, intent_id, params, prepare_digest):
    with pool.connection() as c, c.transaction():
        verify_application_role(c)
        text, signature = signed_command(c, signer, prefix=prefix, digest=digest, capability=capability, intent_id=intent_id, prepare_digest=prepare_digest)
        return _call(c, 'select authz.nexloop_machine_commit(%s,%s,%s,%s,%s::jsonb,%s,%s,%s)',
                     (prefix, digest, capability, intent_id, canonical_payload(params), prepare_digest, text, signature))


assert SIGNED_COMMAND_TTL <= timedelta(seconds=25)  # the SQL bound is 30 s (M5a); 20 s leaves room for clock and call time
