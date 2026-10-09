"""NX-047 outbound recorder: governed Message.agent_create:1 for channel-accepted replies.

The Agent's Action-approved reply is recorded with its intent (0078) and its delivery
state advances only from the effect ledger. This service materializes the reply as a
Message object once the channel accepted/delivered it, appending it to the same
conversation stream. It never writes the relay's inbound outbox, never chooses the
text, sender, conversation or delivery state, and holds no channel/provider secret.
"""
import psycopg
from eios.identity.models import SubjectKind
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.conversation_messages import ConversationDenied,ConversationUnavailable,_ConversationPort

ACTION='Message.agent_create'
PROTOCOL='nexloop-outbound-message-v1'


class OutboundMessageRecorder(_ConversationPort):
    def __init__(self,pool,session,signer):
        super().__init__(pool,session,signer)
        if session.authentication.subject_kind is not SubjectKind.SERVICE:raise ConversationDenied()

    def _command(self,db,verb,**arguments):
        definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(ACTION,1)
        if not (set(definition.required_scopes)|set(capability.required_scopes))<=self.session.authentication.requested_scopes:raise ConversationUnavailable()
        if definition.governance.policy_refs or definition.preconditions or definition.parameters or definition.governance.approval_mode.value!='none':raise ConversationUnavailable()
        envelope,_=self._signed(PROTOCOL,ACTION,{'verb':verb,**arguments},
            definition=definition.model_dump(mode='json'),capability=capability.model_dump(mode='json'))
        return db.execute('select authz.nexloop_outbound_message_command(%s,%s,%s,%s,%s)',
            (self.session.token_digest,self.session.world,envelope['text'],envelope['signature'],envelope['payload'])).fetchone()[0]

    def pending(self,*,limit=20):
        try:
            with self.pool.connection() as db,db.transaction():return self._command(db,'pending',limit=limit)
        except Exception as error:self._raise(error)

    def record(self,*,intent_id):
        """Materialize one reply; replay returns the existing Message unchanged."""
        try:
            with self.pool.connection() as db,db.transaction():
                prepared=self._command(db,'prepare',intent_id=intent_id)
                if prepared.get('replay'):return prepared['result']
                return self._command(db,'commit',intent_id=intent_id,create=self._create(db,ACTION,prepared['payload']))
        except Exception as error:self._raise(error)

    def run_once(self,*,limit=20):
        return [self.record(intent_id=intent_id) for intent_id in self.pending(limit=limit)]
