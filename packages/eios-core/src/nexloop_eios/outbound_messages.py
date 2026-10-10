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


# ---------------------------------------------------------------- standalone entry
import argparse
import json
import logging
import math
from pathlib import Path
import re
import signal
import sys
import threading


class _Parser(argparse.ArgumentParser):
    def error(self,message):self.exit(2,'Outbound Recorder configuration unavailable\n')


def _arguments(argv):
    parser=_Parser(description='Restricted standalone outbound Message recorder; private configuration, no migrations')
    for name in ('database-url-file','signing-key-file','service-credential-file','artifact-root'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--signing-key-id',default='active')
    parser.add_argument('--world',required=True,choices=['real'])
    parser.add_argument('--batch',type=int,default=20)
    parser.add_argument('--tick-seconds',type=float,default=1.0)
    parser.add_argument('--once',action='store_true')
    arguments=parser.parse_args(argv)
    if (not 1<=arguments.batch<=100 or not math.isfinite(arguments.tick_seconds) or not 0<arguments.tick_seconds<=60
        or re.fullmatch('[A-Za-z0-9_-]{1,64}',arguments.signing_key_id) is None):parser.error('configuration')
    return arguments


def run(arguments,stop):
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.private_configuration import read_private_text
    dsn=read_private_text(arguments.database_url_file,maximum=16384)
    read_private_text(arguments.service_credential_file,maximum=16384)
    with open_backend(database_url=dsn,artifact_root=arguments.artifact_root,
        signing_key_file=arguments.signing_key_file,signing_key_id=arguments.signing_key_id) as backend:
        with backend._pool.connection() as connection:
            if verify_application_role(connection)!='nexloop_api':raise ValueError()
        def recorder():
            # Re-read the credential and re-authenticate every tick: revocation applies at once.
            services=backend.authenticate(read_private_text(arguments.service_credential_file,maximum=16384),world=arguments.world)
            return OutboundMessageRecorder(services._backend._pool,services._session,services._backend._signer)
        recorder()
        print('Outbound Recorder ready',flush=True)
        while not stop.is_set():
            try:
                recorded=recorder().run_once(limit=arguments.batch)
                if arguments.once:
                    print(json.dumps({'recorded':sum(1 for item in recorded if item.get('created')),'replayed':sum(1 for item in recorded if not item.get('created'))},separators=(',',':')),flush=True)
                    return 0
            except Exception:
                if arguments.once:
                    print('Outbound Recorder unavailable',file=sys.stderr,flush=True);return 1
            stop.wait(arguments.tick_seconds)
        return 0


def main(argv=None):
    # NX-030 / AT-049: every log record leaves this process as one allowlisted structured line (IDs, codes, durations only).
    from nexloop_eios.structured_log import configure as _structured_logging
    _structured_logging('outbound-recorder')
    arguments=_arguments(argv);stop=threading.Event();previous={}
    logger=logging.getLogger('psycopg.pool');disabled=logger.disabled;logger.disabled=True
    def terminate(signum,frame):stop.set()
    try:
        for signum in (signal.SIGTERM,signal.SIGINT):previous[signum]=signal.signal(signum,terminate)
        return run(arguments,stop)
    except Exception:
        print('Outbound Recorder unavailable',file=sys.stderr,flush=True);return 1
    finally:
        logger.disabled=disabled
        for signum,handler in previous.items():signal.signal(signum,handler)


if __name__=='__main__':raise SystemExit(main())
