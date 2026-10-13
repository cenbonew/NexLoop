-- NX-037 gap B, L4 review of 0400 (F1, F2). Published migrations unchanged.
-- F1: 0400 checked the intent's declared resource against the payload's action_version with a suffix LIKE, so a missing or
--     wildcard action_version passed the SQL find/submit/admit commands. The three intent commands are copied from the
--     current definition (0401) with only that comparison replaced by exact equality with the allow-list row's version.
-- F2: the effect Action allow-list is append-only (no update/delete except by dropping the trigger in a migration) and the
--     revoke covers every application role.

CREATE OR REPLACE FUNCTION authz.nexloop_effect_intent_command_v0040(p_digest text, p_world text, p_claims text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;run_row authz.nexloop_run_credentials;
 ctx control.nexloop_effect_contexts;link control.nexloop_effect_run_contexts;consumer ontology.objects;
 published control.nexloop_action_definitions;stored runtime.nexloop_effect_intents;
 semantic jsonb;expected jsonb;digest text;now_at timestamptz;new_intent uuid;new_receipt uuid;v_action text;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_claims)>1048576 or octet_length(p_payload)>131072 then
  raise exception 'effect authority unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') or not exists(select 1 from control.nexloop_effect_actions ev where ev.resource_id=c->>'resource_id' and ev.version::text=p->>'action_version')
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or p->>'verb' is null or p->>'verb' not in ('submit','find') then raise exception 'effect authority unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is null or identity->'run_context'='null'::jsonb then raise exception 'effect Run required' using errcode='42501';end if;
 select * into run_row from authz.nexloop_run_credentials where token_digest=p_digest for share;
 if not found then raise exception 'effect Run required' using errcode='42501';end if;
 select * into link from control.nexloop_effect_run_contexts where run_id=run_row.run_id and slot_identity=(select ea.slot_identity from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') for share;
 if not found or link.valid_until<=clock_timestamp() then raise exception 'effect context unavailable' using errcode='42501';end if;
 select * into ctx from control.nexloop_effect_contexts where context_id=link.context_id
  and tenant_id=c->>'tenant_id' and world=p_world for update;
 if not found or not ctx.allow_effect or ctx.valid_until<=clock_timestamp() then raise exception 'effect controls unavailable' using errcode='42501';end if;
 select ea.stable_name into v_action from control.nexloop_effect_actions ea where ea.slot_identity=ctx.slot_identity and ea.resource_id=c->>'resource_id';
 if v_action is null then raise exception 'effect Action outside context' using errcode='42501';end if;
 select * into consumer from ontology.objects where tenant_id=ctx.tenant_id and world=p_world and type_name='Consumer' and object_id=ctx.consumer_id for share;
 if not found or consumer.nexloop_revision is distinct from ctx.consumer_revision then raise exception 'effect consumer stale' using errcode='42501';end if;
 select * into published from control.nexloop_action_definitions where tenant_id=ctx.tenant_id and world=p_world and resource_id=c->>'resource_id' and active for share;
 if not found or published.definition is distinct from c->'definition' or published.capability is distinct from c->'capability'
  or published.definition->'governance'->>'approval_mode' is distinct from 'none'
  or published.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(published.definition->'governance'->'policy_refs') is distinct from 0
  or jsonb_array_length(coalesce(published.definition->'preconditions','[]'::jsonb))<>0 then
  raise exception 'effect published Action unavailable' using errcode='42501';end if;
 if not exists(select 1 from jsonb_array_elements(published.definition->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=ctx.tenant_id and r->>'stable_name'='Consumer' and (r->>'version')::integer=consumer.schema_version) then
  raise exception 'effect Consumer outside Action' using errcode='42501';end if;
 for expected in select * from jsonb_array_elements(ctx.expected_versions) loop
  if jsonb_typeof(expected) is distinct from 'object' or expected->>'resource_ref' is distinct from 'consumer:'||ctx.consumer_id
   or jsonb_typeof(expected->'revision') is distinct from 'number' or (expected->>'revision')::bigint is distinct from consumer.nexloop_revision
   or (select count(*) from jsonb_object_keys(expected))<>2 then
   raise exception 'effect expected versions unsupported or stale' using errcode='42501';end if;
 end loop;
 if p->>'verb'='find' then
  select * into stored from runtime.nexloop_effect_intents where intent_id=(p->>'intent_id')::uuid
   and tenant_id=ctx.tenant_id and world=p_world and consumer_id=ctx.consumer_id and goal_identity=ctx.goal_identity
   and plan_step_identity=ctx.plan_step_identity and slot_identity=ctx.slot_identity and action_name=v_action;
  if not found then raise exception 'effect receipt unavailable' using errcode='42501';end if;
 else
  if jsonb_typeof(p->'parameters') is distinct from 'object' or p->>'parameters_text' is null
   or (p->>'parameters_text')::jsonb is distinct from p->'parameters'
   or p->>'provider_payload_digest' is distinct from encode(sha256(convert_to(p->>'parameters_text','UTF8')),'hex') then
   raise exception 'effect parameters unavailable' using errcode='22023';end if;
  if authz.nexloop_effect_parameters_valid(published.definition->'input_schema',p->'parameters') is distinct from true then
   raise exception 'effect parameters schema rejected' using errcode='22023';end if;
  semantic:=jsonb_build_object('consumer_id',ctx.consumer_id,'goal_identity',ctx.goal_identity,'goal_version_ref',ctx.goal_version_ref,
   'plan_step_identity',ctx.plan_step_identity,'slot_identity',ctx.slot_identity,'expected_versions',ctx.expected_versions,
   'consumer_revision',ctx.consumer_revision,'action_name',v_action,'action_version',(p->>'action_version')::integer,
   'action_contract_digest',published.definition->>'contract_digest','capability',published.capability,'parameters',p->'parameters');
  digest:=encode(sha256(convert_to(semantic::text,'UTF8')),'hex');
  select * into stored from runtime.nexloop_effect_intents where tenant_id=ctx.tenant_id and world=p_world and consumer_id=ctx.consumer_id
   and goal_identity=ctx.goal_identity and plan_step_identity=ctx.plan_step_identity and slot_identity=ctx.slot_identity and action_name=v_action for update;
  if found then
   if stored.business_digest is distinct from digest or stored.provider_payload_digest is distinct from p->>'provider_payload_digest' then
    raise exception 'effect_payload_conflict' using errcode='P0001';end if;
  else
   if ctx.reserved_units>=ctx.budget_units then raise exception 'effect budget unavailable' using errcode='42501';end if;
   new_intent:=pg_catalog.gen_random_uuid();new_receipt:=pg_catalog.gen_random_uuid();
   insert into runtime.nexloop_effect_intents values(new_intent,new_receipt,ctx.context_id,ctx.tenant_id,p_world,ctx.consumer_id,ctx.goal_identity,
    ctx.plan_step_identity,ctx.slot_identity,v_action,(p->>'action_version')::integer,digest,p->>'provider_payload_digest',semantic,
    published.definition,published.capability,ctx.executor_principal,ctx.control_revision,clock_timestamp(),'accepted') returning * into stored;
   insert into runtime.nexloop_effect_outbox(intent_id,tenant_id,world,available_at,created_at) values(stored.intent_id,ctx.tenant_id,p_world,clock_timestamp(),clock_timestamp());
   update control.nexloop_effect_contexts set reserved_units=reserved_units+1 where context_id=ctx.context_id;
  end if;
  insert into runtime.nexloop_effect_submissions values(stored.intent_id,run_row.run_id,c->>'principal_id',run_row.source_digest,clock_timestamp()) on conflict do nothing;
 end if;
 -- Repeat all current authority/controls after lock waits and before ACK. Writes
 -- roll back together if a final proof or time check fails.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 perform authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ctx.valid_until<=clock_timestamp() or link.valid_until<=clock_timestamp() or run_row.expires_at<=clock_timestamp()
  or (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect authority expired' using errcode='42501';end if;
 return jsonb_build_object('intent_id',stored.intent_id,'receipt_id',stored.receipt_id,'state',stored.state,
  'payload_digest',stored.business_digest,'provider_payload_digest',stored.provider_payload_digest,'scope','effect_intent','business_action_success',false);
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_intent_command_v0041(p_digest text, p_world text, p_claims text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;r authz.nexloop_run_credentials;link control.nexloop_effect_run_contexts;
 plan jsonb;outcome jsonb;added integer;k bytea;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_claims)>1048576 or octet_length(p_payload)>131072 then raise exception 'effect authority unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or (not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') or not exists(select 1 from control.nexloop_effect_actions ev where ev.resource_id=c->>'resource_id' and ev.version::text=p->>'action_version'))
 then raise exception 'effect authority unavailable' using errcode='42501';end if;
 -- Verify the original full signed chain before trusting any caller envelope or
 -- looking up an owner's linkage. Inner admission remains unchanged and repeats.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 select * into r from authz.nexloop_run_credentials where token_digest=p_digest for share;
 select * into link from control.nexloop_effect_run_contexts where run_id=r.run_id and slot_identity=(select ea.slot_identity from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id');
 plan:=authz.nexloop_assert_effect_plan(link.context_id,c->>'tenant_id',p_world);
 outcome:=authz.nexloop_effect_intent_command_v0040(p_digest,p_world,p_claims,p_signature,p_payload);
 if p->>'verb'='submit' then
  insert into runtime.nexloop_effect_control_reservations values((outcome->>'intent_id')::uuid,c->>'tenant_id',plan->>'control_id',clock_timestamp()) on conflict do nothing;
  get diagnostics added=row_count;
  if added=1 then
   update control.nexloop_effect_control_ledger set reserved_units=reserved_units+1 where control_id=plan->>'control_id' and reserved_units<budget_units;
   if not found then raise exception 'shared effect budget exhausted' using errcode='42501';end if;
  end if;
 end if;
 perform authz.nexloop_assert_effect_plan(link.context_id,c->>'tenant_id',p_world);
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 return outcome;
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_intent_command_v0052(p_digest text, p_world text, p_claims text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;refs jsonb;k bytea;
 outcome jsonb;r authz.nexloop_run_credentials;ctx control.nexloop_effect_contexts;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker')
  or p_claims is null or p_payload is null or octet_length(p_claims)>1048576 or octet_length(p_payload)>131072
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
 then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or (not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') or not exists(select 1 from control.nexloop_effect_actions ev where ev.resource_id=c->>'resource_id' and ev.version::text=p->>'action_version'))
 then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if p ? 'runtime_refs' then
  refs:=p->'runtime_refs';
  if jsonb_typeof(refs) is distinct from 'object' then
   raise exception 'runtime effect context unavailable' using errcode='42501';end if;
  if (select count(*) from jsonb_object_keys(refs))<>2
   or jsonb_typeof(refs->'consumer_ref') is distinct from 'string'
   or jsonb_typeof(refs->'goal_version_ref') is distinct from 'string'
   or length(refs->>'consumer_ref')>128 or length(refs->>'goal_version_ref')>256
  then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
 end if;
 -- The original function acquires ledger -> context -> object locks. Do not
 -- introduce a context lock ahead of the ledger. All writes remain uncommitted;
 -- a declaration mismatch rolls back intent, outbox and quota together.
 outcome:=authz.nexloop_effect_intent_command_v0041(p_digest,p_world,p_claims,p_signature,p_payload);
 if p ? 'runtime_refs' then
  select * into r from authz.nexloop_run_credentials rc where rc.token_digest=p_digest for share;
  if not found then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
  select ec.* into ctx from control.nexloop_effect_run_contexts rl
   join control.nexloop_effect_contexts ec on ec.context_id=rl.context_id
   where rl.run_id=r.run_id and rl.slot_identity=(select ea.slot_identity from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') and rl.valid_until>clock_timestamp()
    and ec.tenant_id=c->>'tenant_id' and ec.world=p_world and ec.valid_until>clock_timestamp()
   for share of rl,ec;
  if not found or refs->>'consumer_ref' is distinct from 'consumer:'||ctx.consumer_id
   or refs->>'goal_version_ref' is distinct from ctx.goal_version_ref
  then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_effect_plan(ctx.context_id,c->>'tenant_id',p_world);
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'runtime effect context unavailable' using errcode='42501';end if;
 return outcome;
end $function$;


create trigger effect_actions_append_only before update or delete on control.nexloop_effect_actions for each row execute function control.nexloop_nx022_append_only();
create trigger effect_actions_no_truncate before truncate on control.nexloop_effect_actions for each statement execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_effect_actions from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics;
