-- NX-018 v5 Context: the Role context producer appends the Run's governed policy
-- provenance (role_policy) and publishes wire nexloop.context-pack.v5. Spliced at the one
-- point where the v3 snapshot is built (0063 body, now the private
-- *_before_role_ttl_v0063 beneath the 0064/0075/0085 wrappers): the rest of the function
-- is byte-identical to 0063, so v5 cannot drift from v3. The section reads the Run's
-- immutable policy binding and the Ceiling/Scope at the revision bound at issuance; an
-- edited, missing or expired policy fails closed. A policy never grants authority.

create function authz.nexloop_role_policy_context_section(p_run uuid,p_tenant text,p_world text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare b authz.nexloop_role_policy_bindings;ceiling ontology.objects;scope ontology.objects;
begin
 -- Own tenant context: a hidden binding must never silently degrade v5 to v3.
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into b from authz.nexloop_role_policy_bindings where run_id=p_run for share;
 if not found then return null;end if;
 if b.tenant_id is distinct from p_tenant or b.world is distinct from p_world or b.expires_at<=clock_timestamp() then
  raise exception 'Role policy Context unavailable' using errcode='42501';end if;
 select * into ceiling from ontology.objects where tenant_id=b.tenant_id and world=p_world and type_name='RoleExecutionCeiling' and object_id=b.ceiling_id for share;
 if not found or ceiling.nexloop_revision is distinct from b.ceiling_revision or ceiling.properties->'active' is distinct from 'true'::jsonb then
  raise exception 'Role ceiling Context changed' using errcode='42501';end if;
 select * into scope from ontology.objects where tenant_id=b.tenant_id and world=p_world and type_name='RoleAssignmentScope' and object_id=b.scope_id for share;
 if not found or scope.nexloop_revision is distinct from b.scope_revision or scope.properties->'active' is distinct from 'true'::jsonb then
  raise exception 'Role scope Context changed' using errcode='42501';end if;
 return jsonb_build_object('binding',to_jsonb(b),'ceiling',ceiling.properties,'scope',scope.properties,
  'ceiling_provenance','eios:object:'||b.ceiling_id,'scope_provenance','eios:object:'||b.scope_id,'grants_authority',false);
end $$;
alter function authz.nexloop_role_policy_context_section(uuid,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_context_section(uuid,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

create or replace function authz.nexloop_role_context_command_before_role_ttl_v0063(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare policy_section jsonb;a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb:=p->'command';k bytea;ident jsonb;tenant text;principal text;
 run authz.nexloop_run_credentials;e runtime.nexloop_role_trigger_events;b runtime.nexloop_role_context_artifacts;
 pub control.nexloop_action_definitions;proof jsonb;role jsonb;catalog jsonb;env jsonb;ctx control.nexloop_effect_contexts;
 plan control.nexloop_effect_plan_bindings;ctl control.nexloop_effect_control_ledger;facts jsonb;snapshot jsonb;binding jsonb;
 artifact runtime.nexloop_local_artifacts;formal_name text;namespace text;chosen_artifact_id text;context_uuid uuid;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>131072 or p->>'verb' is null or p->>'verb' not in('stage','snapshot','bind')
  or a->>'protocol' is distinct from 'nexloop-role-context-v1' or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'resource_id' is distinct from 'eios:action:nexloop.context.bind_role:1'
 then raise exception 'role context unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active for share;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-role-context-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'role context unavailable' using errcode='42501';end if;
 if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Action proof expired' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb then raise exception 'Source required' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 select * into pub from control.nexloop_action_definitions where tenant_id=tenant and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or pub.definition is distinct from a->'definition' or pub.capability is distinct from a->'capability'
  or pub.definition->'governance'->>'approval_mode' is distinct from 'none' or pub.definition->'governance'->>'risk_level' is distinct from 'low'
  or pub.definition->'input_schema' is distinct from '{"type":"object","properties":{"run_id":{"type":"string","format":"uuid"},"trigger_event_id":{"type":"string","format":"uuid"},"body":{"type":"string","minLength":1,"maxLength":8192}},"required":["run_id","trigger_event_id","body"],"additionalProperties":false}'::jsonb
  or pub.definition->'parameters' is distinct from '[]'::jsonb or pub.definition->'preconditions' is distinct from '[]'::jsonb
  or pub.definition->'governance'->'policy_refs' is distinct from '[]'::jsonb
 then raise exception 'role context contract unavailable' using errcode='42501';end if;
 select * into run from authz.nexloop_run_credentials where run_id=(command->>'run_id')::uuid for share;
 if not found or run.source_digest is distinct from p_digest or run.world is distinct from p_world then raise exception 'role Source Run mismatch' using errcode='42501';end if;
 perform authz.nexloop_service_identity_snapshot(run.token_digest,p_world);
 if jsonb_typeof(a->'run_proofs') is distinct from 'array' or jsonb_array_length(a->'run_proofs')<1 then raise exception 'Run proof required' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'run_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Run proof expired' using errcode='42501';end if;perform authz.nexloop_assert_action_authority(run.token_digest,p_world,proof);end loop;
 env:=a->'role_envelope';role:=authz.nexloop_role_run_current(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if role->'binding'->>'run_id' is distinct from run.run_id::text or command->>'role_ref' is distinct from role->'binding'->>'role_ref'
  or command->>'consumer_ref' is distinct from 'consumer:'||(role->'binding'->>'consumer_id') or command->>'tenant_id' is distinct from tenant
  or command->>'world_id' is distinct from p_world or command->>'mode' is distinct from 'real' or command->>'credential_ref' is distinct from 'run:'||run.run_id::text
  or jsonb_typeof(p->'body') is distinct from 'string' or length(p->>'body') not between 1 and 8192
  then raise exception 'role trigger unavailable' using errcode='42501';end if;
 binding:=authz.nexloop_context_command_binding(command);
 if binding is distinct from (p->>'command_binding_text')::jsonb or p->>'command_binding_digest' is distinct from encode(sha256(convert_to(p->>'command_binding_text','UTF8')),'hex') then raise exception 'role command binding unavailable' using errcode='42501';end if;
 if p->>'verb'='stage' then
  insert into runtime.nexloop_role_trigger_events values((command->>'trigger_event_id')::uuid,run.run_id,tenant,p_world,principal,p_digest,p->>'body',binding,clock_timestamp()) on conflict do nothing;
 end if;
 select * into e from runtime.nexloop_role_trigger_events where run_id=run.run_id for share;
 if not found or e.source_digest is distinct from p_digest or e.tenant_id is distinct from tenant or e.world is distinct from p_world
  or e.source_principal is distinct from principal or e.event_id is distinct from (command->>'trigger_event_id')::uuid
  or e.body is distinct from p->>'body' or e.command_binding is distinct from binding then raise exception 'durable role trigger conflict' using errcode='23505';end if;
 if p->>'verb'='stage' then
  if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Action proof expired' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  perform authz.nexloop_role_run_current(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  for proof in select value from jsonb_array_elements(a->'run_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Run proof expired' using errcode='42501';end if;perform authz.nexloop_assert_action_authority(run.token_digest,p_world,proof);end loop;
  return jsonb_build_object('event_id',e.event_id,'run_id',e.run_id,'persisted',true,'source_kind','service');
 end if;
 select context_id into context_uuid from control.nexloop_effect_run_contexts where run_id=run.run_id;
 perform authz.nexloop_assert_effect_plan(context_uuid,tenant,p_world);
 select * into plan from control.nexloop_effect_plan_bindings where context_id=context_uuid;
 select * into ctx from control.nexloop_effect_contexts where context_id=context_uuid;
 select * into ctl from control.nexloop_effect_control_ledger where control_id=plan.control_id;
 if plan.step_id is distinct from role->'binding'->>'step_id' or plan.step_revision::text is distinct from role->'binding'->>'step_revision'
  or command->>'goal_version_ref' is distinct from 'goal:'||plan.goal_id||':revision:'||plan.goal_revision||':step:'||plan.step_revision||':control:'||plan.control_revision
  then raise exception 'role shared Plan mismatch' using errcode='42501';end if;
 env:=a->'catalog_envelope';catalog:=authz.nexloop_service_catalog_scope(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if catalog->'allowed' is distinct from 'true'::jsonb or (env->>'payload')::jsonb->>'consumer_id' is distinct from ctx.consumer_id then raise exception 'role supply unavailable' using errcode='42501';end if;

 if jsonb_typeof(a->'formal_reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(a->'formal_reads'))<>4 or not(a->'formal_reads' ?& array['Consumer','Goal','PlanStep','EffectControl']) then raise exception 'formal Source READ required' using errcode='42501';end if;
 for formal_name,env in select key,value from jsonb_each(a->'formal_reads') loop
  proof:=(env->>'text')::jsonb;
  if proof->>'type_name' is distinct from formal_name or proof->>'object_id' is distinct from (case proof->>'type_name' when 'Consumer' then ctx.consumer_id when 'Goal' then plan.goal_id when 'PlanStep' then plan.step_id when 'EffectControl' then plan.control_id else null end)
   or proof->'fields' is distinct from (case when proof->>'type_name'='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end) then raise exception 'formal Source READ mismatch' using errcode='42501';end if;
  perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 for env in select value from jsonb_each((a->'catalog_envelope'->>'payload')::jsonb->'reads') loop perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');end loop;
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 if (p->>'artifact_identity_text')::jsonb is distinct from jsonb_build_array(tenant,p_world,principal,'context:'||run.run_id::text) then raise exception 'role artifact identity mismatch' using errcode='42501';end if;
 chosen_artifact_id:=substr(encode(sha256(convert_to(p->>'artifact_identity_text','UTF8')),'hex'),1,32);
 select jsonb_agg(jsonb_build_object('type',o.type_name,'id',o.object_id,'revision',o.nexloop_revision,'provenance','eios:object:'||o.object_id) order by o.type_name) into facts
 from ontology.objects o where o.tenant_id=tenant and o.world=p_world and o.object_id in(ctx.consumer_id,plan.goal_id,plan.step_id,plan.control_id);
 snapshot:=jsonb_build_object('schema_version','nexloop.context-pack.v3',
  'bindings',jsonb_build_object('tenant_id',tenant,'world_id',p_world,'run_id',run.run_id,'source_principal',principal,'context_id',context_uuid,'namespace',namespace,'artifact_id',chosen_artifact_id,'command_digest',p->>'command_binding_digest'),
  'trigger_statement',jsonb_build_object('kind','service_trigger','event_id',e.event_id,'source_principal',e.source_principal,'body',e.body,'provenance','eios:role-trigger:'||e.event_id::text),
  'formal_facts',facts,'role_binding',role,'supply',catalog->'supply',
  'current_constraints',jsonb_build_object('action','nexloop.service.request:1','allow_effect',true,'budget_units',ctl.budget_units,'reserved_units',ctl.reserved_units,'valid_until',least(ctx.valid_until,ctl.valid_until,run.expires_at),'executor_principal',ctl.executor_principal));
 -- NX-018 v5: governed Role policy provenance for the Run (null when the Run has no policy).
 policy_section:=authz.nexloop_role_policy_context_section(run.run_id,tenant,p_world);
 if policy_section is not null then snapshot:=jsonb_set(snapshot,'{schema_version}','"nexloop.context-pack.v5"')||jsonb_build_object('role_policy',policy_section);end if;
 if p->>'verb'='bind' then
  if p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 or (p->>'pack_text')::jsonb is distinct from snapshot
   or p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') then raise exception 'role pack snapshot mismatch' using errcode='42501';end if;
  if jsonb_typeof(a->'artifact_proofs') is distinct from 'array' or jsonb_array_length(a->'artifact_proofs')<>2
   or (select jsonb_agg(v->>'operation' order by v->>'operation') from jsonb_array_elements(a->'artifact_proofs') t(v)) is distinct from '["create","read"]'::jsonb then raise exception 'Artifact CREATE/READ required' using errcode='42501';end if;
  for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Artifact proof expired' using errcode='42501';end if;perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);end loop;
  select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and runtime.nexloop_local_artifacts.artifact_id=chosen_artifact_id for share;
  if not found or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal or artifact.sha256 is distinct from p->>'pack_digest'
   or artifact.size_bytes is distinct from octet_length(p->>'pack_text') or artifact.object_key is distinct from namespace||'/'||chosen_artifact_id
   or (artifact.retention_until is null or artifact.retention_until<=clock_timestamp()) then raise exception 'role Artifact unavailable' using errcode='42501';end if;
  insert into runtime.nexloop_role_context_artifacts values(run.run_id,tenant,p_world,principal,p_digest,context_uuid,chosen_artifact_id,namespace,p->>'pack_text',p->>'pack_digest',binding,clock_timestamp()) on conflict do nothing;
  select * into b from runtime.nexloop_role_context_artifacts where run_id=run.run_id;
  if b.pack_text is distinct from p->>'pack_text' or b.command_binding is distinct from binding then raise exception 'role Artifact conflict' using errcode='23505';end if;
 end if;
 if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Action proof expired' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 env:=a->'role_envelope';perform authz.nexloop_role_run_current(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 env:=a->'catalog_envelope';perform authz.nexloop_service_catalog_scope(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if p->>'verb'='bind' then
  for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Artifact proof expired' using errcode='42501';end if;perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);end loop;
  if (artifact.retention_until is null or artifact.retention_until<=clock_timestamp()) then raise exception 'role Artifact expired' using errcode='42501';end if;
 end if;

 if jsonb_typeof(a->'formal_reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(a->'formal_reads'))<>4 or not(a->'formal_reads' ?& array['Consumer','Goal','PlanStep','EffectControl']) then raise exception 'formal Source READ required' using errcode='42501';end if;
 for formal_name,env in select key,value from jsonb_each(a->'formal_reads') loop
  proof:=(env->>'text')::jsonb;
  if proof->>'type_name' is distinct from formal_name or proof->>'object_id' is distinct from (case proof->>'type_name' when 'Consumer' then ctx.consumer_id when 'Goal' then plan.goal_id when 'PlanStep' then plan.step_id when 'EffectControl' then plan.control_id else null end)
   or proof->'fields' is distinct from (case when proof->>'type_name'='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end) then raise exception 'formal Source READ mismatch' using errcode='42501';end if;
  perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 for env in select value from jsonb_each((a->'catalog_envelope'->>'payload')::jsonb->'reads') loop perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');end loop;
 for proof in select value from jsonb_array_elements(a->'run_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Run proof expired' using errcode='42501';end if;perform authz.nexloop_assert_action_authority(run.token_digest,p_world,proof);end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Action proof expired' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'run_proofs') loop if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Run proof expired' using errcode='42501';end if;end loop;
 if p->>'verb'='bind' then
  for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Artifact proof expired' using errcode='42501';end if;end loop;
  if artifact.retention_until is null or artifact.retention_until<=clock_timestamp() then raise exception 'role Artifact expired' using errcode='42501';end if;
 end if;
 return snapshot;
end $$;

create or replace function authz.nexloop_context_artifact_read_dependency_v2_before_relationship(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare r jsonb;b runtime.nexloop_role_context_artifacts;e runtime.nexloop_role_trigger_events;ident jsonb;pack jsonb;message_id text;refs jsonb;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into b from runtime.nexloop_role_context_artifacts where tenant_id=ident->'binding'->>'tenant_id' and world=p_world and artifact_id=p_payload::jsonb->>'artifact_id';
 if not found then
  message_id:=authz.nexloop_context_artifact_read_dependency(p_digest,p_world,p_permit,p_payload);
  if message_id is null then return null;end if;
  return jsonb_build_object('kind','message','message_id',message_id);
 end if;
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 if r->>'media_type' is distinct from 'application/vnd.nexloop.context+json' then raise exception 'role Context media unavailable' using errcode='42501';end if;
 select * into e from runtime.nexloop_role_trigger_events where run_id=b.run_id;
 pack:=b.pack_text::jsonb;
 if not found or b.tenant_id is distinct from ident->'binding'->>'tenant_id' or e.source_principal is distinct from ident->'binding'->>'subject_principal_id'
  or e.world is distinct from p_world or e.tenant_id is distinct from b.tenant_id
  or pack->>'schema_version' not in ('nexloop.context-pack.v3','nexloop.context-pack.v5')
  or pack->'trigger_statement' is distinct from jsonb_build_object('kind','service_trigger','event_id',e.event_id,'source_principal',e.source_principal,'body',e.body,'provenance','eios:role-trigger:'||e.event_id::text)
  or b.pack_digest is distinct from r->>'sha256' then raise exception 'role Context source unavailable' using errcode='42501';end if;
 refs:=jsonb_build_object(
  'role',jsonb_build_object('type_name','RoleDefinition','object_id',pack->'role_binding'->'binding'->>'role_id','fields','["active","ceiling_ref","name","responsibility","valid_from","valid_until"]'::jsonb),
  'link',jsonb_build_object('type_name','ConsumerRoleLink','object_id',pack->'role_binding'->'binding'->>'link_id','fields','["active","consumer_id","role_id","scope","valid_from","valid_until"]'::jsonb),
  'step',jsonb_build_object('type_name','PlanStep','object_id',pack->'role_binding'->'binding'->>'step_id','fields','["consumer_id","state","submitter_principals"]'::jsonb),
  'offering',jsonb_build_object('type_name','ServiceOffering','object_id',pack->'supply'->>'offering_id','fields','["active","allowed_discounts","allowed_guarantees","content_kind","currency","delivery_action","eligibility","evidence_kind","price_amount","service_code","title","valid_until"]'::jsonb),
  'offering_binding',jsonb_build_object('type_name','ConsumerServiceOffering','object_id',pack->'supply'->>'binding_id','fields','["active","consumer_id","offering_id","offering_revision","source_principal"]'::jsonb));
 refs:=refs||(select jsonb_object_agg('fact_'||(f->>'type'),jsonb_build_object('type_name',f->>'type','object_id',f->>'id','fields',
   case when f->>'type'='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end)) from jsonb_array_elements(pack->'formal_facts') t(f));
 if pack->>'schema_version'='nexloop.context-pack.v5' then
  refs:=refs||jsonb_build_object(
   'policy_ceiling',jsonb_build_object('type_name','RoleExecutionCeiling','object_id',pack->'role_policy'->'binding'->>'ceiling_id','fields','["action_resources","active","budget","consumer_ids","effect_units","goal_ids","step_ids","valid_from","valid_until"]'::jsonb),
   'policy_scope',jsonb_build_object('type_name','RoleAssignmentScope','object_id',pack->'role_policy'->'binding'->>'scope_id','fields','["active","consumer_id","goal_ids","role_id","step_ids","valid_from","valid_until"]'::jsonb));
 end if;
 return jsonb_build_object('kind','role','context_version',pack->>'schema_version','run_id',b.run_id,'event_id',e.event_id,'reads',refs,'artifact',r);
end $$;
