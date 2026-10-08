-- DRAFT ONLY: not cataloged, not executed. Append after frozen 0040.
create table control.nexloop_effect_control_ledger (
 control_id text primary key,tenant_id text not null,world text not null,
 consumer_id text not null,owner_principal text not null,owner_digest text not null,
 executor_principal text not null,executor_digest text not null,
 control_revision bigint not null check(control_revision>0),budget_units bigint not null check(budget_units>=0),
 reserved_units bigint not null default 0 check(reserved_units>=0 and reserved_units<=budget_units),valid_until timestamptz not null
);
create table control.nexloop_effect_plan_bindings (
 context_id uuid primary key references control.nexloop_effect_contexts(context_id),
 tenant_id text not null,world text not null,goal_id text not null,goal_revision bigint not null,
 step_id text not null,step_revision bigint not null,control_id text not null references control.nexloop_effect_control_ledger(control_id),
 control_revision bigint not null,submitter_principals jsonb not null,
 unique(tenant_id,world,step_id)
);
create table runtime.nexloop_effect_control_reservations (
 intent_id uuid primary key references runtime.nexloop_effect_intents(intent_id),
 tenant_id text not null,control_id text not null references control.nexloop_effect_control_ledger(control_id),created_at timestamptz not null
);
alter table control.nexloop_effect_control_ledger owner to nexloop_owner;
alter table control.nexloop_effect_plan_bindings owner to nexloop_owner;
alter table runtime.nexloop_effect_control_reservations owner to nexloop_owner;
alter table control.nexloop_effect_control_ledger enable row level security;
alter table control.nexloop_effect_control_ledger force row level security;
alter table control.nexloop_effect_plan_bindings enable row level security;
alter table control.nexloop_effect_plan_bindings force row level security;
alter table runtime.nexloop_effect_control_reservations enable row level security;
alter table runtime.nexloop_effect_control_reservations force row level security;
create policy effect_control_tenant on control.nexloop_effect_control_ledger to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy effect_plan_tenant on control.nexloop_effect_plan_bindings to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy effect_reservation_tenant on runtime.nexloop_effect_control_reservations to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_effect_control_ledger,control.nexloop_effect_plan_bindings,runtime.nexloop_effect_control_reservations
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;

-- Checks current formal object revisions, not caller metadata. Shared control is
-- locked before the context and object locks, matching registrar lock ordering.
create function authz.nexloop_assert_effect_plan(p_context uuid,p_tenant text,p_world text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
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
  or s.properties->'submitter_principals' is distinct from b.submitter_principals or s.properties->>'action_name' is distinct from 'nexloop.service.request'
  or g.properties->>'state' is distinct from 'active' or s.properties->>'state' is distinct from 'ready'
  or o.properties->'allow_effect' is distinct from 'true'::jsonb
  or o.properties->>'owner_principal' is distinct from l.owner_principal or o.properties->>'executor_principal' is distinct from l.executor_principal
  or o.properties->>'consumer_id' is distinct from l.consumer_id or (o.properties->>'budget_units')::bigint is distinct from l.budget_units
  or g.properties->>'valid_until' is null or (g.properties->>'valid_until')::timestamptz<=clock_timestamp()
  or o.properties->>'valid_until' is null or (o.properties->>'valid_until')::timestamptz<=clock_timestamp()
  or ctx.executor_principal is distinct from l.executor_principal or ctx.plan_step_identity is distinct from 'step:'||b.step_id
  or ctx.slot_identity is distinct from 'service.request:primary' or ctx.goal_identity is distinct from 'goal:'||b.goal_id
 then raise exception 'effect plan stale' using errcode='42501';end if;
 perform authz.nexloop_service_identity_snapshot(l.owner_digest,p_world);
 if authz.nexloop_service_identity_snapshot(l.executor_digest,p_world)->'binding'->>'subject_principal_id' is distinct from l.executor_principal then
  raise exception 'effect executor stale' using errcode='42501';end if;
 return jsonb_build_object('control_id',l.control_id,'budget_units',l.budget_units,'reserved_units',l.reserved_units);
end $$;
alter function authz.nexloop_assert_effect_plan(uuid,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_effect_plan(uuid,text,text) from public;

-- This Action supports this exact narrow object schema, not generic JSONSchema.
create function authz.nexloop_effect_registrar_schema(v text) returns jsonb
language sql immutable set search_path=pg_catalog as $$
 select case v when 'configure' then '{"type":"object","properties":{"verb":{"const":"configure"},"control_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"control_revision":{"type":"integer","minimum":1,"maximum":9223372036854775807}},"required":["control_id","control_revision","verb"],"additionalProperties":false}'::jsonb
 when 'bind' then '{"type":"object","properties":{"verb":{"const":"bind"},"step_id":{"type":"string","pattern":"^[a-f0-9]{64}$"},"step_revision":{"type":"integer","minimum":1,"maximum":9223372036854775807},"goal_revision":{"type":"integer","minimum":1,"maximum":9223372036854775807},"consumer_revision":{"type":"integer","minimum":1,"maximum":9223372036854775807},"control_revision":{"type":"integer","minimum":1,"maximum":9223372036854775807}},"required":["consumer_revision","control_revision","goal_revision","step_id","step_revision","verb"],"additionalProperties":false}'::jsonb end
$$;
alter function authz.nexloop_effect_registrar_schema(text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_registrar_schema(text) from public;
create function authz.nexloop_effect_context_command(p_digest text,p_world text,p_claims text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
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
 if c->'executor_proof'->>'resource_id' is distinct from 'eios:action:nexloop.service.request:1'
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
   or c->'run_proof'->>'resource_id' is distinct from 'eios:action:nexloop.service.request:1' then raise exception 'Run required' using errcode='42501';end if;
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
   or step.properties->>'action_name' is distinct from 'nexloop.service.request' or step.properties->>'state' is distinct from 'ready'
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
    'step:'||step.object_id,'service.request:primary',consumer.nexloop_revision,ctl.nexloop_revision,true,least(l.valid_until,(goal.properties->>'valid_until')::timestamptz),
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
end $$;
alter function authz.nexloop_effect_context_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_context_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_effect_context_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

alter function authz.nexloop_effect_intent_command(text,text,text,text,text) rename to nexloop_effect_intent_command_v0040;
revoke all on function authz.nexloop_effect_intent_command_v0040(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker;
create function authz.nexloop_effect_intent_command(p_digest text,p_world text,p_claims text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;r authz.nexloop_run_credentials;link control.nexloop_effect_run_contexts;
 plan jsonb;outcome jsonb;added integer;k bytea;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_claims)>1048576 or octet_length(p_payload)>131072 then raise exception 'effect authority unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or c->>'resource_id' is distinct from 'eios:action:nexloop.service.request:'||(p->>'action_version')
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
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
