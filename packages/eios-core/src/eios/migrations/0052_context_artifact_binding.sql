-- Independent candidate only: real governed ContextArtifact, not S3 ContextEngine.
create table runtime.nexloop_context_artifact_bindings (
 tenant_id text not null,world text not null check(world='real'),message_id text not null,
 run_id uuid not null unique references authz.nexloop_run_credentials(run_id),
 source_principal text not null,source_digest text not null,context_id uuid not null references control.nexloop_effect_contexts(context_id),
 artifact_id text not null,namespace text not null,pack_text text not null,
 pack_digest text not null check(pack_digest ~ '^[a-f0-9]{64}$'),
 command_binding jsonb not null,created_at timestamptz not null,
 primary key(tenant_id,world,message_id),
 foreign key(tenant_id,world,artifact_id) references runtime.nexloop_local_artifacts(tenant_id,world,artifact_id)
);
alter table runtime.nexloop_context_artifact_bindings owner to nexloop_owner;
alter table runtime.nexloop_context_artifact_bindings enable row level security;
alter table runtime.nexloop_context_artifact_bindings force row level security;
create policy context_artifact_tenant on runtime.nexloop_context_artifact_bindings to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_context_artifact_bindings from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_context_command_binding(p jsonb) returns jsonb language sql immutable set search_path=pg_catalog as $$
 select jsonb_object_agg(key,value) from jsonb_each(p) where key=any(array['schema_version','run_id','request_id','tenant_id','world_id','mode','consumer_ref','goal_version_ref','role_ref','runtime_owner_epoch','runtime_profile','trigger_event_id','budget','not_after'])
$$;
alter function authz.nexloop_context_command_binding(jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_command_binding(jsonb) from public;

create function authz.nexloop_context_artifact_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;tenant text;principal text;
 command jsonb:=p->'command';context_uuid uuid;outbox runtime.nexloop_message_outbox;conv runtime.nexloop_conversations;
 issued authz.nexloop_message_run_issuances;plan control.nexloop_effect_plan_bindings;ctx control.nexloop_effect_contexts;
 ctl control.nexloop_effect_control_ledger;artifact runtime.nexloop_local_artifacts;b runtime.nexloop_context_artifact_bindings;
 pub control.nexloop_action_definitions;cl runtime.nexloop_action_claims;snapshot jsonb;facts jsonb;namespace text;binding jsonb;pack jsonb;proof jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>131072
  or a->>'protocol' is distinct from 'nexloop-context-artifact-v1' or p->>'verb' is null or p->>'verb' not in ('snapshot','bind')
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'resource_id' is distinct from 'eios:action:nexloop.context.bind:1'
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then raise exception 'context artifact unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-artifact-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'context artifact unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'context artifact unavailable' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 select * into pub from control.nexloop_action_definitions where tenant_id=tenant and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or pub.definition is distinct from a->'definition' or pub.capability is distinct from a->'capability'
  or pub.definition->'governance'->>'approval_mode' is distinct from 'none' or pub.definition->'governance'->>'risk_level' is distinct from 'low'
  or pub.definition->'input_schema' is distinct from '{"type":"object","properties":{"message_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"run_id":{"type":"string","format":"uuid"}},"required":["message_id","run_id"],"additionalProperties":false}'::jsonb
  or pub.definition->'preconditions' is distinct from '[]'::jsonb or pub.definition->'governance'->'policy_refs' is distinct from '[]'::jsonb then raise exception 'context artifact unavailable' using errcode='42501';end if;
 if jsonb_typeof(a->'artifact_proofs') is distinct from 'array' or jsonb_array_length(a->'artifact_proofs')<>2
  or not exists(select 1 from jsonb_array_elements(a->'artifact_proofs') x where x->>'operation'='create')
  or not exists(select 1 from jsonb_array_elements(a->'artifact_proofs') x where x->>'operation'='read') then raise exception 'context artifact unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop
  if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() or (proof->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then raise exception 'context artifact unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);
 end loop;
 if p->>'message_id' is null or p->>'message_id' !~ '^[a-f0-9]{64}$' then raise exception 'context artifact unavailable' using errcode='42501';end if;
 perform pg_advisory_xact_lock(hashtextextended(tenant||':'||p_world||':message-route:'||(p->>'message_id'),0));
 perform 1 from runtime.nexloop_message_routes where tenant_id=tenant and world=p_world and message_id=p->>'message_id' for update;
 select * into outbox from runtime.nexloop_message_outbox where tenant_id=tenant and world=p_world and message_id=p->>'message_id' for update;
 if not found then raise exception 'context artifact unavailable' using errcode='42501';end if;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=outbox.conversation_id for share;
 if not found then raise exception 'context artifact unavailable' using errcode='42501';end if;
 if not exists(select 1 from ontology.objects mo where mo.tenant_id=tenant and mo.world=p_world and mo.object_id=outbox.message_id and mo.type_name='Message'
  and mo.properties=jsonb_build_object('conversation_id',outbox.conversation_id,'sequence',outbox.sequence,'actor',outbox.record->>'actor','body',outbox.record->>'body','accepted_at',outbox.record->>'accepted_at')) then raise exception 'context artifact unavailable' using errcode='42501';end if;
 select * into issued from authz.nexloop_message_run_issuances where tenant_id=tenant and world=p_world and message_id=outbox.message_id for share;
 if not found or issued.run_id is distinct from (command->>'run_id')::uuid or issued.run_digest is distinct from p->>'run_digest'
  or not exists(select 1 from ontology.objects assignment where assignment.tenant_id=tenant and assignment.world=p_world and assignment.object_id=issued.assignment_id and assignment.nexloop_revision=issued.assignment_revision and assignment.type_name='MessageAssignment' and assignment.properties->>'source_principal'=principal) or issued.expires_at<=clock_timestamp()
  or issued.request_id is distinct from command->>'request_id' or command->>'credential_ref' is distinct from 'run:'||issued.run_id::text then raise exception 'context artifact unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'run_proofs') loop perform authz.nexloop_assert_action_authority(p->>'run_digest',p_world,proof);end loop;
 select context_id into context_uuid from control.nexloop_effect_run_contexts where run_id=issued.run_id;
 if context_uuid is null then raise exception 'context artifact unavailable' using errcode='42501';end if;
 -- Ledger/context locks precede Run-helper SHARE locks and binding locks.
 perform authz.nexloop_assert_effect_plan(context_uuid,tenant,p_world);
 context_uuid:=authz.nexloop_assert_message_source(p->>'run_digest',p_world,tenant,conv.consumer_id,command,a->'run_proofs');
 if authz.nexloop_service_identity_snapshot(p->>'run_digest',p_world)->'binding'->>'subject_principal_id' is distinct from principal then raise exception 'context artifact unavailable' using errcode='42501';end if;
 select * into plan from control.nexloop_effect_plan_bindings where context_id=context_uuid;
 select * into ctx from control.nexloop_effect_contexts where context_id=context_uuid;
 select * into ctl from control.nexloop_effect_control_ledger where control_id=plan.control_id;
 select jsonb_agg(jsonb_build_object('type',o.type_name,'id',o.object_id,'revision',o.nexloop_revision,'provenance','eios:object:'||o.object_id) order by o.type_name)
 into facts from ontology.objects o where o.tenant_id=tenant and o.world=p_world and o.object_id in (conv.consumer_id,plan.goal_id,plan.step_id,plan.control_id);
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 if (p->>'artifact_identity_text')::jsonb is distinct from jsonb_build_array(tenant,p_world,principal,'context:'||issued.run_id::text) then raise exception 'context artifact unavailable' using errcode='42501';end if;
 binding:=authz.nexloop_context_command_binding(command);
 if (p->>'command_binding_text')::jsonb is distinct from binding or p->>'command_binding_digest' is distinct from encode(sha256(convert_to(p->>'command_binding_text','UTF8')),'hex') then raise exception 'context artifact unavailable' using errcode='42501';end if;
 snapshot:=jsonb_build_object('schema_version','nexloop.context-pack.v1',
  'bindings',jsonb_build_object('tenant_id',tenant,'world_id',p_world,'run_id',issued.run_id,'source_principal',principal,'context_id',context_uuid,'namespace',namespace,'artifact_id',substr(encode(sha256(convert_to(p->>'artifact_identity_text','UTF8')),'hex'),1,32),'command_digest',p->>'command_binding_digest'),
  'user_statement',jsonb_build_object('message_id',outbox.message_id,'conversation_id',outbox.conversation_id,'sequence',(outbox.record->>'sequence')::bigint,'body',outbox.record->>'body','provenance','eios:object:'||outbox.message_id),
  'formal_facts',facts,'current_constraints',jsonb_build_object('action','nexloop.service.request:1','allow_effect',true,'budget_units',ctl.budget_units,'reserved_units',ctl.reserved_units,'valid_until',least(ctx.valid_until,ctl.valid_until,issued.expires_at),'executor_principal',ctl.executor_principal));
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=tenant and world=p_world and message_id=outbox.message_id for update;
 if found then
  if b.run_id is distinct from issued.run_id or b.source_principal is distinct from principal or b.context_id is distinct from context_uuid or b.command_binding is distinct from binding then raise exception 'context artifact conflict' using errcode='23505';end if;
  -- Immutable assembly-time constraints survive replay; dispatch rechecks live authority/budget.
  snapshot:=b.pack_text::jsonb;
 end if;
 if p->>'verb'='bind' then
  if p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 or (p->>'pack_text')::jsonb is distinct from snapshot
   or p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') then raise exception 'context artifact conflict' using errcode='23505';end if;
  -- NUL cannot be stored in PostgreSQL text: derive bytes explicitly.
  namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
  select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and artifact_id=p->>'artifact_id' for share;
  if not found or artifact.artifact_id is distinct from snapshot->'bindings'->>'artifact_id' or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal
   or artifact.sha256 is distinct from p->>'pack_digest' or artifact.size_bytes is distinct from octet_length(p->>'pack_text')
   or artifact.media_type is distinct from 'application/vnd.nexloop.context+json' or artifact.retention_until<=clock_timestamp()
   or artifact.object_key is distinct from namespace||'/'||artifact.artifact_id then raise exception 'context artifact unavailable' using errcode='42501';end if;
  for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);end loop;
  if jsonb_typeof(a->'artifact_proofs') is distinct from 'array' or jsonb_array_length(a->'artifact_proofs')<>2
   or not exists(select 1 from jsonb_array_elements(a->'artifact_proofs') x where x->>'operation'='create')
   or not exists(select 1 from jsonb_array_elements(a->'artifact_proofs') x where x->>'operation'='read') then raise exception 'context artifact unavailable' using errcode='42501';end if;
  select * into cl from runtime.nexloop_action_claims where tenant_id=tenant and world=p_world and action_name='nexloop.context.bind' and intent_id=p->>'claim_id' for share;
  if not found or cl.principal_id is distinct from principal or cl.claim->'binding' is distinct from a->'claim_binding' or cl.claim->>'state' not in ('active','terminal') or (cl.claim->>'state'='active' and (cl.claim->>'lease_expires_at')::timestamptz<=clock_timestamp()) or (cl.claim->>'state'='terminal' and (b.run_id is null or cl.claim->'terminal_outcome'->>'status' is distinct from 'succeeded' or cl.claim->'terminal_outcome'->>'outcome_digest' is distinct from artifact.sha256)) then raise exception 'context artifact unavailable' using errcode='42501';end if;
  if b.run_id is not null and (b.artifact_id,b.pack_text) is distinct from (artifact.artifact_id,p->>'pack_text') then raise exception 'context artifact conflict' using errcode='23505';end if;
  insert into runtime.nexloop_context_artifact_bindings values(tenant,p_world,outbox.message_id,issued.run_id,principal,p_digest,context_uuid,artifact.artifact_id,namespace,p->>'pack_text',p->>'pack_digest',binding,clock_timestamp()) on conflict do nothing;
 end if;
 if jsonb_typeof(a->'artifact_proofs') is distinct from 'array' or jsonb_array_length(a->'artifact_proofs')<>2
  or not exists(select 1 from jsonb_array_elements(a->'artifact_proofs') x where x->>'operation'='create')
  or not exists(select 1 from jsonb_array_elements(a->'artifact_proofs') x where x->>'operation'='read') then raise exception 'context artifact unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop
  if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() or (proof->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then raise exception 'context artifact unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);
 end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform authz.nexloop_assert_message_source(p->>'run_digest',p_world,tenant,conv.consumer_id,command,a->'run_proofs');
 return snapshot;
end $$;
alter function authz.nexloop_context_artifact_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_command(text,text,text,text,text) from public,nexloop_identity,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker;
grant execute on function authz.nexloop_context_artifact_command(text,text,text,text,text) to nexloop_api;

-- Separate v2 protocol retains original current proof/event/lease checks.
create function authz.nexloop_context_message_runtime_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;tenant text:=a->>'tenant_id';verb text:=c->>'verb';
 principal text:=a->>'principal_id';route runtime.nexloop_message_routes;outbox runtime.nexloop_message_outbox;
 conv runtime.nexloop_conversations;command jsonb;event jsonb;context_uuid uuid;result jsonb;
 def control.nexloop_action_definitions;binding authz.nexloop_runtime_run_bindings;job runtime.jobs;
 pack_binding runtime.nexloop_context_artifact_bindings;lease_seconds integer;event_uuid uuid;event_hash bytea;event_hex text;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from 'nexloop-context-message-runtime-v1' or a->>'operation' is distinct from 'execute'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or verb is null or verb not in ('message','bind','claim','authorize','ack','read') then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-message-runtime-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',tenant,true);
 select * into def from control.nexloop_action_definitions where tenant_id=tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or def.definition is distinct from a->'definition' or def.capability is distinct from a->'capability'
  or def.definition->'governance'->>'approval_mode' is distinct from 'none'
  or def.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(def.definition->'governance'->'policy_refs')<>0 then raise exception 'message runtime unavailable' using errcode='42501';end if;
 if verb='read' then
  if a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.read:1'
   or not exists(select 1 from control.nexloop_browser_sessions where encode(session_token_digest,'hex')=p_digest) then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
 else
  if a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.route:1'
   or not exists(select 1 from authz.nexloop_service_credentials svc where svc.token_digest=p_digest and svc.tenant_id=tenant
    and svc.binding->>'subject_kind'='service' and svc.status='active') then raise exception 'message runtime unavailable' using errcode='42501';end if;
 end if;
 if verb='claim' then
  if jsonb_typeof(c->'lease_seconds') is distinct from 'number' or c->>'lease_seconds'!~'^[0-9]{1,3}$' then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  lease_seconds=(c->>'lease_seconds')::integer;
  if lease_seconds not between 3 and 300 then raise exception 'message runtime unavailable' using errcode='42501';end if;
  select r.* into route from runtime.nexloop_message_routes r join runtime.nexloop_message_outbox b
   on b.tenant_id=r.tenant_id and b.world=r.world and b.message_id=r.message_id
   where r.tenant_id=tenant and r.world=p_world and b.status='pending' and r.bound_by=principal
    and exists(select 1 from runtime.nexloop_context_artifact_bindings cb where cb.tenant_id=r.tenant_id and cb.world=r.world and cb.run_id=r.run_id and cb.message_id=r.message_id)
    and (r.lease_until is null or r.lease_until<=clock_timestamp()) order by b.conversation_id,b.sequence for update of r skip locked limit 1;
  if not found then perform authz.nexloop_assert_action_authority(p_digest,p_world,a);return null;end if;
  update runtime.nexloop_message_routes set fence=fence+1,lease_credential=p_digest,lease_until=clock_timestamp()+make_interval(secs=>lease_seconds)
   where tenant_id=tenant and world=p_world and message_id=route.message_id returning * into route;
 else
  if verb='bind' then perform pg_advisory_xact_lock(hashtextextended(tenant||':'||p_world||':message-route:'||(c->>'message_id'),0));end if;
  if c->>'message_id' is null or c->>'message_id'!~'^[a-f0-9]{64}$' then raise exception 'message runtime unavailable' using errcode='42501';end if;
  select * into route from runtime.nexloop_message_routes where tenant_id=tenant and world=p_world and message_id=c->>'message_id' for update;
 end if;
 select * into outbox from runtime.nexloop_message_outbox where tenant_id=tenant and world=p_world
  and message_id=case when verb='claim' then route.message_id else c->>'message_id' end for update;
 if not found then raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=outbox.conversation_id for share;
 if not found or not exists(select 1 from ontology.objects where tenant_id=tenant and world=p_world and type_name='Conversation'
  and object_id=conv.conversation_id and properties=jsonb_build_object('consumer_id',conv.consumer_id,'owner_principal',conv.principal_id)) then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 if verb not in ('message','read') then
  if route.message_id is not null and route.bound_by is distinct from principal then raise exception 'message runtime unavailable' using errcode='42501';end if;
  select * into pack_binding from runtime.nexloop_context_artifact_bindings where tenant_id=tenant and world=p_world and message_id=outbox.message_id;
  if not found then raise exception 'message runtime unavailable' using errcode='42501';end if;
  if verb<>'ack' then perform authz.nexloop_assert_effect_plan(pack_binding.context_id,tenant,p_world);end if;
  select * into pack_binding from runtime.nexloop_context_artifact_bindings where tenant_id=tenant and world=p_world and message_id=outbox.message_id for share;
  if not found or pack_binding.pack_text::jsonb->'user_statement'->>'body' is distinct from outbox.record->>'body'
   or pack_binding.pack_text::jsonb->'user_statement'->>'message_id' is distinct from outbox.message_id then raise exception 'message runtime unavailable' using errcode='42501';end if;
  if verb='bind' then command:=(c->>'command_text')::jsonb;else command:=route.command;end if;
  if pack_binding.run_id is distinct from (command->>'run_id')::uuid
   or pack_binding.command_binding is distinct from authz.nexloop_context_command_binding(command)
   or command->>'context_manifest_ref' is distinct from 'artifact:'||pack_binding.artifact_id
   or command->>'credential_ref' is distinct from 'run:'||pack_binding.run_id::text then raise exception 'message_runtime_conflict' using errcode='23505';end if;
  if verb in ('bind','authorize','claim') then
   if not exists(select 1 from runtime.nexloop_local_artifacts ar where ar.tenant_id=tenant and ar.world=p_world and ar.artifact_id=pack_binding.artifact_id
    and ar.status='available' and ar.sha256=pack_binding.pack_digest and ar.retention_until>clock_timestamp()
    and ar.object_key=pack_binding.namespace||'/'||ar.artifact_id) then raise exception 'message runtime unavailable' using errcode='42501';end if;
  end if;
 end if;
 if verb='read' then
  if not exists(select 1 from ontology.objects where tenant_id=tenant and world=p_world and type_name='Consumer' and object_id=conv.consumer_id)
   or conv.principal_id is distinct from principal or not exists(select 1 from control.nexloop_consumer_owners o join ontology.objects f
    on f.tenant_id=o.tenant_id and f.world=o.world and f.object_id=o.ownership_id and f.type_name='ConsumerOwnership'
    and f.properties=jsonb_build_object('consumer_id',o.consumer_id,'principal_id',o.principal_id)
    where o.tenant_id=tenant and o.world=p_world and o.principal_id=principal and o.consumer_id=conv.consumer_id) then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if route.task_id is null or outbox.status<>'delivered' then return null;end if;
  return jsonb_build_object('message_id',route.message_id,'run_id',route.run_id,'request_id',route.command->>'request_id','task_id',route.task_id,'status','queued');
 end if;
 if route.message_id is not null and route.bound_by is distinct from principal then raise exception 'message runtime unavailable' using errcode='42501';end if;
 if verb='message' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('record',outbox.record,'source_event_id',outbox.event_id);
 elsif verb='bind' then
  if jsonb_typeof(c->'command_text') is distinct from 'string' or jsonb_typeof(c->'event_text') is distinct from 'string'
   or c->>'command_digest' is distinct from encode(sha256(convert_to(c->>'command_text','UTF8')),'hex')
   or c->>'input_digest' is distinct from pack_binding.pack_digest
   or c->>'queue' is null or c->>'queue'!~'^[A-Za-z][A-Za-z0-9_-]{0,63}$' then raise exception 'message runtime unavailable' using errcode='42501';end if;
  command=(c->>'command_text')::jsonb;event=(c->>'event_text')::jsonb;event_uuid=(event->>'event_id')::uuid;
  if jsonb_typeof(c->'event_name_text') is distinct from 'string'
   or (c->>'event_name_text')::jsonb is distinct from jsonb_build_array(tenant,p_world,'webchat',outbox.event_id) then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  event_hash=extensions.digest(uuid_send('e0963cdf-ea02-587c-a8bc-c04f7c2c4029'::uuid)||convert_to(c->>'event_name_text','UTF8'),'sha1');
  event_hex=encode(event_hash,'hex');
  if event_uuid is distinct from (substr(event_hex,1,8)||'-'||substr(event_hex,9,4)||'-5'||substr(event_hex,14,3)||'-'
    ||to_hex(((get_byte(event_hash,8)>>4)&3)|8)||substr(event_hex,18,3)||'-'||substr(event_hex,21,12))::uuid then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  if event is distinct from jsonb_build_object('schema_version','1.0','event_id',event_uuid::text,'tenant_id',tenant,'world_id',p_world,
   'mode','real','event_type','message.accepted','source','webchat','source_event_id',outbox.event_id,
   'occurred_at',outbox.record->>'accepted_at','recorded_at',outbox.record->>'accepted_at','subject_ref','message:'||outbox.message_id,
   'correlation_id',command->>'run_id','causation_id',null,'payload',jsonb_build_object('message_ref','message:'||outbox.message_id,
    'conversation_ref','conversation:'||outbox.conversation_id)) then raise exception 'message runtime unavailable' using errcode='42501';end if;
  if event->>'schema_version' is distinct from '1.0' or event->>'tenant_id' is distinct from tenant
   or event->>'world_id' is distinct from p_world or event->>'mode' is distinct from 'real'
   or event->>'source' is distinct from 'webchat' or event->>'source_event_id' is distinct from outbox.event_id
   or event->>'event_type' is distinct from 'message.accepted' or event->>'subject_ref' is distinct from 'message:'||outbox.message_id
   or event->>'correlation_id' is distinct from command->>'run_id' or event->'causation_id' is distinct from 'null'::jsonb
   or event->>'occurred_at' is distinct from outbox.record->>'accepted_at' or event->>'recorded_at' is distinct from outbox.record->>'accepted_at'
   or event->'payload' is distinct from jsonb_build_object('message_ref','message:'||outbox.message_id,'conversation_ref','conversation:'||outbox.conversation_id)
   or command->>'trigger_event_id' is distinct from event->>'event_id' then raise exception 'message runtime unavailable' using errcode='42501';end if;
  context_uuid=authz.nexloop_assert_message_source(c->>'run_digest',p_world,tenant,conv.consumer_id,command,c->'run_proofs');
  if route.message_id is not null then
   if (route.run_digest,route.command_digest,route.input_digest,route.queue,route.event_envelope)
    is distinct from (c->>'run_digest',c->>'command_digest',c->>'input_digest',c->>'queue',event) then
    raise exception 'message_runtime_conflict' using errcode='P0001';end if;
  else
   if outbox.status<>'pending' then raise exception 'message runtime unavailable' using errcode='42501';end if;
   insert into runtime.nexloop_message_routes(tenant_id,world,message_id,run_id,run_digest,queue,event_id,event_envelope,command,command_digest,input_digest,bound_by,context_id)
    values(tenant,p_world,outbox.message_id,(command->>'run_id')::uuid,c->>'run_digest',c->>'queue',event_uuid,event,command,c->>'command_digest',c->>'input_digest',principal,context_uuid) returning * into route;
  end if;
  perform authz.nexloop_assert_message_source(route.run_digest,p_world,tenant,conv.consumer_id,route.command,c->'run_proofs');
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('message_id',route.message_id,'run_id',route.run_id,'event_id',route.event_id,'bound',true);
 end if;
 if route.message_id is null or route.lease_credential is distinct from p_digest or route.lease_until is null or route.lease_until<=clock_timestamp()
  or (verb<>'claim' and (jsonb_typeof(c->'fence') is distinct from 'number' or c->>'fence'!~'^[1-9][0-9]{0,18}$'
   or route.fence::text is distinct from c->>'fence')) then raise exception 'message runtime unavailable' using errcode='42501';end if;
 if verb='claim' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('message_id',route.message_id,'event_id',route.event_id,'fence',route.fence,'queue',route.queue,
   'command',route.command,'input',pack_binding.pack_text,'_run_digest',route.run_digest,'event_envelope',route.event_envelope);
 elsif verb='authorize' then
  perform authz.nexloop_assert_message_source(route.run_digest,p_world,tenant,conv.consumer_id,route.command,c->'run_proofs');
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if route.lease_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
  return jsonb_build_object('authorized',true);
 elsif verb='ack' then
  if c->'allow_missing' not in ('true'::jsonb,'false'::jsonb) or c->'allow_missing' is null then raise exception 'message runtime unavailable' using errcode='42501';end if;
  select b.* into binding from authz.nexloop_runtime_run_bindings b join runtime.nexloop_inbox i
   on i.tenant_id=b.tenant_id and i.world=b.world and i.job_id=b.task_id and i.source_id='webchat' and i.event_id=route.event_id::text
   where b.run_id=route.run_id and b.tenant_id=tenant and b.world=p_world for share of b,i;
  if not found then
   if c->'allow_missing'='true'::jsonb then
    perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
    if route.lease_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
    return null;end if;
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  select * into job from runtime.jobs where tenant_id=tenant and job_id=binding.task_id for share;
  if not found or binding.run_digest is distinct from route.run_digest or binding.command_digest is distinct from route.command_digest
   or binding.input_digest is distinct from route.input_digest or binding.queue is distinct from route.queue
   or binding.owner_epoch is distinct from (route.command->>'runtime_owner_epoch')::bigint
   or job.normalized_input is distinct from jsonb_build_object('run_command',route.command,'input',pack_binding.pack_text) then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  update runtime.nexloop_message_routes set task_id=binding.task_id where tenant_id=tenant and world=p_world and message_id=route.message_id;
  update runtime.nexloop_message_outbox set status='delivered' where tenant_id=tenant and world=p_world and message_id=route.message_id;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if route.lease_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
  return jsonb_build_object('message_id',route.message_id,'run_id',route.run_id,'request_id',route.command->>'request_id','task_id',binding.task_id,'status','queued');
 end if;
 raise exception 'message runtime unavailable' using errcode='42501';
end $$;
alter function authz.nexloop_context_message_runtime_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_message_runtime_command(text,text,text,text,text) from public,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_context_message_runtime_command(text,text,text,text,text) to nexloop_api;

-- Guard every actual model/tool authorization, not only admission-time bridge.
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) rename to nexloop_runtime_activation_command_v0051;
revoke all on function authz.nexloop_runtime_activation_command_v0051(text,text,text,text,text)
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb;v_result jsonb;
 b runtime.nexloop_context_artifact_bindings;ar runtime.nexloop_local_artifacts;identity jsonb;proof jsonb;v_tenant text;
begin
 -- Original signed queue + Run + active task/fence/TTL/marker checks execute
 -- first; any later denial rolls all original changes back in this transaction.
 v_result:=authz.nexloop_runtime_activation_command_v0051(p_digest,p_world,p_text,p_signature,p_payload);
 command:=(p->>'command_text')::jsonb;
 if not exists(select 1 from authz.nexloop_message_run_issuances i where i.run_id=(command->>'run_id')::uuid) then return v_result;end if;
 v_tenant:=a->>'tenant_id';
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=v_tenant and world=p_world and run_id=(command->>'run_id')::uuid;
 if not found then raise exception 'context artifact unavailable' using errcode='42501';end if;
 -- Same ledger/context→binding ordering as producer bind.
 if p->>'verb'<>'resolve' then
 perform authz.nexloop_assert_effect_plan(b.context_id,v_tenant,p_world);
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=v_tenant and world=p_world and run_id=(command->>'run_id')::uuid for share;
 end if;
 if b.run_id is null or command->>'context_manifest_ref' is distinct from 'artifact:'||b.artifact_id
  or command->>'credential_ref' is distinct from 'run:'||b.run_id::text
  or b.command_binding is distinct from authz.nexloop_context_command_binding(command)
  or (p->>'input_digest' is not null and p->>'input_digest' is distinct from b.pack_digest) then raise exception 'context artifact unavailable' using errcode='42501';end if;
 if p->>'verb'='resolve' then
  select * into ar from runtime.nexloop_local_artifacts where tenant_id=v_tenant and world=p_world and artifact_id=b.artifact_id;
 else
  select * into ar from runtime.nexloop_local_artifacts where tenant_id=v_tenant and world=p_world and artifact_id=b.artifact_id for share;
 end if;
 if not found or ar.status is distinct from 'available' or ar.retention_until<=clock_timestamp() or ar.sha256 is distinct from b.pack_digest
  or ar.size_bytes is distinct from octet_length(b.pack_text) or ar.object_key is distinct from b.namespace||'/'||ar.artifact_id then raise exception 'context artifact unavailable' using errcode='42501';end if;
 if p->>'verb'='resolve' then
  return v_result||jsonb_build_object('_context_source_digest',b.source_digest);
 elsif p->>'verb'='authorize' then
  proof:=a->'context_artifact_proof';
  if jsonb_typeof(proof) is distinct from 'object' or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp()
   or (proof->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' or proof->>'operation' is distinct from 'read' then raise exception 'context artifact unavailable' using errcode='42501';end if;
  identity:=authz.nexloop_assert_artifact_authority(b.source_digest,p_world,proof);
  if identity->>'tenant_id' is distinct from v_tenant or identity->>'subject_principal_id' is distinct from b.source_principal then raise exception 'context artifact unavailable' using errcode='42501';end if;
  v_result:=authz.nexloop_runtime_activation_command_v0051(p_digest,p_world,p_text,p_signature,p_payload);
  perform authz.nexloop_assert_artifact_authority(b.source_digest,p_world,proof);
  if (proof->>'expires_at')::timestamptz<=clock_timestamp() or ar.retention_until<=clock_timestamp() then raise exception 'context artifact unavailable' using errcode='42501';end if;
 end if;
 v_result:=v_result||jsonb_build_object('context_artifact',jsonb_build_object('artifact_ref','artifact:'||b.artifact_id,'sha256',b.pack_digest,'command_binding_digest',b.pack_text::jsonb->'bindings'->>'command_digest'));
 return v_result;
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public,nexloop_identity,nexloop_action_worker;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;
