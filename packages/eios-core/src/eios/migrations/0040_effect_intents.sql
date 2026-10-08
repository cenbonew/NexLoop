-- Candidate NX-015 lineage. Context/control authority has no application write
-- endpoint yet: without an independently governed planner, production is closed.
create table control.nexloop_effect_contexts (
 context_id uuid primary key,tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 consumer_id text not null,goal_identity text not null,goal_version_ref text not null,
 plan_step_identity text not null,slot_identity text not null,
 consumer_revision bigint not null check(consumer_revision>0),control_revision bigint not null check(control_revision>0),
 allow_effect boolean not null,valid_until timestamptz not null,
 budget_units bigint not null check(budget_units>=0),reserved_units bigint not null default 0 check(reserved_units>=0 and reserved_units<=budget_units),
 expected_versions jsonb not null check(jsonb_typeof(expected_versions)='array' and jsonb_array_length(expected_versions)>0),
 executor_principal text not null,
 unique(tenant_id,world,consumer_id,goal_identity,plan_step_identity,slot_identity)
);
create function authz.nexloop_effect_context_identity_immutable() returns trigger
language plpgsql set search_path=pg_catalog as $$
begin
 if (new.context_id,new.tenant_id,new.world,new.consumer_id,new.goal_identity,new.plan_step_identity,new.slot_identity,new.executor_principal)
  is distinct from (old.context_id,old.tenant_id,old.world,old.consumer_id,old.goal_identity,old.plan_step_identity,old.slot_identity,old.executor_principal) then
  raise exception 'effect context identity immutable' using errcode='22023';end if;
 return new;
end $$;
alter function authz.nexloop_effect_context_identity_immutable() owner to nexloop_owner;
revoke all on function authz.nexloop_effect_context_identity_immutable() from public;
create trigger effect_context_identity_immutable before update on control.nexloop_effect_contexts
 for each row execute function authz.nexloop_effect_context_identity_immutable();
create table control.nexloop_effect_run_contexts (
 run_id uuid primary key references authz.nexloop_run_credentials(run_id),
 context_id uuid not null references control.nexloop_effect_contexts(context_id),valid_until timestamptz not null
);
create table runtime.nexloop_effect_intents (
 intent_id uuid primary key,receipt_id uuid not null unique,
 context_id uuid not null references control.nexloop_effect_contexts(context_id),tenant_id text not null,world text not null,
 consumer_id text not null,goal_identity text not null,plan_step_identity text not null,slot_identity text not null,
 action_name text not null,action_version integer not null check(action_version>0),
 business_digest text not null check(business_digest ~ '^[a-f0-9]{64}$'),
 provider_payload_digest text not null check(provider_payload_digest ~ '^[a-f0-9]{64}$'),
 frozen_request jsonb not null,action_definition jsonb not null,capability jsonb not null,
 executor_principal text not null,control_revision bigint not null,created_at timestamptz not null,
 state text not null check(state in ('accepted','dispatching','unknown','fulfilled','failed','confirmed')),
 unique(tenant_id,world,consumer_id,goal_identity,plan_step_identity,slot_identity,action_name)
);
create table runtime.nexloop_effect_submissions (
 intent_id uuid not null references runtime.nexloop_effect_intents(intent_id),run_id uuid not null references authz.nexloop_run_credentials(run_id),
 principal_id text not null,source_digest text not null,created_at timestamptz not null,primary key(intent_id,run_id)
);
create table runtime.nexloop_effect_outbox (
 intent_id uuid primary key references runtime.nexloop_effect_intents(intent_id),tenant_id text not null,world text not null,
 state text not null default 'pending' check(state in ('pending','leased','unknown','fulfilled','failed')),
 fence bigint not null default 0 check(fence>=0),lease_until timestamptz,lease_credential text,
 available_at timestamptz not null,created_at timestamptz not null
);
alter table control.nexloop_effect_contexts owner to nexloop_owner;
alter table control.nexloop_effect_run_contexts owner to nexloop_owner;
alter table runtime.nexloop_effect_intents owner to nexloop_owner;
alter table runtime.nexloop_effect_submissions owner to nexloop_owner;
alter table runtime.nexloop_effect_outbox owner to nexloop_owner;
alter table control.nexloop_effect_contexts enable row level security;
alter table control.nexloop_effect_contexts force row level security;
create policy effect_context_tenant on control.nexloop_effect_contexts to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
alter table runtime.nexloop_effect_intents enable row level security;
alter table runtime.nexloop_effect_intents force row level security;
create policy effect_intent_tenant on runtime.nexloop_effect_intents to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
alter table runtime.nexloop_effect_submissions enable row level security;
alter table runtime.nexloop_effect_submissions force row level security;
create policy effect_submission_tenant on runtime.nexloop_effect_submissions to nexloop_owner
 using(exists(select 1 from runtime.nexloop_effect_intents i where i.intent_id=nexloop_effect_submissions.intent_id
  and i.tenant_id=current_setting('eios.tenant_id',true)))
 with check(exists(select 1 from runtime.nexloop_effect_intents i where i.intent_id=nexloop_effect_submissions.intent_id
  and i.tenant_id=current_setting('eios.tenant_id',true)));
alter table runtime.nexloop_effect_outbox enable row level security;
alter table runtime.nexloop_effect_outbox force row level security;
create policy effect_outbox_tenant on runtime.nexloop_effect_outbox to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
-- Run-context is an owner-only technical linkage; runtime ledger tables all
-- enforce tenant RLS in addition to the signed current authorization boundary.
revoke all on control.nexloop_effect_contexts,control.nexloop_effect_run_contexts,runtime.nexloop_effect_intents,
 runtime.nexloop_effect_submissions,runtime.nexloop_effect_outbox
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;

-- Deliberately small supported schema subset; unsupported schema keywords
-- fail closed in SQL too. This is not advertised as generic JSON Schema.
create function authz.nexloop_effect_parameters_valid(s jsonb,p jsonb) returns boolean
language plpgsql immutable set search_path=pg_catalog as $$
declare name text;spec jsonb;value jsonb;
begin
 if jsonb_typeof(s) is distinct from 'object' or s->>'type' is distinct from 'object'
  or jsonb_typeof(s->'properties') is distinct from 'object' or jsonb_typeof(s->'required') is distinct from 'array'
  or s->'additionalProperties' is distinct from 'false'::jsonb or jsonb_typeof(p) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(s) x where x not in ('type','properties','required','additionalProperties')) then return false;end if;
 for value in select * from jsonb_array_elements(s->'required') loop
  if jsonb_typeof(value) is distinct from 'string' or not(p ? (value#>>'{}')) then return false;end if;
 end loop;
 for name,value in select * from jsonb_each(p) loop
  spec:=s->'properties'->name;
  if spec is null or spec->>'type' is distinct from 'string' or jsonb_typeof(value) is distinct from 'string'
   or exists(select 1 from jsonb_object_keys(spec) x where x not in ('type','minLength','maxLength','enum')) then return false;end if;
  if spec ? 'minLength' and char_length(value#>>'{}')<(spec->>'minLength')::integer then return false;end if;
  if spec ? 'maxLength' and char_length(value#>>'{}')>(spec->>'maxLength')::integer then return false;end if;
  if spec ? 'enum' and (jsonb_typeof(spec->'enum') is distinct from 'array' or not(spec->'enum' @> jsonb_build_array(value))) then return false;end if;
 end loop;
 return true;
exception when others then return false;
end $$;
alter function authz.nexloop_effect_parameters_valid(jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_parameters_valid(jsonb,jsonb) from public;
create function authz.nexloop_effect_intent_command(p_digest text,p_world text,p_claims text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;run_row authz.nexloop_run_credentials;
 ctx control.nexloop_effect_contexts;link control.nexloop_effect_run_contexts;consumer ontology.objects;
 published control.nexloop_action_definitions;stored runtime.nexloop_effect_intents;
 semantic jsonb;expected jsonb;digest text;now_at timestamptz;new_intent uuid;new_receipt uuid;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_claims)>1048576 or octet_length(p_payload)>131072 then
  raise exception 'effect authority unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or c->>'resource_id' is distinct from 'eios:action:nexloop.service.request:'||(p->>'action_version')
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
   and plan_step_identity=ctx.plan_step_identity and slot_identity=ctx.slot_identity and action_name='nexloop.service.request';
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
   'consumer_revision',ctx.consumer_revision,'action_name','nexloop.service.request','action_version',(p->>'action_version')::integer,
   'action_contract_digest',published.definition->>'contract_digest','capability',published.capability,'parameters',p->'parameters');
  digest:=encode(sha256(convert_to(semantic::text,'UTF8')),'hex');
  select * into stored from runtime.nexloop_effect_intents where tenant_id=ctx.tenant_id and world=p_world and consumer_id=ctx.consumer_id
   and goal_identity=ctx.goal_identity and plan_step_identity=ctx.plan_step_identity and slot_identity=ctx.slot_identity and action_name='nexloop.service.request' for update;
  if found then
   if stored.business_digest is distinct from digest or stored.provider_payload_digest is distinct from p->>'provider_payload_digest' then
    raise exception 'effect_payload_conflict' using errcode='P0001';end if;
  else
   if ctx.reserved_units>=ctx.budget_units then raise exception 'effect budget unavailable' using errcode='42501';end if;
   new_intent:=pg_catalog.gen_random_uuid();new_receipt:=pg_catalog.gen_random_uuid();
   insert into runtime.nexloop_effect_intents values(new_intent,new_receipt,ctx.context_id,ctx.tenant_id,p_world,ctx.consumer_id,ctx.goal_identity,
    ctx.plan_step_identity,ctx.slot_identity,'nexloop.service.request',(p->>'action_version')::integer,digest,p->>'provider_payload_digest',semantic,
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
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
