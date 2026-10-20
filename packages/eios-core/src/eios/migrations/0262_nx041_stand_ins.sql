-- NX-041 S4 (design §3, D4, D11, D12, §11 S4): Action stand-ins, the simulated ledger and an executor without network egress. The
-- migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. control.nexloop_stand_ins: the stand-in catalog (D11): every external effect path in place today has one - service request over
--    the HTTP provider and over local JSON delivery, incentive issue, outreach send. An effect without a stand-in is unavailable in a
--    simulation world (fail closed).
-- 2. control.nexloop_stand_in_configs: a simulation world's stand-in behaviour (D12). Absent means all fulfilled with zero latency;
--    failure, unknown and latency must be written explicitly (frozen with the experiment's inputs).
-- 3. The simulated ledger, physically apart from the real one: runtime.nexloop_sim_effect_intents and runtime.nexloop_sim_effect_receipts.
--    Every row is world sim:/shadow:, data_mode simulation; every receipt simulated=true with its stand-in version and output kind
--    simulated_output (never a fact). No real metric, reconciliation or cost function reads these tables.
-- 4. Entries for a simulation world credential only: authz.nexloop_sim_effect_submit (record an intent), authz.nexloop_sim_effect_execute
--    (the stand-in executor: deterministic receipt from (stand-in version, world, intent, simulated clock), a repeat returns the same
--    receipt and counts the repeat), authz.nexloop_sim_effect_query (by intent, as GET /v1/effects/<intent_id>) and
--    authz.nexloop_sim_effect_pending. Real and test are refused; the real dispatch switch is never consulted (it is closed for any
--    world but real, 0153).
-- 5. The real effect path never holds a sim:/shadow: row (a CHECK on every declared external effect table, review 1.1/1.3), so no
--    real executor can lease one. control.nexloop_real_effect_tables declares each table with a world column on an outbound path and
--    whether it is guarded (a guard test fails for an undeclared one).
-- 6. Review fixes: the receipt outcome is drawn from the world's clock (S5 runtime.nexloop_world_now, never the client) and a
--    server-generated salt kept with the intent (review 4.1/4.2); the stand-in executor connects as its own role
--    nexloop_stand_in_worker (review 3.1), which executes the stand-in entries only.

-- 1. The catalog ----------------------------------------------------------------------------------------------------------------
create table control.nexloop_stand_ins (
 stand_in_id text primary key check(stand_in_id~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
 action_name text not null check(action_name~'^nexloop\.[a-z0-9_.]+$'),action_version integer not null check(action_version>0),
 provider_kind text not null check(provider_kind in ('http','local_json','incentive_ledger','outreach_outbox')),
 stand_in_version text not null check(stand_in_version~'^[0-9]+\.[0-9]+\.[0-9]+$'),
 semantics text not null check(char_length(semantics) between 1 and 500),
 registered_at timestamptz not null default clock_timestamp(),
 unique(action_name,action_version,provider_kind));
alter table control.nexloop_stand_ins owner to nexloop_owner;
revoke all on control.nexloop_stand_ins from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics;
create trigger nx041_stand_ins_append_only before update or delete on control.nexloop_stand_ins for each row execute function control.nexloop_nx022_append_only();
insert into control.nexloop_stand_ins(stand_in_id,action_name,action_version,provider_kind,stand_in_version,semantics) values
 ('service.request.http','nexloop.service.request',1,'http','1.0.0','HTTP service endpoint: the receipt of GET /v1/effects/<intent_id> (status, external reference, latency), no request leaves the host'),
 ('service.request.local_json','nexloop.service.request',1,'local_json','1.0.0','Local JSON delivery (NX-016): a delivered receipt by intent, no file is written'),
 ('incentive.issue','nexloop.incentive.issue',1,'incentive_ledger','1.0.0','Incentive issue: the loopback ledger receipt (incentives.LocalIncentiveLedger semantics), no value is issued'),
 ('outreach.send','nexloop.outreach.send',1,'outreach_outbox','1.0.0','Outreach send: the loopback outbox receipt (text digest only, receipt by intent), nothing is sent');

-- 2. Behaviour per simulation world (D12) ---------------------------------------------------------------------------------------
create table control.nexloop_stand_in_configs (
 world text primary key check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 config jsonb not null check(jsonb_typeof(config)='object'),config_digest text not null check(config_digest~'^[0-9a-f]{64}$'),
 recorded_by text not null check(char_length(recorded_by) between 1 and 200),recorded_at timestamptz not null default clock_timestamp());

-- 3. The simulated ledger -------------------------------------------------------------------------------------------------------
create table runtime.nexloop_sim_effect_intents (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 intent_id uuid not null,stand_in_id text not null references control.nexloop_stand_ins(stand_in_id),
 business_digest text not null check(business_digest~'^[0-9a-f]{64}$'),request_digest text not null check(request_digest~'^[0-9a-f]{64}$'),
 origin text not null check(origin in ('twin','replay','injected')),data_mode text not null default 'simulation' check(data_mode='simulation'),
 sim_clock timestamptz not null,outcome_salt text not null check(outcome_salt~'^[0-9a-f]{64}$'),
 submitted_by text not null check(char_length(submitted_by) between 1 and 200),
 submitted_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,intent_id));
create table runtime.nexloop_sim_effect_receipts (
 tenant_id text not null,world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 intent_id uuid not null,stand_in_id text not null references control.nexloop_stand_ins(stand_in_id),
 stand_in_version text not null,simulated boolean not null check(simulated),
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 output_kind text not null default 'simulated_output' check(output_kind='simulated_output'),
 status text not null check(status in ('fulfilled','failed','unknown')),failure_code text check((status='failed')=(failure_code is not null)),
 external_ref text not null check(external_ref~'^stand-in:'),latency_ms integer not null check(latency_ms between 0 and 86400000),
 config_digest text,executions integer not null default 1 check(executions>=1),
 first_executed_at timestamptz not null default clock_timestamp(),last_executed_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,intent_id),
 foreign key(tenant_id,world,intent_id) references runtime.nexloop_sim_effect_intents(tenant_id,world,intent_id));
do $$declare t text;begin
 foreach t in array array['control.nexloop_stand_in_configs','runtime.nexloop_sim_effect_intents','runtime.nexloop_sim_effect_receipts'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics',t);
 end loop;
end $$;
-- An intent and the stand-in configuration are written once; a receipt changes only its repeat count (whole deletion of a world, S2,
-- is the one way rows leave: the purge's erasure scope opens these tables for that world's rows only).
create function runtime.nexloop_sim_ledger_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' then
  if runtime.nexloop_erasure_permits(current_user,tg_table_schema||'.'||tg_table_name,'DELETE',to_jsonb(old),null) then return old;end if;
  raise exception '% is append-only',tg_table_name using errcode='42501';end if;
 if tg_op='TRUNCATE' then raise exception '% is append-only',tg_table_name using errcode='42501';end if;
 if tg_table_name='nexloop_sim_effect_receipts' then
  if (to_jsonb(new)-'executions'-'last_executed_at')=(to_jsonb(old)-'executions'-'last_executed_at')
   and (to_jsonb(new)->>'executions')::integer>(to_jsonb(old)->>'executions')::integer then return new;end if;
 end if;
 raise exception '% is append-only',tg_table_name using errcode='42501';
end $$;
alter function runtime.nexloop_sim_ledger_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_ledger_guard() from public;
do $$declare t text;begin
 foreach t in array array['control.nexloop_stand_in_configs','runtime.nexloop_sim_effect_intents','runtime.nexloop_sim_effect_receipts'] loop
  execute format('create trigger nx041_sim_ledger_guard before update or delete on %s for each row execute function runtime.nexloop_sim_ledger_guard()',t);
  execute format('create trigger nx041_sim_ledger_no_truncate before truncate on %s for each statement execute function runtime.nexloop_sim_ledger_guard()',t);
 end loop;
end $$;

-- Trusted configuration (or the experiment's frozen inputs, S3) records a world's behaviour once; no role holds this function.
create function runtime.nexloop_stand_in_configure(p_tenant text,p_world text,p_config jsonb,p_by text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare f numeric;u numeric;lo integer;hi integer;v_digest text;
begin
 if coalesce(runtime.nexloop_world_kind(p_world),'') not in ('simulation','shadow') then raise exception 'stand-in: real_world_refused' using errcode='42501';end if;
 if control.nexloop_sim_world_status(p_tenant,p_world) is distinct from 'active' then raise exception 'stand-in: world_unavailable' using errcode='42501';end if;
 if jsonb_typeof(p_config) is distinct from 'object' or exists(select 1 from jsonb_object_keys(p_config) k where k not in ('fail_rate','unknown_rate','latency_ms','failure_codes'))
  or jsonb_typeof(coalesce(p_config->'fail_rate','0'::jsonb))<>'number' or jsonb_typeof(coalesce(p_config->'unknown_rate','0'::jsonb))<>'number'
  or (p_config ? 'latency_ms' and (jsonb_typeof(p_config->'latency_ms') is distinct from 'object' or jsonb_typeof(p_config->'latency_ms'->'min') is distinct from 'number'
     or jsonb_typeof(p_config->'latency_ms'->'max') is distinct from 'number'))
  or (p_config ? 'failure_codes' and (jsonb_typeof(p_config->'failure_codes')<>'array' or jsonb_array_length(p_config->'failure_codes') not between 1 and 16
     or exists(select 1 from jsonb_array_elements(p_config->'failure_codes') c where jsonb_typeof(c)<>'string' or c#>>'{}'!~'^[a-z][a-z0-9_]{0,39}$'))) then
  raise exception 'stand-in: config_invalid' using errcode='22023';end if;
 f:=coalesce((p_config->>'fail_rate')::numeric,0);u:=coalesce((p_config->>'unknown_rate')::numeric,0);
 lo:=coalesce((p_config->'latency_ms'->>'min')::numeric,0)::integer;hi:=coalesce((p_config->'latency_ms'->>'max')::numeric,0)::integer;
 if f<0 or u<0 or f+u>1 or lo<0 or hi<lo or hi>86400000 then raise exception 'stand-in: config_invalid' using errcode='22023';end if;
 v_digest:=encode(sha256(convert_to(p_config::text,'UTF8')),'hex');
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_stand_in_configs(world,tenant_id,config,config_digest,recorded_by) values(p_world,p_tenant,p_config,v_digest,p_by);
 return v_digest;
end $$;
alter function runtime.nexloop_stand_in_configure(text,text,jsonb,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_stand_in_configure(text,text,jsonb,text) from public;

-- 4. Entries ----------------------------------------------------------------------------------------------------------------------
-- The tenant of a simulation world credential, for a registered active simulation world; real and test are refused before any lookup.
create function authz.nexloop_sim_effect_tenant(p_digest text,p_world text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if coalesce(runtime.nexloop_world_kind(p_world),'') not in ('simulation','shadow') then
  raise exception 'stand-in: real_world_refused' using errcode='42501';end if;
 v_tenant:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'tenant_id';
 if v_tenant is null or control.nexloop_sim_world_status(v_tenant,p_world) is distinct from 'active' then
  raise exception 'stand-in: world_unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return v_tenant;
end $$;
alter function authz.nexloop_sim_effect_tenant(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_sim_effect_tenant(text,text) from public;

create function authz.nexloop_sim_effect_receipt(r runtime.nexloop_sim_effect_receipts,p_repeated boolean) returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('intent_id',r.intent_id,'world',r.world,'status',r.status,'failure_code',r.failure_code,'external_ref',r.external_ref,
  'latency_ms',r.latency_ms,'simulated',r.simulated,'stand_in_id',r.stand_in_id,'stand_in_version',r.stand_in_version,'data_mode',r.data_mode,
  'output_kind',r.output_kind,'executions',r.executions,'repeated',p_repeated)
$$;
alter function authz.nexloop_sim_effect_receipt(runtime.nexloop_sim_effect_receipts,boolean) owner to nexloop_owner;
revoke all on function authz.nexloop_sim_effect_receipt(runtime.nexloop_sim_effect_receipts,boolean) from public;

create function authz.nexloop_sim_effect_submit(p_digest text,p_world text,p_payload jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;s control.nexloop_stand_ins;x runtime.nexloop_sim_effect_intents;v_principal text;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if jsonb_typeof(p_payload) is distinct from 'object' or exists(select 1 from jsonb_object_keys(p_payload) k where k not in
   ('intent_id','action_name','action_version','provider_kind','business_digest','request_digest','origin'))
  or coalesce(p_payload->>'intent_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(p_payload->>'business_digest','')!~'^[0-9a-f]{64}$' or coalesce(p_payload->>'request_digest','')!~'^[0-9a-f]{64}$'
  or coalesce(p_payload->>'origin','') not in ('twin','replay','injected') or jsonb_typeof(p_payload->'action_version') is distinct from 'number' then
  raise exception 'stand-in: invalid' using errcode='22023';end if;
 -- D11: no stand-in, no effect in a simulation world.
 select * into s from control.nexloop_stand_ins where action_name=p_payload->>'action_name' and action_version=(p_payload->>'action_version')::integer
  and provider_kind=p_payload->>'provider_kind';
 if not found then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_principal:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'principal_id';
 -- One open intent per business intent in a world (review N4): an intent is open until its receipt is fulfilled or failed (unknown
 -- is not terminal: query first). A second intent id for the same business digest while one is open is refused, so a submitter
 -- cannot resubmit under new intent ids and keep only the fulfilled receipt. Serialized per business digest.
 perform pg_advisory_xact_lock(hashtextextended('nx041-stand-in:'||v_tenant||':'||p_world||':'||(p_payload->>'business_digest'),0));
 if exists(select 1 from runtime.nexloop_sim_effect_intents i where i.tenant_id=v_tenant and i.world=p_world and i.business_digest=p_payload->>'business_digest'
     and i.intent_id<>(p_payload->>'intent_id')::uuid
     and not exists(select 1 from runtime.nexloop_sim_effect_receipts r where r.tenant_id=i.tenant_id and r.world=i.world and r.intent_id=i.intent_id
       and r.status in ('fulfilled','failed'))) then
  raise exception 'stand-in: business_intent_open' using errcode='22023';end if;
 insert into runtime.nexloop_sim_effect_intents(tenant_id,world,intent_id,stand_in_id,business_digest,request_digest,origin,sim_clock,outcome_salt,submitted_by)
  -- The simulated clock is the world's (S5), the salt the server's: the submitter can predict neither, so it cannot pick an outcome
  -- by choosing its intent id or a clock (review 4.1/4.2). Both stay with the intent, so a repeat gives the same receipt.
  values(v_tenant,p_world,(p_payload->>'intent_id')::uuid,s.stand_in_id,p_payload->>'business_digest',p_payload->>'request_digest',p_payload->>'origin',
   runtime.nexloop_world_now(v_tenant,p_world),encode(sha256(convert_to(gen_random_uuid()::text||gen_random_uuid()::text||clock_timestamp()::text,'UTF8')),'hex'),
   coalesce(v_principal,'service'))
  on conflict(tenant_id,world,intent_id) do nothing;
 select * into x from runtime.nexloop_sim_effect_intents where tenant_id=v_tenant and world=p_world and intent_id=(p_payload->>'intent_id')::uuid;
 if x.stand_in_id<>s.stand_in_id or x.business_digest<>p_payload->>'business_digest' or x.request_digest<>p_payload->>'request_digest' then
  raise exception 'stand-in: intent_conflict' using errcode='22023';end if;
 return jsonb_build_object('intent_id',x.intent_id,'world',p_world,'stand_in_id',s.stand_in_id,'stand_in_version',s.stand_in_version,'state','accepted','simulated',true);
end $$;

-- The stand-in executor (D4: no provider, no network; it only calls this). Deterministic in (stand-in version, world, intent, simulated
-- clock, the intent's server salt) and the world's configuration (D12, default all fulfilled, zero latency).
create function authz.nexloop_sim_effect_execute(p_digest text,p_world text,p_intent uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;x runtime.nexloop_sim_effect_intents;s control.nexloop_stand_ins;cfg control.nexloop_stand_in_configs;r runtime.nexloop_sim_effect_receipts;
 h text;u numeric;f numeric;un numeric;lo integer;hi integer;v_status text;v_code text;codes jsonb;
begin
 if session_user is distinct from 'nexloop_stand_in_worker' then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 select * into x from runtime.nexloop_sim_effect_intents where tenant_id=v_tenant and world=p_world and intent_id=p_intent;
 if not found then raise exception 'stand-in: intent_unavailable' using errcode='42501';end if;
 select * into r from runtime.nexloop_sim_effect_receipts where tenant_id=v_tenant and world=p_world and intent_id=p_intent for update;
 if found then
  update runtime.nexloop_sim_effect_receipts set executions=executions+1,last_executed_at=clock_timestamp()
   where tenant_id=v_tenant and world=p_world and intent_id=p_intent returning * into r;
  return authz.nexloop_sim_effect_receipt(r,true);
 end if;
 select * into s from control.nexloop_stand_ins where stand_in_id=x.stand_in_id;
 select * into cfg from control.nexloop_stand_in_configs where world=p_world and tenant_id=v_tenant;
 h:=encode(sha256(convert_to(s.stand_in_version||'|'||p_world||'|'||p_intent::text||'|'||to_char(x.sim_clock at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US')||'|'||x.outcome_salt,'UTF8')),'hex');
 u:=('x'||substr(h,1,8))::bit(32)::bigint::numeric/4294967296;
 f:=coalesce((cfg.config->>'fail_rate')::numeric,0);un:=coalesce((cfg.config->>'unknown_rate')::numeric,0);
 lo:=coalesce((cfg.config->'latency_ms'->>'min')::numeric,0)::integer;hi:=coalesce((cfg.config->'latency_ms'->>'max')::numeric,0)::integer;
 codes:=coalesce(cfg.config->'failure_codes','["stand_in_failure"]'::jsonb);
 if u<f then v_status:='failed';v_code:=codes->>((('x'||substr(h,9,8))::bit(32)::bigint % jsonb_array_length(codes))::integer);
 elsif u<f+un then v_status:='unknown';
 else v_status:='fulfilled';end if;
 insert into runtime.nexloop_sim_effect_receipts(tenant_id,world,intent_id,stand_in_id,stand_in_version,simulated,status,failure_code,external_ref,latency_ms,config_digest)
  values(v_tenant,p_world,p_intent,s.stand_in_id,s.stand_in_version,true,v_status,v_code,'stand-in:'||s.stand_in_id||':'||substr(h,17,24),
   lo+(('x'||substr(h,41,8))::bit(32)::bigint % (hi-lo+1))::integer,cfg.config_digest)
  returning * into r;
 return authz.nexloop_sim_effect_receipt(r,false);
end $$;

create function authz.nexloop_sim_effect_query(p_digest text,p_world text,p_intent uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;r runtime.nexloop_sim_effect_receipts;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_stand_in_worker') then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 select * into r from runtime.nexloop_sim_effect_receipts where tenant_id=v_tenant and world=p_world and intent_id=p_intent;
 if not found then return null;end if;
 return authz.nexloop_sim_effect_receipt(r,false);
end $$;

create function authz.nexloop_sim_effect_pending(p_digest text,p_world text,p_limit integer) returns setof uuid
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if session_user is distinct from 'nexloop_stand_in_worker' then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 return query select i.intent_id from runtime.nexloop_sim_effect_intents i where i.tenant_id=v_tenant and i.world=p_world
  and not exists(select 1 from runtime.nexloop_sim_effect_receipts r where r.tenant_id=i.tenant_id and r.world=i.world and r.intent_id=i.intent_id)
  order by i.submitted_at,i.intent_id limit greatest(1,least(coalesce(p_limit,50),500));
end $$;

do $$declare f text;begin
 foreach f in array array['authz.nexloop_sim_effect_submit(text,text,jsonb)','authz.nexloop_sim_effect_execute(text,text,uuid)',
   'authz.nexloop_sim_effect_query(text,text,uuid)','authz.nexloop_sim_effect_pending(text,text,integer)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
 end loop;
end $$;
-- The stand-in executor's own role (review 3.1): it executes the three stand-in entries and nothing else - no real executor
-- function, no dispatch summary, no readiness, no table. Its connection (password or pg_hba line) is deployment configuration,
-- described in the implementation document; nothing here holds a credential.
do $$begin
 if not exists(select 1 from pg_roles where rolname='nexloop_stand_in_worker') then
  create role nexloop_stand_in_worker login nosuperuser nocreatedb nocreaterole noinherit noreplication nobypassrls;
 end if;
end $$;
-- A role of that name that already exists (made by hand, another lineage) is brought to exactly this shape (review N2): its
-- attributes, no role membership, and no privilege granted to it before; the grants below are then its only ones. The password
-- (or pg_hba line) is left to the deployment.
do $$declare r pg_roles;m text;s text;begin
 select * into r from pg_roles where rolname='nexloop_stand_in_worker';
 if not r.rolcanlogin or r.rolinherit or r.rolcreatedb or r.rolcreaterole then
  execute 'alter role nexloop_stand_in_worker login noinherit nocreatedb nocreaterole';end if;
 if r.rolsuper or r.rolreplication or r.rolbypassrls then
  execute 'alter role nexloop_stand_in_worker nosuperuser noreplication nobypassrls';end if;
 for m in select g.rolname from pg_auth_members a join pg_roles g on g.oid=a.roleid where a.member=r.oid loop
  execute format('revoke %I from nexloop_stand_in_worker',m);
 end loop;
 for s in select nspname from pg_namespace where nspname in ('authz','control','ontology','runtime','extensions','public') loop
  execute format('revoke all on all tables in schema %I from nexloop_stand_in_worker',s);
  execute format('revoke all on all sequences in schema %I from nexloop_stand_in_worker',s);
  execute format('revoke all on all functions in schema %I from nexloop_stand_in_worker',s);
  execute format('revoke all on schema %I from nexloop_stand_in_worker',s);
 end loop;
end $$;
grant usage on schema authz to nexloop_stand_in_worker;
grant execute on function authz.nexloop_sim_effect_submit(text,text,jsonb) to nexloop_api,nexloop_domain_worker;
grant execute on function authz.nexloop_sim_effect_execute(text,text,uuid) to nexloop_stand_in_worker;
grant execute on function authz.nexloop_sim_effect_pending(text,text,integer) to nexloop_stand_in_worker;
grant execute on function authz.nexloop_sim_effect_query(text,text,uuid) to nexloop_api,nexloop_domain_worker,nexloop_stand_in_worker;

-- 5. The real effect path never holds a simulation world row ------------------------------------------------------------------
-- A CHECK, not a trigger (review 1.1): it holds the row as finally written, after every BEFORE trigger, and only DDL removes it.
-- control.nexloop_real_effect_tables declares every table with a world column on an outbound path (review 1.3): guarded, or not
-- with the reason.
create table control.nexloop_real_effect_tables (
 table_name regclass primary key,guarded boolean not null,reason text not null check(char_length(reason) between 1 and 300));
alter table control.nexloop_real_effect_tables owner to nexloop_owner;
revoke all on control.nexloop_real_effect_tables from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker;
insert into control.nexloop_real_effect_tables(table_name,guarded,reason) values
 ('runtime.nexloop_effect_intents',true,'governed external effect intent (S4)'),
 ('runtime.nexloop_effect_outbox',true,'the effect executor leases from it'),
 ('runtime.nexloop_effect_attempts',true,'a provider call'),
 ('runtime.nexloop_effect_observations',true,'a provider receipt'),
 ('runtime.nexloop_incentive_issues',true,'value issued to a Consumer'),
 ('runtime.nexloop_outreach_sends',true,'a proactive message'),
 ('runtime.nexloop_outbound_messages',true,'a message to a channel'),
 ('control.nexloop_real_dispatch_switches',true,'the real dispatch switch'),
 ('control.nexloop_effect_contexts',true,'effect context of the real executor'),
 ('runtime.nexloop_message_outbox',true,'webchat delivery and the relay read it (review 1.3)'),
 ('runtime.nexloop_relay_handoffs',true,'relay delivery (review 1.3)'),
 ('runtime.nexloop_relay_claims',true,'relay delivery (review 1.3)'),
 ('runtime.nexloop_relay_attempts',true,'relay delivery (review 1.3)'),
 ('control.nexloop_data_exports',true,'a file of personal data leaves the system (review 1.3)'),
 ('runtime.nexloop_outbound_delivery_events',true,'delivery events of an outbound message (derived; guarded as well)'),
 ('runtime.nexloop_local_delivery_recoveries',true,'local JSON delivery recovery (derived; guarded as well)'),
 ('control.nexloop_relay_enrollments',true,'relay registration of a delivery channel (guard test)'),
 ('runtime.nexloop_relay_consumer_cursors',true,'relay delivery position (guard test)'),
 ('runtime.nexloop_message_relay_leases',true,'relay delivery lease (guard test)'),
 ('runtime.nexloop_effect_dispatch_controls',true,'dispatch control of a real effect intent (guard test)'),
 ('runtime.nexloop_dispatch_refusals',false,'a record that a dispatch was refused; nothing leaves the system through it'),
 ('runtime.nexloop_task_dispatch_controls',false,'internal job dispatch lease control (0035 queue), no external request'),
 ('runtime.nexloop_outbox',false,'the durable job queue outbox (0035): internal job hand-off to workers, no external request; a job reaches outside only through a governed effect intent, which is guarded'),
 ('runtime.nexloop_sim_effect_intents',false,'the simulated ledger itself (simulation worlds only)'),
 -- Review fixes 2 (1.3 by structure): every table with a world column that a dispatch, relay, executor, delivery, effect, outreach,
 -- outbound, incentive, export, send, lease, webhook, notification or mail function writes (guard test test_every_outbound_table_is_declared).
 ('control.nexloop_effect_control_ledger',true,'effect control reservations of a real effect intent'),
 ('control.nexloop_effect_plan_bindings',true,'plan binding of a real effect context'),
 ('runtime.nexloop_effect_catalog_bindings',true,'catalog binding of a real effect intent'),
 ('runtime.nexloop_effect_events',true,'event history of a real effect intent'),
 ('runtime.nexloop_effect_provider_bindings',true,'provider binding of a real effect execution'),
 ('runtime.nexloop_effect_requery_orders',true,'a provider requery of a real effect'),
 ('runtime.nexloop_effect_determinations',true,'owner determination of a real effect outcome'),
 ('runtime.nexloop_message_routes',true,'relay routing of an outbound message'),
 ('runtime.nexloop_outreach_approvals',true,'approval of a proactive message (its send is guarded)'),
 ('control.nexloop_reply_escalations',true,'escalation of an undelivered real message to a human'),
 ('control.nexloop_restore_channels',true,'owner notification channel of a restore (name net: channel)'),
 ('authz.nexloop_run_credentials',false,'Run credentials exist in simulation worlds (S2 D10); the relay revokes them, nothing leaves'),
 ('runtime.nexloop_work_feed',false,'internal work feed of every world (S5 re-evaluates simulation items on it); no external request'),
 ('control.nexloop_budget_consumption',false,'budget accounting of every world (S6 counts simulation model spend through it); no external request'),
 ('runtime.nexloop_budget_settlements',false,'budget settlement accounting of every world; no external request'),
 ('control.nexloop_action_effect_categories',false,'trusted configuration of effect categories; nothing leaves'),
 ('control.nexloop_action_effect_externality',false,'trusted configuration of effect externality; nothing leaves'),
 ('ontology.nexloop_claims',false,'Claims written when a candidate is merged or published; internal, and real only through conversations (S4 5.2 guard)'),
 ('ontology.nexloop_definition_aliases',false,'definition aliases written by a candidate merge; internal ontology'),
 ('runtime.nexloop_conversations',false,'conversation of an outbound message; real only by its own CHECK (world = real)'),
 ('runtime.nexloop_conversation_messages',false,'the message record of an outbound message; real only through its conversation'),
 ('runtime.nexloop_commitment_evidence',false,'commitment evidence from an outbound message (guarded); simulation commitments are allowed (S5)'),
 ('runtime.nexloop_human_requests',false,'a human request record (the owner asks for an effect query); no external request'),
 ('runtime.nexloop_sim_effect_receipts',false,'the simulated ledger itself (simulation worlds only)');
do $$declare t regclass;begin
 for t in select table_name from control.nexloop_real_effect_tables where guarded loop
  execute format('alter table %s add constraint nx041_real_effect_world check(coalesce(runtime.nexloop_world_kind(world),'''') not in (''simulation'',''shadow'')) not valid',t);
  execute format('alter table %s validate constraint nx041_real_effect_world',t);
 end loop;
end $$;
