-- ADR-030 (NX-037 gap B slice 2): a Run's effect contexts are keyed by (run_id, slot). A Run without an enterprise slot
-- keeps exactly its one reply (or single) row, so every existing path reads the same row as before. The slot of a link is
-- the slot of its context (filled by trigger; the inserters are unchanged except the conflict target). Intent admission
-- takes the context of the claimed Action's slot; readers that serve the reply prefer the reply slot (a single-slot Run is
-- unaffected); the catalog metadata selects the slot and the offering of the Action (message Runs: supply for the reply,
-- the effect_supplies item for another Action; other Runs: the bound offering that delivers through the Action).
-- Lock order when a Run gets two contexts in one transaction: reply slot first, then the enterprise slot (ADR-030 §2.5).
-- Bodies are copied from their current definitions except the marked substitutions; published migrations unchanged.

alter table control.nexloop_effect_run_contexts add column slot_identity text;
update control.nexloop_effect_run_contexts rl set slot_identity=c.slot_identity from control.nexloop_effect_contexts c where c.context_id=rl.context_id;
alter table control.nexloop_effect_run_contexts alter column slot_identity set not null;
alter table control.nexloop_effect_run_contexts drop constraint nexloop_effect_run_contexts_pkey;
alter table control.nexloop_effect_run_contexts add primary key(run_id,slot_identity);
alter table control.nexloop_effect_run_contexts add foreign key(slot_identity) references control.nexloop_effect_actions(slot_identity);
create function control.nexloop_effect_run_context_slot() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v text;
begin
 select c.slot_identity into v from control.nexloop_effect_contexts c where c.context_id=new.context_id;
 if v is null or new.slot_identity is not null and new.slot_identity is distinct from v then
  raise exception 'effect run context slot unavailable' using errcode='42501';end if;
 new.slot_identity:=v;return new;
end $$;
alter function control.nexloop_effect_run_context_slot() owner to nexloop_owner;
revoke all on function control.nexloop_effect_run_context_slot() from public;
create trigger effect_run_context_slot before insert or update on control.nexloop_effect_run_contexts
 for each row execute function control.nexloop_effect_run_context_slot();


CREATE OR REPLACE FUNCTION authz.nexloop_effect_context_command(p_digest text, p_world text, p_claims text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;executor_identity jsonb;run_identity jsonb;
 pub control.nexloop_action_definitions;l control.nexloop_effect_control_ledger;ctx control.nexloop_effect_contexts;
 goal ontology.objects;step ontology.objects;consumer ontology.objects;ctl ontology.objects;r ontology.objects;
 run_row authz.nexloop_run_credentials;claim runtime.nexloop_action_claims;field_name text;schema_body jsonb;context_uuid uuid;limit_at timestamptz;existing control.nexloop_effect_plan_bindings;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_claims)>1048576 or octet_length(p_payload)>65536 then raise exception 'effect registration unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-context-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-context-v1' or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or p->>'verb' is null or p->>'verb' not in ('configure','bind')
  or c->>'resource_id' is distinct from (case p->>'verb' when 'configure' then 'eios:action:nexloop.effect.configure_control:1' else 'eios:action:nexloop.plan.bind_effect_context:1' end)
 then raise exception 'effect registration unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is not null and identity->'run_context'<>'null'::jsonb then raise exception 'independent authority required' using errcode='42501';end if;
 select * into pub from control.nexloop_action_definitions where tenant_id=c->>'tenant_id' and world=p_world and resource_id=c->>'resource_id' and active for share;
 if not found or pub.definition is distinct from c->'definition' or pub.capability is distinct from c->'capability'
  or pub.definition->'governance'->>'approval_mode' is distinct from 'none' or pub.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(pub.definition->'governance'->'policy_refs') is distinct from 0 or jsonb_array_length(coalesce(pub.definition->'preconditions','[]'::jsonb))<>0
 then raise exception 'published registrar unavailable' using errcode='42501';end if;
 if pub.definition->'input_schema' is distinct from authz.nexloop_effect_registrar_schema(p->>'verb')
  or jsonb_typeof(p) is distinct from 'object'
  or (select count(*) from jsonb_object_keys(p)) is distinct from jsonb_array_length(pub.definition->'input_schema'->'required')
  or c->'definition_reference'->>'stable_name' is distinct from pub.definition->>'stable_name'
  or c->'definition_reference'->>'contract_digest' is distinct from pub.definition->>'contract_digest'
  or c->'definition_reference'->>'tenant_id' is distinct from c->>'tenant_id'
  or c->'definition_reference'->>'version' is distinct from '1'
  or c->'definition_reference'->>'definition_type' is distinct from 'action'
 then raise exception 'registrar input schema unavailable' using errcode='42501';end if;
 for field_name in select jsonb_array_elements_text(pub.definition->'input_schema'->'required') loop
  if field_name='verb' then continue;end if;
  if field_name in ('control_id','step_id') then
   if jsonb_typeof(p->field_name) is distinct from 'string' or (p->>field_name) !~ '^[a-f0-9]{64}$' then raise exception 'registrar input invalid' using errcode='22023';end if;
  else
   if jsonb_typeof(p->field_name) is distinct from 'number' or (p->>field_name)::numeric<>trunc((p->>field_name)::numeric)
    or (p->>field_name)::numeric not between 1 and 9223372036854775807 then raise exception 'registrar revision invalid' using errcode='22023';end if;
  end if;
 end loop;
 perform 1 from ontology.object_type_versions t where t.tenant_id=c->>'tenant_id' and exists(select 1 from jsonb_array_elements(c->'schemas') s
  where s->>'type_name'=t.type_name and (s->>'version')::integer=t.version) order by t.type_name,t.version for share;
 for schema_body in select * from jsonb_array_elements(c->'schemas') loop
  if schema_body->'only_edit_via_actions' is distinct from 'true'::jsonb or not exists(select 1 from ontology.object_type_versions t
   where t.tenant_id=c->>'tenant_id' and t.type_name=schema_body->>'type_name' and t.version=(schema_body->>'version')::integer and t.definition=schema_body)
  then raise exception 'registrar schema stale' using errcode='42501';end if;
 end loop;
 -- Real Action reserve/finalize is supplied by the private assembly on this same
 -- connection. This endpoint cannot act under an unrelated/terminal claim.
 select * into claim from runtime.nexloop_action_claims where tenant_id=c->>'tenant_id' and world=p_world
  and action_name=pub.definition->>'stable_name' and intent_id=c->>'claim_id' for update;
 if not found or claim.principal_id is distinct from c->>'principal_id'
  or claim.claim->>'state' is null or claim.claim->>'state' not in ('active','terminal')
  or claim.claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or claim.claim->'binding'->'action_reference' is distinct from c->'definition_reference'
  or claim.claim->'binding'->'capability_binding' is distinct from c->'capability_binding'
  or claim.claim->>'state'='active' and (claim.claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or claim.claim->>'state'='terminal' and claim.claim->'terminal_outcome'->>'status' is distinct from 'succeeded'
 then raise exception 'registrar Action claim unavailable' using errcode='42501';end if;
 if c->'executor_proof'->>'tenant_id' is distinct from c->>'tenant_id' or c->'executor_proof'->>'world' is distinct from p_world
  or p->>'verb'='bind' and (c->'run_proof'->>'tenant_id' is distinct from c->>'tenant_id' or c->'run_proof'->>'world' is distinct from p_world)
  or c->'executor_proof'->>'expires_at' is null
  or (c->'executor_proof'->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->'executor_proof'->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or p->>'verb'='bind' and (c->'run_proof'->>'expires_at' is null or (c->'run_proof'->>'expires_at')::timestamptz<=clock_timestamp()
   or (c->'run_proof'->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds')
 then raise exception 'registrar proof unavailable' using errcode='42501';end if;
 executor_identity:=authz.nexloop_service_identity_snapshot(c->>'executor_digest',p_world);
 perform authz.nexloop_assert_action_authority(c->>'executor_digest',p_world,c->'executor_proof');
 if not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->'executor_proof'->>'resource_id')
  or executor_identity->'run_context' is not null and executor_identity->'run_context'<>'null'::jsonb then raise exception 'executor unavailable' using errcode='42501';end if;
 if p->>'verb'='configure' then
  select * into ctl from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world and type_name='EffectControl' and object_id=p->>'control_id' for share;
  if not found or ctl.nexloop_revision is distinct from (p->>'control_revision')::bigint
   or ctl.properties->>'owner_principal' is distinct from c->>'principal_id'
   or ctl.properties->>'executor_principal' is distinct from executor_identity->'binding'->>'subject_principal_id'
   or ctl.properties->'allow_effect' is distinct from 'true'::jsonb or jsonb_typeof(ctl.properties->'budget_units') is distinct from 'number'
   or (ctl.properties->>'budget_units')::bigint<0 or ctl.properties->>'valid_until' is null
   or (ctl.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'control owner unavailable' using errcode='42501';end if;
  if not exists(select 1 from jsonb_array_elements(pub.definition->'governance'->'change_scope'->'object_types') t
   where t->>'tenant_id'=c->>'tenant_id' and t->>'stable_name'='EffectControl' and (t->>'version')::integer=ctl.schema_version)
  then raise exception 'control schema scope unavailable' using errcode='42501';end if;
  if not exists(select 1 from jsonb_array_elements(c->'schemas') t where t->>'type_name'='EffectControl' and (t->>'version')::integer=ctl.schema_version)
  then raise exception 'control schema snapshot unavailable' using errcode='42501';end if;
  select * into consumer from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world and type_name='Consumer' and object_id=ctl.properties->>'consumer_id' for share;
  if not found then raise exception 'Consumer unavailable' using errcode='42501';end if;
  insert into control.nexloop_effect_control_ledger values(ctl.object_id,c->>'tenant_id',p_world,consumer.object_id,c->>'principal_id',p_digest,
   ctl.properties->>'executor_principal',c->>'executor_digest',ctl.nexloop_revision,(ctl.properties->>'budget_units')::bigint,0,(ctl.properties->>'valid_until')::timestamptz)
  on conflict(control_id) do update set control_revision=excluded.control_revision,budget_units=excluded.budget_units,valid_until=excluded.valid_until
   where nexloop_effect_control_ledger.owner_principal=excluded.owner_principal and nexloop_effect_control_ledger.owner_digest=excluded.owner_digest
    and nexloop_effect_control_ledger.executor_principal=excluded.executor_principal and nexloop_effect_control_ledger.executor_digest=excluded.executor_digest
    and nexloop_effect_control_ledger.consumer_id=excluded.consumer_id and nexloop_effect_control_ledger.tenant_id=excluded.tenant_id and nexloop_effect_control_ledger.world=excluded.world
  returning * into l;
  if not found then raise exception 'control identity conflict' using errcode='42501';end if;
  context_uuid:=null;
 else
  run_identity:=authz.nexloop_service_identity_snapshot(c->>'run_digest',p_world);
  perform authz.nexloop_assert_action_authority(c->>'run_digest',p_world,c->'run_proof');
  if run_identity->'run_context' is null or run_identity->'run_context'='null'::jsonb
   or not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->'run_proof'->>'resource_id') then raise exception 'Run required' using errcode='42501';end if;
  select * into run_row from authz.nexloop_run_credentials where token_digest=c->>'run_digest' for share;
  if not found then raise exception 'Run required' using errcode='42501';end if;
  -- Lookup Step is not authority until actual planner proof plus schema bundle
  -- and all real linked object revisions are checked below.
  select * into step from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world and type_name='PlanStep' and object_id=p->>'step_id';
  if not found then raise exception 'Step unavailable' using errcode='42501';end if;
  select * into l from control.nexloop_effect_control_ledger where control_id=step.properties->>'control_id' and tenant_id=c->>'tenant_id' and world=p_world for update;
  if not found then raise exception 'configured control required' using errcode='42501';end if;
  for r in select * from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world
   and object_id in(step.object_id,step.properties->>'goal_id',l.control_id,l.consumer_id) order by object_id for share loop
   if r.object_id=step.object_id then step:=r;elsif r.object_id=step.properties->>'goal_id' then goal:=r;
   elsif r.object_id=l.control_id then ctl:=r;elsif r.object_id=l.consumer_id then consumer:=r;end if;
  end loop;
  if goal.object_id is null or ctl.object_id is null or consumer.object_id is null or goal.type_name<>'Goal' or ctl.type_name<>'EffectControl' or consumer.type_name<>'Consumer'
   or step.nexloop_revision is distinct from (p->>'step_revision')::bigint or goal.nexloop_revision is distinct from (p->>'goal_revision')::bigint
   or consumer.nexloop_revision is distinct from (p->>'consumer_revision')::bigint or ctl.nexloop_revision is distinct from (p->>'control_revision')::bigint
   or l.control_revision is distinct from ctl.nexloop_revision or l.valid_until<=clock_timestamp()
   or step.properties->>'consumer_id' is distinct from consumer.object_id or goal.properties->>'consumer_id' is distinct from consumer.object_id
   or jsonb_typeof(step.properties->'submitter_principals') is distinct from 'array'
   or jsonb_array_length(step.properties->'submitter_principals') not between 1 and 32
   or not(step.properties->'submitter_principals' ? (run_identity->'binding'->>'subject_principal_id'))
   or exists(select 1 from jsonb_array_elements(step.properties->'submitter_principals') v where jsonb_typeof(v)<>'string' or length(v#>>'{}') not between 1 and 200)
   or step.properties->'submitter_principals' is distinct from (select jsonb_agg(v order by v collate "C") from (select distinct value#>>'{}' v from jsonb_array_elements(step.properties->'submitter_principals')) allowed)
   or c->>'principal_id'=run_identity->'binding'->>'subject_principal_id' or c->>'principal_id'=l.owner_principal
   or l.executor_principal is distinct from executor_identity->'binding'->>'subject_principal_id' or l.executor_digest is distinct from c->>'executor_digest'
   or not exists(select 1 from control.nexloop_effect_actions ea where ea.stable_name=step.properties->>'action_name'
    and ea.resource_id=c->'run_proof'->>'resource_id' and ea.resource_id=c->'executor_proof'->>'resource_id')
   or step.properties->>'state' is distinct from 'ready'
   or goal.properties->>'state' is distinct from 'active' or ctl.properties->'allow_effect' is distinct from 'true'::jsonb
  then raise exception 'plan authority unavailable' using errcode='42501';end if;
  -- Current published registrar must explicitly govern every formal type/version.
  for r in select * from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world and object_id in(step.object_id,goal.object_id,ctl.object_id,consumer.object_id) loop
   if not exists(select 1 from jsonb_array_elements(pub.definition->'governance'->'change_scope'->'object_types') t
    where t->>'tenant_id'=c->>'tenant_id' and t->>'stable_name'=r.type_name and (t->>'version')::integer=r.schema_version)
   then raise exception 'registrar schema scope unavailable' using errcode='42501';end if;
   if not exists(select 1 from jsonb_array_elements(c->'schemas') t where t->>'type_name'=r.type_name and (t->>'version')::integer=r.schema_version)
   then raise exception 'registrar schema snapshot unavailable' using errcode='42501';end if;
  end loop;
  limit_at:=least(run_row.expires_at,l.valid_until,(goal.properties->>'valid_until')::timestamptz);
  if limit_at is null or limit_at<=clock_timestamp() then raise exception 'plan expired' using errcode='42501';end if;
  select * into existing from control.nexloop_effect_plan_bindings where tenant_id=c->>'tenant_id' and world=p_world and step_id=step.object_id for update;
  if found then
   context_uuid:=existing.context_id;
   if existing.goal_id<>goal.object_id or existing.goal_revision<>goal.nexloop_revision or existing.step_revision<>step.nexloop_revision
    or existing.control_id<>ctl.object_id or existing.control_revision<>ctl.nexloop_revision or existing.submitter_principals is distinct from step.properties->'submitter_principals'
   then raise exception 'effect context conflict' using errcode='P0001';end if;
  else
   context_uuid:=gen_random_uuid();
   insert into control.nexloop_effect_contexts values(context_uuid,c->>'tenant_id',p_world,consumer.object_id,'goal:'||goal.object_id,
    'goal:'||goal.object_id||':revision:'||goal.nexloop_revision||':step:'||step.nexloop_revision||':control:'||ctl.nexloop_revision,
    'step:'||step.object_id,(select ea.slot_identity from control.nexloop_effect_actions ea where ea.stable_name=step.properties->>'action_name'),consumer.nexloop_revision,ctl.nexloop_revision,true,least(l.valid_until,(goal.properties->>'valid_until')::timestamptz),
    l.budget_units,0,jsonb_build_array(jsonb_build_object('resource_ref','consumer:'||consumer.object_id,'revision',consumer.nexloop_revision)),l.executor_principal);
   insert into control.nexloop_effect_plan_bindings values(context_uuid,c->>'tenant_id',p_world,goal.object_id,goal.nexloop_revision,step.object_id,step.nexloop_revision,ctl.object_id,ctl.nexloop_revision,step.properties->'submitter_principals');
  end if;
  insert into control.nexloop_effect_run_contexts values(run_row.run_id,context_uuid,limit_at) on conflict(run_id,slot_identity) do nothing;
  if not exists(select 1 from control.nexloop_effect_run_contexts where run_id=run_row.run_id and context_id=context_uuid and valid_until>clock_timestamp()) then raise exception 'Run context conflict' using errcode='42501';end if;
  perform authz.nexloop_assert_effect_plan(context_uuid,c->>'tenant_id',p_world);
  perform authz.nexloop_assert_action_authority(c->>'run_digest',p_world,c->'run_proof');
 end if;
 perform authz.nexloop_assert_action_authority(c->>'executor_digest',p_world,c->'executor_proof');
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if l.valid_until<=clock_timestamp() or (ctl.properties->>'valid_until')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->'executor_proof'->>'expires_at')::timestamptz<=clock_timestamp()
  or p->>'verb'='bind' and (c->'run_proof'->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'registrar expired' using errcode='42501';end if;
 return jsonb_build_object('context_ref',context_uuid,'control_id',coalesce(ctl.object_id,l.control_id),'scope','effect_context','business_action_success',false);
end $function$
;

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
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') or c->>'resource_id' not like '%:'||(p->>'action_version')
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
end $function$
;

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
  or (not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') or c->>'resource_id' not like '%:'||(p->>'action_version'))
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
end $function$
;

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
  or (not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') or c->>'resource_id' not like '%:'||(p->>'action_version'))
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
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_assert_message_source(p_digest text, p_world text, p_tenant text, p_consumer text, p_command jsonb, p_proofs jsonb)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare identity jsonb;r authz.nexloop_run_credentials;link control.nexloop_effect_run_contexts;
 ctx control.nexloop_effect_contexts;plan control.nexloop_effect_plan_bindings;
 ctl control.nexloop_effect_control_ledger;proof jsonb;o ontology.objects;matched integer:=0;
begin
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is null or identity->'run_context'='null'::jsonb
  or identity->'binding'->>'tenant_id' is distinct from p_tenant then raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into r from authz.nexloop_run_credentials where token_digest=p_digest for share;
 if not found or r.run_id is distinct from (p_command->>'run_id')::uuid or r.expires_at<=clock_timestamp()
  or p_command->>'tenant_id' is distinct from p_tenant or p_command->>'world_id' is distinct from p_world
  or p_command->>'mode' is distinct from 'real' or p_command->>'consumer_ref' is distinct from 'consumer:'||p_consumer
  or (p_command->>'not_after')::timestamptz>r.expires_at or (p_command->>'not_after')::timestamptz<=clock_timestamp()
  or jsonb_typeof(p_proofs) is distinct from 'array' or jsonb_array_length(p_proofs) not between 1 and 32 then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(p_proofs) loop
  perform authz.nexloop_assert_action_authority(p_digest,p_world,proof);
  if proof->>'tenant_id' is distinct from p_tenant then raise exception 'message runtime unavailable' using errcode='42501';end if;
 end loop;
 -- Exact actual Run allowlist coverage; no missing source Action proof.
 if exists(select 1 from jsonb_array_elements_text(identity->'run_context'->'allowed_resources') x
   where not exists(select 1 from jsonb_array_elements(p_proofs) y where y->>'action_resource'=x)) then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into link from control.nexloop_effect_run_contexts where run_id=r.run_id order by (slot_identity<>'service.request:primary'),slot_identity limit 1 for share;
 if not found or link.valid_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into ctx from control.nexloop_effect_contexts where context_id=link.context_id and tenant_id=p_tenant and world=p_world for share;
 if not found or ctx.consumer_id is distinct from p_consumer or not ctx.allow_effect or ctx.valid_until<=clock_timestamp()
  or ctx.goal_version_ref is distinct from p_command->>'goal_version_ref' then raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into plan from control.nexloop_effect_plan_bindings where context_id=ctx.context_id and tenant_id=p_tenant and world=p_world for share;
 if not found or not(plan.submitter_principals ? (identity->'binding'->>'subject_principal_id')) then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into ctl from control.nexloop_effect_control_ledger where control_id=plan.control_id and tenant_id=p_tenant and world=p_world for share;
 if not found or ctl.consumer_id is distinct from p_consumer or ctl.control_revision<>plan.control_revision
  or ctl.valid_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
 for o in select * from ontology.objects where tenant_id=p_tenant and world=p_world
  and object_id in(p_consumer,plan.goal_id,plan.step_id,plan.control_id) order by object_id for share loop
  if o.object_id=p_consumer and o.type_name='Consumer' and o.nexloop_revision=ctx.consumer_revision then matched:=matched+1;
  elsif o.object_id=plan.goal_id and o.type_name='Goal' and o.nexloop_revision=plan.goal_revision
   and o.properties->>'consumer_id'=p_consumer and o.properties->>'state'='active'
   and (o.properties->>'valid_until')::timestamptz>clock_timestamp() then matched:=matched+1;
  elsif o.object_id=plan.step_id and o.type_name='PlanStep' and o.nexloop_revision=plan.step_revision
   and o.properties->>'consumer_id'=p_consumer and o.properties->>'goal_id'=plan.goal_id
   and o.properties->>'control_id'=plan.control_id and o.properties->>'state'='ready'
   and o.properties->'submitter_principals'=plan.submitter_principals then matched:=matched+1;
  elsif o.object_id=plan.control_id and o.type_name='EffectControl' and o.nexloop_revision=plan.control_revision
   and o.properties->>'consumer_id'=p_consumer and o.properties->'allow_effect'='true'::jsonb
   and (o.properties->>'valid_until')::timestamptz>clock_timestamp() then matched:=matched+1;
  else raise exception 'message runtime unavailable' using errcode='42501';end if;
 end loop;
 if matched<>4 or r.expires_at<=clock_timestamp() or link.valid_until<=clock_timestamp() or ctx.valid_until<=clock_timestamp()
  or ctl.valid_until<=clock_timestamp() or (p_command->>'not_after')::timestamptz<=clock_timestamp() then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(p_proofs) loop perform authz.nexloop_assert_action_authority(p_digest,p_world,proof);end loop;
 return ctx.context_id;
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_context_artifact_command_v0052(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
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
end $function$
;

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

CREATE OR REPLACE FUNCTION authz.nexloop_context_artifact_fallback_core(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
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
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_relationship_context_artifact_command(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
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
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_role_context_command_before_role_ttl_v0063(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
 SET "TimeZone" TO 'UTC'
AS $function$
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
 select context_id into context_uuid from control.nexloop_effect_run_contexts where run_id=run.run_id order by (slot_identity<>'service.request:primary'),slot_identity limit 1;
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
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_role_formal_current(r uuid, w text, reads jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare b authz.nexloop_role_run_bindings;cb runtime.nexloop_role_context_artifacts;rc control.nexloop_effect_run_contexts;ctx control.nexloop_effect_contexts;pb control.nexloop_effect_plan_bindings;facts jsonb;fact jsonb;expected text;
begin
 select * into b from authz.nexloop_role_run_bindings where run_id=r and world=w for share;
 if not found then raise exception 'role formal binding unavailable' using errcode='42501';end if;
 select * into cb from runtime.nexloop_role_context_artifacts where run_id=r and tenant_id=b.tenant_id and world=w for share;
 if not found or cb.source_digest is distinct from b.source_digest then raise exception 'role formal Context unavailable' using errcode='42501';end if;
 select * into rc from control.nexloop_effect_run_contexts where run_id=r order by (slot_identity<>'service.request:primary'),slot_identity limit 1 for share;
 if not found or rc.context_id is distinct from cb.context_id then raise exception 'role formal chain unavailable' using errcode='42501';end if;
 select * into ctx from control.nexloop_effect_contexts where context_id=rc.context_id and tenant_id=b.tenant_id and world=w for share;
 if not found or ctx.consumer_id is distinct from b.consumer_id then raise exception 'role formal Consumer chain unavailable' using errcode='42501';end if;
 select * into pb from control.nexloop_effect_plan_bindings where context_id=rc.context_id and tenant_id=b.tenant_id and world=w for share;
 if not found or pb.step_id is distinct from b.step_id then raise exception 'role formal plan unavailable' using errcode='42501';end if;
 facts:=cb.pack_text::jsonb->'formal_facts';
 for fact in select value from jsonb_array_elements(facts) loop
  expected:=case fact->>'type' when 'Consumer' then b.consumer_id when 'Goal' then pb.goal_id when 'PlanStep' then pb.step_id when 'EffectControl' then pb.control_id else null end;
  if expected is null or fact->>'id' is distinct from expected or fact->>'provenance' is distinct from 'eios:object:'||expected then raise exception 'role formal actual reference mismatch' using errcode='42501';end if;
 end loop;
 return authz.nexloop_context_formal_current(b.source_digest,w,facts,reads);
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_catalog_metadata_v0401(p_run uuid, p_tenant text, p_world text, p_resource text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare r authz.nexloop_run_credentials;link control.nexloop_effect_run_contexts;reply_link control.nexloop_effect_run_contexts;item jsonb;ctx control.nexloop_effect_contexts;
 b runtime.nexloop_context_artifact_bindings;identity jsonb;candidate ontology.objects;offered ontology.objects;n integer:=0;supply jsonb;
begin
 if p_run is null or p_tenant is null or p_world is distinct from 'real'
  or not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=p_resource) then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from authz.nexloop_run_credentials rc where rc.run_id=p_run and rc.world=p_world;
 if not found or r.status is distinct from 'active' or r.expires_at<=clock_timestamp() then raise exception 'effect catalog Run unavailable' using errcode='42501';end if;
 identity:=authz.nexloop_service_identity_snapshot(r.source_digest,p_world);
 if identity->'binding'->>'tenant_id' is distinct from p_tenant or identity->'run_context' is distinct from 'null'::jsonb then raise exception 'effect catalog Source unavailable' using errcode='42501';end if;
 select * into link from control.nexloop_effect_run_contexts er where er.run_id=r.run_id and er.slot_identity=(select ea.slot_identity from control.nexloop_effect_actions ea where ea.resource_id=p_resource);
 if not found or link.valid_until<=clock_timestamp() then raise exception 'effect catalog Plan unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_effect_plan(link.context_id,p_tenant,p_world);
 select * into ctx from control.nexloop_effect_contexts ec where ec.context_id=link.context_id and ec.tenant_id=p_tenant and ec.world=p_world;
 if not found then raise exception 'effect catalog Plan unavailable' using errcode='42501';end if;
 if authz.nexloop_is_message_run(r.run_id) then
  select * into b from runtime.nexloop_run_binding(p_tenant,p_world,r.run_id,false);
  -- The Run's context artifact is bound to its reply context, whichever Action is claimed.
  select * into reply_link from control.nexloop_effect_run_contexts er where er.run_id=r.run_id and er.slot_identity='service.request:primary';
  if not found or b.source_digest is distinct from r.source_digest or b.context_id is distinct from reply_link.context_id
   or (b.pack_text::jsonb->>'schema_version' is null or b.pack_text::jsonb->>'schema_version' not in ('nexloop.context-pack.v2','nexloop.context-pack.v4','nexloop.context-pack.v6'))
   or substr(b.command_binding->>'consumer_ref',10) is distinct from ctx.consumer_id then raise exception 'effect catalog mandatory' using errcode='42501';end if;
  if p_resource='eios:action:nexloop.service.request:1' then
   supply:=b.pack_text::jsonb->'supply';
  else
   select e.value into item from jsonb_array_elements(coalesce(b.pack_text::jsonb->'effect_supplies','[]'::jsonb)) e where 'eios:action:'||(e.value->>'action')=p_resource;
   if item is null then raise exception 'effect catalog mandatory' using errcode='42501';end if;
   supply:=item-'action';
  end if;
 else
  -- No guessed default or silent first row: multiple governed grants require
  -- explicit future selection; this minimal entry remains closed on ambiguity.
  for candidate in select o.* from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='ConsumerServiceOffering'
   and o.properties->>'consumer_id'=ctx.consumer_id and o.properties->>'source_principal'=identity->'binding'->>'subject_principal_id'
   and o.properties->'active'='true'::jsonb
   and exists(select 1 from ontology.objects so where so.object_id=o.properties->>'offering_id' and so.tenant_id=p_tenant and so.world=p_world
    and so.type_name='ServiceOffering' and 'eios:action:'||(so.properties->>'delivery_action')=p_resource)
   order by o.object_id limit 2 loop n:=n+1;end loop;
  if n<>1 then raise exception 'effect catalog registration unavailable' using errcode='42501';end if;
  select * into offered from ontology.objects o where o.object_id=candidate.properties->>'offering_id' and o.tenant_id=p_tenant and o.world=p_world and o.type_name='ServiceOffering';
  if not found then raise exception 'effect catalog unavailable' using errcode='42501';end if;
  supply:=jsonb_build_object('offering_id',offered.object_id,'offering_revision',offered.nexloop_revision,'binding_id',candidate.object_id,
   'binding_revision',candidate.nexloop_revision,'provenance','eios:object:'||offered.object_id,'properties',offered.properties);
 end if;
 return jsonb_build_object('_source_digest',r.source_digest,'supply',supply,'consumer_id',ctx.consumer_id);
end $function$
;

alter function authz.nexloop_effect_catalog_metadata_v0401(uuid,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_catalog_metadata_v0401(uuid,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create or replace function authz.nexloop_effect_catalog_metadata(p_run uuid,p_tenant text,p_world text) returns jsonb
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select authz.nexloop_effect_catalog_metadata_v0401(p_run,p_tenant,p_world,'eios:action:nexloop.service.request:1')
$$;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_catalog_hint(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;r authz.nexloop_run_credentials;b runtime.nexloop_context_artifact_bindings;identity jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker') or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>131072
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'action_resource') or c->>'action_resource' is distinct from c->>'resource_id' then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is distinct from 'null'::jsonb and identity->'run_context' is not null then
  select * into r from authz.nexloop_run_credentials rc where rc.token_digest=p_digest and rc.world=p_world;
 else raise exception 'effect catalog Run required' using errcode='42501';end if;
 if r.run_id is null then raise exception 'effect catalog Run required' using errcode='42501';end if;
 identity:=authz.nexloop_effect_catalog_metadata_v0401(r.run_id,c->>'tenant_id',p_world,c->>'action_resource');
 if 'eios:action:'||(identity->'supply'->'properties'->>'delivery_action') is distinct from c->>'action_resource' then
  raise exception 'outside catalog terms' using errcode='22023';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect catalog expired' using errcode='42501';end if;
 return identity;
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_execution_command_before_roles_v0062(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
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
 hint:=authz.nexloop_effect_catalog_metadata_v0401(r.run_id,i.tenant_id,p_world,'eios:action:'||i.action_name||':'||i.action_version);
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
end $function$
;
