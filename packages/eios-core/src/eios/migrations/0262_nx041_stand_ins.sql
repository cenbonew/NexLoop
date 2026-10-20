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
-- 5. The real effect path refuses a simulation world at its first write: effect intents, outbox, attempts, observations, incentive
--    issues, outreach sends, outbound messages and dispatch switches never hold a sim:/shadow: row, so no real executor can lease one.

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
 sim_clock timestamptz not null,submitted_by text not null check(char_length(submitted_by) between 1 and 200),
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
   ('intent_id','action_name','action_version','provider_kind','business_digest','request_digest','origin','sim_clock'))
  or coalesce(p_payload->>'intent_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(p_payload->>'business_digest','')!~'^[0-9a-f]{64}$' or coalesce(p_payload->>'request_digest','')!~'^[0-9a-f]{64}$'
  or coalesce(p_payload->>'origin','') not in ('twin','replay','injected') or jsonb_typeof(p_payload->'action_version') is distinct from 'number'
  or jsonb_typeof(p_payload->'sim_clock') is distinct from 'string' then
  raise exception 'stand-in: invalid' using errcode='22023';end if;
 -- D11: no stand-in, no effect in a simulation world.
 select * into s from control.nexloop_stand_ins where action_name=p_payload->>'action_name' and action_version=(p_payload->>'action_version')::integer
  and provider_kind=p_payload->>'provider_kind';
 if not found then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_principal:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'principal_id';
 insert into runtime.nexloop_sim_effect_intents(tenant_id,world,intent_id,stand_in_id,business_digest,request_digest,origin,sim_clock,submitted_by)
  values(v_tenant,p_world,(p_payload->>'intent_id')::uuid,s.stand_in_id,p_payload->>'business_digest',p_payload->>'request_digest',p_payload->>'origin',
   (p_payload->>'sim_clock')::timestamptz,coalesce(v_principal,'service'))
  on conflict(tenant_id,world,intent_id) do nothing;
 select * into x from runtime.nexloop_sim_effect_intents where tenant_id=v_tenant and world=p_world and intent_id=(p_payload->>'intent_id')::uuid;
 if x.stand_in_id<>s.stand_in_id or x.business_digest<>p_payload->>'business_digest' or x.request_digest<>p_payload->>'request_digest' then
  raise exception 'stand-in: intent_conflict' using errcode='22023';end if;
 return jsonb_build_object('intent_id',x.intent_id,'world',p_world,'stand_in_id',s.stand_in_id,'stand_in_version',s.stand_in_version,'state','accepted','simulated',true);
end $$;

-- The stand-in executor (D4: no provider, no network; it only calls this). Deterministic in (stand-in version, world, intent, simulated
-- clock) and the world's configuration (D12, default all fulfilled, zero latency).
create function authz.nexloop_sim_effect_execute(p_digest text,p_world text,p_intent uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;x runtime.nexloop_sim_effect_intents;s control.nexloop_stand_ins;cfg control.nexloop_stand_in_configs;r runtime.nexloop_sim_effect_receipts;
 h text;u numeric;f numeric;un numeric;lo integer;hi integer;v_status text;v_code text;codes jsonb;
begin
 if session_user is distinct from 'nexloop_action_worker' then raise exception 'stand-in: unavailable' using errcode='42501';end if;
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
 h:=encode(sha256(convert_to(s.stand_in_version||'|'||p_world||'|'||p_intent::text||'|'||to_char(x.sim_clock at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US'),'UTF8')),'hex');
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
 if session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker') then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 select * into r from runtime.nexloop_sim_effect_receipts where tenant_id=v_tenant and world=p_world and intent_id=p_intent;
 if not found then return null;end if;
 return authz.nexloop_sim_effect_receipt(r,false);
end $$;

create function authz.nexloop_sim_effect_pending(p_digest text,p_world text,p_limit integer) returns setof uuid
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if session_user is distinct from 'nexloop_action_worker' then raise exception 'stand-in: unavailable' using errcode='42501';end if;
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
grant execute on function authz.nexloop_sim_effect_submit(text,text,jsonb) to nexloop_api,nexloop_domain_worker;
grant execute on function authz.nexloop_sim_effect_execute(text,text,uuid) to nexloop_action_worker;
grant execute on function authz.nexloop_sim_effect_pending(text,text,integer) to nexloop_action_worker;
grant execute on function authz.nexloop_sim_effect_query(text,text,uuid) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

-- 5. The real effect path never holds a simulation world row ------------------------------------------------------------------
-- Named aa_ so it fires before every other row trigger of these tables (PostgreSQL fires triggers in name order).
create function runtime.nexloop_real_effect_world_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if coalesce(runtime.nexloop_world_kind(new.world),'') in ('simulation','shadow') then
  raise exception 'real effect path refused in a simulation world (%): only the stand-in executes',tg_table_name using errcode='42501';end if;
 return new;
end $$;
alter function runtime.nexloop_real_effect_world_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_real_effect_world_guard() from public;
do $$declare t text;begin
 foreach t in array array['runtime.nexloop_effect_intents','runtime.nexloop_effect_outbox','runtime.nexloop_effect_attempts','runtime.nexloop_effect_observations',
   'runtime.nexloop_incentive_issues','runtime.nexloop_outreach_sends','runtime.nexloop_outbound_messages','control.nexloop_real_dispatch_switches',
   'control.nexloop_effect_contexts'] loop
  execute format('create trigger aa_nx041_real_effect_world before insert or update of world on %s for each row execute function runtime.nexloop_real_effect_world_guard()',t);
 end loop;
end $$;
