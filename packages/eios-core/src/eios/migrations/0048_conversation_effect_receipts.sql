-- CANDIDATE DRAFT: not scanned/catalogued/applied. Requires frozen 0047.
-- A Function is ATOMIC/read-only. The separate Conversation Action proof is
-- only an ownership/visibility gate, never permission to execute service.send.
create table control.nexloop_function_definitions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,resource_id text not null,
 definition jsonb not null,capability jsonb not null,schemas jsonb not null,
 active boolean not null default true,primary key(tenant_id,world,resource_id),
 check(world='real'),
 check(jsonb_typeof(definition)='object'),check(jsonb_typeof(capability)='object'),check(jsonb_typeof(schemas)='array'),
 check(definition->>'tenant_id' is not null and definition->>'tenant_id'=tenant_id),
 check(definition->>'definition_type' is not null and definition->>'definition_type'='function'),
 check(definition->>'status' is not null and definition->>'status'='published'),
 check(definition->>'stable_name' is not null and definition->>'stable_name'<>'') ,
 check(definition->>'version' is not null and definition->>'version' ~ '^[1-9][0-9]*$'),
 check(resource_id='eios:function:'||(definition->>'stable_name')||':'||(definition->>'version'))
);
alter table control.nexloop_function_definitions owner to nexloop_owner;
alter table control.nexloop_function_definitions enable row level security;
alter table control.nexloop_function_definitions force row level security;
create policy function_definition_tenant on control.nexloop_function_definitions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_function_definitions from public,nexloop_api,nexloop_identity,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_runtime;

create function authz.nexloop_function_definition_immutable() returns trigger language plpgsql set search_path=pg_catalog as $$
begin
 if (to_jsonb(new)-'active') is distinct from (to_jsonb(old)-'active') then
  raise exception 'published Function version is immutable' using errcode='22000';end if;
 return new;
end $$;
alter function authz.nexloop_function_definition_immutable() owner to nexloop_owner;
revoke all on function authz.nexloop_function_definition_immutable() from public;
create trigger function_definition_immutable before update on control.nexloop_function_definitions
 for each row execute function authz.nexloop_function_definition_immutable();
create trigger function_definition_authority_revision after insert or update or delete on control.nexloop_function_definitions
 for each row execute function authz.nexloop_authority_epoch_guard();

create function authz.nexloop_conversation_effect_receipt_query(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;owner_proof jsonb;
 v_tenant text;v_principal text;v_publication control.nexloop_function_definitions;schema_entry jsonb;v_schema jsonb;
 v_message runtime.nexloop_conversation_messages;v_conversation runtime.nexloop_conversations;v_owner control.nexloop_consumer_owners;
 v_route runtime.nexloop_message_routes;v_run authz.nexloop_run_credentials;v_binding authz.nexloop_runtime_run_bindings;
 v_link control.nexloop_effect_run_contexts;v_context control.nexloop_effect_contexts;v_job runtime.jobs;
 v_intent runtime.nexloop_effect_intents;v_attempt runtime.nexloop_effect_attempts;v_claim runtime.nexloop_action_claims;
 v_provider runtime.nexloop_effect_provider_bindings;v_observation runtime.nexloop_effect_observations;
 v_receipt jsonb:=null;v_run_projection jsonb:=null;v_success boolean:=false;v_found_intents integer;
 v_function text:='eios:function:nexloop.conversation.service_receipt:1';v_capability text:='nexloop.conversation.receipts.read';
begin
 -- Validate signature/current identity before any owner lookup or lock.
 if session_user<>'nexloop_api' or p_world is distinct from 'real'
  or p_text is null or p_payload is null or octet_length(p_text)>1048576 or octet_length(p_payload)>32768
  or jsonb_typeof(a) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
  or a->>'protocol' is distinct from 'nexloop-conversation-effect-query-v1'
  or a->>'resource_id' is distinct from v_function or a->>'action_resource' is distinct from v_function
  or a->>'operation' is distinct from 'execute'
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
 then raise exception 'conversation receipt denied' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-conversation-effect-query-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
 then raise exception 'conversation receipt denied' using errcode='42501';end if;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);
 v_tenant:=identity->'binding'->>'tenant_id';v_principal:=identity->'binding'->>'subject_principal_id';
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select * into v_publication from control.nexloop_function_definitions
  where tenant_id=v_tenant and world=p_world and resource_id=v_function and active for share;
 if not found or v_publication.definition->>'definition_type' is distinct from 'function'
  or v_publication.definition->>'status' is distinct from 'published'
  or v_publication.definition->>'tenant_id' is distinct from v_tenant
  or v_publication.definition->>'stable_name' is distinct from 'nexloop.conversation.service_receipt'
  or v_publication.definition->>'version' is distinct from '1'
  or v_publication.capability->>'kind' is distinct from 'atomic'
  or v_publication.capability->'has_side_effects' is distinct from 'false'::jsonb
  or v_publication.capability->>'capability_name' is distinct from v_capability
  or v_publication.definition->'capability_binding'->>'capability_name' is distinct from v_capability
  or v_publication.definition->'capability_binding'->'capability_version' is distinct from v_publication.capability->'capability_version'
  or v_publication.definition->'capability_binding'->'schema_hash' is distinct from v_publication.capability->'schema_hash'
  or jsonb_typeof(v_publication.schemas) is distinct from 'array' or jsonb_array_length(v_publication.schemas)<>3
 then raise exception 'conversation receipt Function unavailable' using errcode='42501';end if;
 for schema_entry in select value from jsonb_array_elements(v_publication.schemas) order by value->'reference'->>'stable_name' loop
  select definition into v_schema from ontology.object_type_versions where tenant_id=v_tenant
   and type_name=schema_entry->'reference'->>'stable_name' and version=(schema_entry->'reference'->>'version')::integer for share;
  if not found or v_schema is distinct from schema_entry->'definition'
   or schema_entry->'reference'->>'tenant_id' is distinct from v_tenant
   or schema_entry->'reference'->>'stable_name' not in ('Consumer','Conversation','Message')
  then raise exception 'conversation receipt Function schema stale' using errcode='42501';end if;
 end loop;
 if p->>'verb'='bundle' then
  if (select count(*) from jsonb_object_keys(p))<>1 then raise exception 'conversation receipt denied' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('definition',v_publication.definition,'capability',v_publication.capability,'schemas',v_publication.schemas);
 end if;
 owner_proof:=a->'ownership_proof';
 if p->>'verb' is distinct from 'receipt' or jsonb_typeof(p->'message_id') is distinct from 'string'
  or p->>'message_id' !~ '^[a-f0-9]{64}$' or (select count(*) from jsonb_object_keys(p))<>2
  or a->'definition' is distinct from v_publication.definition or a->'capability' is distinct from v_publication.capability
  or authz.nexloop_effect_parameters_valid(v_publication.definition->'input_schema',jsonb_build_object('message_id',p->>'message_id')) is distinct from true
  or jsonb_typeof(owner_proof) is distinct from 'object'
  or owner_proof->>'protocol' is distinct from 'nexloop-conversation-effect-query-v1'
  or owner_proof->>'resource_id' is distinct from 'eios:action:nexloop.conversation.read:1'
  or owner_proof->>'action_resource' is distinct from 'eios:action:nexloop.conversation.read:1'
  or owner_proof->>'parameters_digest' is distinct from a->>'parameters_digest'
  or owner_proof->>'expires_at' is null or (owner_proof->>'expires_at')::timestamptz<=clock_timestamp()
  or (owner_proof->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
 then raise exception 'conversation receipt denied' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,owner_proof);
 select * into v_owner from control.nexloop_consumer_owners where tenant_id=v_tenant and world=p_world and principal_id=v_principal for share;
 if not found or not exists(select 1 from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='ConsumerOwnership'
  and object_id=v_owner.ownership_id and properties->>'consumer_id'=v_owner.consumer_id and properties->>'principal_id'=v_principal)
 then raise exception 'conversation receipt denied' using errcode='42501';end if;
 select * into v_message from runtime.nexloop_conversation_messages where tenant_id=v_tenant and world=p_world and message_id=p->>'message_id';
 if not found then raise exception 'conversation receipt denied' using errcode='42501';end if;
 select * into v_conversation from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world
  and conversation_id=v_message.conversation_id and principal_id=v_principal and consumer_id=v_owner.consumer_id for share;
 if not found then raise exception 'conversation receipt denied' using errcode='42501';end if;
 select * into v_route from runtime.nexloop_message_routes where tenant_id=v_tenant and world=p_world and message_id=v_message.message_id;
 if found and v_route.task_id is not null then
  if not exists(select 1 from runtime.nexloop_message_outbox mo where mo.tenant_id=v_tenant and mo.world=p_world
    and mo.message_id=v_message.message_id and mo.status='delivered')
   or not exists(select 1 from runtime.nexloop_inbox ib where ib.tenant_id=v_tenant and ib.world=p_world
    and ib.source_id='webchat' and ib.event_id=v_route.event_id::text and ib.job_id=v_route.task_id)
  then raise exception 'conversation receipt ACK inconsistent' using errcode='42501';end if;
  select * into v_binding from authz.nexloop_runtime_run_bindings where run_id=v_route.run_id and tenant_id=v_tenant and world=p_world;
  if not found or v_binding.task_id is distinct from v_route.task_id or v_binding.run_digest is distinct from v_route.run_digest
   or v_binding.queue is distinct from v_route.queue or v_binding.command_digest is distinct from v_route.command_digest
   or v_binding.input_digest is distinct from v_route.input_digest then raise exception 'conversation receipt binding inconsistent' using errcode='42501';end if;
  select * into v_run from authz.nexloop_run_credentials where run_id=v_route.run_id and token_digest=v_route.run_digest;
  if not found or v_run.world is distinct from p_world
   or not exists(select 1 from authz.nexloop_service_credentials sc where sc.token_digest=v_run.source_digest and sc.tenant_id=v_tenant) then raise exception 'conversation receipt binding inconsistent' using errcode='42501';end if;
  select * into v_job from runtime.jobs where tenant_id=v_tenant and world=p_world and job_id=v_route.task_id;
  if not found or v_job.queue is distinct from v_route.queue or v_job.normalized_input->'run_command' is distinct from v_route.command
   or encode(sha256(convert_to(v_job.normalized_input->>'input','UTF8')),'hex') is distinct from v_route.input_digest
   or v_route.command->>'run_id' is distinct from v_route.run_id::text
  then raise exception 'conversation receipt binding inconsistent' using errcode='42501';end if;
  select * into v_link from control.nexloop_effect_run_contexts where run_id=v_route.run_id and context_id=v_route.context_id;
  if not found then raise exception 'conversation receipt binding inconsistent' using errcode='42501';end if;
  select * into v_context from control.nexloop_effect_contexts where context_id=v_link.context_id and tenant_id=v_tenant and world=p_world;
  if not found or v_context.consumer_id is distinct from v_owner.consumer_id
   or v_route.command->>'consumer_ref' is distinct from 'consumer:'||v_context.consumer_id
   or v_route.command->>'goal_version_ref' is distinct from v_context.goal_version_ref
  then raise exception 'conversation receipt binding inconsistent' using errcode='42501';end if;
  v_run_projection:=jsonb_build_object('run_id',v_route.run_id,'request_id',v_route.command->>'request_id','task_id',v_route.task_id,'status','queued');
  select count(*) into v_found_intents from runtime.nexloop_effect_intents i join runtime.nexloop_effect_submissions s on s.intent_id=i.intent_id
   where s.run_id=v_route.run_id and i.tenant_id=v_tenant and i.world=p_world and i.context_id=v_context.context_id
    and i.consumer_id=v_owner.consumer_id and s.source_digest=v_run.source_digest and i.action_name='nexloop.service.request';
  if v_found_intents>1 then raise exception 'conversation receipt association ambiguous' using errcode='42501';end if;
  if v_found_intents=1 then
   select i.* into v_intent from runtime.nexloop_effect_intents i join runtime.nexloop_effect_submissions s on s.intent_id=i.intent_id
    where s.run_id=v_route.run_id and i.tenant_id=v_tenant and i.world=p_world and i.context_id=v_context.context_id
     and i.consumer_id=v_owner.consumer_id and s.source_digest=v_run.source_digest and i.action_name='nexloop.service.request';
   if v_intent.frozen_request->>'consumer_id' is distinct from v_owner.consumer_id
    or v_intent.frozen_request->>'goal_version_ref' is distinct from v_context.goal_version_ref
   then raise exception 'conversation receipt association inconsistent' using errcode='42501';end if;
   select * into v_attempt from runtime.nexloop_effect_attempts where intent_id=v_intent.intent_id and tenant_id=v_tenant and world=p_world;
   select * into v_provider from runtime.nexloop_effect_provider_bindings where intent_id=v_intent.intent_id and tenant_id=v_tenant and world=p_world;
   if v_intent.state<>'accepted' and (v_attempt.intent_id is null or v_provider.intent_id is null)
    or v_attempt.intent_id is not null and (v_provider.intent_id is null or v_attempt.provider_payload_digest is distinct from v_intent.provider_payload_digest
      or v_attempt.executor_principal is distinct from v_intent.executor_principal)
   then raise exception 'conversation receipt provider inconsistent' using errcode='42501';end if;
   if v_intent.state in ('fulfilled','confirmed') or v_intent.governed_claim_finalized then
    select * into v_claim from runtime.nexloop_action_claims where tenant_id=v_tenant and world=p_world
     and action_name=v_intent.action_name and intent_id=v_intent.intent_id::text;
    if not found or v_claim.principal_id is distinct from v_intent.executor_principal
     or v_claim.claim->>'state' is distinct from 'terminal' or v_claim.claim->'terminal_outcome'->>'status' is distinct from 'succeeded'
     or v_claim.claim->'terminal_outcome'->>'outcome_id' is distinct from v_intent.receipt_id::text
     or v_attempt.state is distinct from 'fulfilled' or v_intent.governed_claim_finalized is distinct from true
     or not exists(select 1 from runtime.nexloop_effect_outbox where intent_id=v_intent.intent_id and tenant_id=v_tenant and world=p_world and state='fulfilled')
    then raise exception 'conversation governed receipt inconsistent' using errcode='42501';end if;
    v_success:=true;
   end if;
   select * into v_observation from runtime.nexloop_effect_observations where intent_id=v_intent.intent_id and tenant_id=v_tenant and world=p_world
    order by observed_at desc,observation_id desc limit 1;
   v_receipt:=jsonb_build_object('intent_id',v_intent.intent_id,'receipt_id',v_intent.receipt_id,'state',v_intent.state,
    'scope','service_delivery','governed_claim_finalized',v_success,'business_action_success',v_success,
    'provider_state',case when v_success then 'fulfilled' else v_observation.provider_state end);
  end if;
 end if;
 -- Both current proof chains and their independent deadlines are checked after
 -- all potential metadata/ownership waits. This query never finalizes a claim.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,owner_proof);
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (owner_proof->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz<=clock_timestamp()
 then raise exception 'conversation receipt expired' using errcode='42501';end if;
 return jsonb_build_object('message_id',v_message.message_id,'run',v_run_projection,'receipt',v_receipt);
end $$;
alter function authz.nexloop_conversation_effect_receipt_query(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_conversation_effect_receipt_query(text,text,text,text,text) from public,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime;
grant execute on function authz.nexloop_conversation_effect_receipt_query(text,text,text,text,text) to nexloop_api;
