-- NX-018: relationship correction enters a new explicit nexloop.context-pack.v4 Context.
-- Human corrections stay user_statement/evidence zones, never formal facts. The v4
-- branches sit at the bottom of the existing 0063..0066 wrapper chains (private
-- *_before_roles_v0062 aliases); public Role/receipt/formal-current wrappers are kept.
-- Shared nexloop_assert_context_proof_deadlines / nexloop_context_formal_current come from 0066.
create function authz.nexloop_relationship_message_read(p_digest text,p_world text,p_message text,env jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb;proof jsonb;r jsonb;
begin
 a:=(env->>'text')::jsonb;
 if a->>'type_name' is distinct from 'Message' or a->>'object_id' is distinct from p_message or a->'fields' is distinct from '["actor","body"]'::jsonb
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or jsonb_typeof(a->'property_authorities') is distinct from 'array' or jsonb_array_length(a->'property_authorities')<>2 then raise exception 'context Message read missing' using errcode='42501';end if;
 r:=authz.nexloop_read_object(p_digest,p_world,env->>'text',env->>'signature');
 if r is null or not(r->'properties' ?& array['actor','body']) then raise exception 'context Message unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 for proof in select value from jsonb_array_elements(a->'property_authorities') loop
  if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context Message read expired' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context Message read expired' using errcode='42501';end if;
 return r;
end $$;
alter function authz.nexloop_relationship_message_read(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_relationship_message_read(text,text,text,jsonb) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

-- Candidate append only; server-owned technical recipe, not a business fact store.
create table control.nexloop_relationship_context_recipes (
 tenant_id text not null,world text not null,source_principal text not null,consumer_id text not null,
 assessment_ids jsonb not null check(jsonb_typeof(assessment_ids)='array' and jsonb_array_length(assessment_ids) between 1 and 4),
 primary key(tenant_id,world,source_principal,consumer_id)
);
alter table control.nexloop_relationship_context_recipes owner to nexloop_owner;
alter table control.nexloop_relationship_context_recipes enable row level security;
alter table control.nexloop_relationship_context_recipes force row level security;
create policy relationship_recipe_tenant on control.nexloop_relationship_context_recipes to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_relationship_context_recipes from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

create function authz.nexloop_relationship_context_snapshot(p_digest text,p_world text,p_consumer text,p_envelopes jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare identity jsonb;tenant text;principal text;recipe jsonb;env jsonb;claim jsonb;head jsonb;values jsonb;
 selected jsonb:='[]';statements jsonb:='[]';evidence jsonb:='[]';item jsonb;source jsonb;object jsonb;prefix text;valid_now boolean;message jsonb;proof jsonb;tail_identity jsonb;
begin
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_scheduler') or p_world is distinct from 'real' or identity->'run_context' is distinct from 'null'::jsonb
  or identity->'binding'->>'subject_kind' is distinct from 'service' or p_consumer !~ '^[a-f0-9]{64}$'
  or jsonb_typeof(p_envelopes) is distinct from 'array' or jsonb_array_length(p_envelopes) not between 1 and 4 then
  raise exception 'relationship context unavailable' using errcode='42501';end if;
 tenant:=identity->'binding'->>'tenant_id';principal:=identity->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',tenant,true);
 select assessment_ids into recipe from control.nexloop_relationship_context_recipes where tenant_id=tenant and world=p_world and source_principal=principal and consumer_id=p_consumer for share;
 if not found then raise exception 'relationship recipe unavailable' using errcode='42501';end if;
 for env in select value from jsonb_array_elements(p_envelopes) loop
  claim:=(env->>'text')::jsonb;
  if claim->>'tenant_id' is distinct from tenant or claim->>'principal_id' is distinct from principal
   or claim->>'credential_id' is distinct from identity->'binding'->>'credential_id' or claim->>'directory_hash' is distinct from identity->>'directory_hash'
   or claim->>'type_name' is distinct from 'RelationshipAssessment' then raise exception 'relationship source mismatch' using errcode='42501';end if;
  head:=authz.nexloop_read_assessment_object(p_digest,p_world,env->>'text',env->>'signature');values:=head->'properties';
  if head is null or (values->>'source_type' is distinct from 'Consumer' or values->>'source_id' is distinct from p_consumer)
   and (values->>'target_type' is distinct from 'Consumer' or values->>'target_id' is distinct from p_consumer) then raise exception 'relationship consumer mismatch' using errcode='42501';end if;
  selected:=selected||jsonb_build_array(claim->>'object_id');
  if (select count(*) from jsonb_object_keys(values))<>14 or jsonb_typeof(env->'sources') is distinct from 'array'
   or jsonb_array_length(env->'sources')<>(case when values->>'epistemic_kind'='user_statement' then 3 else 2 end) then raise exception 'relationship typed read incomplete' using errcode='42501';end if;
  for prefix in select unnest(array['source','target']) loop
   select value into source from jsonb_array_elements(env->'sources') s where (s->>'text')::jsonb->>'type_name'=values->>(prefix||'_type') and (s->>'text')::jsonb->>'object_id'=values->>(prefix||'_id') limit 1;
   if not found then raise exception 'relationship endpoint read missing' using errcode='42501';end if;
   object:=authz.nexloop_read_object(p_digest,p_world,source->>'text',source->>'signature');
   if object is null then raise exception 'relationship endpoint unavailable' using errcode='42501';end if;
  end loop;
  if values->>'epistemic_kind'='user_statement' then
   select value into source from jsonb_array_elements(env->'sources') s where (s->>'text')::jsonb->>'type_name'='Message' and (s->>'text')::jsonb->>'object_id'=values->>'evidence_message_id';
   if not found then raise exception 'relationship Message read missing' using errcode='42501';end if;
   message:=authz.nexloop_read_object(p_digest,p_world,source->>'text',source->>'signature');
   if not (message->'properties' ?& array['actor','body']) or message->'properties'->>'body' is distinct from values->>'conclusion'
    or encode(sha256(convert_to(message->'properties'->>'body','UTF8')),'hex') is distinct from values->>'evidence_content_hash'
    or not exists(select 1 from runtime.nexloop_conversation_messages cm where cm.tenant_id=tenant and cm.world=p_world and cm.message_id=values->>'evidence_message_id' and cm.record->>'body'=message->'properties'->>'body' and cm.record->>'actor'=message->'properties'->>'actor')
    or not exists(select 1 from control.nexloop_browser_memberships b join control.nexloop_browser_subjects s on s.subject_id=b.subject_id where b.tenant_id=tenant and b.principal_id=message->'properties'->>'actor' and s.payload->>'kind'='human') then raise exception 'relationship Human Message unavailable' using errcode='42501';end if;
  elsif values->>'evidence_message_id' is distinct from '' or values->>'evidence_content_hash' is distinct from '' then raise exception 'relationship hypothesis provenance invalid' using errcode='42501';end if;
  item:=jsonb_build_object('assessment_ref','eios:object:RelationshipAssessment/'||(head->>'object_id'),'revision',head->'revision',
   'relation_type_ref',case when (values->>'relation_version')::integer>0 then 'eios:link_type:'||(values->>'relation_name')||':'||(values->>'relation_version') else null end,
   'source_ref','eios:object:'||(values->>'source_type')||'/'||(values->>'source_id'),'target_ref','eios:object:'||(values->>'target_type')||'/'||(values->>'target_id'),
   'epistemic_kind',values->'epistemic_kind','resolution_state',values->'resolution_state','conclusion',values->'conclusion','valid_from',values->'valid_from','valid_to',nullif(values->>'valid_to',''),
   'source_message_ref',case when values->>'epistemic_kind'='user_statement' then 'eios:object:Message/'||(values->>'evidence_message_id') else null end,'source_content_hash',nullif(values->>'evidence_content_hash',''));
  valid_now:=(values->>'relation_version')::integer>0 and values->>'epistemic_kind'='user_statement' and values->>'resolution_state'='resolved'
   and (values->>'valid_from')::timestamptz<=clock_timestamp() and (values->>'valid_to'='' or (values->>'valid_to')::timestamptz>clock_timestamp());
  if valid_now then statements:=statements||jsonb_build_array(item);else evidence:=evidence||jsonb_build_array(item);end if;
 end loop;
 if selected is distinct from recipe or (select count(distinct value) from jsonb_array_elements(selected))<>jsonb_array_length(selected) then raise exception 'relationship recipe mismatch' using errcode='42501';end if;
 -- Every item and endpoint is rechecked after the entire projection. A later
 -- row lock wait must not let an earlier expired/revoked proof leave the helper.
 for env in select value from jsonb_array_elements(p_envelopes) loop
  claim:=(env->>'text')::jsonb;
  if claim->>'expires_at' is null or (claim->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'relationship read expired' using errcode='42501';end if;
  perform authz.nexloop_read_assessment_object(p_digest,p_world,env->>'text',env->>'signature');
  for source in select value from jsonb_array_elements(env->'sources') loop
   claim:=(source->>'text')::jsonb;
   if claim->>'expires_at' is null or (claim->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'relationship source read expired' using errcode='42501';end if;
   perform authz.nexloop_read_object(p_digest,p_world,source->>'text',source->>'signature');
   perform authz.nexloop_assert_read_authority(p_digest,p_world,claim);
   for proof in select value from jsonb_array_elements(claim->'property_authorities') loop
    if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'relationship source read expired' using errcode='42501';end if;
    perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
   end loop;
  end loop;
 end loop;
 tail_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if tail_identity is distinct from identity then raise exception 'relationship Source changed' using errcode='42501';end if;
 for env in select value from jsonb_array_elements(p_envelopes) loop
  claim:=(env->>'text')::jsonb;
  for proof in select claim union all select value from jsonb_array_elements(claim->'property_authorities') union all select claim->'relation_authority' where claim->'relation_authority' is distinct from 'null'::jsonb loop
   if proof is null or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'relationship final read expired' using errcode='42501';end if;
  end loop;
  for source in select value from jsonb_array_elements(env->'sources') loop
   claim:=(source->>'text')::jsonb;
   for proof in select claim union all select value from jsonb_array_elements(claim->'property_authorities') loop
    if proof is null or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'relationship final source expired' using errcode='42501';end if;
   end loop;
  end loop;
 end loop;
 for item in select value from jsonb_array_elements(statements) loop
  if (item->>'valid_from')::timestamptz>clock_timestamp() or (item->>'valid_to' is not null and (item->>'valid_to')::timestamptz<=clock_timestamp()) then raise exception 'relationship validity expired at return' using errcode='42501';end if;
 end loop;

 return jsonb_build_object('current_statements',statements,'evidence',evidence);
end $$;
alter function authz.nexloop_relationship_context_snapshot(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_relationship_context_snapshot(text,text,text,jsonb) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
grant execute on function authz.nexloop_relationship_context_snapshot(text,text,text,jsonb) to nexloop_api;

-- Pure metadata checks follow all potentially blocking authority/row reads.
create function authz.nexloop_assert_relationship_deadlines(envelopes jsonb,zone jsonb) returns void
language plpgsql security definer set search_path=pg_catalog as $$declare e jsonb;s jsonb;i jsonb;begin
 if jsonb_typeof(envelopes) is distinct from 'array' or jsonb_array_length(envelopes) not between 1 and 4 then raise exception 'context final relationships unavailable' using errcode='42501';end if;
 if jsonb_typeof(zone) is distinct from 'object' or jsonb_typeof(zone->'current_statements') is distinct from 'array' or jsonb_typeof(zone->'evidence') is distinct from 'array' then raise exception 'context final zone unavailable' using errcode='42501';end if;
 for e in select value from jsonb_array_elements(envelopes) loop
  if jsonb_typeof(e->'sources') is distinct from 'array' or jsonb_array_length(e->'sources') not between 2 and 3 then raise exception 'context final sources unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_context_proof_deadlines((e->>'text')::jsonb);
  for s in select value from jsonb_array_elements(e->'sources') loop perform authz.nexloop_assert_context_proof_deadlines((s->>'text')::jsonb);end loop;
 end loop;
 for i in select value from jsonb_array_elements(zone->'current_statements') loop
  if i->>'valid_from' is null or (i->>'valid_from')::timestamptz>clock_timestamp() or (i->>'valid_to' is not null and (i->>'valid_to')::timestamptz<=clock_timestamp()) then raise exception 'context final statement expired' using errcode='42501';end if;
 end loop;
end $$;
alter function authz.nexloop_assert_relationship_deadlines(jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_relationship_deadlines(jsonb,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
-- Shared v3/v4 Source gate: refs are passed only from the actual immutable binding.
create function authz.nexloop_relationship_context_artifact_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;tenant text;principal text;
 command jsonb:=p->'command';context_uuid uuid;outbox runtime.nexloop_message_outbox;conv runtime.nexloop_conversations;
 issued authz.nexloop_message_run_issuances;plan control.nexloop_effect_plan_bindings;ctx control.nexloop_effect_contexts;
 ctl control.nexloop_effect_control_ledger;artifact runtime.nexloop_local_artifacts;b runtime.nexloop_context_artifact_bindings;
 pub control.nexloop_action_definitions;cl runtime.nexloop_action_claims;snapshot jsonb;facts jsonb;namespace text;binding jsonb;pack jsonb;proof jsonb;
 catalog_env jsonb;catalog_result jsonb;relationships jsonb;trigger_env jsonb;trigger_message jsonb;tail_env jsonb;formal_name text;formal_env jsonb;formal_value jsonb;
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

 catalog_env:=a->'catalog_envelope';
 if jsonb_typeof(catalog_env) is distinct from 'object' then raise exception 'catalog unavailable' using errcode='42501';end if;
 catalog_result:=authz.nexloop_service_catalog_scope(p_digest,p_world,catalog_env->>'text',catalog_env->>'signature',catalog_env->>'payload');
 if catalog_result->'allowed' is distinct from 'true'::jsonb or (catalog_env->>'payload')::jsonb->>'consumer_id' is distinct from conv.consumer_id then raise exception 'catalog unavailable' using errcode='42501';end if;
 trigger_env:=a->'trigger_message_envelope';
 trigger_message:=authz.nexloop_relationship_message_read(p_digest,p_world,outbox.message_id,trigger_env);
 if trigger_message->'properties'->>'body' is distinct from outbox.record->>'body' or trigger_message->'properties'->>'actor' is distinct from outbox.record->>'actor' then raise exception 'context trigger mismatch' using errcode='42501';end if;
 if jsonb_typeof(a->'formal_reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(a->'formal_reads'))<>4 or not(a->'formal_reads' ?& array['Consumer','Goal','PlanStep','EffectControl']) then raise exception 'formal Source READ required' using errcode='42501';end if;
 for formal_name,formal_env in select key,value from jsonb_each(a->'formal_reads') loop
  proof:=(formal_env->>'text')::jsonb;
  if proof->>'type_name' is distinct from formal_name or proof->>'object_id' is distinct from (case formal_name when 'Consumer' then ctx.consumer_id when 'Goal' then plan.goal_id when 'PlanStep' then plan.step_id when 'EffectControl' then plan.control_id end)
   or proof->'fields' is distinct from (case when formal_name='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end) then raise exception 'formal Source READ mismatch' using errcode='42501';end if;
  formal_value:=authz.nexloop_read_object(p_digest,p_world,formal_env->>'text',formal_env->>'signature');
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  for tail_env in select value from jsonb_array_elements(proof->'property_authorities') loop perform authz.nexloop_assert_read_authority(p_digest,p_world,tail_env);end loop;
  perform authz.nexloop_assert_context_proof_deadlines(proof);
 end loop;
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 if (p->>'artifact_identity_text')::jsonb is distinct from jsonb_build_array(tenant,p_world,principal,'context:'||issued.run_id::text) then raise exception 'context artifact unavailable' using errcode='42501';end if;
 binding:=authz.nexloop_context_command_binding(command);
 if (p->>'command_binding_text')::jsonb is distinct from binding or p->>'command_binding_digest' is distinct from encode(sha256(convert_to(p->>'command_binding_text','UTF8')),'hex') then raise exception 'context artifact unavailable' using errcode='42501';end if;
 snapshot:=jsonb_build_object('schema_version','nexloop.context-pack.v4',
  'bindings',jsonb_build_object('tenant_id',tenant,'world_id',p_world,'run_id',issued.run_id,'source_principal',principal,'context_id',context_uuid,'namespace',namespace,'artifact_id',substr(encode(sha256(convert_to(p->>'artifact_identity_text','UTF8')),'hex'),1,32),'command_digest',p->>'command_binding_digest'),
  'user_statement',jsonb_build_object('message_id',outbox.message_id,'conversation_id',outbox.conversation_id,'sequence',(outbox.record->>'sequence')::bigint,'body',outbox.record->>'body','provenance','eios:object:'||outbox.message_id),
  'formal_facts',facts,'current_constraints',jsonb_build_object('action','nexloop.service.request:1','allow_effect',true,'budget_units',ctl.budget_units,'reserved_units',ctl.reserved_units,'valid_until',least(ctx.valid_until,ctl.valid_until,issued.expires_at),'executor_principal',ctl.executor_principal));
 relationships:=authz.nexloop_relationship_context_snapshot(p_digest,p_world,conv.consumer_id,a->'relationship_envelopes');
 snapshot:=snapshot||jsonb_build_object('supply',catalog_result->'supply','relationship_context',relationships);
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=tenant and world=p_world and message_id=outbox.message_id for update;
 if found then
  if b.run_id is distinct from issued.run_id or b.source_principal is distinct from principal or b.context_id is distinct from context_uuid or b.command_binding is distinct from binding then raise exception 'context artifact conflict' using errcode='23505';end if;
  -- Immutable assembly-time constraints survive replay; dispatch rechecks live authority/budget.
  if b.pack_text::jsonb->'supply' is distinct from catalog_result->'supply' then raise exception 'catalog revision stale' using errcode='42501';end if;
  if b.pack_text::jsonb->'relationship_context' is distinct from relationships then raise exception 'relationship context stale' using errcode='42501';end if;
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
  if not found or cl.principal_id is distinct from principal or cl.claim->'binding' is distinct from a->'claim_binding' or cl.claim->>'state' is null or cl.claim->>'state' not in ('active','terminal') or (cl.claim->>'state'='active' and (cl.claim->>'lease_expires_at' is null or (cl.claim->>'lease_expires_at')::timestamptz<=clock_timestamp())) or (cl.claim->>'state'='terminal' and (b.run_id is null or cl.claim->'terminal_outcome'->>'status' is distinct from 'succeeded' or cl.claim->'terminal_outcome'->>'outcome_digest' is distinct from artifact.sha256)) then raise exception 'context artifact unavailable' using errcode='42501';end if;
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
 if relationships is distinct from authz.nexloop_relationship_context_snapshot(p_digest,p_world,conv.consumer_id,a->'relationship_envelopes') then raise exception 'relationship context stale' using errcode='42501';end if;
 if jsonb_typeof(a->'formal_reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(a->'formal_reads'))<>4 or not(a->'formal_reads' ?& array['Consumer','Goal','PlanStep','EffectControl']) then raise exception 'formal Source READ required' using errcode='42501';end if;
 for formal_name,formal_env in select key,value from jsonb_each(a->'formal_reads') loop
  proof:=(formal_env->>'text')::jsonb;
  if proof->>'type_name' is distinct from formal_name or proof->>'object_id' is distinct from (case formal_name when 'Consumer' then ctx.consumer_id when 'Goal' then plan.goal_id when 'PlanStep' then plan.step_id when 'EffectControl' then plan.control_id end)
   or proof->'fields' is distinct from (case when formal_name='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end) then raise exception 'formal Source READ mismatch' using errcode='42501';end if;
  formal_value:=authz.nexloop_read_object(p_digest,p_world,formal_env->>'text',formal_env->>'signature');
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  for tail_env in select value from jsonb_array_elements(proof->'property_authorities') loop perform authz.nexloop_assert_read_authority(p_digest,p_world,tail_env);end loop;
  perform authz.nexloop_assert_context_proof_deadlines(proof);
 end loop;
 perform authz.nexloop_relationship_message_read(p_digest,p_world,outbox.message_id,trigger_env);
 if p->>'verb'='bind' then
  select * into cl from runtime.nexloop_action_claims where tenant_id=tenant and world=p_world and action_name='nexloop.context.bind' and intent_id=p->>'claim_id' for share;
  if not found or cl.claim->>'state' is null or cl.claim->>'state' not in ('active','terminal')
   or (cl.claim->>'state'='active' and (cl.claim->>'lease_expires_at' is null or (cl.claim->>'lease_expires_at')::timestamptz<=clock_timestamp()))
   or (cl.claim->>'state'='terminal' and (cl.claim->'terminal_outcome'->>'status' is distinct from 'succeeded' or cl.claim->'terminal_outcome'->>'outcome_digest' is distinct from artifact.sha256)) then raise exception 'context claim expired at return' using errcode='42501';end if;
 end if;
 -- No row/authority read follows this complete pure deadline fence.
 perform authz.nexloop_assert_context_proof_deadlines(a);
 for proof in select value from jsonb_array_elements(a->'artifact_proofs') union all select value from jsonb_array_elements(a->'run_proofs') loop perform authz.nexloop_assert_context_proof_deadlines(proof);end loop;
 for tail_env in select value from jsonb_each(a->'formal_reads') loop perform authz.nexloop_assert_context_proof_deadlines((tail_env->>'text')::jsonb);end loop;
 perform authz.nexloop_assert_relationship_deadlines(a->'relationship_envelopes',relationships);
 perform authz.nexloop_assert_context_proof_deadlines((trigger_env->>'text')::jsonb);
 for tail_env in select value from jsonb_each((catalog_env->>'payload')::jsonb->'reads') loop perform authz.nexloop_assert_context_proof_deadlines((tail_env->>'text')::jsonb);end loop;
 if issued.expires_at is null or issued.expires_at<=clock_timestamp() or ctx.valid_until is null or ctx.valid_until<=clock_timestamp() or ctl.valid_until is null or ctl.valid_until<=clock_timestamp()
  or snapshot->'current_constraints'->>'valid_until' is null or (snapshot->'current_constraints'->>'valid_until')::timestamptz<=clock_timestamp()
  or catalog_result->'supply'->'properties'->>'valid_until' is null or (catalog_result->'supply'->'properties'->>'valid_until')::timestamptz<=clock_timestamp()
  or p->>'verb'='bind' and (artifact.retention_until is null or artifact.retention_until<=clock_timestamp()) then raise exception 'context final validity expired' using errcode='42501';end if;
 return snapshot;
end $$;
alter function authz.nexloop_relationship_context_artifact_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relationship_context_artifact_command(text,text,text,text,text) from public,nexloop_identity,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker;
grant execute on function authz.nexloop_relationship_context_artifact_command(text,text,text,text,text) to nexloop_api;

-- v4 activation branch beneath the Role/TTL/formal-current wrappers; 0053 body plus v4 checks.
create or replace function authz.nexloop_runtime_activation_command_before_roles_v0062(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb;result jsonb;b runtime.nexloop_context_artifact_bindings;env jsonb;checked jsonb;supply jsonb;tail_env jsonb;tail_proof jsonb;formal_meta jsonb;
begin
 result:=authz.nexloop_runtime_activation_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
 command:=(p->>'command_text')::jsonb;
 if not exists(select 1 from authz.nexloop_message_run_issuances i where i.run_id=(command->>'run_id')::uuid) then return result;end if;
 -- Original 52 already owns lease/fence and validates immutable pack binding.
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=a->>'tenant_id' and world=p_world and run_id=(command->>'run_id')::uuid;
 if not found or (b.pack_text::jsonb->>'schema_version' is null or b.pack_text::jsonb->>'schema_version' not in ('nexloop.context-pack.v2','nexloop.context-pack.v4')) then raise exception 'catalog context mandatory' using errcode='42501';end if;
 supply:=b.pack_text::jsonb->'supply';
 if jsonb_typeof(supply) is distinct from 'object' then raise exception 'catalog context mandatory' using errcode='42501';end if;
 if p->>'verb'='resolve' then
  if b.pack_text::jsonb->>'schema_version'='nexloop.context-pack.v4' then result:=result||jsonb_build_object('_context_relationship',b.pack_text::jsonb->'relationship_context','_context_formal_facts',b.pack_text::jsonb->'formal_facts');end if;
  return result||jsonb_build_object('_context_catalog',supply,'_context_consumer_id',substr(command->>'consumer_ref',10));
 elsif p->>'verb'='authorize' then
  env:=a->'context_catalog_envelope';
  if jsonb_typeof(env) is distinct from 'object' then raise exception 'catalog context mandatory' using errcode='42501';end if;
  checked:=authz.nexloop_service_catalog_scope(b.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  if checked->'allowed' is distinct from 'true'::jsonb or checked->'supply' is distinct from supply
   or (env->>'payload')::jsonb->>'consumer_id' is distinct from substr(command->>'consumer_ref',10) then raise exception 'catalog context stale' using errcode='42501';end if;
  if p->>'verb'='authorize' and b.pack_text::jsonb->>'schema_version'='nexloop.context-pack.v4' and authz.nexloop_relationship_context_snapshot(b.source_digest,p_world,substr(command->>'consumer_ref',10),a->'context_relationship_envelopes') is distinct from b.pack_text::jsonb->'relationship_context' then raise exception 'relationship context stale' using errcode='42501';end if;
  result:=authz.nexloop_runtime_activation_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
  checked:=authz.nexloop_service_catalog_scope(b.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  if checked->'allowed' is distinct from 'true'::jsonb or checked->'supply' is distinct from supply then raise exception 'catalog context stale' using errcode='42501';end if;
 end if;
  if p->>'verb'='authorize' and b.pack_text::jsonb->>'schema_version'='nexloop.context-pack.v4' and authz.nexloop_relationship_context_snapshot(b.source_digest,p_world,substr(command->>'consumer_ref',10),a->'context_relationship_envelopes') is distinct from b.pack_text::jsonb->'relationship_context' then raise exception 'relationship context stale' using errcode='42501';end if;
 if p->>'verb'='authorize' and b.pack_text::jsonb->>'schema_version'='nexloop.context-pack.v4' then
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
alter function authz.nexloop_runtime_activation_command_before_roles_v0062(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command_before_roles_v0062(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create or replace function authz.nexloop_effect_catalog_metadata(p_run uuid,p_tenant text,p_world text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
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
 if exists(select 1 from authz.nexloop_message_run_issuances mr where mr.run_id=r.run_id and mr.tenant_id=p_tenant and mr.world=p_world) then
  select * into b from runtime.nexloop_context_artifact_bindings cb where cb.run_id=r.run_id and cb.tenant_id=p_tenant and cb.world=p_world;
  if not found or b.source_digest is distinct from r.source_digest or b.context_id is distinct from link.context_id
   or (b.pack_text::jsonb->>'schema_version' is null or b.pack_text::jsonb->>'schema_version' not in ('nexloop.context-pack.v2','nexloop.context-pack.v4'))
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
alter function authz.nexloop_effect_catalog_metadata(uuid,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_catalog_metadata(uuid,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

-- v4 copy-read dependency; legacy text-returning dependency and Role v3 resolver retained.
create function authz.nexloop_relationship_context_read_dependency(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare r jsonb;b runtime.nexloop_context_artifact_bindings;version text;ids jsonb;formals jsonb;
begin
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=r->>'tenant_id' and world=p_world and artifact_id=r->>'artifact_id';
 if not found then
  if r->>'media_type'='application/vnd.nexloop.context+json' then raise exception 'context artifact unavailable' using errcode='42501';end if;
  return null;
 end if;
 version:=b.pack_text::jsonb->>'schema_version';
 if (b.pack_text::jsonb)->'user_statement'->>'message_id' is distinct from b.message_id then raise exception 'context source invalid' using errcode='42501';end if;
 if version in ('nexloop.context-pack.v1','nexloop.context-pack.v2') then return to_jsonb(b.message_id);end if;
 if version is distinct from 'nexloop.context-pack.v4' then raise exception 'context protocol unavailable' using errcode='42501';end if;
 select jsonb_agg(substr(value->>'assessment_ref',36) order by value->>'assessment_ref') into ids from jsonb_array_elements((b.pack_text::jsonb->'relationship_context'->'current_statements')||(b.pack_text::jsonb->'relationship_context'->'evidence'));
 select jsonb_object_agg(value->>'type',value->>'id') into formals from jsonb_array_elements(b.pack_text::jsonb->'formal_facts');
 return jsonb_build_object('formal_refs',formals,'kind','relationship_context','message_id',b.message_id,'consumer_id',substr(b.command_binding->>'consumer_ref',10),'assessment_ids',ids);
end $$;
alter function authz.nexloop_relationship_context_read_dependency(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relationship_context_read_dependency(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

alter function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) rename to nexloop_context_artifact_read_dependency_v2_before_relationship;
revoke all on function authz.nexloop_context_artifact_read_dependency_v2_before_relationship(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_context_artifact_read_dependency_v2(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare i jsonb;b runtime.nexloop_context_artifact_bindings;
begin
 i:=authz.nexloop_service_identity_snapshot(p_digest,p_world);perform set_config('eios.tenant_id',i->'binding'->>'tenant_id',true);
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=i->'binding'->>'tenant_id' and world=p_world and artifact_id=p_payload::jsonb->>'artifact_id';
 if found and b.pack_text::jsonb->>'schema_version'='nexloop.context-pack.v4' then return authz.nexloop_relationship_context_read_dependency(p_digest,p_world,p_permit,p_payload);end if;
 return authz.nexloop_context_artifact_read_dependency_v2_before_relationship(p_digest,p_world,p_permit,p_payload);
end $$;
alter function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) from public,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- v4 artifact READ at the Message level of the read chain (beneath the Role and TTL wrappers).
alter function authz.nexloop_read_local_artifact_before_roles_v0062(text,text,text,text) rename to nexloop_read_local_artifact_before_relationship_v0066;
revoke all on function authz.nexloop_read_local_artifact_before_relationship_v0066(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_read_local_artifact_before_roles_v0062(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare binding runtime.nexloop_context_artifact_bindings;identity jsonb;r jsonb;d jsonb:=p_payload::jsonb->'context_dependency';checked jsonb;message jsonb;permit jsonb;consumer text;name text;env jsonb;proof jsonb;row jsonb;expected jsonb;
begin
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);perform set_config('eios.tenant_id',identity->'binding'->>'tenant_id',true);
 select * into binding from runtime.nexloop_context_artifact_bindings where tenant_id=identity->'binding'->>'tenant_id' and world=p_world and artifact_id=p_payload::jsonb->>'artifact_id' for share;
 if not found or binding.pack_text::jsonb->>'schema_version' is distinct from 'nexloop.context-pack.v4' then return authz.nexloop_read_local_artifact_before_relationship_v0066(p_digest,p_world,p_permit,p_payload);end if;
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 if d->>'kind' is distinct from 'relationship_context' then raise exception 'context dependency unavailable' using errcode='42501';end if;
 message:=authz.nexloop_relationship_message_read(p_digest,p_world,binding.message_id,d->'message');
 if message->'properties'->>'body' is distinct from binding.pack_text::jsonb->'user_statement'->>'body' then raise exception 'context Message changed' using errcode='42501';end if;
 if jsonb_typeof(d->'formal_reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(d->'formal_reads'))<>4 or not(d->'formal_reads' ?& array['Consumer','Goal','PlanStep','EffectControl']) then raise exception 'context reader formal READ required' using errcode='42501';end if;
 for name,env in select key,value from jsonb_each(d->'formal_reads') loop
  proof:=(env->>'text')::jsonb;
  select value into expected from jsonb_array_elements(binding.pack_text::jsonb->'formal_facts') where value->>'type'=name;
  if not found or proof->>'type_name' is distinct from name or proof->>'object_id' is distinct from expected->>'id' or proof->'fields' is distinct from (case when name='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end) then raise exception 'context reader formal READ mismatch' using errcode='42501';end if;
  row:=authz.nexloop_read_object(p_digest,p_world,env->>'text',env->>'signature');
  if row->'revision' is distinct from expected->'revision' then raise exception 'context formal revision changed' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  for expected in select value from jsonb_array_elements(proof->'property_authorities') loop perform authz.nexloop_assert_read_authority(p_digest,p_world,expected);end loop;
 end loop;
 consumer:=substr(binding.command_binding->>'consumer_ref',10);
 checked:=authz.nexloop_relationship_context_snapshot(p_digest,p_world,consumer,d->'relationships');
 if checked is distinct from binding.pack_text::jsonb->'relationship_context' then raise exception 'context relationships changed' using errcode='42501';end if;
 perform authz.nexloop_relationship_message_read(p_digest,p_world,binding.message_id,d->'message');
 if authz.nexloop_relationship_context_snapshot(p_digest,p_world,consumer,d->'relationships') is distinct from checked then raise exception 'context relationships changed' using errcode='42501';end if;
 select claims into permit from authz.nexloop_artifact_permits where tenant_id=binding.tenant_id and permit_id=p_permit;
 if permit is null or permit->>'expires_at' is null or (permit->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read expired' using errcode='42501';end if;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,permit);
 perform authz.nexloop_assert_context_proof_deadlines(permit);
 perform authz.nexloop_assert_context_proof_deadlines((d->'message'->>'text')::jsonb);
 for env in select value from jsonb_each(d->'formal_reads') loop perform authz.nexloop_assert_context_proof_deadlines((env->>'text')::jsonb);end loop;
 perform authz.nexloop_assert_relationship_deadlines(d->'relationships',checked);
 if binding.pack_text::jsonb->'current_constraints'->>'valid_until' is null or (binding.pack_text::jsonb->'current_constraints'->>'valid_until')::timestamptz<=clock_timestamp() or r->>'retention_until' is null or (r->>'retention_until')::timestamptz<=clock_timestamp() then raise exception 'context read validity expired' using errcode='42501';end if;
 return r;
end $$;
alter function authz.nexloop_read_local_artifact_before_roles_v0062(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_local_artifact_before_roles_v0062(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
