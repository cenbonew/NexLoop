-- Candidate owner-only assertion, not registered or executable application API.
-- Formal ServiceOffering/ConsumerServiceOffering objects are written solely by
-- existing governed object Actions. This is never a replacement for READ proof.
create function authz.nexloop_assert_service_offering(p_tenant text,p_world text,p_consumer text,p_source text,p_offering text,p_revision bigint,p_binding text,p_scope jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare o ontology.objects;b ontology.objects;scope jsonb;
begin
 if p_tenant is null or p_world is distinct from 'real' or p_consumer is null or p_source is null
  or p_offering is null or p_offering !~ '^[a-f0-9]{64}$' or p_binding is null or p_binding !~ '^[a-f0-9]{64}$'
  or p_revision is null or p_revision<1 or p_revision>9007199254740991 or jsonb_typeof(p_scope) is distinct from 'object'
  or (select count(*) from jsonb_object_keys(p_scope))<>4
  or p_scope->>'offering_id' is distinct from p_offering or jsonb_typeof(p_scope->'offering_revision') is distinct from 'number'
  or p_scope->>'offering_revision' is distinct from p_revision::text
  or jsonb_typeof(p_scope->'requested_guarantees') is distinct from 'array' or jsonb_typeof(p_scope->'requested_discounts') is distinct from 'array'
 then raise exception 'offering scope unavailable' using errcode='42501';end if;
 if jsonb_array_length(p_scope->'requested_guarantees')>16 or jsonb_array_length(p_scope->'requested_discounts')>16
  or exists(select 1 from jsonb_array_elements((p_scope->'requested_guarantees')||(p_scope->'requested_discounts')) v
    where jsonb_typeof(v.value) is distinct from 'string' or length(v.value#>>'{}') not between 1 and 128)
  or (select count(*) from jsonb_array_elements(p_scope->'requested_guarantees'))<>(select count(distinct v.value) from jsonb_array_elements(p_scope->'requested_guarantees') v)
  or (select count(*) from jsonb_array_elements(p_scope->'requested_discounts'))<>(select count(distinct v.value) from jsonb_array_elements(p_scope->'requested_discounts') v)
 then raise exception 'offering scope unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Fixed object ID ordering, regardless of binding versus offering identity.
 for o in select * from ontology.objects x where x.tenant_id=p_tenant and x.world=p_world
  and x.object_id in(p_offering,p_binding) order by x.object_id for share loop null;end loop;
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=p_offering and type_name='ServiceOffering';
 if not found or o.nexloop_revision is distinct from p_revision then raise exception 'offering revision stale' using errcode='42501';end if;
 select * into b from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=p_binding and type_name='ConsumerServiceOffering';
 if not found or b.properties is distinct from jsonb_build_object('consumer_id',p_consumer,'offering_id',p_offering,
  'offering_revision',p_revision,'source_principal',p_source,'active',true) then raise exception 'offering binding unavailable' using errcode='42501';end if;
 if (select count(*) from jsonb_object_keys(o.properties))<>12
  or o.properties->>'service_code' is distinct from 'local.json-export'
  or o.properties->>'delivery_action' is distinct from 'nexloop.service.request:1'
  or o.properties->>'content_kind' is distinct from 'json-message-export'
  or o.properties->>'price_amount' is distinct from '0' or o.properties->>'currency' is distinct from 'CNY'
  or o.properties->>'eligibility' is distinct from 'current_consumer_plan'
  or o.properties->'allowed_guarantees' is distinct from '[]'::jsonb or o.properties->'allowed_discounts' is distinct from '[]'::jsonb
  or o.properties->>'evidence_kind' is distinct from 'fsynced_json_export' or o.properties->'active' is distinct from 'true'::jsonb
  or jsonb_typeof(o.properties->'title') is distinct from 'string' or length(o.properties->>'title') not between 1 and 256
  or o.properties->>'valid_until' is null or (o.properties->>'valid_until')::timestamptz<=clock_timestamp()
 then raise exception 'offering unavailable' using errcode='42501';end if;
 return jsonb_build_object('offering_id',o.object_id,'revision',o.nexloop_revision,'properties',o.properties,
  'provenance','eios:object:'||o.object_id);
end $$;
alter function authz.nexloop_assert_service_offering(text,text,text,text,text,bigint,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_service_offering(text,text,text,text,text,bigint,text,jsonb)
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;
-- No grants: companion signed proof wrapper and immutable context/intent link
-- must be implemented before this draft enters migration catalog.

create function authz.nexloop_service_catalog_scope(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;tenant text;principal text;env jsonb;row jsonb;result jsonb;offering jsonb;allowed boolean;proof jsonb;binding_revision bigint;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker') or p_text is null or p_payload is null
  or octet_length(p_text)>4096 or octet_length(p_payload)>524288 or jsonb_typeof(c) is distinct from 'object'
  or jsonb_typeof(p) is distinct from 'object' or c->>'protocol' is distinct from 'nexloop-service-catalog-v1'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then raise exception 'catalog unavailable' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-service-catalog-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'catalog unavailable' using errcode='42501';end if;
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if p_world is distinct from 'real' or identity->'run_context' is distinct from 'null'::jsonb
  or identity->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'catalog unavailable' using errcode='42501';end if;
 tenant:=identity->'binding'->>'tenant_id';principal:=identity->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',tenant,true);
 if jsonb_typeof(p->'reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(p->'reads'))<>2 then raise exception 'catalog unavailable' using errcode='42501';end if;
 -- Each proof is an actual existing signed object/property READ. Fixed order.
 for env in select value from jsonb_each(p->'reads') order by key loop
  proof:=(env->>'text')::jsonb;
  if (select count(*) from jsonb_object_keys(env))<>2 or jsonb_typeof(env->'text') is distinct from 'string' or jsonb_typeof(env->'signature') is distinct from 'string' then raise exception 'catalog unavailable' using errcode='42501';end if;
  row:=authz.nexloop_read_object(p_digest,p_world,env->>'text',env->>'signature');
  if proof->>'type_name'='ServiceOffering' then
   if row->>'object_id' is distinct from p->>'offering_id' or row->>'type_name' is distinct from 'ServiceOffering'
    or proof->'fields' is distinct from '["active","allowed_discounts","allowed_guarantees","content_kind","currency","delivery_action","eligibility","evidence_kind","price_amount","service_code","title","valid_until"]'::jsonb then raise exception 'catalog unavailable' using errcode='42501';end if;
  elsif proof->>'type_name'='ConsumerServiceOffering' then
   binding_revision:=(row->>'revision')::bigint;
   if row->>'object_id' is distinct from p->>'binding_id' or row->>'type_name' is distinct from 'ConsumerServiceOffering'
    or proof->'fields' is distinct from '["active","consumer_id","offering_id","offering_revision","source_principal"]'::jsonb then raise exception 'catalog unavailable' using errcode='42501';end if;
  else raise exception 'catalog unavailable' using errcode='42501';end if;
 end loop;
 if (p->'reads'->'offering'->>'text')::jsonb->>'type_name' is distinct from 'ServiceOffering'
  or (p->'reads'->'binding'->>'text')::jsonb->>'type_name' is distinct from 'ConsumerServiceOffering' then raise exception 'catalog unavailable' using errcode='42501';end if;
 offering:=authz.nexloop_assert_service_offering(tenant,p_world,p->>'consumer_id',principal,p->>'offering_id',
  (p->'request_scope'->>'offering_revision')::bigint,p->>'binding_id',p->'request_scope');
 allowed:=p->'request_scope'->'requested_guarantees'='[]'::jsonb and p->'request_scope'->'requested_discounts'='[]'::jsonb;
 result:=jsonb_build_object('allowed',allowed,'dispatch_permit',false,'reason',case when allowed then 'within_catalog' else 'outside_catalog_terms' end,
  'offering_id',offering->>'offering_id','revision',offering->'revision','provenance',offering->>'provenance',
  'supply',jsonb_build_object('offering_id',offering->>'offering_id','offering_revision',offering->'revision','binding_id',p->>'binding_id','binding_revision',binding_revision,'provenance',offering->>'provenance','properties',offering->'properties'),
  'scope',jsonb_build_object('service_code','local.json-export','deliverable','固定私有目录内可核验的 JSON 文本导出文件','price_amount','0','currency','CNY','guarantees','[]'::jsonb,'discounts','[]'::jsonb,'limitations',jsonb_build_array('不提供目录外保证或折扣','不代表第三方渠道送达、付款或问题解决'),'evidence_kind','fsynced_json_export'));
 -- Lock waits cannot resurrect expired READ proof or expired formal offer.
 for env in select value from jsonb_each(p->'reads') order by key loop
  perform authz.nexloop_read_object(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 perform authz.nexloop_assert_service_offering(tenant,p_world,p->>'consumer_id',principal,p->>'offering_id',
  (p->'request_scope'->>'offering_revision')::bigint,p->>'binding_id',p->'request_scope');
 return result;
end $$;
alter function authz.nexloop_service_catalog_scope(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_service_catalog_scope(text,text,text,text,text) from public,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_service_catalog_scope(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

alter function authz.nexloop_context_artifact_command(text,text,text,text,text) rename to nexloop_context_artifact_command_v0052;
revoke all on function authz.nexloop_context_artifact_command_v0052(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_context_artifact_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;tenant text;principal text;
 command jsonb:=p->'command';context_uuid uuid;outbox runtime.nexloop_message_outbox;conv runtime.nexloop_conversations;
 issued authz.nexloop_message_run_issuances;plan control.nexloop_effect_plan_bindings;ctx control.nexloop_effect_contexts;
 ctl control.nexloop_effect_control_ledger;artifact runtime.nexloop_local_artifacts;b runtime.nexloop_context_artifact_bindings;
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
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 if (p->>'artifact_identity_text')::jsonb is distinct from jsonb_build_array(tenant,p_world,principal,'context:'||issued.run_id::text) then raise exception 'context artifact unavailable' using errcode='42501';end if;
 binding:=authz.nexloop_context_command_binding(command);
 if (p->>'command_binding_text')::jsonb is distinct from binding or p->>'command_binding_digest' is distinct from encode(sha256(convert_to(p->>'command_binding_text','UTF8')),'hex') then raise exception 'context artifact unavailable' using errcode='42501';end if;
 snapshot:=jsonb_build_object('schema_version','nexloop.context-pack.v2',
  'bindings',jsonb_build_object('tenant_id',tenant,'world_id',p_world,'run_id',issued.run_id,'source_principal',principal,'context_id',context_uuid,'namespace',namespace,'artifact_id',substr(encode(sha256(convert_to(p->>'artifact_identity_text','UTF8')),'hex'),1,32),'command_digest',p->>'command_binding_digest'),
  'user_statement',jsonb_build_object('message_id',outbox.message_id,'conversation_id',outbox.conversation_id,'sequence',(outbox.record->>'sequence')::bigint,'body',outbox.record->>'body','provenance','eios:object:'||outbox.message_id),
  'formal_facts',facts,'current_constraints',jsonb_build_object('action','nexloop.service.request:1','allow_effect',true,'budget_units',ctl.budget_units,'reserved_units',ctl.reserved_units,'valid_until',least(ctx.valid_until,ctl.valid_until,issued.expires_at),'executor_principal',ctl.executor_principal));
 snapshot:=snapshot||jsonb_build_object('supply',catalog_result->'supply');
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
end $$;
alter function authz.nexloop_context_artifact_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_command(text,text,text,text,text) from public,nexloop_identity,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker;
grant execute on function authz.nexloop_context_artifact_command(text,text,text,text,text) to nexloop_api;

-- Separate v2 protocol retains original current proof/event/lease checks.

-- Make every real Message Run consume its formal catalog, not optional hints.
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) rename to nexloop_runtime_activation_command_v0052;
revoke all on function authz.nexloop_runtime_activation_command_v0052(text,text,text,text,text)
 from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb;result jsonb;b runtime.nexloop_context_artifact_bindings;env jsonb;checked jsonb;supply jsonb;
begin
 result:=authz.nexloop_runtime_activation_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
 command:=(p->>'command_text')::jsonb;
 if not exists(select 1 from authz.nexloop_message_run_issuances i where i.run_id=(command->>'run_id')::uuid) then return result;end if;
 -- Original 52 already owns lease/fence and validates immutable pack binding.
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=a->>'tenant_id' and world=p_world and run_id=(command->>'run_id')::uuid;
 if not found or b.pack_text::jsonb->>'schema_version' is distinct from 'nexloop.context-pack.v2' then raise exception 'catalog context mandatory' using errcode='42501';end if;
 supply:=b.pack_text::jsonb->'supply';
 if jsonb_typeof(supply) is distinct from 'object' then raise exception 'catalog context mandatory' using errcode='42501';end if;
 if p->>'verb'='resolve' then return result||jsonb_build_object('_context_catalog',supply,'_context_consumer_id',substr(command->>'consumer_ref',10));
 elsif p->>'verb'='authorize' then
  env:=a->'context_catalog_envelope';
  if jsonb_typeof(env) is distinct from 'object' then raise exception 'catalog context mandatory' using errcode='42501';end if;
  checked:=authz.nexloop_service_catalog_scope(b.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  if checked->'allowed' is distinct from 'true'::jsonb or checked->'supply' is distinct from supply
   or (env->>'payload')::jsonb->>'consumer_id' is distinct from substr(command->>'consumer_ref',10) then raise exception 'catalog context stale' using errcode='42501';end if;
  result:=authz.nexloop_runtime_activation_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
  checked:=authz.nexloop_service_catalog_scope(b.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  if checked->'allowed' is distinct from 'true'::jsonb or checked->'supply' is distinct from supply then raise exception 'catalog context stale' using errcode='42501';end if;
 end if;
 return result;
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;

create table runtime.nexloop_effect_catalog_bindings (
 intent_id uuid primary key,tenant_id text not null,world text not null,
 offering_id text not null check(offering_id~'^[a-f0-9]{64}$'),offering_revision bigint not null check(offering_revision between 1 and 9007199254740991),
 offering_properties jsonb not null,created_at timestamptz not null,
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world)
);
alter table runtime.nexloop_effect_catalog_bindings owner to nexloop_owner;
alter table runtime.nexloop_effect_catalog_bindings enable row level security;
alter table runtime.nexloop_effect_catalog_bindings force row level security;
create policy effect_catalog_tenant on runtime.nexloop_effect_catalog_bindings to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_effect_catalog_bindings from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_catalog_immutable() returns trigger language plpgsql set search_path=pg_catalog as $$
begin raise exception 'effect catalog binding immutable' using errcode='42501';end $$;
alter function authz.nexloop_effect_catalog_immutable() owner to nexloop_owner;
revoke all on function authz.nexloop_effect_catalog_immutable() from public;
create trigger effect_catalog_immutable before update or delete on runtime.nexloop_effect_catalog_bindings for each row execute function authz.nexloop_effect_catalog_immutable();

-- Private metadata discovery is not a READ authorization or dispatch grant.
-- Every selected row subsequently requires Source signed object/property READ.
create function authz.nexloop_effect_catalog_metadata(p_run uuid,p_tenant text,p_world text)
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
   or b.pack_text::jsonb->>'schema_version' is distinct from 'nexloop.context-pack.v2'
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

create function authz.nexloop_effect_catalog_hint(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;r authz.nexloop_run_credentials;b runtime.nexloop_context_artifact_bindings;identity jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker') or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>131072
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or c->>'action_resource' is distinct from 'eios:action:nexloop.service.request:1' then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is distinct from 'null'::jsonb and identity->'run_context' is not null then
  select * into r from authz.nexloop_run_credentials rc where rc.token_digest=p_digest and rc.world=p_world;
 else raise exception 'effect catalog Run required' using errcode='42501';end if;
 if r.run_id is null then raise exception 'effect catalog Run required' using errcode='42501';end if;
 identity:=authz.nexloop_effect_catalog_metadata(r.run_id,c->>'tenant_id',p_world);
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect catalog expired' using errcode='42501';end if;
 return identity;
end $$;
alter function authz.nexloop_effect_catalog_hint(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_catalog_hint(text,text,text,text,text) from public,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_effect_catalog_hint(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

alter function authz.nexloop_effect_intent_command(text,text,text,text,text) rename to nexloop_effect_intent_command_v0052;
revoke all on function authz.nexloop_effect_intent_command_v0052(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_intent_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;result jsonb;hint jsonb;env jsonb;catalog jsonb;supply jsonb;bound runtime.nexloop_effect_catalog_bindings;
begin
 result:=authz.nexloop_effect_intent_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
 hint:=authz.nexloop_effect_catalog_hint(p_digest,p_world,p_text,p_signature,p_payload);
 env:=c->'catalog_envelope';
 if jsonb_typeof(env) is distinct from 'object' then raise exception 'effect catalog mandatory' using errcode='42501';end if;
 catalog:=authz.nexloop_service_catalog_scope(hint->>'_source_digest',p_world,env->>'text',env->>'signature',env->>'payload');supply:=catalog->'supply';
 if catalog->'allowed' is distinct from 'true'::jsonb then raise exception 'outside catalog terms' using errcode='22023';end if;
 if supply is distinct from hint->'supply' or (env->>'payload')::jsonb->>'consumer_id' is distinct from hint->>'consumer_id' then raise exception 'effect catalog stale' using errcode='42501';end if;
 if p->>'verb'='submit' then
  insert into runtime.nexloop_effect_catalog_bindings values((result->>'intent_id')::uuid,c->>'tenant_id',p_world,supply->>'offering_id',
   (supply->>'offering_revision')::bigint,supply->'properties',clock_timestamp()) on conflict do nothing;
 end if;
 select * into bound from runtime.nexloop_effect_catalog_bindings b where b.intent_id=(result->>'intent_id')::uuid and b.tenant_id=c->>'tenant_id' and b.world=p_world for share;
 if not found or bound.offering_id is distinct from supply->>'offering_id' or bound.offering_revision is distinct from (supply->>'offering_revision')::bigint or bound.offering_properties is distinct from supply->'properties' then raise exception 'effect catalog conflict' using errcode='23505';end if;
 -- Original Action/Run/ledger and current Source READ both rechecked after writes.
 perform authz.nexloop_effect_intent_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
 catalog:=authz.nexloop_service_catalog_scope(hint->>'_source_digest',p_world,env->>'text',env->>'signature',env->>'payload');
 if catalog->'allowed' is distinct from 'true'::jsonb or catalog->'supply' is distinct from supply then raise exception 'effect catalog stale' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect catalog expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- Existing QUERY observations remain independent. Only SEND/finalization add
-- catalog authority, and all original Action/Run/fence checks remain intact.
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) rename to nexloop_effect_execution_command_v0052;
revoke all on function authz.nexloop_effect_execution_command_v0052(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_execution_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;result jsonb;i runtime.nexloop_effect_intents;
 r authz.nexloop_run_credentials;b runtime.nexloop_context_artifact_bindings;pin runtime.nexloop_effect_catalog_bindings;
 env jsonb;catalog jsonb;supply jsonb;hint jsonb;old_lease timestamptz;action_lease timestamptz;k bytea;
begin
 if p->>'verb' in ('admit','finalize') then
  if session_user<>'nexloop_action_worker' or c->>'protocol' is distinct from 'nexloop-effect-execution-v1'
   or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then raise exception 'effect catalog unavailable' using errcode='42501';end if;
  select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
  if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-execution-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'effect catalog unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
  select eo.lease_until into old_lease from runtime.nexloop_effect_outbox eo where eo.intent_id=(p->>'intent_id')::uuid and eo.tenant_id=c->>'tenant_id' and eo.world=p_world;
 end if;
 -- The original signed function verifies caller, proofs and owns all its locks
 -- before any new metadata access. A later denial rolls its writes back.
 result:=authz.nexloop_effect_execution_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
 if p->>'verb' not in ('admit','finalize') then return result;end if;
 select ei.* into i from runtime.nexloop_effect_intents ei where ei.intent_id=(p->>'intent_id')::uuid and ei.tenant_id=c->>'tenant_id' and ei.world=p_world;
 if not found then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 select rc.* into r from authz.nexloop_run_credentials rc where rc.run_id=i.origin_run_id and rc.world=p_world;
 if not found then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 hint:=authz.nexloop_effect_catalog_metadata(r.run_id,i.tenant_id,p_world);
 env:=c->'catalog_envelope';
 if jsonb_typeof(env) is distinct from 'object' then raise exception 'effect catalog mandatory' using errcode='42501';end if;
 catalog:=authz.nexloop_service_catalog_scope(hint->>'_source_digest',p_world,env->>'text',env->>'signature',env->>'payload');supply:=catalog->'supply';
 if catalog->'allowed' is distinct from 'true'::jsonb or supply is distinct from hint->'supply'
  or (env->>'payload')::jsonb->>'consumer_id' is distinct from hint->>'consumer_id' then raise exception 'effect catalog stale' using errcode='42501';end if;
 select ecb.* into pin from runtime.nexloop_effect_catalog_bindings ecb where ecb.intent_id=i.intent_id and ecb.tenant_id=i.tenant_id and ecb.world=p_world for share;
 if not found or pin.offering_id is distinct from supply->>'offering_id' or pin.offering_revision is distinct from (supply->>'offering_revision')::bigint
  or pin.offering_properties is distinct from supply->'properties' then raise exception 'effect catalog stale' using errcode='42501';end if;
 -- finalize commit clears technical lease; the authoritative original call
 -- already captured and checked it before clearing. Recheck current Action,
 -- source proof and catalog TTL here; never replay a mutating original call.
 select (ac.claim->>'lease_expires_at')::timestamptz into action_lease from runtime.nexloop_action_claims ac
  where ac.intent_id=i.intent_id::text and ac.tenant_id=i.tenant_id and ac.world=p_world and ac.action_name=i.action_name;
 catalog:=authz.nexloop_service_catalog_scope(hint->>'_source_digest',p_world,env->>'text',env->>'signature',env->>'payload');
 if catalog->'allowed' is distinct from 'true'::jsonb or catalog->'supply' is distinct from supply then raise exception 'effect catalog stale' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(r.token_digest,p_world,p->'origin_proof');
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if r.expires_at<=clock_timestamp() or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (p->'origin_proof'->>'expires_at') is null or (p->'origin_proof'->>'expires_at')::timestamptz<=clock_timestamp()
  or old_lease is null or old_lease<=clock_timestamp() or action_lease is null or action_lease<=clock_timestamp() then raise exception 'effect catalog expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_execution_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_effect_execution_command(text,text,text,text,text) to nexloop_action_worker;

-- Preserve unchanged formal properties for every governed instance EDIT.
create or replace function authz.nexloop_edit_object_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;s jsonb;v_id text;v_outcome jsonb;v_object ontology.objects%rowtype;field text;proof jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'instance command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-object-edit-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-object-edit-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'instance permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active;
 if not found or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or d->'governance'->>'risk_level'<>'low' or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or d->'capability_binding'->>'capability_name' not in ('consumer.edit','ontology.object.edit')
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or jsonb_array_length(d->'governance'->'change_scope'->'properties')<>0
 then raise exception 'instance Action contract unavailable' using errcode='42501';end if;
 if not exists(select 1 from jsonb_array_elements(d->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=a->>'tenant_id' and r->>'stable_name'=body->>'type_name' and (r->>'version')::integer=(a->'schema'->>'version')::integer) then
  raise exception 'instance type outside Action scope' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id' and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer;
 if not found or s is distinct from a->'schema' or s->>'only_edit_via_actions'<>'true' then
  raise exception 'instance schema stale' using errcode='42501';end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=a->>'tenant_id' and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from a->>'principal_id' or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'instance claim fenced' using errcode='40001';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_id:=body->>'object_id';
 if v_id !~ '^[a-f0-9]{64}$' or jsonb_typeof(body->'properties') is distinct from 'object'
  or (body->>'expected_revision')::bigint<1 or body->'properties'='{}'::jsonb
  or a->'object_authority'->>'target_resource' is distinct from 'eios:object:'||(body->>'type_name')||'/'||v_id then
  raise exception 'object edit target invalid' using errcode='42501';end if;
 perform authz.nexloop_assert_edit_authority(p_digest,p_world,a->'object_authority');
 for field in select jsonb_object_keys(body->'properties') order by 1 loop
  select value into proof from jsonb_array_elements(a->'property_authorities')
   where value->>'target_resource'='eios:property:'||(body->>'type_name')||'/'||v_id||'/'||field;
  if not found then raise exception 'property edit proof missing' using errcode='42501';end if;
  perform authz.nexloop_assert_edit_authority(p_digest,p_world,proof);
 end loop;
 select * into v_object from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world
  and type_name=body->>'type_name' and object_id=v_id for update;
 if not found then raise exception 'object unavailable' using errcode='42501';end if;
 if v_object.schema_version<>(s->>'version')::integer or v_object.nexloop_revision<>(body->>'expected_revision')::bigint then
  raise exception 'object revision changed' using errcode='40001';end if;
 update ontology.objects set properties=properties||(body->'properties'),nexloop_revision=nexloop_revision+1,updated_at=clock_timestamp()
  where tenant_id=a->>'tenant_id' and world=p_world and type_name=body->>'type_name' and object_id=v_id;
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',v_object.nexloop_revision+1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 return jsonb_build_object('object_id',v_id,'type_name',body->>'type_name','world',p_world,'revision',v_object.nexloop_revision+1);
end $$;
