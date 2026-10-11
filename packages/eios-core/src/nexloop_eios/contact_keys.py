"""NX-053 (0240): an erased contact key refuses contact (NXK01 'contact unavailable', never which key).

The refused attempt rolls back; the refusal is then recorded in its own transaction by
authz.nexloop_record_contact_key_refusal, which re-derives the identity references from the authenticated context."""
import psycopg

CODE = 'NXK01'


class ContactKeyRefused(PermissionError):
    """Contact refused: the identity was erased and its contact key is not revoked."""


def refused(error):
    """Whether an exception (or one it was raised from) is the NXK01 refusal."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, psycopg.Error) and getattr(error, 'sqlstate', None) == CODE:
            return True
        error = error.__cause__ or error.__context__
    return False


def record(pool, session, path, principal_id=None):
    """Best effort: never turns the refusal into anything else."""
    try:
        with pool.connection() as db, db.transaction():
            db.execute('select authz.nexloop_record_contact_key_refusal(%s,%s,%s,%s)', (session.token_digest, session.world, path, principal_id))
    except Exception:
        pass
