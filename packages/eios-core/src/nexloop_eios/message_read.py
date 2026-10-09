"""Governed READ on an accepted Message, derived instead of configured per Message.

A service principal may READ an accepted Message's actor/body only when trusted
configuration published its explicit ``message_read_rule`` and it currently holds
generic READ on the Message's Consumer. SQL decides which path applies, rebuilds
every condition under share locks at each use, and lets configured Message
authority take precedence. This module only signs the typed proof shape; it never
grants anything or reads business rows itself.
"""
import hmac
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from eios.authz.resources import ResourceType
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload

DERIVATION = 'accepted-message-v1'
FIELDS = ('actor', 'body')


class MessageReadRule(BaseModel):
    """Trusted-configuration ``message_read_rule`` payload (key: [principal_id])."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    tenant_id: str
    principal_id: str
    type_name: Literal['Message']
    fields: tuple[Literal['actor'], Literal['body']]
    active: bool
    valid_until: AwareDatetime


def message_read_basis(pool, session, message_id):
    with pool.connection() as db, db.transaction():
        verify_application_role(db)
        return db.execute('select authz.nexloop_message_read_basis(%s,%s,%s)',
                          (session.token_digest, session.world, message_id)).fetchone()[0]


def derived_message_read_envelope(pool, session, signer, message_id, basis, fields=FIELDS):
    fields = tuple(sorted(set(fields)))
    if not fields or not set(fields) <= set(FIELDS):
        raise ValueError('derived message fields unavailable')
    from nexloop_eios.object_reads import AuthorizedObjectReader
    reader = AuthorizedObjectReader(pool, session, signer)
    consumer = reader._authority(ResourceType.OBJECT, 'Consumer/' + basis['consumer_id'])
    consumer.update(protocol='nexloop-object-read-v1', key_id=signer.key_id, type_name='Consumer',
                    object_id=basis['consumer_id'], fields=[], property_authorities=[])
    consumer_text = canonical_payload(consumer)
    consumer_read = {'text': consumer_text, 'signature': _sign(signer, consumer_text)}
    expires = min(datetime.fromisoformat(consumer['expires_at']), datetime.fromisoformat(basis['rule_valid_until']),
                  datetime.now(UTC) + timedelta(seconds=25)).isoformat()
    authentication = session.authentication
    derivation_basis = {'rule_hash': basis['rule_hash'], 'consumer_id': basis['consumer_id'],
                        'conversation_id': basis['conversation_id'], 'consumer_read': consumer_read}

    def claim(target):
        return {'tenant_id': authentication.tenant_id, 'principal_id': authentication.subject_principal_id,
                'credential_id': authentication.credential_id, 'directory_hash': session.directory_hash,
                'world': session.world, 'resource_id': target, 'target_resource': target, 'operation': 'read',
                'expires_at': expires, 'facts': [], 'derivation': DERIVATION, 'derivation_basis': derivation_basis}

    claims = claim('eios:object:Message/' + message_id)
    claims.update(protocol='nexloop-object-read-v1', key_id=signer.key_id, type_name='Message', object_id=message_id,
                  fields=list(fields),
                  property_authorities=[claim('eios:property:Message/' + message_id + '/' + field) for field in fields])
    text = canonical_payload(claims)
    return {'text': text, 'signature': _sign(signer, text)}


def _sign(signer, text):
    return hmac.new(signer.material, ('nexloop-object-read-v1:' + text).encode(), 'sha256').hexdigest()
