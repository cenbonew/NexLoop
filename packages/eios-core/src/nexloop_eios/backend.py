"""Trusted backend composition; runtime callers never receive database/key handles.

This is the embedded service boundary, not an HTTP server or RuntimeAdapter.
It performs no schema migration, authority provisioning or Memory fallback.
"""
from contextlib import contextmanager
import os
import stat
import hmac
import secrets
from threading import RLock

from nexloop_eios.assembly import open_core, verify_application_role
from eios.adapters.postgres.database import StorageUnavailable
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.local_artifacts import LocalBlobStore
from nexloop_eios.relation_actions import GovernedRelationLinker
from nexloop_eios.artifact_orphans import FinalOrphanCollector
from nexloop_eios.object_edits import GovernedObjectEditor
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.postgres_artifacts import (
    AuthoritySigner, LocalArtifactService, PostgresArtifactRepository,
)


class BackendClosed(RuntimeError):
    pass


def verify_backend_signer(pool, signer):
    challenge = secrets.token_hex(32)
    signature = hmac.new(signer.material,
        ('nexloop-backend-signer-v1:'+challenge).encode(), 'sha256').hexdigest()
    with pool.connection() as connection, connection.transaction():
        connection.execute('set transaction read only')
        verify_application_role(connection)
        valid = connection.execute('select authz.nexloop_verify_backend_signer(%s,%s,%s)',
            (signer.key_id, challenge, signature)).fetchone()[0]
    if valid is not True:
        raise StorageUnavailable('backend authority signing key unavailable')


class AuthenticatedServices:
    """Server-derived identity; all operations still recheck live EIOS authority."""
    def __init__(self, backend, session):
        self._backend, self._session = backend, session

    def accept_runtime_event(self, *, queue, source_id, event_id, run_token, command, input, max_attempts=3):
        return self._backend._invoke(self._session, 'accept_runtime_event', queue=queue,
            source_id=source_id, event_id=event_id, run_token=run_token, command=command, input=input, max_attempts=max_attempts)

    def register_runtime_run(self, *, queue, task_id, run_token, command, input):
        return self._backend._invoke(self._session, 'register_runtime_run', queue=queue,
            task_id=task_id, run_token=run_token, command=command, input=input)

    def create_runtime_activation(self, *, queue, task_id, fence, run_id, command, input, owner_epoch):
        return self._backend._invoke(self._session, 'create_runtime_activation', queue=queue,
            task_id=task_id, fence=fence, run_id=run_id, command=command, input=input, owner_epoch=owner_epoch)

    def runtime_effect_tool(self, *, activation_ref, command, tool_operation, parameters=None, intent_id=None, request_scope=None):
        return self._backend._invoke(self._session, 'runtime_effect_tool', activation_ref=activation_ref,
            command=command, tool_operation=tool_operation, parameters=parameters, intent_id=intent_id, request_scope=request_scope)

    def authorize_runtime_activation(self, *, activation_ref, command, operation, input=None):
        return self._backend._invoke(self._session, 'authorize_runtime_activation',
            activation_ref=activation_ref, command=command, runtime_operation=operation, input=input)

    def assert_task_lease(self, *, queue, task_id, fence):
        return self._backend._invoke(self._session, 'assert_task_lease', queue=queue, task_id=task_id, fence=fence)

    def inspect_task(self, *, queue, task_id):
        return self._backend._invoke(self._session, 'inspect_task', queue=queue, task_id=task_id)

    def accept_event(self, *, queue, source_id, event_id, payload, max_attempts=3):
        return self._backend._invoke(self._session, 'accept_event', queue=queue,
            source_id=source_id, event_id=event_id, payload=payload, max_attempts=max_attempts)

    def claim_task(self, *, queue, lease_seconds=30):
        return self._backend._invoke(self._session, 'claim_task', queue=queue, lease_seconds=lease_seconds)

    def finish_task(self, *, queue, task_id, fence, status, result=None, retry_seconds=0):
        return self._backend._invoke(self._session, 'finish_task', queue=queue,
            task_id=task_id, fence=fence, status=status, result=result, retry_seconds=retry_seconds)

    def renew_task(self, *, queue, task_id, fence, lease_seconds=30):
        return self._backend._invoke(self._session, 'renew_task', queue=queue,
            task_id=task_id, fence=fence, lease_seconds=lease_seconds)

    def claim_outbox(self, *, queue, lease_seconds=30):
        return self._backend._invoke(self._session, 'claim_outbox', queue=queue, lease_seconds=lease_seconds)

    def acknowledge_outbox(self, *, queue, outbox_id, fence):
        return self._backend._invoke(self._session, 'acknowledge_outbox', queue=queue,
            outbox_id=outbox_id, fence=fence)

    def issue_run_credential(self, *, action_resources, ttl_seconds=300):
        return self._backend._invoke(self._session,'issue_run',action_resources=action_resources,ttl_seconds=ttl_seconds)

    def claim_effect(self, *, lease_seconds=30):
        return self._backend._invoke(self._session, 'claim_effect', lease_seconds=lease_seconds)

    def prepare_effect_dispatch(self, *, intent_id, fence, provider_profile_digest):
        return self._backend._invoke(self._session, 'prepare_effect_dispatch', intent_id=intent_id, fence=fence, provider_profile_digest=provider_profile_digest)

    def authorize_effect_query(self, *, intent_id, fence, provider_profile_digest):
        return self._backend._invoke(self._session, 'authorize_effect_query', intent_id=intent_id, fence=fence, provider_profile_digest=provider_profile_digest)

    def record_effect_unknown(self, *, intent_id, fence):
        return self._backend._invoke(self._session, 'record_effect_unknown', intent_id=intent_id, fence=fence)

    def record_effect_observation(self, *, intent_id, fence, provider_profile_digest, provider_payload_digest, provider_state, provider_reference):
        return self._backend._invoke(self._session, 'record_effect_observation', intent_id=intent_id, fence=fence, provider_profile_digest=provider_profile_digest, provider_payload_digest=provider_payload_digest, provider_state=provider_state, provider_reference=provider_reference)

    def record_effect_query_observation(self, **arguments):
        return self._backend._invoke(self._session, 'record_effect_query_observation', **arguments)

    def reconcile_effect_receipt(self, **arguments):
        return self._backend._invoke(self._session, 'reconcile_effect_receipt', **arguments)

    def read_effect_receipt(self, *, intent_id):
        return self._backend._invoke(self._session, 'read_effect_receipt', intent_id=intent_id)

    def configure_effect_control(self, *, control_id, control_revision, executor_token):
        return self._backend._invoke(self._session, 'configure_effect_control',
            control_id=control_id, control_revision=control_revision, executor_token=executor_token)

    def bind_effect_context(self, *, step_id, step_revision, goal_revision, consumer_revision,
                            control_revision, run_id, run_token, executor_token):
        return self._backend._invoke(self._session, 'bind_effect_context', step_id=step_id,
            step_revision=step_revision, goal_revision=goal_revision, consumer_revision=consumer_revision,
            control_revision=control_revision, run_id=run_id, run_token=run_token, executor_token=executor_token)

    def submit_effect_intent(self, *, parameters, action_version=1,request_scope=None):
        return self._backend._invoke(self._session, 'submit_effect_intent',
            parameters=parameters, action_version=action_version,request_scope=request_scope)

    def find_effect_receipt(self, *, intent_id, action_version=1):
        return self._backend._invoke(self._session, 'find_effect_receipt',
            intent_id=intent_id, action_version=action_version)

    def create_object(self, *, action_name, action_version, intent_id, type_name, properties):
        return self._backend._invoke(self._session, 'create',
            action_name=action_name, action_version=action_version,
            intent_id=intent_id, type_name=type_name, properties=properties)

    def link_relation(self, *, action_name, action_version, intent_id, relation_name, source_id, target_id, metadata):
        return self._backend._invoke(self._session, 'link', action_name=action_name,
            action_version=action_version, intent_id=intent_id, relation_name=relation_name,
            source_id=source_id, target_id=target_id, metadata=metadata)

    def edit_object(self, *, action_name, action_version, intent_id, type_name, object_id, expected_revision, properties):
        return self._backend._invoke(self._session, 'edit', action_name=action_name,
            action_version=action_version, intent_id=intent_id, type_name=type_name,
            object_id=object_id, expected_revision=expected_revision, properties=properties)

    def read_object(self, *, type_name, object_id, fields=()):
        return self._backend._invoke(self._session, 'read_object',
            type_name=type_name, object_id=object_id, fields=fields)

    def create_relationship_assessment(self, *, action_name, action_version, intent_id, properties):
        return self._backend._invoke(self._session, 'create_relationship_assessment',
            action_name=action_name, action_version=action_version, intent_id=intent_id, properties=properties)

    def correct_relationship_assessment(self, *, action_name, action_version, intent_id, object_id, expected_revision, properties):
        return self._backend._invoke(self._session, 'correct_relationship_assessment',
            action_name=action_name, action_version=action_version, intent_id=intent_id,
            object_id=object_id, expected_revision=expected_revision, properties=properties)

    def read_relationship_assessment(self, *, object_id, valid_at=None):
        return self._backend._invoke(self._session, 'read_relationship_assessment', object_id=object_id, valid_at=valid_at)

    def read_relationship_assessment_history(self, *, object_id, known_at, valid_at, evidence_message_id=None):
        return self._backend._invoke(self._session, 'read_relationship_assessment_history',
            object_id=object_id, known_at=known_at, valid_at=valid_at, evidence_message_id=evidence_message_id)

    def prepare_message_context(self, *, message_id, run_token, command,offering_id,binding_id):
        return self._backend._invoke(self._session, 'prepare_message_context', message_id=message_id,run_token=run_token,command=command,offering_id=offering_id,binding_id=binding_id)

    def put_artifact(self, *, request_id, payload, media_type, retention_until):
        return self._backend._invoke(self._session, 'put_artifact',
            request_id=request_id, payload=payload, media_type=media_type,
            retention_until=retention_until)

    def read_artifact(self, artifact_id, *, start=0, stop=None):
        return self._backend._invoke(self._session, 'read_artifact',
            artifact_id=artifact_id, start=start, stop=stop)

    def delete_expired_artifact(self, artifact_id):
        return self._backend._invoke(self._session, 'delete_artifact', artifact_id=artifact_id)

    def collect_final_artifact_orphans(self, *, older_than, limit=25, after=''):
        return self._backend._invoke(self._session,'collect_orphans',older_than=older_than,limit=limit,after=after)

    def resume_final_artifact_orphan_sweep(self, sweep_id):
        return self._backend._invoke(self._session,'resume_orphans',sweep_id=sweep_id)

    def collect_temporary_artifact_files(self, *, older_than, limit=50, after=''):
        return self._backend._invoke(self._session, 'collect_temporary', older_than=older_than,limit=limit,after=after)

    def collect_expired_artifacts(self, *, limit=50, after=''):
        return self._backend._invoke(self._session, 'collect_artifacts', limit=limit, after=after)


class BrowserServices:
    """Actual Human session only; service/Run ports are deliberately separate."""
    def __init__(self, backend, inspected_session):
        self._backend, self._inspected_session = backend, inspected_session

    def link_authenticated_identity(self):
        return self._backend._invoke_browser(
            self._inspected_session, 'link_authenticated_identity')

    def create_conversation(self, *, idempotency_key):
        return self._backend._invoke_browser(self._inspected_session, 'create_conversation',
            idempotency_key=idempotency_key)

    def list_conversations(self, *, after='', limit=50):
        return self._backend._invoke_browser(self._inspected_session, 'list_conversations',
            after=after, limit=limit)

    def accept_message(self, *, conversation_id, idempotency_key, body):
        return self._backend._invoke_browser(self._inspected_session, 'accept_message',
            conversation_id=conversation_id, idempotency_key=idempotency_key, body=body)

    def accept_native_message(self, *, conversation_id, idempotency_key, provider_event_id, body):
        return self._backend._invoke_browser(self._inspected_session, 'accept_native_message',
            conversation_id=conversation_id, idempotency_key=idempotency_key, provider_event_id=provider_event_id, body=body)

    def read_messages(self, *, conversation_id, after_sequence=0, limit=50):
        return self._backend._invoke_browser(self._inspected_session, 'read_messages',
            conversation_id=conversation_id, after_sequence=after_sequence, limit=limit)

    def read_events(self, *, conversation_id, after_sequence=0, limit=50):
        return self._backend._invoke_browser(self._inspected_session, 'read_events',
            conversation_id=conversation_id, after_sequence=after_sequence, limit=limit)

    def read_message_run(self, *, message_id):
        return self._backend._invoke_browser(self._inspected_session, 'read_message_run', message_id=message_id)

    def read_message_scope_denial(self, *, message_id):
        return self._backend._invoke_browser(self._inspected_session, 'read_message_scope_denial', message_id=message_id)

    def read_message_service_receipt(self, *, message_id):
        return self._backend._invoke_browser(self._inspected_session, 'read_message_service_receipt', message_id=message_id)


class ReviewServices:
    """NX-046 workbench reads for an actual Human session; decisions are NX-044 and not exposed here."""
    def __init__(self, backend, inspected_session):
        self._backend, self._inspected_session = backend, inspected_session

    def review_queue(self, *, limit=50):
        return self._backend._invoke_review(self._inspected_session, 'pending', limit=limit)

    def review_candidate(self, *, candidate_id):
        return self._backend._invoke_review(self._inspected_session, 'candidate', candidate_id=candidate_id)


class Backend:
    def __init__(self, pool, store, signer):
        self._pool, self._store, self._signer = pool, store, signer
        self._lock = RLock()
        self._closed = False

    def _assert_open(self):
        if self._closed:
            raise BackendClosed('backend is closed')

    def foundation_readiness(self):
        """Fresh read-only dependency checks; no product or runtime readiness claim."""
        from nexloop_eios.bootstrap import verify
        with self._lock:
            self._assert_open()
            checks={'postgres':False,'signer':False,'artifact_directory':False}
            try:
                with self._pool.connection() as c, c.transaction():
                    c.execute('set transaction read only')
                    verify_application_role(c);verify(c)
                checks['postgres']=True
            except Exception:pass
            try:verify_backend_signer(self._pool,self._signer);checks['signer']=True
            except Exception:pass
            try:
                opened=os.fstat(self._store._root)
                current=os.stat(self._store.root,follow_symlinks=False)
                checks['artifact_directory']=(stat.S_ISDIR(current.st_mode) and current.st_uid==os.geteuid()
                    and not current.st_mode&0o077 and (opened.st_dev,opened.st_ino)==(current.st_dev,current.st_ino))
            except Exception:pass
            return {'checks':checks,'foundation_ready':all(checks.values()),'product_ready':False}

    def authenticate(self, token, *, world):
        with self._lock:
            self._assert_open()
            return AuthenticatedServices(self, authenticate_service(self._pool, token, world=world))

    def authenticate_run(self, token, *, world, run_id):
        from nexloop_eios.run_credentials import AUDIENCE
        with self._lock:
            self._assert_open()
            return AuthenticatedServices(self,authenticate_service(self._pool,token,world=world,run_id=run_id,audience=AUDIENCE))

    def authenticate_browser(self, inspected_session):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        with self._lock:
            self._assert_open()
            # Independent current PG authentication; never converts the Human
            # principal to a service credential or accepts caller-supplied scope.
            authenticate_browser_business(self._pool, inspected_session, world='real')
            return BrowserServices(self, inspected_session)

    def authenticate_browser_reviewer(self, inspected_session):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        with self._lock:
            self._assert_open()
            authenticate_browser_business(self._pool, inspected_session, world='real')
            return ReviewServices(self, inspected_session)

    def _invoke_review(self, inspected_session, operation, **arguments):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.candidate_merge import ReviewQueueReader
        with self._lock:
            self._assert_open()
            if operation not in ('pending', 'candidate'):
                raise ValueError('unsupported review operation')
            # Current PG authentication of the Human session on every read.
            session = authenticate_browser_business(self._pool, inspected_session, world='real')
            return getattr(ReviewQueueReader(self._pool, session, self._signer), operation)(**arguments)

    def _invoke_browser(self, inspected_session, operation, **arguments):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        from nexloop_eios.conversation_messages import ConversationMessagePort
        allowed = {'link_authenticated_identity', 'create_conversation', 'list_conversations', 'accept_message', 'accept_native_message',
            'read_messages', 'read_events', 'read_message_run', 'read_message_service_receipt', 'read_message_scope_denial'}
        with self._lock:
            self._assert_open()
            if operation not in allowed:
                raise ValueError('unsupported browser operation')
            session = authenticate_browser_business(self._pool, inspected_session, world='real')
            if operation == 'link_authenticated_identity':
                from nexloop_eios.identity_link import BrowserConsumerIdentityLink
                return BrowserConsumerIdentityLink(self._pool, session, self._signer).link()
            if operation == 'read_message_run':
                from nexloop_eios.conversation_runtime_bridge import ConversationRunReader
                return ConversationRunReader(self._pool, session, self._signer).read_message_run(**arguments)
            if operation == 'read_message_scope_denial':
                from nexloop_eios.conversation_scope_denials import ConversationScopeDenialPort
                return ConversationScopeDenialPort(self._pool, session, self._signer).read_message_scope_denial(**arguments)
            if operation == 'read_message_service_receipt':
                from nexloop_eios.conversation_effect_receipts import ConversationEffectReceiptPort
                return ConversationEffectReceiptPort(self._pool, session, self._signer).read_message_service_receipt(**arguments)
            if operation == 'accept_native_message':
                from nexloop_eios.native_web_inbound import NativeWebMessagePort
                return NativeWebMessagePort(self._pool, session, self._signer).accept_native_message(**arguments)
            return getattr(ConversationMessagePort(self._pool, session, self._signer), operation)(**arguments)

    def _invoke(self, session, operation, **arguments):
        # Serialize lifecycle with requests so shutdown cannot close a file FD or
        # connection pool during a commit/fsync. This first host is synchronous.
        with self._lock:
            self._assert_open()
            activation_operations = {
                'accept_runtime_event': 'accept', 'register_runtime_run': 'register', 'create_runtime_activation': 'create',
                'authorize_runtime_activation': 'authorize', 'runtime_effect_tool': 'effect_tool',
            }
            if operation in activation_operations:
                from nexloop_eios.runtime_activation import RuntimeActivationPort
                if operation == 'authorize_runtime_activation':
                    arguments['operation'] = arguments.pop('runtime_operation')
                return getattr(RuntimeActivationPort(self._pool,session,self._signer),
                    activation_operations[operation])(**arguments)
            queue_operations = {
                'assert_task_lease': 'assert_lease', 'inspect_task': 'inspect', 'accept_event': 'accept', 'claim_task': 'claim',
                'finish_task': 'finish', 'renew_task': 'renew',
                'claim_outbox': 'claim_outbox', 'acknowledge_outbox': 'acknowledge_outbox',
            }
            if operation in queue_operations:
                from nexloop_eios.durable_queue import PostgresDurableQueue
                queue = PostgresDurableQueue(self._pool, session, self._signer,
                    queue=arguments.pop('queue'))
                return getattr(queue, queue_operations[operation])(**arguments)
            if operation in ('claim_effect','prepare_effect_dispatch','authorize_effect_query',
                              'record_effect_unknown','record_effect_observation','record_effect_query_observation','reconcile_effect_receipt','read_effect_receipt'):
                from nexloop_eios.effect_execution import EffectExecutionPort
                return getattr(EffectExecutionPort(self._pool,session,self._signer),operation)(**arguments)
            if operation in ('configure_effect_control','bind_effect_context'):
                from nexloop_eios.effect_contexts import EffectContextRegistrar,EffectContextUnavailable,EffectContextConflict
                from nexloop_eios.run_credentials import AUDIENCE
                try:
                    executor=authenticate_service(self._pool,arguments.pop('executor_token'),world=session.world)
                    registrar=EffectContextRegistrar(self._pool,session,self._signer)
                    if operation=='configure_effect_control':
                        return registrar.configure_control(executor_session=executor,**arguments)
                    run=authenticate_service(self._pool,arguments.pop('run_token'),world=session.world,
                        run_id=arguments.pop('run_id'),audience=AUDIENCE)
                    return registrar.bind(run_session=run,executor_session=executor,**arguments)
                except EffectContextConflict:
                    raise
                except Exception:
                    raise EffectContextUnavailable('effect context unavailable') from None
            if operation in ('submit_effect_intent','find_effect_receipt'):
                from nexloop_eios.effect_intents import EffectIntentPort
                intents=EffectIntentPort(self._pool,session,self._signer)
                return intents.submit(**arguments) if operation=='submit_effect_intent' else intents.find(**arguments)
            if operation == 'issue_run':
                from nexloop_eios.run_credentials import issue_run_credential
                return issue_run_credential(self._pool,session,self._signer,**arguments)
            if operation == 'create':
                return GovernedObjectCreator(self._pool, session, self._signer).create(**arguments)
            if operation == 'link':
                return GovernedRelationLinker(self._pool, session, self._signer).link(**arguments)
            if operation == 'edit':
                return GovernedObjectEditor(self._pool, session, self._signer).edit(**arguments)
            if operation == 'read_object':
                return AuthorizedObjectReader(self._pool, session, self._signer).get(**arguments)
            if operation in ('create_relationship_assessment','correct_relationship_assessment','read_relationship_assessment'):
                from nexloop_eios.assessment_actions import GovernedAssessmentCreator,GovernedAssessmentCorrector,AuthorizedAssessmentProjection
                if operation=='create_relationship_assessment':
                    return GovernedAssessmentCreator(self._pool,session,self._signer).create(**arguments)
                if operation=='correct_relationship_assessment':
                    return GovernedAssessmentCorrector(self._pool,session,self._signer).correct(**arguments)
                return AuthorizedAssessmentProjection(self._pool,session,self._signer).current(**arguments)
            if operation=='read_relationship_assessment_history':
                from nexloop_eios.assessment_history import AuthorizedAssessmentHistory
                return AuthorizedAssessmentHistory(self._pool,session,self._signer).as_of(**arguments)
            if operation in ('collect_orphans','resume_orphans'):
                collector=FinalOrphanCollector(self._pool,session,self._signer,self._store)
                return collector.collect(**arguments) if operation=='collect_orphans' else collector.resume(**arguments)
            artifacts = LocalArtifactService(
                PostgresArtifactRepository(self._pool, session, self._signer), self._store)
            if operation == 'prepare_message_context':
                from nexloop_eios.context_artifacts import ContextArtifactProducer
                return ContextArtifactProducer(AuthenticatedServices(self,session)).prepare(**arguments)
            if operation == 'put_artifact':
                return artifacts.put(**arguments)
            if operation == 'read_artifact':
                return artifacts.read(**arguments)
            if operation == 'delete_artifact':
                return artifacts.delete_expired(**arguments)
            if operation == 'collect_temporary':
                return artifacts.collect_temporary_files(**arguments)
            if operation == 'collect_artifacts':
                return artifacts.collect_expired(**arguments)
            raise ValueError('unsupported backend operation')

    def _shutdown(self):
        with self._lock:
            self._closed = True
            self._store.close()


@contextmanager
def open_backend(*, database_url, artifact_root, signing_key_file, signing_key_id='active'):
    """Open mandatory PG/Artifact services; close all owned resources on exit.

    The owner must provision the same signing key in PostgreSQL beforehand.
    A fresh read-only HMAC challenge checks the active database key before opening
    Artifact storage. SQL rechecks the key at each dispatch. Startup verification
    is not a product-readiness claim. Key files are never generated here.
    """
    signer = AuthoritySigner.from_file(signing_key_file, key_id=signing_key_id)
    # Verify the restricted role and exact catalog before creating directories.
    with open_core(database_url) as pool:
        verify_backend_signer(pool, signer)
        with LocalBlobStore(artifact_root) as store:
            backend = Backend(pool, store, signer)
            try:
                yield backend
            finally:
                backend._shutdown()
