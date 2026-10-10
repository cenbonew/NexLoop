-- NX-025 / ADR-023 §2.7 (temporary number 0110): the fallback reply Run, on the message path (dispatcher decision A).
--
-- A pending reply still unsettled when its fallback time comes starts at most ONE fallback reply Run for that inbound
-- message: a second Run of the same message, kind=fallback, explicitly separate from the original single-Run path.
-- * Issuance (authz.nexloop_reply_fallback_command, verb 'issue'): the Source's own Run credential, the MessageAssignment
--   checks of 0049 'issue', a separate fallback issuance table (primary key = message: at most one, replay-idempotent,
--   a different second issuance is a conflict), and mutual exclusion with settlement: the message's pending-reply item
--   must still exist and no reply bound to it may be accepted, both under the item's row lock (settlement deletes that
--   row in the acceptance transaction).
-- * Context v6 (authz.nexloop_context_v6_fallback_command): the 0105 message bind re-derived over the 0053/0062 core with
--   the fallback issuance and a separate fallback binding table (same layout as the primary binding table).
-- * Run-keyed lookups (activation, catalog, scope denial, artifact reads, v6 binding, outbound record) now find a
--   fallback Run through two helpers; every message-keyed lookup of the original path is untouched, so the original
--   single-Run path behaves exactly as before.
-- * Server-side tool restriction: a fallback Run's only effect is 'submit' of a reply bound to its message (the outbound
--   record is derived from the fallback issuance); 'find' and any submission without a reply text are refused in SQL,
--   a plan outcome is refused by 0106 (not a plan Run).
-- * Dispatch (control.nexloop_contact_assert_intent, 0109 rules + fallback): a fallback reply always obeys the bound-reply
--   rules (window, one reply per message) and is refused once another reply to that message was accepted.
-- Published migrations are not edited; the original functions are replaced in place by bodies copied from their latest
-- definition with only the stated changes (see each section).

-- 1. Fallback issuance and binding tables ------------------------------------------------------------------------
create table authz.nexloop_message_fallback_issuances (like authz.nexloop_message_run_issuances including defaults including constraints);
alter table authz.nexloop_message_fallback_issuances add primary key(tenant_id,world,message_id),
 add unique(run_id),add unique(run_digest),
 add foreign key(run_id) references authz.nexloop_run_credentials(run_id),add foreign key(run_digest) references authz.nexloop_run_credentials(token_digest);
create table runtime.nexloop_fallback_context_bindings (like runtime.nexloop_context_artifact_bindings including defaults including constraints);
alter table runtime.nexloop_fallback_context_bindings add primary key(tenant_id,world,message_id),add unique(run_id),
 add foreign key(run_id) references authz.nexloop_run_credentials(run_id),
 add foreign key(context_id) references control.nexloop_effect_contexts(context_id),
 add foreign key(tenant_id,world,artifact_id) references runtime.nexloop_local_artifacts(tenant_id,world,artifact_id);
do $rls$
declare t text;
begin
 foreach t in array array['authz.nexloop_message_fallback_issuances','runtime.nexloop_fallback_context_bindings'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator',t);
 end loop;
end $rls$;

-- 2. Run-keyed helpers ---------------------------------------------------------------------------------------------
create function authz.nexloop_is_fallback_run(p_run uuid) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from authz.nexloop_message_fallback_issuances f where f.run_id=p_run) $$;
create function authz.nexloop_is_message_run(p_run uuid) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from authz.nexloop_message_run_issuances i where i.run_id=p_run) or authz.nexloop_is_fallback_run(p_run) $$;
create function runtime.nexloop_run_binding(p_tenant text,p_world text,p_run uuid,p_lock boolean) returns setof runtime.nexloop_context_artifact_bindings
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if p_lock then
  return query select * from runtime.nexloop_context_artifact_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run for share;
  if found then return;end if;
  return query select * from runtime.nexloop_fallback_context_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run for share;
 else
  return query select * from runtime.nexloop_context_artifact_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run
   union all select * from runtime.nexloop_fallback_context_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run;
 end if;
end $$;
create view runtime.nexloop_all_context_bindings as
 select * from runtime.nexloop_context_artifact_bindings union all select * from runtime.nexloop_fallback_context_bindings;
alter function authz.nexloop_is_fallback_run(uuid) owner to nexloop_owner;
alter function authz.nexloop_is_message_run(uuid) owner to nexloop_owner;
alter function runtime.nexloop_run_binding(text,text,uuid,boolean) owner to nexloop_owner;
alter view runtime.nexloop_all_context_bindings owner to nexloop_owner;
alter view runtime.nexloop_all_context_bindings set (security_barrier=true);
revoke all on function authz.nexloop_is_fallback_run(uuid),authz.nexloop_is_message_run(uuid),runtime.nexloop_run_binding(text,text,uuid,boolean) from public;
revoke all on runtime.nexloop_all_context_bindings from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

create or replace function runtime.nexloop_context_v6_binding(p_tenant text,p_world text,p_run uuid) returns table(tenant_id text,world text,run_id uuid,context_id uuid,pack_text text)
language sql stable set search_path=pg_catalog,pg_temp as $$
 select b.tenant_id,b.world,b.run_id,b.context_id,b.pack_text from runtime.nexloop_context_artifact_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run
 union all
 select b.tenant_id,b.world,b.run_id,b.context_id,b.pack_text from runtime.nexloop_fallback_context_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run
 union all
 select r.tenant_id,r.world,r.run_id,runtime.nexloop_role_pack_context_id(r.run_id),r.pack_text from runtime.nexloop_role_context_artifacts r
  where r.tenant_id=p_tenant and r.world=p_world and r.run_id=p_run and r.pack_text::jsonb->>'schema_version'='nexloop.context-pack.v6' $$;

-- 3. Run-keyed lookups of the existing chain: latest bodies, issuance/binding lookups by Run made fallback-aware ------
-- authz.nexloop_runtime_activation_command_v0052: latest body from 0052_context_artifact_binding.sql; Run-keyed issuance/binding lookups find fallback Runs too.
create or replace function authz.nexloop_runtime_activation_command_v0052(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb;v_result jsonb;
 b runtime.nexloop_context_artifact_bindings;ar runtime.nexloop_local_artifacts;identity jsonb;proof jsonb;v_tenant text;
begin
 -- Original signed queue + Run + active task/fence/TTL/marker checks execute
 -- first; any later denial rolls all original changes back in this transaction.
 v_result:=authz.nexloop_runtime_activation_command_v0051(p_digest,p_world,p_text,p_signature,p_payload);
 command:=(p->>'command_text')::jsonb;
 if not authz.nexloop_is_message_run((command->>'run_id')::uuid) then return v_result;end if;
 v_tenant:=a->>'tenant_id';
 select * into b from runtime.nexloop_run_binding(v_tenant,p_world,(command->>'run_id')::uuid,false);
 if not found then raise exception 'context artifact unavailable' using errcode='42501';end if;
 -- Same ledger/context→binding ordering as producer bind.
 if p->>'verb'<>'resolve' then
 perform authz.nexloop_assert_effect_plan(b.context_id,v_tenant,p_world);
 select * into b from runtime.nexloop_run_binding(v_tenant,p_world,(command->>'run_id')::uuid,true);
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

-- authz.nexloop_runtime_activation_command_before_roles_v0062: latest body from 0099_nx023_context_v6_relationships_copy.sql; Run-keyed issuance/binding lookups find fallback Runs too.
create or replace function authz.nexloop_runtime_activation_command_before_roles_v0062(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb;result jsonb;b runtime.nexloop_context_artifact_bindings;env jsonb;checked jsonb;supply jsonb;tail_env jsonb;tail_proof jsonb;formal_meta jsonb;
begin
 result:=authz.nexloop_runtime_activation_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
 command:=(p->>'command_text')::jsonb;
 if not authz.nexloop_is_message_run((command->>'run_id')::uuid) then return result;end if;
 -- Original 52 already owns lease/fence and validates immutable pack binding.
 select * into b from runtime.nexloop_run_binding(a->>'tenant_id',p_world,(command->>'run_id')::uuid,false);
 if not found or (b.pack_text::jsonb->>'schema_version' is null or b.pack_text::jsonb->>'schema_version' not in ('nexloop.context-pack.v2','nexloop.context-pack.v4','nexloop.context-pack.v6')) then raise exception 'catalog context mandatory' using errcode='42501';end if;
 supply:=b.pack_text::jsonb->'supply';
 if jsonb_typeof(supply) is distinct from 'object' then raise exception 'catalog context mandatory' using errcode='42501';end if;
 if p->>'verb'='resolve' then
  if authz.nexloop_context_has_relationships(b.pack_text::jsonb) then result:=result||jsonb_build_object('_context_relationship',b.pack_text::jsonb->'relationship_context','_context_formal_facts',b.pack_text::jsonb->'formal_facts');end if;
  return result||jsonb_build_object('_context_catalog',supply,'_context_consumer_id',substr(command->>'consumer_ref',10));
 elsif p->>'verb'='authorize' then
  env:=a->'context_catalog_envelope';
  if jsonb_typeof(env) is distinct from 'object' then raise exception 'catalog context mandatory' using errcode='42501';end if;
  checked:=authz.nexloop_service_catalog_scope(b.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  if checked->'allowed' is distinct from 'true'::jsonb or checked->'supply' is distinct from supply
   or (env->>'payload')::jsonb->>'consumer_id' is distinct from substr(command->>'consumer_ref',10) then raise exception 'catalog context stale' using errcode='42501';end if;
  if p->>'verb'='authorize' and authz.nexloop_context_has_relationships(b.pack_text::jsonb) then if authz.nexloop_relationship_context_snapshot(b.source_digest,p_world,substr(command->>'consumer_ref',10),a->'context_relationship_envelopes') is distinct from b.pack_text::jsonb->'relationship_context' then raise exception 'relationship context stale' using errcode='42501';end if;end if;
  result:=authz.nexloop_runtime_activation_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
  checked:=authz.nexloop_service_catalog_scope(b.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  if checked->'allowed' is distinct from 'true'::jsonb or checked->'supply' is distinct from supply then raise exception 'catalog context stale' using errcode='42501';end if;
 end if;
  if p->>'verb'='authorize' and authz.nexloop_context_has_relationships(b.pack_text::jsonb) then if authz.nexloop_relationship_context_snapshot(b.source_digest,p_world,substr(command->>'consumer_ref',10),a->'context_relationship_envelopes') is distinct from b.pack_text::jsonb->'relationship_context' then raise exception 'relationship context stale' using errcode='42501';end if;end if;
 if p->>'verb'='authorize' and authz.nexloop_context_has_relationships(b.pack_text::jsonb) then
  formal_meta:=authz.nexloop_context_formal_current(b.source_digest,p_world,b.pack_text::jsonb->'formal_facts',a->'context_formal_reads');
  perform authz.nexloop_assert_context_proof_deadlines(a);
  perform authz.nexloop_assert_context_proof_deadlines(a->'context_artifact_proof');
  for tail_proof in select value from jsonb_array_elements(p->'run_proofs') loop perform authz.nexloop_assert_context_proof_deadlines(tail_proof);end loop;
  for tail_env in select value from jsonb_each((a->'context_catalog_envelope'->>'payload')::jsonb->'reads') loop perform authz.nexloop_assert_context_proof_deadlines((tail_env->>'text')::jsonb);end loop;
  for tail_env in select value from jsonb_each(a->'context_formal_reads') loop perform authz.nexloop_assert_context_proof_deadlines((tail_env->>'text')::jsonb);end loop;
  perform authz.nexloop_assert_relationship_deadlines(a->'context_relationship_envelopes',b.pack_text::jsonb->'relationship_context');
  if formal_meta->>'control_valid_until' is null or (formal_meta->>'control_valid_until')::timestamptz<=clock_timestamp() then raise exception 'context activation formal expired' using errcode='42501';end if;
  if command->>'not_after' is null or (command->>'not_after')::timestamptz<=clock_timestamp() or b.pack_text::jsonb->'current_constraints'->>'valid_until' is null or (b.pack_text::jsonb->'current_constraints'->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'context activation expired at return' using errcode='42501';end if;
 end if;
 return result;
end $$;

-- authz.nexloop_effect_catalog_metadata: latest body from 0092_nx023_context_bind_v6.sql; Run-keyed issuance/binding lookups find fallback Runs too.
create or replace function authz.nexloop_effect_catalog_metadata(p_run uuid,p_tenant text,p_world text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r authz.nexloop_run_credentials;link control.nexloop_effect_run_contexts;ctx control.nexloop_effect_contexts;
 b runtime.nexloop_context_artifact_bindings;identity jsonb;candidate ontology.objects;offered ontology.objects;n integer:=0;supply jsonb;
begin
 if p_run is null or p_tenant is null or p_world is distinct from 'real' then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from authz.nexloop_run_credentials rc where rc.run_id=p_run and rc.world=p_world;
 if not found or r.status is distinct from 'active' or r.expires_at<=clock_timestamp() then raise exception 'effect catalog Run unavailable' using errcode='42501';end if;
 identity:=authz.nexloop_service_identity_snapshot(r.source_digest,p_world);
 if identity->'binding'->>'tenant_id' is distinct from p_tenant or identity->'run_context' is distinct from 'null'::jsonb then raise exception 'effect catalog Source unavailable' using errcode='42501';end if;
 select * into link from control.nexloop_effect_run_contexts er where er.run_id=r.run_id;
 if not found or link.valid_until<=clock_timestamp() then raise exception 'effect catalog Plan unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_effect_plan(link.context_id,p_tenant,p_world);
 select * into ctx from control.nexloop_effect_contexts ec where ec.context_id=link.context_id and ec.tenant_id=p_tenant and ec.world=p_world;
 if not found then raise exception 'effect catalog Plan unavailable' using errcode='42501';end if;
 if authz.nexloop_is_message_run(r.run_id) then
  select * into b from runtime.nexloop_run_binding(p_tenant,p_world,r.run_id,false);
  if not found or b.source_digest is distinct from r.source_digest or b.context_id is distinct from link.context_id
   or (b.pack_text::jsonb->>'schema_version' is null or b.pack_text::jsonb->>'schema_version' not in ('nexloop.context-pack.v2','nexloop.context-pack.v4','nexloop.context-pack.v6'))
   or substr(b.command_binding->>'consumer_ref',10) is distinct from ctx.consumer_id then raise exception 'effect catalog mandatory' using errcode='42501';end if;
  supply:=b.pack_text::jsonb->'supply';
 else
  -- No guessed default or silent first row: multiple governed grants require
  -- explicit future selection; this minimal entry remains closed on ambiguity.
  for candidate in select o.* from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='ConsumerServiceOffering'
   and o.properties->>'consumer_id'=ctx.consumer_id and o.properties->>'source_principal'=identity->'binding'->>'subject_principal_id'
   and o.properties->'active'='true'::jsonb order by o.object_id limit 2 loop n:=n+1;end loop;
  if n<>1 then raise exception 'effect catalog registration unavailable' using errcode='42501';end if;
  select * into offered from ontology.objects o where o.object_id=candidate.properties->>'offering_id' and o.tenant_id=p_tenant and o.world=p_world and o.type_name='ServiceOffering';
  if not found then raise exception 'effect catalog unavailable' using errcode='42501';end if;
  supply:=jsonb_build_object('offering_id',offered.object_id,'offering_revision',offered.nexloop_revision,'binding_id',candidate.object_id,
   'binding_revision',candidate.nexloop_revision,'provenance','eios:object:'||offered.object_id,'properties',offered.properties);
 end if;
 return jsonb_build_object('_source_digest',r.source_digest,'supply',supply,'consumer_id',ctx.consumer_id);
end $$;

-- authz.nexloop_scope_denial_command: latest body from 0056_scope_denials.sql; Run-keyed issuance/binding lookups find fallback Runs too.
create or replace function authz.nexloop_scope_denial_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;g jsonb;gp jsonb;command jsonb;catalog jsonb;pub control.nexloop_action_definitions;
 b runtime.nexloop_context_artifact_bindings;r runtime.nexloop_message_routes;cl runtime.nexloop_action_claims;old runtime.nexloop_scope_denials;
 sd text;pd text;rid text;output jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is distinct from 'real'
  or p_text is null or p_payload is null or octet_length(p_text)>1048576 or octet_length(p_payload)>131072
  or c->>'protocol' is distinct from 'nexloop-scope-denial-v1'
  or c->>'resource_id' is distinct from 'eios:action:nexloop.service.scope_denial.record:1'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
 then raise exception 'scope denial unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-scope-denial-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
 then raise exception 'scope denial unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'scope denial unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',c->>'tenant_id',true);
 select * into pub from control.nexloop_action_definitions where tenant_id=c->>'tenant_id' and world=p_world and resource_id=c->>'resource_id' and active for share;
 if not found or pub.definition is distinct from c->'definition' or pub.capability is distinct from c->'capability'
  or pub.definition->'governance'->>'approval_mode' is distinct from 'none' or pub.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(pub.definition->'governance'->'policy_refs') is distinct from 0
  or pub.definition->'input_schema' is distinct from authz.nexloop_scope_denial_schema()
  or jsonb_typeof(p) is distinct from 'object' or (select count(*) from jsonb_object_keys(p))<>3
  or jsonb_typeof(p->'run_id') is distinct from 'string' or p->>'run_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or jsonb_typeof(p->'parameters_text') is distinct from 'string' or octet_length(p->>'parameters_text')>65536
  or jsonb_typeof(p->'request_scope') is distinct from 'object'
  or c->'definition_reference'->>'stable_name' is distinct from 'nexloop.service.scope_denial.record'
  or c->'definition_reference'->>'tenant_id' is distinct from c->>'tenant_id'
  or c->'definition_reference'->>'contract_digest' is distinct from pub.definition->>'contract_digest'
  or c->'capability_binding' is distinct from pub.definition->'capability_binding'
 then raise exception 'scope denial contract unavailable' using errcode='42501';end if;
 -- Original guard owns Run advisory -> plan ledger -> binding locks. It
 -- validates Source Artifact/catalog, all Run rights, lease/fence/RDA/TTL.
 g:=c->'guard';gp:=(g->>'payload')::jsonb;command:=(gp->>'command_text')::jsonb;
 if gp->>'verb' is distinct from 'authorize' or gp->>'operation' is distinct from 'tool'
  or command->>'run_id' is distinct from p->>'run_id' then raise exception 'scope denial unavailable' using errcode='42501';end if;
 output:=authz.nexloop_runtime_activation_command(c->>'worker_digest',p_world,g->>'text',g->>'signature',g->>'payload');
 if output->'authorized' is distinct from 'true'::jsonb then raise exception 'scope denial unavailable' using errcode='42501';end if;
 select * into b from runtime.nexloop_run_binding(c->>'tenant_id',p_world,(p->>'run_id')::uuid,true);
 if not found or b.source_digest is distinct from p_digest or b.source_principal is distinct from c->>'principal_id' then raise exception 'scope denial unavailable' using errcode='42501';end if;
 select * into r from runtime.nexloop_message_routes where tenant_id=b.tenant_id and world=p_world and message_id=b.message_id;
 if not found or r.run_id is distinct from b.run_id or r.task_id is null
  or not exists(select 1 from runtime.nexloop_message_outbox where tenant_id=b.tenant_id and world=p_world and message_id=b.message_id and status='delivered')
 then raise exception 'scope denial unavailable' using errcode='42501';end if;
 catalog:=authz.nexloop_service_catalog_scope(p_digest,p_world,c->'catalog'->>'text',c->'catalog'->>'signature',c->'catalog'->>'payload');
 if catalog->'allowed' is distinct from 'false'::jsonb or catalog->>'reason' is distinct from 'outside_catalog_terms'
  or (c->'catalog'->>'payload')::jsonb->'request_scope' is distinct from p->'request_scope'
  or (c->'catalog'->>'payload')::jsonb->>'consumer_id' is distinct from substr(command->>'consumer_ref',10)
  or catalog->'supply' is distinct from b.pack_text::jsonb->'supply'
 then raise exception 'scope denial unavailable' using errcode='42501';end if;
 select * into cl from runtime.nexloop_action_claims where tenant_id=b.tenant_id and world=p_world and action_name='nexloop.service.scope_denial.record' and intent_id=c->>'claim_id' for update;
 if not found or cl.principal_id is distinct from c->>'principal_id'
  or cl.claim->'binding'->>'request_digest' is distinct from c->>'parameters_digest'
  or cl.claim->'binding'->'action_reference' is distinct from c->'definition_reference'
  or cl.claim->'binding'->'capability_binding' is distinct from c->'capability_binding'
  or cl.claim->>'state' is null or cl.claim->>'state' not in ('active','terminal')
  or cl.claim->>'state'='active' and (cl.claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or cl.claim->>'state'='terminal' and cl.claim->'terminal_outcome'->>'status' is distinct from 'succeeded'
 then raise exception 'scope denial claim unavailable' using errcode='42501';end if;
 sd:=encode(sha256(convert_to((p->'request_scope')::text,'UTF8')),'hex');pd:=encode(sha256(convert_to(p->>'parameters_text','UTF8')),'hex');
 rid:=encode(sha256(convert_to(b.tenant_id||':'||p_world||':'||b.run_id::text||':'||sd,'UTF8')),'hex');
 select * into old from runtime.nexloop_scope_denials where tenant_id=b.tenant_id and world=p_world and run_id=b.run_id and scope_digest=sd for update;
 if found then
  if old.source_principal is distinct from c->>'principal_id' or old.claim_id is distinct from c->>'claim_id' or old.request_digest is distinct from c->>'parameters_digest' or old.parameters_digest is distinct from pd or old.scope is distinct from catalog->'scope' or old.message_id is distinct from b.message_id then raise exception 'scope denial conflict' using errcode='23505';end if;
 else
  insert into runtime.nexloop_scope_denials values(b.tenant_id,p_world,b.run_id,b.message_id,substr(command->>'consumer_ref',10),sd,pd,rid,catalog->'scope',p->'request_scope',clock_timestamp(),c->>'principal_id',c->>'claim_id',c->>'parameters_digest') returning * into old;
 end if;
 if cl.claim->>'state'='terminal' and cl.claim->'terminal_outcome'->>'outcome_id' is distinct from old.record_id then raise exception 'scope denial claim unavailable' using errcode='42501';end if;
 -- A rejection is independently recorded, never replacing an existing Intent.
 perform authz.nexloop_runtime_activation_command(c->>'worker_digest',p_world,g->>'text',g->>'signature',g->>'payload');
 perform authz.nexloop_service_catalog_scope(p_digest,p_world,c->'catalog'->>'text',c->'catalog'->>'signature',c->'catalog'->>'payload');
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'scope denial expired' using errcode='42501';end if;
 return jsonb_build_object('record_id',old.record_id,'code','outside_catalog_terms','scope',old.scope,'recorded_at',old.recorded_at);
end $$;

-- authz.nexloop_read_local_artifact: latest body from 0099_nx023_context_v6_relationships_copy.sql; Run-keyed issuance/binding lookups find fallback Runs too.
create or replace function authz.nexloop_read_local_artifact(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare i jsonb;dep jsonb;d jsonb:=p_payload::jsonb->'context_dependency';r jsonb;name text;reference jsonb;env jsonb;a jsonb;permit jsonb;
begin
 i:=authz.nexloop_service_identity_snapshot(p_digest,p_world);perform set_config('eios.tenant_id',i->'binding'->>'tenant_id',true);
 dep:=authz.nexloop_context_v6_copy_dependency(i->'binding'->>'tenant_id',p_world,p_payload::jsonb->>'artifact_id',i->'binding'->>'subject_principal_id');
 if dep is null then return authz.nexloop_read_local_artifact_before_v6(p_digest,p_world,p_permit,p_payload);end if;
 if jsonb_typeof(d) is distinct from 'object' or d->>'kind' is distinct from 'v6' or d->>'run_id' is distinct from dep->>'run_id'
  or jsonb_typeof(d->'reads') is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(d->'reads') k) is distinct from (select array_agg(k order by k) from jsonb_object_keys(dep->'reads') k) then
  raise exception 'context v6 copy READ required' using errcode='42501';end if;
 -- Every source object, with exactly the fields the pack shows, under the reader's own current READ.
 for name,reference in select key,value from jsonb_each(dep->'reads') order by key loop
  env:=d->'reads'->name;a:=(env->>'text')::jsonb;
  if env->>'text' is null or env->>'signature' is null or a->>'type_name' is distinct from reference->>'type_name'
   or a->>'object_id' is distinct from reference->>'object_id' or a->'fields' is distinct from reference->'fields'
   or a->>'operation' is distinct from 'read' or a->>'principal_id' is distinct from i->'binding'->>'subject_principal_id' then
   raise exception 'context v6 copy READ required' using errcode='42501';end if;
  perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 select claims into permit from authz.nexloop_artifact_permits where tenant_id=r->>'tenant_id' and permit_id=p_permit;
 if permit is null or permit->>'expires_at' is null or (permit->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read expired' using errcode='42501';end if;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,permit);
 if not exists(select 1 from runtime.nexloop_all_context_bindings b where b.tenant_id=r->>'tenant_id' and b.world=p_world and b.artifact_id=r->>'artifact_id' and b.pack_digest=r->>'sha256'
   union all select 1 from runtime.nexloop_role_context_artifacts b where b.tenant_id=r->>'tenant_id' and b.world=p_world and b.artifact_id=r->>'artifact_id' and b.pack_digest=r->>'sha256')
  or r->>'retention_until' is null or (r->>'retention_until')::timestamptz<=clock_timestamp() then raise exception 'context artifact unavailable' using errcode='42501';end if;
 return r;
end $$;

-- authz.nexloop_context_v6_copy_dependency: latest body from 0099_nx023_context_v6_relationships_copy.sql; Run-keyed issuance/binding lookups find fallback Runs too.
create or replace function authz.nexloop_context_v6_copy_dependency(p_tenant text,p_world text,p_artifact text,p_principal text) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pack jsonb;run uuid;message text;source text;reads jsonb:='{}'::jsonb;x jsonb;n integer:=0;fields jsonb;consumer text;
begin
 select b.pack_text::jsonb,b.run_id,b.message_id,b.source_principal into pack,run,message,source from runtime.nexloop_all_context_bindings b
  where b.tenant_id=p_tenant and b.world=p_world and b.artifact_id=p_artifact;
 if not found then
  select r.pack_text::jsonb,r.run_id,null,r.source_principal into pack,run,message,source from runtime.nexloop_role_context_artifacts r
   where r.tenant_id=p_tenant and r.world=p_world and r.artifact_id=p_artifact;
  if not found then return null;end if;
 end if;
 if pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' then return null;end if;
 consumer:=(select f->>'id' from jsonb_array_elements(pack->'formal_facts') f where f->>'type'='Consumer');
 if message is not null then
  reads:=reads||jsonb_build_object('message',jsonb_build_object('type_name','Message','object_id',message,'fields','["actor","body"]'::jsonb));
 else
  -- Role trigger text is service-internal: only the producing Source copies a Role pack (as v3/v5).
  if source is distinct from p_principal then raise exception 'role Context source unavailable' using errcode='42501';end if;
  reads:=reads||jsonb_build_object(
   'role',jsonb_build_object('type_name','RoleDefinition','object_id',pack->'role'->'role_binding'->'binding'->>'role_id','fields','["active","ceiling_ref","name","responsibility","valid_from","valid_until"]'::jsonb),
   'link',jsonb_build_object('type_name','ConsumerRoleLink','object_id',pack->'role'->'role_binding'->'binding'->>'link_id','fields','["active","consumer_id","role_id","scope","valid_from","valid_until"]'::jsonb),
   'step',jsonb_build_object('type_name','PlanStep','object_id',pack->'role'->'role_binding'->'binding'->>'step_id','fields','["consumer_id","state","submitter_principals"]'::jsonb));
 end if;
 reads:=reads||jsonb_build_object(
  'offering',jsonb_build_object('type_name','ServiceOffering','object_id',pack->'supply'->>'offering_id','fields','["active","allowed_discounts","allowed_guarantees","content_kind","currency","delivery_action","eligibility","evidence_kind","price_amount","service_code","title","valid_until"]'::jsonb),
  'offering_binding',jsonb_build_object('type_name','ConsumerServiceOffering','object_id',pack->'supply'->>'binding_id','fields','["active","consumer_id","offering_id","offering_revision","source_principal"]'::jsonb));
 reads:=reads||(select jsonb_object_agg('fact_'||(f->>'type'),jsonb_build_object('type_name',f->>'type','object_id',f->>'id','fields',
   case when f->>'type'='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end)) from jsonb_array_elements(pack->'formal_facts') f);
 if jsonb_typeof(pack->'relationship_context')='object' then
  for x in select value from jsonb_array_elements((pack->'relationship_context'->'current_statements')||(pack->'relationship_context'->'evidence')) order by value->>'assessment_ref' loop
   n:=n+1;
   reads:=reads||jsonb_build_object('relationship_'||n,jsonb_build_object('type_name','RelationshipAssessment','object_id',substr(x->>'assessment_ref',36),
    'fields','["conclusion","corrects_revision","epistemic_kind","evidence_content_hash","evidence_message_id","relation_name","relation_version","resolution_state","source_id","source_type","target_id","target_type","valid_from","valid_to"]'::jsonb));
  end loop;
 end if;
 -- Quoted Claims: the Conversation they were read under (same basis as at assembly).
 reads:=reads||coalesce((select jsonb_object_agg('conversation_'||c.conversation_id,jsonb_build_object('type_name','Conversation','object_id',c.conversation_id,'fields','[]'::jsonb))
  from (select distinct x2.conversation_id from jsonb_array_elements(pack->'evidence') i join ontology.nexloop_claims x2
   on x2.tenant_id=p_tenant and x2.world=p_world and x2.claim_id=substr(i->>'ref',7) where i->>'ref' like 'claim:%') c),'{}'::jsonb);
 -- Consumer open work and formal state: Consumer READ plus every shown property.
 if jsonb_array_length(pack->'open_work')>0 or jsonb_array_length(pack->'consumer_state')>0 or jsonb_array_length(pack->'constraints')>0 then
  select coalesce(jsonb_agg(distinct k order by k),'[]'::jsonb) into fields from (select jsonb_object_keys(i->'content'->'properties') k
   from jsonb_array_elements((pack->'consumer_state')||(pack->'constraints')) i where jsonb_typeof(i->'content'->'properties')='object') q;
  reads:=reads||jsonb_build_object('consumer_items',jsonb_build_object('type_name','Consumer','object_id',consumer,'fields',fields));
 end if;
 return jsonb_build_object('kind','v6','run_id',run,'reads',reads);
end $$;

-- authz.nexloop_effect_intent_command_before_role_policy_v0081: 0079 body; fallback Runs bind to their issuance and may only submit a reply.
create or replace function authz.nexloop_effect_intent_command_before_role_policy_v0081(d text,w text,t text,s text,body text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=t::jsonb;p jsonb:=body::jsonb;result jsonb;i runtime.nexloop_effect_intents%rowtype;run uuid;issued authz.nexloop_message_run_issuances%rowtype;
 cm runtime.nexloop_conversation_messages%rowtype;reply text;fallback boolean;v_message text;
begin
 -- ADR-023 §2.7: a fallback reply Run has one tool only, a reply bound to its message (server-side, not Host config).
 select run_id into run from authz.nexloop_run_credentials where token_digest=d;
 -- Tenant context of the Run itself (RLS), before deciding whether it is a fallback Run.
 perform set_config('eios.tenant_id',authz.nexloop_service_identity_snapshot(d,w)->'binding'->>'tenant_id',true);
 fallback:=authz.nexloop_is_fallback_run(run);
 if fallback and p->>'verb' is distinct from 'submit' then raise exception 'fallback reply Run: only the bound reply' using errcode='42501';end if;
 result:=authz.nexloop_effect_intent_command_before_outbound_v0077(d,w,t,s,body);
 if p->>'verb' is distinct from 'submit' or result->>'intent_id' is null then return result;end if;
 select * into i from runtime.nexloop_effect_intents where intent_id=(result->>'intent_id')::uuid;
 reply:=i.frozen_request->'parameters'->>'message';
 if reply is null or jsonb_typeof(i.frozen_request->'parameters'->'message') is distinct from 'string' then
  if fallback then raise exception 'fallback reply Run: only the bound reply' using errcode='42501';end if;
  return result;end if;
 select message_id into v_message from authz.nexloop_message_run_issuances where run_id=run and tenant_id=i.tenant_id and world=i.world;
 if not found then
  select message_id into v_message from authz.nexloop_message_fallback_issuances where run_id=run and tenant_id=i.tenant_id and world=i.world;
  if not found then return result;end if;  -- not a conversation reply Run
 end if;
 select * into cm from runtime.nexloop_conversation_messages where tenant_id=i.tenant_id and world=i.world and message_id=v_message;
 if not found then raise exception 'outbound trigger message unavailable' using errcode='42501';end if;
 insert into runtime.nexloop_outbound_messages(tenant_id,world,intent_id,receipt_id,conversation_id,trigger_message_id,run_id,sender_kind,sender_principal,role_ref,body_digest)
  values(i.tenant_id,i.world,i.intent_id,i.receipt_id,cm.conversation_id,cm.message_id,run,'agent',c->>'principal_id',
   coalesce(c->'role_envelope'->>'role_ref',''),encode(sha256(convert_to(reply,'UTF8')),'hex'))
  on conflict(tenant_id,world,intent_id) do nothing;
 return result;
end $$;

-- 4. Fallback issuance ---------------------------------------------------------------------------------------------
create function authz.nexloop_reply_fallback_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-reply-fallback-v1','eios:action:nexloop.reply.fallback:1',array['nexloop_api']);
 c jsonb:=p_payload::jsonb;a jsonb:=p_text::jsonb;v_principal text:=a->>'principal_id';v_out runtime.nexloop_message_outbox;v_conv runtime.nexloop_conversations;
 v_issued authz.nexloop_message_fallback_issuances;v_item runtime.nexloop_work_feed;v_assignment ontology.objects;v_goal ontology.objects;v_step ontology.objects;
 v_control ontology.objects;v_consumer ontology.objects;v_ctl control.nexloop_effect_control_ledger;v_source_def control.nexloop_action_definitions;
 v_props jsonb;v_root jsonb;v_proof jsonb;v_name text;v_seconds int;v_ts timestamptz;v_expiry timestamptz;
begin
 perform set_config('eios.tenant_id',v_tenant,true);
 if c->>'message_id' is null or c->>'message_id'!~'^[a-f0-9]{64}$' or c->>'verb' not in ('issue','lookup') then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for share;
 if not found then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_conv from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=v_out.conversation_id for share;
 if not found then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 -- Serializes against settlement (it deletes this row when a bound reply is accepted) and against a second issuance.
 select * into v_item from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed='reply-due' and item_key='reply:'||v_out.message_id for update;
 select * into v_issued from authz.nexloop_message_fallback_issuances where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
 if c->>'verb'='lookup' then
  if v_issued.message_id is null then return null;end if;
  return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at);
 end if;
 if v_issued.message_id is null then
  if v_item.item_key is null or v_item.available_at>clock_timestamp() then raise exception 'reply fallback not due' using errcode='40001';end if;
  if exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=v_tenant and o.world=p_world and o.trigger_message_id=v_out.message_id
    and o.delivery_state in ('provider_accepted','delivered')) then raise exception 'reply already settled' using errcode='40001';end if;
 end if;
 -- Same Run-issuance inputs and MessageAssignment checks as 0049 'issue'.
 if jsonb_typeof(c->'run_id') is distinct from 'string' or c->>'run_id'!~'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
  or c->>'source_digest' is null or c->>'source_digest'!~'^[a-f0-9]{64}$' or c->>'assignment_id' is null or c->>'assignment_id'!~'^[a-f0-9]{64}$'
  or c->>'assignment_revision' is distinct from '1' or c->>'assignment_digest' is null or c->>'assignment_digest'!~'^[a-f0-9]{64}$'
  or c->>'token_digest' is null or c->>'token_digest'!~'^[a-f0-9]{64}$' or c->>'issuance_nonce' is null or c->>'issuance_nonce'!~'^[a-f0-9]{64}$'
  or jsonb_typeof(c->'ttl_seconds') is distinct from 'number' or c->>'ttl_seconds'!~'^[0-9]{1,3}$'
  or jsonb_typeof(c->'source_proofs') is distinct from 'array' or jsonb_array_length(c->'source_proofs')<>1
  or c->>'request_id' is distinct from 'reply-fallback-'||encode(sha256(convert_to('["'||v_tenant||'","real","'||v_out.message_id||'"]','UTF8')),'hex') then
  raise exception 'reply fallback unavailable' using errcode='42501';end if;
 v_seconds=(c->>'ttl_seconds')::int;
 if v_seconds not between 1 and 300 then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_assignment from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'assignment_id' and type_name='MessageAssignment' for share;
 if not found or v_assignment.nexloop_revision<>1 then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 v_props=v_assignment.properties;
 if jsonb_typeof(v_props) is distinct from 'object' or (select count(*) from jsonb_object_keys(v_props))<>15
  or not(v_props ?& array['message_id','consumer_id','goal_id','goal_revision','step_id','step_revision','control_id','control_revision','consumer_revision','source_principal','executor_principal','recipe_digest','allowed_actions','state','valid_until']) then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 for v_name in select unnest(array['message_id','consumer_id','goal_id','step_id','control_id','recipe_digest']) loop
  if jsonb_typeof(v_props->v_name) is distinct from 'string' or v_props->>v_name!~'^[a-f0-9]{64}$' then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 end loop;
 for v_name in select unnest(array['goal_revision','step_revision','control_revision','consumer_revision']) loop
  if jsonb_typeof(v_props->v_name) is distinct from 'number' or v_props->>v_name!~'^[1-9][0-9]{0,17}$' then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 end loop;
 if jsonb_typeof(c->'assignment_text') is distinct from 'string' or (c->>'assignment_text')::jsonb is distinct from v_props
  or c->>'assignment_digest' is distinct from encode(sha256(convert_to(c->>'assignment_text','UTF8')),'hex')
  or v_props->>'message_id' is distinct from v_out.message_id or v_props->>'consumer_id' is distinct from v_conv.consumer_id or v_props->>'state' is distinct from 'active'
  or v_props->'allowed_actions' is distinct from '["eios:action:nexloop.service.request:1"]'::jsonb or (v_props->>'valid_until')::timestamptz<=clock_timestamp()
  or v_props->>'source_principal' is distinct from authz.nexloop_root_identity_snapshot(c->>'source_digest',p_world)->'binding'->>'subject_principal_id' then
  raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_consumer from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_conv.consumer_id and type_name='Consumer' for share;
 if not found or v_consumer.nexloop_revision<>(v_props->>'consumer_revision')::bigint then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_goal from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'goal_id' and type_name='Goal' for share;
 if not found or v_goal.nexloop_revision<>(v_props->>'goal_revision')::bigint or v_goal.properties->>'consumer_id' is distinct from v_conv.consumer_id
  or v_goal.properties->>'state' is distinct from 'active' or (v_goal.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_step from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'step_id' and type_name='PlanStep' for share;
 if not found or v_step.nexloop_revision<>(v_props->>'step_revision')::bigint or v_step.properties->>'consumer_id' is distinct from v_conv.consumer_id
  or v_step.properties->>'goal_id' is distinct from v_goal.object_id or v_step.properties->>'control_id' is distinct from v_props->>'control_id'
  or v_step.properties->>'state' is distinct from 'ready' or v_step.properties->>'action_name' is distinct from 'nexloop.service.request'
  or v_step.properties->'submitter_principals' is distinct from jsonb_build_array(v_props->>'source_principal') then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_control from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'control_id' and type_name='EffectControl' for share;
 if not found or v_control.nexloop_revision<>(v_props->>'control_revision')::bigint or v_control.properties->>'consumer_id' is distinct from v_conv.consumer_id
  or v_control.properties->'allow_effect' is distinct from 'true'::jsonb or v_control.properties->>'executor_principal' is distinct from v_props->>'executor_principal'
  or (v_control.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world and control_id=v_control.object_id for share;
 if not found or v_ctl.valid_until is null or v_ctl.consumer_id is distinct from v_conv.consumer_id or v_ctl.control_revision<>v_control.nexloop_revision
  or v_ctl.executor_principal is distinct from v_props->>'executor_principal' or v_ctl.valid_until<=clock_timestamp() then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 perform 1 from authz.nexloop_service_credentials where token_digest=c->>'source_digest' for share;
 if not found then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 v_root=authz.nexloop_root_identity_snapshot(c->>'source_digest',p_world);
 if jsonb_typeof(v_root->'expires_at') is distinct from 'string' or v_root->>'directory_hash' is null or v_root->'binding'->>'tenant_id' is distinct from v_tenant then
  raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_source_def from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id='eios:action:nexloop.service.request:1' and active for share;
 if not found or v_source_def.definition is distinct from c->'source_definition' or v_source_def.capability is distinct from c->'source_capability' then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 v_proof=c->'source_proofs'->0;
 if v_proof->>'resource_id' is distinct from 'eios:action:nexloop.service.request:1' then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 if v_issued.message_id is not null then
  -- Replay of the same issuance only; never a second fallback Run for this message.
  if v_issued.run_id is distinct from (c->>'run_id')::uuid or v_issued.run_digest is distinct from c->>'token_digest' or v_issued.issuance_nonce is distinct from c->>'issuance_nonce'
   or v_issued.assignment_id is distinct from v_assignment.object_id or v_issued.assignment_digest is distinct from c->>'assignment_digest' or v_issued.issuer_principal is distinct from v_principal then
   raise exception 'reply fallback already issued' using errcode='23505';end if;
 else
  v_ts=clock_timestamp();v_expiry=least((v_root->>'expires_at')::timestamptz,v_ts+make_interval(secs=>v_seconds),(v_props->>'valid_until')::timestamptz,(v_goal.properties->>'valid_until')::timestamptz,v_ctl.valid_until);
  insert into authz.nexloop_run_credentials values(c->>'token_digest',(c->>'run_id')::uuid,c->>'source_digest',v_root->>'directory_hash',p_world,'nexloop-agent-host',array['eios:action:nexloop.service.request:1'],'active',v_ts,v_expiry);
  insert into authz.nexloop_message_fallback_issuances values(v_tenant,p_world,v_out.message_id,(c->>'run_id')::uuid,c->>'request_id',c->>'issuance_nonce',v_assignment.object_id,
   v_assignment.nexloop_revision,c->>'assignment_digest',v_principal,c->>'token_digest',v_ts,v_expiry) returning * into v_issued;
 end if;
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at,'replay',v_issued.issued_at<transaction_timestamp());
end $$;
alter function authz.nexloop_reply_fallback_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_reply_fallback_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_reply_fallback_command(text,text,text,text,text) to nexloop_api;

-- 5. Fallback Context v6: the 0053/0062 core and the 0105 bind over the fallback issuance and binding -------------
-- authz.nexloop_context_artifact_fallback_core: 0053 core body over the fallback issuance and binding.
create function authz.nexloop_context_artifact_fallback_core(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;tenant text;principal text;
 command jsonb:=p->'command';context_uuid uuid;outbox runtime.nexloop_message_outbox;conv runtime.nexloop_conversations;
 issued authz.nexloop_message_fallback_issuances;plan control.nexloop_effect_plan_bindings;ctx control.nexloop_effect_contexts;
 ctl control.nexloop_effect_control_ledger;artifact runtime.nexloop_local_artifacts;b runtime.nexloop_fallback_context_bindings;
 pub control.nexloop_action_definitions;cl runtime.nexloop_action_claims;snapshot jsonb;facts jsonb;namespace text;binding jsonb;pack jsonb;proof jsonb;
 catalog_env jsonb;catalog_result jsonb;
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
 select * into issued from authz.nexloop_message_fallback_issuances where tenant_id=tenant and world=p_world and message_id=outbox.message_id for share;
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
 select * into b from runtime.nexloop_fallback_context_bindings where tenant_id=tenant and world=p_world and message_id=outbox.message_id for update;
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
  insert into runtime.nexloop_fallback_context_bindings values(tenant,p_world,outbox.message_id,issued.run_id,principal,p_digest,context_uuid,artifact.artifact_id,namespace,p->>'pack_text',p->>'pack_digest',binding,clock_timestamp()) on conflict do nothing;
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
end $$;
alter function authz.nexloop_context_artifact_fallback_core(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_fallback_core(text,text,text,text,text) from public;

create function authz.nexloop_context_artifact_fallback_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;v jsonb;
begin
 perform authz.nexloop_check_context_message_read(p_digest,p_world,a->'message_read_envelope',p->>'message_id');
 v:=authz.nexloop_context_artifact_fallback_core(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_check_context_message_read(p_digest,p_world,a->'message_read_envelope',p->>'message_id');
 return v;
end $$;
alter function authz.nexloop_context_artifact_fallback_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_fallback_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_context_artifact_fallback_command(text,text,text,text,text) to nexloop_api;

-- authz.nexloop_context_v6_fallback_command: 0105 message bind over the fallback core and binding.
create function authz.nexloop_context_v6_fallback_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb;k bytea;ident jsonb;tenant text;principal text;ca jsonb;cp jsonb;snapshot jsonb;pack jsonb;
 b runtime.nexloop_fallback_context_bindings;artifact runtime.nexloop_local_artifacts;cl runtime.nexloop_action_claims;
 conv runtime.nexloop_conversations;consumer text;proof jsonb;key text;fresh boolean;namespace text;context_uuid uuid;run uuid;rel boolean;core jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
  or a->>'protocol' is distinct from 'nexloop-context-v6-v1' or a->>'resource_id' is distinct from 'eios:action:nexloop.context.assemble:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-v6-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 p:=p_payload::jsonb;
 if p->>'verb' is distinct from 'bind' or jsonb_typeof(p->'core') is distinct from 'object' or jsonb_typeof(p->'omitted_items') is distinct from 'array'
  or jsonb_array_length(p->'omitted_items')>1024 or p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 then raise exception 'context v6 unavailable' using errcode='42501';end if;
 ca:=(p->'core'->>'text')::jsonb;cp:=(p->'core'->>'payload')::jsonb;
 if cp->>'verb' is distinct from 'snapshot' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 pack:=(p->>'pack_text')::jsonb;rel:=pack ? 'relationship_context';
 -- Core: the frozen v2 chain (or the v4 chain when the pack carries relationship assessments)
 -- re-derives and authorizes its sections under its own signed envelope.
 -- Fallback reply Runs use the v2 core (no relationship section).
 if rel then raise exception 'context v6 fallback: relationship core unsupported' using errcode='42501';end if;
 snapshot:=authz.nexloop_context_artifact_fallback_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');
 if p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') or runtime.nexloop_canonical_json(pack) is distinct from p->>'pack_text'
  or pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' or (select count(*) from jsonb_object_keys(pack))<>(case when rel then 19 else 18 end)
  or not pack ?& array['strategy_ref','bindings','role','current_event','user_statement','goal','formal_facts','current_constraints','supply','constraints','consumer_state',
   'open_work','evidence','semantics','experience','budget_report','insufficient'] then raise exception 'context v6 pack invalid' using errcode='42501';end if;
 foreach key in array array['bindings','user_statement','formal_facts','current_constraints','supply']||case when rel then array['relationship_context'] else array[]::text[] end loop
  if pack->key is distinct from snapshot->key then raise exception 'context v6 core mismatch' using errcode='42501';end if;
 end loop;
 if pack->'role' is distinct from 'null'::jsonb then raise exception 'context v6 Role section unsupported' using errcode='42501';end if;
 if pack->'current_event' is distinct from jsonb_build_object('kind','consumer_message','message_id',pack->'user_statement'->'message_id','provenance',pack->'user_statement'->'provenance') then
  raise exception 'context v6 current event mismatch' using errcode='42501';end if;
 context_uuid:=(pack->'bindings'->>'context_id')::uuid;run:=(pack->'bindings'->>'run_id')::uuid;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=pack->'user_statement'->>'conversation_id' for share;
 select value->>'id' into consumer from jsonb_array_elements(pack->'formal_facts') where value->>'type'='Consumer';
 if not found or conv.consumer_id is distinct from consumer then raise exception 'context v6 consumer mismatch' using errcode='42501';end if;
 -- A bound pack replays unchanged: it was fully verified when bound (its read proofs have
 -- since expired); only the Artifact, the bind claim and the core above are rechecked.
 select * into b from runtime.nexloop_fallback_context_bindings where tenant_id=tenant and world=p_world and message_id=pack->'user_statement'->>'message_id' for update;
 fresh:=not found;
 if not fresh and (b.pack_text is distinct from p->>'pack_text' or b.run_id is distinct from run) then raise exception 'context artifact conflict' using errcode='23505';end if;
 if fresh then
  -- Core sources: what the frozen chain re-derived, cited under the envelope it verified;
  -- v4 relationship rows keep their v4 meaning (hypotheses are evidence only).
  core:=jsonb_build_array(
    jsonb_build_object('section','current_event','ref','eios:object:Message/'||(pack->'user_statement'->>'message_id'),'revision',pack->'user_statement'->>'sequence',
     'hash',runtime.nexloop_context_hash(pack->'user_statement'),'kind','current_message','decision',runtime.nexloop_context_hash(coalesce(ca->'message_read_envelope',ca->'trigger_message_envelope'))),
    jsonb_build_object('section','constraints','ref','eios:action:nexloop.service.request:1','revision',runtime.nexloop_context_hash(pack->'current_constraints'),
     'hash',runtime.nexloop_context_hash(pack->'current_constraints'),'kind','policy','decision',runtime.nexloop_context_hash(ca)),
    jsonb_build_object('section','constraints','ref','eios:object:'||(pack->'supply'->>'offering_id'),'revision',pack->'supply'->>'offering_revision',
     'hash',runtime.nexloop_context_hash(pack->'supply'),'kind','formal_object','decision',runtime.nexloop_context_hash(ca->'catalog_envelope')))
   ||coalesce((select jsonb_agg(jsonb_build_object('section','constraints','ref','eios:object:'||(f->>'type')||'/'||(f->>'id'),'revision',f->>'revision',
     'hash',runtime.nexloop_context_hash(f),'kind','formal_object','decision',runtime.nexloop_context_hash(ca))) from jsonb_array_elements(pack->'formal_facts') f),'[]'::jsonb)
   ||case when rel then coalesce((select jsonb_agg(jsonb_build_object('section','evidence','ref',x->>'assessment_ref','revision',x->>'revision','hash',runtime.nexloop_context_hash(x),
     'kind',x->>'epistemic_kind','decision',runtime.nexloop_context_hash(ca->'relationship_envelopes')))
     from jsonb_array_elements((pack->'relationship_context'->'current_statements')||(pack->'relationship_context'->'evidence')) x),'[]'::jsonb) else '[]'::jsonb end;
  perform authz.nexloop_context_v6_sections(p_digest,p_world,tenant,principal,a,p,pack,array[conv.conversation_id],consumer,run,context_uuid,core);
 end if;
 -- Artifact and governed bind claim: same rules as the frozen bind, against the v6 bytes.
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and artifact_id=p->>'artifact_id' for share;
 if not found or artifact.artifact_id is distinct from pack->'bindings'->>'artifact_id' or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal
  or artifact.sha256 is distinct from p->>'pack_digest' or artifact.size_bytes is distinct from octet_length(p->>'pack_text')
  or artifact.media_type is distinct from 'application/vnd.nexloop.context+json' or artifact.retention_until<=clock_timestamp()
  or artifact.object_key is distinct from namespace||'/'||artifact.artifact_id then raise exception 'context artifact unavailable' using errcode='42501';end if;
 select * into cl from runtime.nexloop_action_claims where tenant_id=tenant and world=p_world and action_name='nexloop.context.bind' and intent_id=p->>'claim_id' for share;
 if not found or cl.principal_id is distinct from principal or cl.claim->'binding' is distinct from ca->'claim_binding' or cl.claim->>'state' not in ('active','terminal')
  or (cl.claim->>'state'='active' and (cl.claim->>'lease_expires_at')::timestamptz<=clock_timestamp())
  or (cl.claim->>'state'='terminal' and (fresh or cl.claim->'terminal_outcome'->>'status' is distinct from 'succeeded' or cl.claim->'terminal_outcome'->>'outcome_digest' is distinct from artifact.sha256)) then
  raise exception 'context artifact unavailable' using errcode='42501';end if;
 if fresh then
  insert into runtime.nexloop_fallback_context_bindings values(tenant,p_world,pack->'user_statement'->>'message_id',run,principal,p_digest,context_uuid,artifact.artifact_id,namespace,
   p->>'pack_text',p->>'pack_digest',authz.nexloop_context_command_binding(cp->'command'),clock_timestamp());
 elsif not exists(select 1 from runtime.nexloop_context_packs c where c.tenant_id=tenant and c.world=p_world and c.context_id=context_uuid and c.run_id=run and c.pack_digest=p->>'pack_digest') then
  raise exception 'context artifact conflict' using errcode='23505';
 end if;
 -- Tail: every authority used above is still current at return.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read proof expired' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 return jsonb_build_object('context_id',context_uuid,'run_id',run,'pack_digest',p->>'pack_digest','strategy_ref',pack->>'strategy_ref','bound',true);
end $$;
alter function authz.nexloop_context_v6_fallback_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_v6_fallback_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_context_v6_fallback_command(text,text,text,text,text) to nexloop_api;

-- 6. Dispatch: 0109 bound-reply rules, now also for every fallback reply, and mutual exclusion with settlement --------
create or replace function control.nexloop_contact_assert_intent(p_tenant text,p_world text,p_intent uuid) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;o runtime.nexloop_outbound_messages;
 m runtime.nexloop_conversation_messages;restricted boolean;fallback boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if not found then raise exception 'intent unavailable' using errcode='42501';end if;
 restricted:=exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant and r.world=p_world and r.consumer_id=i.consumer_id and r.active);
 select * into o from runtime.nexloop_outbound_messages where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 fallback:=found and authz.nexloop_is_fallback_run(o.run_id);
 if restricted or fallback then
  if o.intent_id is null then raise exception 'contact restricted: outreach is not a reply to an inbound message' using errcode='NXC05';end if;
  select cm.* into m from runtime.nexloop_conversation_messages cm join runtime.nexloop_conversations cv
   on cv.tenant_id=cm.tenant_id and cv.world=cm.world and cv.conversation_id=cm.conversation_id
   where cm.tenant_id=p_tenant and cm.world=p_world and cm.conversation_id=o.conversation_id and cm.message_id=o.trigger_message_id
    and cv.consumer_id=i.consumer_id and not (cm.record ? 'direction') and cm.idempotency_key not like 'agent-%';
  if not found then raise exception 'contact restricted: reply not bound to an inbound message of this consumer' using errcode='NXC05';end if;
  if fallback and not exists(select 1 from authz.nexloop_message_fallback_issuances f where f.run_id=o.run_id and f.message_id=m.message_id) then
   raise exception 'fallback reply bound to another message' using errcode='NXC05';end if;
  if (m.record->>'accepted_at')::timestamptz+make_interval(secs=>control.nexloop_reply_window_seconds())<=clock_timestamp() then
   raise exception 'contact restricted: reply window closed' using errcode='NXC05';end if;
  perform pg_advisory_xact_lock(hashtextextended(p_tenant||':'||p_world||':reply:'||m.message_id,0));
  if exists(select 1 from runtime.nexloop_outbound_messages x where x.tenant_id=p_tenant and x.world=p_world and x.trigger_message_id=m.message_id
    and x.intent_id<>p_intent and x.delivery_state in ('dispatching','provider_accepted','delivered','unknown')) then
   raise exception 'inbound message already answered' using errcode='NXC05';end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;

-- 7. Reply guarantee port: 0109 body; the state now reports the fallback issuance.
create or replace function authz.nexloop_reply_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-reply-guarantee-v1','eios:action:nexloop.reply.guarantee:1',
  array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;m runtime.nexloop_conversation_messages;v_consumer text;
begin
 perform set_config('eios.tenant_id',t,true);
 select cm.* into m from runtime.nexloop_conversation_messages cm where cm.tenant_id=t and cm.world=p_world and cm.message_id=c->>'message_id'
  and not (cm.record ? 'direction') and cm.idempotency_key not like 'agent-%';
 if not found then raise exception 'reply message unavailable' using errcode='42501';end if;
 select consumer_id into v_consumer from runtime.nexloop_conversations where tenant_id=t and world=p_world and conversation_id=m.conversation_id;
 if c->>'verb'='state' then
  return jsonb_build_object('message_id',m.message_id,'conversation_id',m.conversation_id,'consumer_id',v_consumer,
   'accepted_at',m.record->>'accepted_at',
   'settled',exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
     and o.delivery_state in ('provider_accepted','delivered')),
   'in_flight',exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
     and o.delivery_state in ('persisted','dispatching','unknown')),
   'restricted',exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=t and r.world=p_world and r.consumer_id=v_consumer and r.active),
   'escalated',exists(select 1 from control.nexloop_reply_escalations e where e.tenant_id=t and e.world=p_world and e.message_id=m.message_id),
   'fallback_run_id',(select f.run_id from authz.nexloop_message_fallback_issuances f where f.tenant_id=t and f.world=p_world and f.message_id=m.message_id));
 elsif c->>'verb'='escalate' then
  if coalesce(c->>'reason','')!~'^[a-z_]{1,64}$' or jsonb_typeof(c->'detail') is distinct from 'object' then raise exception 'reply escalation invalid' using errcode='22023';end if;
  -- Evidence only for a message still unanswered: a settled message is never escalated.
  if exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
    and o.delivery_state in ('provider_accepted','delivered')) then raise exception 'reply already settled' using errcode='40001';end if;
  insert into control.nexloop_reply_escalations(tenant_id,world,message_id,conversation_id,consumer_id,reason,detail)
   values(t,p_world,m.message_id,m.conversation_id,v_consumer,c->>'reason',c->'detail') on conflict do nothing;
  return jsonb_build_object('message_id',m.message_id,'escalated',true);
 end if;
 raise exception 'reply command invalid' using errcode='22023';
end $$;
