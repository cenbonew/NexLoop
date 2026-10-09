"""Governed READ on an accepted Message, derived instead of configured per Message.

A service principal may READ an accepted Message's fields listed in its explicit
``message_read_rule`` (always actor and body; optionally accepted_at, conversation_id,
sequence; listing conversation_id also covers the Message's own Conversation) only
while it currently holds generic READ on the Conversation's Consumer. SQL decides which path applies, rebuilds
every condition under share locks at each use, and lets configured Message
authority take precedence. This module only signs the typed proof shape; it never
grants anything or reads business rows itself.
"""
import hmac
from datetime import UTC, datetime, timedelta
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload

DERIVATION = 'accepted-message-v1'
FIELDS = ('actor', 'body')
RULE_FIELDS = ('accepted_at', 'actor', 'body', 'conversation_id', 'sequence')
CONVERSATION_FIELDS = ('consumer_id', 'owner_principal')
_MESSAGE_TARGET = re.compile(r'eios:(object|property):Message/([a-f0-9]{64})(?:/(accepted_at|actor|body|conversation_id|sequence))?')
_CONVERSATION_TARGET = re.compile(r'eios:(object|property):Conversation/([a-f0-9]{64})(?:/(consumer_id|owner_principal))?')


class MessageReadRule(BaseModel):
    """Trusted-configuration ``message_read_rule`` payload (key: [principal_id])."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    tenant_id: str
    principal_id: str
    type_name: Literal['Message']
    fields: tuple[Literal['accepted_at', 'actor', 'body', 'conversation_id', 'sequence'], ...]
    active: bool
    valid_until: AwareDatetime

    @field_validator('fields')
    @classmethod
    def _explicit_fields(cls, value):
        if list(value) != sorted(set(value)) or not {'actor', 'body'} <= set(value):
            raise ValueError('fields must be a sorted unique set containing actor and body')
        return value


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


def conversation_read_basis(pool, session, conversation_id):
    with pool.connection() as db, db.transaction():
        verify_application_role(db)
        return db.execute('select authz.nexloop_conversation_read_basis(%s,%s,%s)',
                          (session.token_digest, session.world, conversation_id)).fetchone()[0]


class DerivedEvidenceReads:
    """Per-target derived claims for one reader; basis and Consumer READ are reused.

    Returns None whenever SQL selects the configured path (configured grants win,
    no active rule, Run credential, non-real world, field not listed in the rule).
    """

    def __init__(self, reader):
        self.reader = reader
        self._basis = {}
        self._consumer = {}

    def claim(self, target):
        message = _MESSAGE_TARGET.fullmatch(target)
        conversation = _CONVERSATION_TARGET.fullmatch(target)
        if message:
            key = ('Message', message.group(2))
            if key not in self._basis:
                self._basis[key] = message_read_basis(self.reader.pool, self.reader.session, message.group(2))
            field = message.group(3)
        elif conversation:
            key = ('Conversation', conversation.group(2))
            if key not in self._basis:
                self._basis[key] = conversation_read_basis(self.reader.pool, self.reader.session, conversation.group(2))
            field = None
        else:
            return None
        basis = self._basis[key]
        if basis.get('mode') != 'derived' or (message and field and field not in basis.get('fields', FIELDS)):
            return None
        return self._derived(target, basis)

    def _derived(self, target, basis):
        reader = self.reader; session = reader.session; signer = reader.signer
        consumer_id = basis['consumer_id']
        if consumer_id not in self._consumer:
            consumer = reader._configured(ResourceType.OBJECT, 'Consumer/' + consumer_id, Operation.READ)
            consumer.update(protocol='nexloop-object-read-v1', key_id=signer.key_id, type_name='Consumer',
                            object_id=consumer_id, fields=[], property_authorities=[])
            text = canonical_payload(consumer)
            self._consumer[consumer_id] = (consumer, {'text': text, 'signature': _sign(signer, text)})
        consumer, consumer_read = self._consumer[consumer_id]
        expires = min(datetime.fromisoformat(consumer['expires_at']), datetime.fromisoformat(basis['rule_valid_until']),
                      datetime.now(UTC) + timedelta(seconds=25)).isoformat()
        authentication = session.authentication
        return {'tenant_id': authentication.tenant_id, 'principal_id': authentication.subject_principal_id,
                'credential_id': authentication.credential_id, 'directory_hash': session.directory_hash,
                'world': session.world, 'resource_id': target, 'target_resource': target, 'operation': 'read',
                'expires_at': expires, 'facts': [], 'derivation': DERIVATION,
                'derivation_basis': {'rule_hash': basis['rule_hash'], 'consumer_id': consumer_id,
                                     'conversation_id': basis['conversation_id'], 'consumer_read': consumer_read}}
