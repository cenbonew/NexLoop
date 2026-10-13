-- NX-037 gap B (dispatcher 2026-10-13): a second governed effect Action, nexloop.enterprise.deliver:1, next to the Agent reply
-- channel nexloop.service.request:1. The admission path named the reply Action literally; this migration introduces the
-- deployment-level allow-list control.nexloop_effect_actions (seeded here, changed only by migrations: no runtime role can
-- write or read it) and replaces the literal checks with it. Each Action has its own plan slot; an effect context is bound
-- to the Action of its PlanStep, and an intent's Action is the one of its context. The reply-only paths stay literal on
-- purpose: Message Run issuance (until the context pack carries enterprise offerings, slice 2), the reply fallback Run
-- (ADR-023: exactly one reply tool), local JSON delivery and the conversation receipt queries.
-- The service offering assertion admits enterprise offerings (delivery_action nexloop.enterprise.deliver:1, priced in CNY),
-- and the catalog hint now requires the offering's delivery_action to be the intent's Action.
-- Bodies are copied unchanged from their current definitions except the marked substitutions; create or replace keeps
-- owner, grants and search_path. Published migrations and checksums are untouched.

create table control.nexloop_effect_actions (
 stable_name text primary key check(stable_name ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
 version integer not null check(version between 1 and 1000),
 slot_identity text not null unique check(slot_identity ~ '^[a-z0-9.]+:primary$'),
 resource_id text generated always as ('eios:action:'||stable_name||':'||version) stored unique
);
alter table control.nexloop_effect_actions owner to nexloop_owner;
alter table control.nexloop_effect_actions enable row level security;
alter table control.nexloop_effect_actions force row level security;
create policy effect_actions_owner on control.nexloop_effect_actions to nexloop_owner using(true) with check(true);
revoke all on control.nexloop_effect_actions from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity,nexloop_metrics;
insert into control.nexloop_effect_actions(stable_name,version,slot_identity) values
 ('nexloop.service.request',1,'service.request:primary'),
 ('nexloop.enterprise.deliver',1,'enterprise.deliver:primary');


CREATE OR REPLACE FUNCTION authz.nexloop_assert_effect_plan(p_context uuid, p_tenant text, p_world text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare b control.nexloop_effect_plan_bindings;l control.nexloop_effect_control_ledger;
 ctx control.nexloop_effect_contexts;g ontology.objects;s ontology.objects;o ontology.objects;r ontology.objects;
begin
 select * into b from control.nexloop_effect_plan_bindings where context_id=p_context and tenant_id=p_tenant and world=p_world;
 if not found then raise exception 'effect plan unavailable' using errcode='42501';end if;
 select * into l from control.nexloop_effect_control_ledger where control_id=b.control_id and tenant_id=p_tenant and world=p_world for update;
 if not found or l.control_revision is distinct from b.control_revision or l.valid_until<=clock_timestamp() then raise exception 'effect control stale' using errcode='42501';end if;
 select * into ctx from control.nexloop_effect_contexts where context_id=p_context and tenant_id=p_tenant and world=p_world for update;
 if not found or not ctx.allow_effect or ctx.valid_until<=clock_timestamp() then raise exception 'effect context unavailable' using errcode='42501';end if;
 for r in select * from ontology.objects where tenant_id=p_tenant and world=p_world and object_id in (b.goal_id,b.step_id,b.control_id) order by object_id for share loop
  if r.object_id=b.goal_id then g:=r;elsif r.object_id=b.step_id then s:=r;elsif r.object_id=b.control_id then o:=r;end if;
 end loop;
 if g.object_id is null or s.object_id is null or o.object_id is null
  or g.type_name is distinct from 'Goal' or s.type_name is distinct from 'PlanStep' or o.type_name is distinct from 'EffectControl'
  or g.nexloop_revision is distinct from b.goal_revision or s.nexloop_revision is distinct from b.step_revision or o.nexloop_revision is distinct from b.control_revision
  or g.properties->>'consumer_id' is distinct from ctx.consumer_id or s.properties->>'consumer_id' is distinct from ctx.consumer_id
  or s.properties->>'goal_id' is distinct from b.goal_id or s.properties->>'control_id' is distinct from b.control_id
  or s.properties->'submitter_principals' is distinct from b.submitter_principals or not exists(select 1 from control.nexloop_effect_actions ea where ea.stable_name=s.properties->>'action_name' and ea.slot_identity=ctx.slot_identity)
  or g.properties->>'state' is distinct from 'active' or s.properties->>'state' is distinct from 'ready'
  or o.properties->'allow_effect' is distinct from 'true'::jsonb
  or o.properties->>'owner_principal' is distinct from l.owner_principal or o.properties->>'executor_principal' is distinct from l.executor_principal
  or o.properties->>'consumer_id' is distinct from l.consumer_id or (o.properties->>'budget_units')::bigint is distinct from l.budget_units
  or g.properties->>'valid_until' is null or (g.properties->>'valid_until')::timestamptz<=clock_timestamp()
  or o.properties->>'valid_until' is null or (o.properties->>'valid_until')::timestamptz<=clock_timestamp()
  or ctx.executor_principal is distinct from l.executor_principal or ctx.plan_step_identity is distinct from 'step:'||b.step_id
  or ctx.slot_identity is null or ctx.goal_identity is distinct from 'goal:'||b.goal_id
 then raise exception 'effect plan stale' using errcode='42501';end if;
 perform authz.nexloop_service_identity_snapshot(l.owner_digest,p_world);
 if authz.nexloop_service_identity_snapshot(l.executor_digest,p_world)->'binding'->>'subject_principal_id' is distinct from l.executor_principal then
  raise exception 'effect executor stale' using errcode='42501';end if;
 return jsonb_build_object('control_id',l.control_id,'budget_units',l.budget_units,'reserved_units',l.reserved_units);
end $function$
;

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
  insert into control.nexloop_effect_run_contexts values(run_row.run_id,context_uuid,limit_at) on conflict(run_id) do nothing;
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
 select * into link from control.nexloop_effect_run_contexts where run_id=run_row.run_id for share;
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
 select * into link from control.nexloop_effect_run_contexts where run_id=r.run_id;
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
   where rl.run_id=r.run_id and rl.valid_until>clock_timestamp()
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
 identity:=authz.nexloop_effect_catalog_metadata(r.run_id,c->>'tenant_id',p_world);
 if 'eios:action:'||(identity->'supply'->'properties'->>'delivery_action') is distinct from c->>'action_resource' then
  raise exception 'outside catalog terms' using errcode='22023';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect catalog expired' using errcode='42501';end if;
 return identity;
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_assert_service_offering(p_tenant text, p_world text, p_consumer text, p_source text, p_offering text, p_revision bigint, p_binding text, p_scope jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
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
  or not (o.properties->>'delivery_action'='nexloop.service.request:1' and o.properties->>'service_code'='local.json-export'
    and o.properties->>'content_kind'='json-message-export' and o.properties->>'price_amount'='0'
    and o.properties->>'evidence_kind'='fsynced_json_export'
   or o.properties->>'delivery_action'='nexloop.enterprise.deliver:1' and o.properties->>'service_code' ~ '^enterprise\.[a-z0-9][a-z0-9.-]{0,62}$'
    and o.properties->>'content_kind'='enterprise-delivery' and o.properties->>'price_amount' ~ '^(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?$'
    and o.properties->>'evidence_kind'='enterprise_receipt')
  or o.properties->>'currency' is distinct from 'CNY'
  or o.properties->>'eligibility' is distinct from 'current_consumer_plan'
  or o.properties->'allowed_guarantees' is distinct from '[]'::jsonb or o.properties->'allowed_discounts' is distinct from '[]'::jsonb
  or o.properties->'active' is distinct from 'true'::jsonb
  or jsonb_typeof(o.properties->'title') is distinct from 'string' or length(o.properties->>'title') not between 1 and 256
  or o.properties->>'valid_until' is null or (o.properties->>'valid_until')::timestamptz<=clock_timestamp()
 then raise exception 'offering unavailable' using errcode='42501';end if;
 return jsonb_build_object('offering_id',o.object_id,'revision',o.nexloop_revision,'properties',o.properties,
  'provenance','eios:object:'||o.object_id);
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_service_catalog_scope(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
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
  'scope',case when offering->'properties'->>'delivery_action'='nexloop.enterprise.deliver:1' then jsonb_build_object(
   'service_code',offering->'properties'->>'service_code','deliverable',offering->'properties'->>'title','delivery_action','nexloop.enterprise.deliver:1',
   'price_amount',offering->'properties'->>'price_amount','currency',offering->'properties'->>'currency','guarantees','[]'::jsonb,'discounts','[]'::jsonb,
   'limitations',jsonb_build_array('不提供目录外保证或折扣','由企业系统执行，结果以企业回执为准'),'evidence_kind',offering->'properties'->>'evidence_kind')
  else jsonb_build_object('service_code','local.json-export','deliverable','固定私有目录内可核验的 JSON 文本导出文件','price_amount','0','currency','CNY','guarantees','[]'::jsonb,'discounts','[]'::jsonb,'limitations',jsonb_build_array('不提供目录外保证或折扣','不代表第三方渠道送达、付款或问题解决'),'evidence_kind','fsynced_json_export') end);
 -- Lock waits cannot resurrect expired READ proof or expired formal offer.
 for env in select value from jsonb_each(p->'reads') order by key loop
  perform authz.nexloop_read_object(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 perform authz.nexloop_assert_service_offering(tenant,p_world,p->>'consumer_id',principal,p->>'offering_id',
  (p->'request_scope'->>'offering_revision')::bigint,p->>'binding_id',p->'request_scope');
 return result;
end $function$
;
