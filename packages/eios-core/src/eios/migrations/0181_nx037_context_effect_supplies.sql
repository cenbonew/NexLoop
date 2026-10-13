-- ADR-030 (NX-037 gap B slice 2): the Message Run context pack (v2 path, the relay's default) carries the enterprise slot's
-- offering as effect_supplies when the Run is bound to that slot: the consumer's enrolled enterprise offering, currently
-- valid and asserted like the reply offering. The snapshot and the bind compute it the same way, so the stored pack must
-- match. A Run without the enterprise slot gets exactly the previous pack. Body copied from the current definition
-- (0179) except the marked addition; published migrations unchanged.
CREATE OR REPLACE FUNCTION authz.nexloop_context_artifact_command_v0060(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;tenant text;principal text;
 command jsonb:=p->'command';context_uuid uuid;outbox runtime.nexloop_message_outbox;conv runtime.nexloop_conversations;
 issued authz.nexloop_message_run_issuances;plan control.nexloop_effect_plan_bindings;ctx control.nexloop_effect_contexts;
 ctl control.nexloop_effect_control_ledger;artifact runtime.nexloop_local_artifacts;b runtime.nexloop_context_artifact_bindings;
 pub control.nexloop_action_definitions;cl runtime.nexloop_action_claims;snapshot jsonb;facts jsonb;namespace text;binding jsonb;pack jsonb;proof jsonb;
 catalog_env jsonb;catalog_result jsonb;ent_e control.nexloop_relay_enrollments;ent_o ontology.objects;ent_b ontology.objects;
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
 select context_id into context_uuid from control.nexloop_effect_run_contexts where run_id=issued.run_id order by (slot_identity<>'service.request:primary'),slot_identity limit 1;
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

 catalog_env:=a->'catalog_envelope';
 if jsonb_typeof(catalog_env) is distinct from 'object' then raise exception 'catalog unavailable' using errcode='42501';end if;
 catalog_result:=authz.nexloop_service_catalog_scope(p_digest,p_world,catalog_env->>'text',catalog_env->>'signature',catalog_env->>'payload');
 if catalog_result->'allowed' is distinct from 'true'::jsonb or (catalog_env->>'payload')::jsonb->>'consumer_id' is distinct from conv.consumer_id then raise exception 'catalog unavailable' using errcode='42501';end if;
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 if (p->>'artifact_identity_text')::jsonb is distinct from jsonb_build_array(tenant,p_world,principal,'context:'||issued.run_id::text) then raise exception 'context artifact unavailable' using errcode='42501';end if;
 binding:=authz.nexloop_context_command_binding(command);
 if (p->>'command_binding_text')::jsonb is distinct from binding or p->>'command_binding_digest' is distinct from encode(sha256(convert_to(p->>'command_binding_text','UTF8')),'hex') then raise exception 'context artifact unavailable' using errcode='42501';end if;
 snapshot:=jsonb_build_object('schema_version','nexloop.context-pack.v2',
  'bindings',jsonb_build_object('tenant_id',tenant,'world_id',p_world,'run_id',issued.run_id,'source_principal',principal,'context_id',context_uuid,'namespace',namespace,'artifact_id',substr(encode(sha256(convert_to(p->>'artifact_identity_text','UTF8')),'hex'),1,32),'command_digest',p->>'command_binding_digest'),
  'user_statement',jsonb_build_object('message_id',outbox.message_id,'conversation_id',outbox.conversation_id,'sequence',(outbox.record->>'sequence')::bigint,'body',outbox.record->>'body','provenance','eios:object:'||outbox.message_id),
  'formal_facts',facts,'current_constraints',jsonb_build_object('action','nexloop.service.request:1','allow_effect',true,'budget_units',ctl.budget_units,'reserved_units',ctl.reserved_units,'valid_until',least(ctx.valid_until,ctl.valid_until,issued.expires_at),'executor_principal',ctl.executor_principal));
 snapshot:=snapshot||jsonb_build_object('supply',catalog_result->'supply');
 -- ADR-030: a Run bound to the enterprise slot sees the consumer's enrolled enterprise offering as effect_supplies (current,
 -- asserted like the reply offering). Absent otherwise, so a reply-only pack is unchanged.
 if exists(select 1 from control.nexloop_effect_run_contexts rl where rl.run_id=issued.run_id and rl.slot_identity='enterprise.deliver:primary') then
  ent_e:=control.nexloop_relay_enrollment_current(tenant,p_world,conv.consumer_id);
  perform set_config('eios.tenant_id',tenant,true);
  if control.nexloop_relay_enterprise_problem(tenant,p_world,conv.consumer_id,ent_e) is not null then raise exception 'enterprise catalog unavailable' using errcode='42501';end if;
  perform set_config('eios.tenant_id',tenant,true);
  select * into ent_o from ontology.objects where tenant_id=tenant and world=p_world and object_id=ent_e.enterprise_offering_id and type_name='ServiceOffering';
  select * into ent_b from ontology.objects where tenant_id=tenant and world=p_world and object_id=ent_e.enterprise_binding_id and type_name='ConsumerServiceOffering';
  perform authz.nexloop_assert_service_offering(tenant,p_world,conv.consumer_id,principal,ent_o.object_id,ent_o.nexloop_revision,ent_b.object_id,
   jsonb_build_object('offering_id',ent_o.object_id,'offering_revision',ent_o.nexloop_revision,'requested_guarantees','[]'::jsonb,'requested_discounts','[]'::jsonb));
  snapshot:=snapshot||jsonb_build_object('effect_supplies',jsonb_build_array(jsonb_build_object('action','nexloop.enterprise.deliver:1',
   'offering_id',ent_o.object_id,'offering_revision',ent_o.nexloop_revision,'binding_id',ent_b.object_id,'binding_revision',ent_b.nexloop_revision,
   'provenance','eios:object:'||ent_o.object_id,'properties',ent_o.properties)));
 end if;
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=tenant and world=p_world and message_id=outbox.message_id for update;
 if found then
  if b.run_id is distinct from issued.run_id or b.source_principal is distinct from principal or b.context_id is distinct from context_uuid or b.command_binding is distinct from binding then raise exception 'context artifact conflict' using errcode='23505';end if;
  -- Immutable assembly-time constraints survive replay; dispatch rechecks live authority/budget.
  if b.pack_text::jsonb->'supply' is distinct from catalog_result->'supply' then raise exception 'catalog revision stale' using errcode='42501';end if;
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
 perform authz.nexloop_service_catalog_scope(p_digest,p_world,catalog_env->>'text',catalog_env->>'signature',catalog_env->>'payload');
 return snapshot;
end $function$
;
