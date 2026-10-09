"""Durable opaque Run activation through current EIOS queue and Run authority.

API possession of an actual Run credential enrolls its immutable task binding.
Workers can only activate/recover enrolled Runs for their own current lease.
No raw credential, DSN or prompt is persisted or returned to Host.
"""
from datetime import UTC,datetime,timedelta
import hashlib,hmac,re,time,uuid
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.service_offerings import CatalogScopeDenied
from nexloop_eios.authorization import PostgresAuthorityProvider,_identity,authenticate_service,resolve_authority
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.run_credentials import AUDIENCE

_OPERATIONS={'start','resume','inspect','cancel','model','tool'}

def _input_digest(value):
    if not isinstance(value,str) or len(value.encode())>131072:raise ValueError('bounded input required')
    return hashlib.sha256(value.encode()).hexdigest()

def _command(value):
    # Match the live RunCommand shape and bounds before signing; all identity
    # still comes from the stored Run/task binding, never these caller fields.
    keys={'schema_version','run_id','tenant_id','world_id','mode','request_id','trigger_event_id','role_ref','consumer_ref','goal_version_ref','context_manifest_ref','runtime_profile','credential_ref','budget','not_after','runtime_owner_epoch'}
    if type(value) is not dict or set(value)!=keys or value['schema_version']!='1.0':raise ValueError('Run command unavailable')
    for key in ('run_id','tenant_id','trigger_event_id'):
        if not isinstance(value[key],str) or re.fullmatch(r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}',value[key]) is None:raise ValueError('Run ID unavailable')
        uuid.UUID(value[key])
    if value['mode'] not in ('real','simulation','shadow','test') or not isinstance(value['world_id'],str) or not value['world_id'] or (value['mode']=='real')!=(value['world_id']=='real'):raise ValueError('world unavailable')
    if not isinstance(value['request_id'],str) or not 16<=len(value['request_id'])<=200:raise ValueError('request unavailable')
    for key in ('role_ref','consumer_ref','goal_version_ref','context_manifest_ref','credential_ref'):
        if not isinstance(value[key],str) or not 1<=len(value[key])<=512 or re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*:\S+',value[key]) is None:raise ValueError('reference unavailable')
    if not isinstance(value['runtime_profile'],str) or not value['runtime_profile']:raise ValueError('profile unavailable')
    if type(value['runtime_owner_epoch']) is not int or not 1<=value['runtime_owner_epoch']<=9007199254740991:raise ValueError('owner unavailable')
    budget=value['budget']
    if type(budget) is not dict or set(budget)!={'maximum_model_turns','maximum_tool_calls','active_timeout_seconds','maximum_cost','currency'}:raise ValueError('budget unavailable')
    for key,maximum in [('maximum_model_turns',64),('maximum_tool_calls',128),('active_timeout_seconds',3600)]:
        if type(budget[key]) is not int or not 1<=budget[key]<=maximum:raise ValueError('budget unavailable')
    if not isinstance(budget['maximum_cost'],str) or re.fullmatch(r'\d+(?:\.\d{1,8})?',budget['maximum_cost'],flags=re.ASCII) is None:raise ValueError('cost unavailable')
    if not isinstance(budget['currency'],str) or re.fullmatch('[A-Z]{3}',budget['currency']) is None:raise ValueError('currency unavailable')
    if not isinstance(value['not_after'],str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})',value['not_after'],flags=re.ASCII) is None:raise ValueError('expiry unavailable')
    stamp=value['not_after']
    if stamp[-1]!='Z' and (int(stamp[-5:-3])>23 or int(stamp[-2:])>59):raise ValueError('expiry unavailable')
    expiry=datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if expiry.tzinfo is None or expiry<=datetime.now(UTC):raise ValueError('expiry unavailable')
    text=canonical_payload(value)
    if len(text.encode())>65536:raise ValueError('bounded command required')
    return text,hashlib.sha256(text.encode()).hexdigest()

_SNAPSHOT_KEYS={'call_sequence','model_provider','model_id','request_text','request_digest','tool_manifest_digest','settings_digest'}

def _request_snapshot(operation,value):
    # Host-computed; SQL recomputes every digest from the request text before recording.
    if operation!='model' or type(value) is not dict or set(value)!=_SNAPSHOT_KEYS:raise ValueError('model request snapshot unavailable')
    if type(value['call_sequence']) is not int or not 1<=value['call_sequence']<=99999:raise ValueError('model request snapshot unavailable')
    if any(type(value[k]) is not str or not 1<=len(value[k])<=128 for k in ('model_provider','model_id')):raise ValueError('model request snapshot unavailable')
    if type(value['request_text']) is not str or len(value['request_text'].encode())>196608:raise ValueError('model request snapshot unavailable')
    if any(type(value[k]) is not str or re.fullmatch('[a-f0-9]{64}',value[k]) is None for k in ('request_digest','tool_manifest_digest','settings_digest')):raise ValueError('model request snapshot unavailable')
    if hashlib.sha256(value['request_text'].encode()).hexdigest()!=value['request_digest']:raise ValueError('model request snapshot unavailable')

_RESULT_KEYS={'call_sequence','result_status','usage','cost','response_digest'}

def _model_result(operation,value):
    # Host-reported outcome of an already recorded request; SQL validates and writes it once.
    if operation!='model' or type(value) is not dict or set(value)!=_RESULT_KEYS:raise ValueError('model result unavailable')
    if type(value['call_sequence']) is not int or not 1<=value['call_sequence']<=99999 or value['result_status'] not in ('succeeded','failed','unknown'):raise ValueError('model result unavailable')
    if value['usage'] is not None and (type(value['usage']) is not dict or set(value['usage'])!={'input','output','cache_read','cache_write','total'}
            or any(type(v) is not int or not 0<=v<10**12 for v in value['usage'].values())):raise ValueError('model result unavailable')
    if value['cost'] is not None and (type(value['cost']) is not str or re.fullmatch(r'[0-9]{1,10}(\.[0-9]{1,8})?',value['cost']) is None):raise ValueError('model result unavailable')
    if value['response_digest'] is not None and (type(value['response_digest']) is not str or re.fullmatch('[a-f0-9]{64}',value['response_digest']) is None):raise ValueError('model result unavailable')

def _execution_lock(db,run_id):
    """NX-049: the Run's execution lock (0039 key, reentrant) before any row lock of the statement.

    0039 takes it in the middle of an authorize/create statement, after the baseline checks
    have share-locked their rows. A same-Run authorize could then hold those rows and wait for
    the lock while an effect_tool, holding it since its first guard, needed one of the rows
    exclusively (40P01). Taking it first in both paths gives one order: [tool: Run lock] ->
    execution lock -> rows. The statement still re-runs every proof, lease and deadline check
    after the wait; the wait itself is bounded by lock_timeout.
    """
    db.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',('nexloop-runtime-execution:'+run_id,))


class RuntimeActivationPort:
    def __init__(self,pool,session,signer):
        if session.run_context is not None:raise AuthorizationUnavailable('runtime activation unavailable')
        self.pool,self.session,self.signer=pool,session,signer

    def _proof(self,session,target):
        entries=[];query=session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(resolve_authority(self.pool,session,query,entries))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise AuthorizationUnavailable('runtime activation unavailable')
        return {'tenant_id':session.authentication.tenant_id,'principal_id':session.authentication.subject_principal_id,'credential_id':session.authentication.credential_id,
            'directory_hash':session.directory_hash,'world':session.world,'resource_id':target,'action_resource':target,'operation':'execute',
            'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),'facts':sorted(entries,key=lambda r:(r['kind'],r['key']))}

    def _run_proofs(self,run):
        from nexloop_eios.action_definitions import PostgresActionDefinitionReader
        if run.run_context is None:raise AuthorizationUnavailable('runtime activation unavailable')
        reader=PostgresActionDefinitionReader(self.pool,run,self.signer);proofs=[]
        for target in sorted(run.run_context.allowed_resources):
            name,version=target.removeprefix('eios:action:').rsplit(':',1);reader.get(name,int(version))
            proofs.append(self._proof(run,target))
        return proofs

    def _signed(self,queue,verb,*,proof=None,protocol='nexloop-runtime-activation-v1',limit=1048576,context_artifact_proof=None,context_catalog_envelope=None,context_role_envelope=None,context_relationship_envelopes=None,context_formal_reads=None,**parameters):
        if not isinstance(queue,str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',queue) is None:raise ValueError('queue unavailable')
        if proof is None:proof=self._proof(self.session,f'eios:action:NexLoop.queue.{queue}:1')
        payload=canonical_payload({'queue':queue,'verb':verb,**parameters})
        if len(payload.encode())>limit:raise ValueError('bounded activation required')
        claims={'protocol':protocol,'key_id':self.signer.key_id,**proof,'parameters_digest':hashlib.sha256(payload.encode()).hexdigest()}
        if context_artifact_proof is not None:claims['context_artifact_proof']=context_artifact_proof
        if context_catalog_envelope is not None:claims['context_catalog_envelope']=context_catalog_envelope
        from nexloop_eios.role_runs import role_envelope_for_run
        if context_role_envelope is None and parameters.get('run_digest') is not None:
            context_role_envelope=role_envelope_for_run(self.pool,self.signer,self.session.world,parameters['run_digest'])
        if context_role_envelope is not None:
            claims['context_role_envelope']=context_role_envelope
            from nexloop_eios.role_runs import formal_reads_for_role
            claims['formal_reads']=formal_reads_for_role(self.pool,self.signer,self.session,context_role_envelope)
            from nexloop_eios.role_policies import policy_envelope_for_role
            claims['context_policy_envelope']=policy_envelope_for_role(self.pool,self.signer,self.session,context_role_envelope)
        if context_relationship_envelopes is not None:claims['context_relationship_envelopes']=context_relationship_envelopes
        if context_formal_reads is not None:claims['context_formal_reads']=context_formal_reads
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,(protocol+':'+text).encode(),'sha256').hexdigest()
        return text,signature,payload

    def _execute(self,db,envelope,*,queue_command=False):
        # Only this private backend method accepts an existing connection.
        statement=('select authz.nexloop_queue_command(%s,%s,%s,%s,%s)' if queue_command else
            'select authz.nexloop_runtime_activation_command(%s,%s,%s,%s,%s)')
        return db.execute(statement,(self.session.token_digest,self.session.world,*envelope)).fetchone()[0]

    def _call(self,queue,verb,execution_run=None,**parameters):
        envelope=self._signed(queue,verb,**parameters)
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            if execution_run is not None:_execution_lock(db,execution_run)
            return self._execute(db,envelope)

    def accept(self,*,queue,source_id,event_id,run_token,command,input,max_attempts=3):
        try:
            text,digest=_command(command);input_hash=_input_digest(input)
            if type(max_attempts) is not int or not 1<=max_attempts<=10:raise ValueError()
            if any(not isinstance(v,str) or not 1<=len(v)<=255 for v in (source_id,event_id)):raise ValueError()
            run=authenticate_service(self.pool,run_token,world=self.session.world,run_id=command['run_id'],audience=AUDIENCE)
            if run_token in text or run_token in input:raise ValueError('credential payload rejected')
            proofs=self._run_proofs(run)
            proof=self._proof(self.session,f'eios:action:NexLoop.queue.{queue}:1')
            accept=self._signed(queue,'accept',proof=proof,protocol='nexloop-queue-command-v1',limit=262144,
                source_id=source_id,event_id=event_id,payload={'run_command':command,'input':input},max_attempts=max_attempts)
            with self.pool.connection() as db,db.transaction():
                verify_application_role(db)
                accepted=self._execute(db,accept,queue_command=True)
                # Sign only after the actual definer-produced task ID exists.
                # Both existing definers recheck current EIOS authority after
                # lock waits; registration rechecks every Run proof at return.
                enrollment=self._signed(queue,'register',proof=proof,task_id=accepted['task_id'],run_digest=run.token_digest,
                    command_text=text,command_digest=digest,input_digest=input_hash,run_proofs=proofs)
                registered=self._execute(db,enrollment)
                result={**accepted,**registered}
            # The transaction context commits before any acceptance leaves us.
            return result
        except Exception as error:
            import psycopg
            from nexloop_eios.durable_queue import QueueConflict
            if isinstance(error,psycopg.Error) and error.diag.message_primary in {'queue_payload_conflict','activation enrollment conflict'}:
                raise QueueConflict('runtime_event_conflict') from None
            raise AuthorizationUnavailable('runtime activation unavailable') from None

    def register(self,*,queue,task_id,run_token,command,input):
        try:
            text,digest=_command(command);run=authenticate_service(self.pool,run_token,world=self.session.world,run_id=command['run_id'],audience=AUDIENCE)
            return self._call(queue,'register',task_id=task_id,run_digest=run.token_digest,command_text=text,command_digest=digest,
                input_digest=_input_digest(input),run_proofs=self._run_proofs(run))
        except Exception:raise AuthorizationUnavailable('runtime activation unavailable') from None

    def create(self,*,queue,task_id,fence,run_id,command,input,owner_epoch):
        try:
            text,digest=_command(command)
            if type(fence) is not int or fence<1 or type(owner_epoch) is not int or owner_epoch!=command['runtime_owner_epoch'] or run_id!=command['run_id']:raise ValueError()
            first=self._call(queue,'create',task_id=task_id,fence=fence,run_id=run_id,command_text=text,command_digest=digest,input_digest=_input_digest(input),owner_epoch=owner_epoch)
            # Full current EIOS Run chain is checked before the reference leaves
            # backend. Technical registration alone grants no formal Action.
            self.authorize(activation_ref=first['activation_ref'],command=command,operation='start',input=input)
            return first
        except Exception:raise AuthorizationUnavailable('runtime activation unavailable') from None

    def authorize(self,*,activation_ref,command,operation,input=None,request_snapshot=None,model_result=None):
        try:
            text,digest=_command(command)
            if operation not in _OPERATIONS or not isinstance(activation_ref,str) or re.fullmatch(r'activation_[a-f0-9-]{36}',activation_ref) is None:raise ValueError()
            if operation in ('start','resume') and input is None:raise ValueError()
            if request_snapshot is not None:_request_snapshot(operation,request_snapshot)
            if model_result is not None:
                if request_snapshot is not None:raise ValueError()
                _model_result(operation,model_result)
            input_hash=None if input is None else _input_digest(input)
            # This private hint exposes only a queue for the caller's owned,
            # active task. It neither authenticates a Run nor returns authority.
            with self.pool.connection() as db,db.transaction():
                verify_application_role(db)
                queue=db.execute('select authz.nexloop_runtime_activation_hint(%s,%s,%s)',(self.session.token_digest,self.session.world,activation_ref)).fetchone()[0]
            parameters=dict(activation_ref=activation_ref,command_text=text,command_digest=digest,input_digest=input_hash,operation=operation)
            first=self._call(queue,'resolve',**parameters)
            with self.pool.connection() as db,db.transaction():
                verify_application_role(db);run=_identity(db,first['_run_digest'],self.session.world)
            context_proof=self._context_read_proof(first)
            # A v6 model call is recorded in the same transaction as its authorization (0091).
            snapshot={} if request_snapshot is None else {'request_snapshot':request_snapshot}
            if model_result is not None:snapshot['model_result']=model_result
            result=self._call(queue,'authorize',execution_run=run.run_context.run_id,**parameters,**snapshot,run_proofs=self._run_proofs(run),context_artifact_proof=context_proof,context_catalog_envelope=self._context_catalog_envelope(first),context_role_envelope=__import__('nexloop_eios.role_runs',fromlist=['role_envelope_for_run']).role_envelope_for_run(self.pool,self.signer,self.session.world,first['_run_digest']),context_relationship_envelopes=self._context_relationship_envelopes(first),context_formal_reads=self._context_formal_envelopes(first))
            return {key:value for key,value in result.items() if not key.startswith('_')}
        except Exception:raise AuthorizationUnavailable('runtime activation unavailable') from None

    def _context_read_proof(self,resolved):
        if '_context_source_digest' not in resolved:return None
        from nexloop_eios.context_artifacts import artifact_authority_proof
        from eios.authz.operations import Operation
        with self.pool.connection() as connection,connection.transaction():
            verify_application_role(connection)
            source=_identity(connection,resolved['_context_source_digest'],self.session.world)
        return artifact_authority_proof(self.pool,source,Operation.READ)

    def _context_relationship_envelopes(self,resolved):
        if '_context_relationship' not in resolved:return None
        from nexloop_eios.role_runs import scoped_envelope
        return scoped_envelope(('relationship',id(self.pool),self.signer.key_id,self.session.world,resolved['_context_source_digest'],canonical_payload(resolved['_context_relationship'])),lambda:self._context_relationship_envelopes_now(resolved))

    def _context_relationship_envelopes_now(self,resolved):
        from nexloop_eios.relationship_context import RelationshipContextRecipe,RelationshipContextReader
        zone=resolved['_context_relationship'];rows=zone['current_statements']+zone['evidence']
        ids=tuple(sorted(row['assessment_ref'].removeprefix('eios:object:RelationshipAssessment/') for row in rows))
        with self.pool.connection() as connection,connection.transaction():
            verify_application_role(connection)
            source=_identity(connection,resolved['_context_source_digest'],self.session.world)
        return RelationshipContextReader(self.pool,source,self.signer,RelationshipContextRecipe(ids)).envelopes()

    def _context_formal_envelopes(self,resolved):
        if '_context_formal_facts' not in resolved:return None
        from nexloop_eios.role_runs import scoped_envelope
        return scoped_envelope(('context_formal',id(self.pool),self.signer.key_id,self.session.world,resolved['_context_source_digest'],canonical_payload(resolved['_context_formal_facts'])),lambda:self._context_formal_envelopes_now(resolved))

    def _context_formal_envelopes_now(self,resolved):
        from types import SimpleNamespace
        from nexloop_eios.service_offerings import _read_envelope
        # Actual bound Run's Source chain, not the requesting Artifact reader.
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db);source=_identity(db,resolved['_context_source_digest'],self.session.world)
        holder=SimpleNamespace(_session=source,_backend=SimpleNamespace(_pool=self.pool,_signer=self.signer))
        return {row['type']:_read_envelope(holder,row['type'],row['id'],('allow_effect','budget_units','executor_principal','valid_until') if row['type']=='EffectControl' else ()) for row in resolved['_context_formal_facts']}

    def _context_catalog_envelope(self,resolved):
        if '_context_catalog' not in resolved:return None
        from nexloop_eios.role_runs import scoped_envelope
        return scoped_envelope(('catalog',id(self.pool),self.signer.key_id,self.session.world,resolved['_context_source_digest'],resolved['_context_consumer_id'],canonical_payload(resolved['_context_catalog'])),lambda:self._context_catalog_envelope_now(resolved))

    def _context_catalog_envelope_now(self,resolved):
        from nexloop_eios.service_offerings import catalog_envelope_from_hint
        return catalog_envelope_from_hint(self.pool,self.signer,self.session.world,
          {'_source_digest':resolved['_context_source_digest'],'supply':resolved['_context_catalog'],'consumer_id':resolved['_context_consumer_id']})

    def effect_tool(self,**arguments):
        # One guarded tool request, including a scope-denial record after rollback,
        # reuses its own Source-signed envelopes; SQL re-verifies them at each use.
        from nexloop_eios.role_runs import request_envelope_scope
        with request_envelope_scope():return self._effect_tool(**arguments)

    def _effect_tool(self,*,activation_ref,command,tool_operation,parameters=None,intent_id=None,request_scope=None):
        """Trusted Host bridge; actual owned activation selects the Run.

        Admission and the final lease/source recheck share one transaction.
        An expired lease after a lock wait rolls back Intent and quota writes.
        No raw Run credential, private digest or executor authority is returned.
        """
        from nexloop_eios.effect_intents import EffectIntentPort,EffectIntentConflict,unavailable
        # Operator-only diagnosis of a fail-closed outcome (never on the wire): which
        # step failed, how long the request ran, and whether the Run deadline passed.
        started=time.monotonic();stage='validate';run=None
        try:
            if tool_operation not in ('submit','find'):raise ValueError()
            if tool_operation=='submit':
                if type(parameters) is not dict or intent_id is not None:raise ValueError()
                arguments={'parameters':parameters,'request_scope':request_scope}
            else:
                if request_scope is not None or parameters is not None or type(intent_id) is not str or str(uuid.UUID(intent_id))!=intent_id:raise ValueError()
                arguments={'intent_id':intent_id}
            text,digest=_command(command)
            if type(activation_ref) is not str or re.fullmatch(r'activation_[a-f0-9-]{36}',activation_ref) is None:raise ValueError()
            stage='connect'
            with self.pool.connection() as db,db.transaction():
                stage='resolve'
                verify_application_role(db)
                queue=db.execute('select authz.nexloop_runtime_activation_hint(%s,%s,%s)',
                    (self.session.token_digest,self.session.world,activation_ref)).fetchone()[0]
                binding=dict(activation_ref=activation_ref,command_text=text,command_digest=digest,input_digest=None,operation='tool')
                resolved=self._execute(db,self._signed(queue,'resolve',**binding))
                run=_identity(db,resolved['_run_digest'],self.session.world)
                if run.run_context is None or run.run_context.run_id!=command['run_id']:raise ValueError()
                # Same-Run tool requests serialize here, before the first guard, in one lock
                # order (same key as the 0085 submit lock, reentrant in this transaction); this
                # removes the 0039 execution-marker / row-lock cycle between concurrent guards.
                stage='run_lock'
                db.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',('nexloop-role-effect:'+run.run_context.run_id,))
                def guard():
                    envelope=self._signed(queue,'authorize',**binding,run_proofs=self._run_proofs(run),context_artifact_proof=self._context_read_proof(resolved),context_catalog_envelope=self._context_catalog_envelope(resolved),context_role_envelope=__import__('nexloop_eios.role_runs',fromlist=['role_envelope_for_run']).role_envelope_for_run(self.pool,self.signer,self.session.world,resolved['_run_digest']),context_relationship_envelopes=self._context_relationship_envelopes(resolved),context_formal_reads=self._context_formal_envelopes(resolved))
                    # Built before the lock; the execution lock comes before any row lock of the guard.
                    _execution_lock(db,run.run_context.run_id)
                    result=self._execute(db,envelope)
                    if result.get('authorized') is not True or result.get('ever_execution_authorized') is not True:raise ValueError()
                stage='guard_before'
                guard()
                stage='intent'
                receipt=EffectIntentPort(self.pool,run,self.signer)._execute_in_transaction(
                    db,tool_operation,action_version=1,runtime_refs={'consumer_ref':command['consumer_ref'],
                        'goal_version_ref':command['goal_version_ref']},**arguments)
                stage='guard_after'
                guard()
                stage='receipt'
                keys={'intent_id','receipt_id','state','payload_digest','provider_payload_digest','scope','business_action_success'}
                if type(receipt) is not dict or set(receipt)!=keys or receipt['scope']!='effect_intent' or receipt['business_action_success'] is not False:raise ValueError()
                response={'run_id':command['run_id'],'receipt':receipt}
                stage='commit'
            return response
        except EffectIntentConflict:raise
        except CatalogScopeDenied:
            # Original attempted submission transaction has already rolled back.
            # Recompute independent current Source record EXECUTE + catalog READ
            # and original opaque Run guard before committing an operational fact.
            from nexloop_eios.scope_denials import record_runtime_scope_denial
            denial=record_runtime_scope_denial(self,activation_ref=activation_ref,command=command,
                parameters=parameters,request_scope=request_scope)
            raise CatalogScopeDenied(denial['scope']) from None
        except Exception as error:
            raise unavailable(error,stage=stage,deadline=None if run is None else run.expires_at,started=started) from None
