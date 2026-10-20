-- NX-041 S6 (design §6, D3, D6, §11 S6): the simulation process group's database side. The migration number lives only in the file
-- name and the catalog; nothing below repeats it.
-- 1. Simulation model budget (D6): every simulation or shadow world gets its own model budget for its lifetime (S2 creation), and
--    the tenant's simulation account has a monthly cap of 100 CNY across all its simulation and shadow worlds; a reservation past
--    it is refused (NXB01). The spend is kept in control.nexloop_sim_model_spend, which no world deletion removes (a deleted world's
--    spend still counts that month). Costs keep data mode simulation (S1) and never touch a real budget (budgets are per world).
-- 2. World deletion (S2) needs nothing new for these rows: the nx022 append-only guard (0129) already lets the owner delete inside
--    the erasure scope the S2 purge opens per table, so a world's budget rows leave with it.
-- 3. Off-database data (S2 review 1.1): starting a world's deletion queues its Pi directory on the simulation Host;
--    the simulation group's file purger (nexloop_scheduler) lists the queue and confirms each outcome (purged or absent).
-- 4. The stand-in intent (S4) carries the world's time: submitted_at is runtime.nexloop_world_now (S5) when present, else the wall
--    clock (until S5 is integrated).

-- 1. Simulation model budget -------------------------------------------------------------------------------------------------
create function runtime.nexloop_sim_model_monthly_cap() returns numeric language sql immutable set search_path=pg_catalog,pg_temp as $$ select 100::numeric $$;
alter function runtime.nexloop_sim_model_monthly_cap() owner to nexloop_owner;
grant execute on function runtime.nexloop_sim_model_monthly_cap() to public;

create table control.nexloop_sim_model_spend (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 sim_world text not null check(coalesce(runtime.nexloop_world_kind(sim_world),'') in ('simulation','shadow')),
 consumption_id text not null,amount numeric not null,unit text not null check(unit='CNY'),
 month date not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,sim_world,consumption_id));
alter table control.nexloop_sim_model_spend owner to nexloop_owner;
alter table control.nexloop_sim_model_spend enable row level security;
alter table control.nexloop_sim_model_spend force row level security;
create policy tenant_boundary on control.nexloop_sim_model_spend to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_sim_model_spend from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics;
create trigger nx041_sim_spend_append_only before update or delete on control.nexloop_sim_model_spend for each row execute function control.nexloop_nx022_append_only();
create index nexloop_sim_model_spend_month on control.nexloop_sim_model_spend(tenant_id,month);

-- Every model consumption of a simulation world (0068 authz.nexloop_reserve_budget, settlements included) is also the tenant's
-- simulation spend; a positive one past the monthly cap is refused before it is written.
create function control.nexloop_sim_model_budget() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_month date:=date_trunc('month',clock_timestamp() at time zone 'UTC')::date;v_used numeric;
begin
 if new.budget_kind<>'model' or coalesce(runtime.nexloop_world_kind(new.world),'') not in ('simulation','shadow') then return new;end if;
 if new.unit<>'CNY' then raise exception 'simulation model budget is in CNY' using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nx041-sim-model-budget:'||new.tenant_id,0));
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select coalesce(sum(amount),0) into v_used from control.nexloop_sim_model_spend where tenant_id=new.tenant_id and month=v_month;
 if new.amount>0 and v_used+new.amount>runtime.nexloop_sim_model_monthly_cap() then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  raise exception 'simulation model budget exhausted: used % of % CNY this month',v_used,runtime.nexloop_sim_model_monthly_cap() using errcode='NXB01';
 end if;
 insert into control.nexloop_sim_model_spend(tenant_id,sim_world,consumption_id,amount,unit,month)
  values(new.tenant_id,new.world,new.consumption_id,new.amount,new.unit,v_month);
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return new;
end $$;
alter function control.nexloop_sim_model_budget() owner to nexloop_owner;
revoke all on function control.nexloop_sim_model_budget() from public;
create trigger nx041_sim_model_budget before insert on control.nexloop_budget_consumption for each row execute function control.nexloop_sim_model_budget();

-- A new simulation world gets its model budget for its lifetime (the monthly account cap above still applies).
create function control.nexloop_sim_world_model_budget() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
  values(new.tenant_id,new.world,'model','CNY',runtime.nexloop_sim_model_monthly_cap(),new.created_at,new.expires_at,1)
  on conflict(tenant_id,world,budget_kind) do nothing;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function control.nexloop_sim_world_model_budget() owner to nexloop_owner;
revoke all on function control.nexloop_sim_world_model_budget() from public;
create trigger nx041_sim_world_model_budget after insert on control.nexloop_sim_worlds for each row execute function control.nexloop_sim_world_model_budget();

-- 3. The simulation Host's directory of a deleted world --------------------------------------------------------------------------
create table runtime.nexloop_sim_world_file_purges (
 sim_world text primary key check(coalesce(runtime.nexloop_world_kind(sim_world),'') in ('simulation','shadow')),
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 requested_at timestamptz not null default clock_timestamp(),
 outcome text check(outcome in ('purged','absent')),confirmed_at timestamptz,check((outcome is null)=(confirmed_at is null)));
alter table runtime.nexloop_sim_world_file_purges owner to nexloop_owner;
alter table runtime.nexloop_sim_world_file_purges enable row level security;
alter table runtime.nexloop_sim_world_file_purges force row level security;
create policy tenant_boundary on runtime.nexloop_sim_world_file_purges to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_sim_world_file_purges from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics;

create function control.nexloop_sim_world_files_queue() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);
begin
 if new.status='deleting' and old.status='active' then
  perform set_config('eios.tenant_id',new.tenant_id,true);
  insert into runtime.nexloop_sim_world_file_purges(sim_world,tenant_id) values(new.world,new.tenant_id) on conflict do nothing;
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 end if;
 return null;
end $$;
alter function control.nexloop_sim_world_files_queue() owner to nexloop_owner;
revoke all on function control.nexloop_sim_world_files_queue() from public;
create trigger nx041_sim_world_files after update of status on control.nexloop_sim_worlds for each row execute function control.nexloop_sim_world_files_queue();

-- The simulation group's file purger: deleted worlds whose directory is not confirmed gone (world ids only), and the confirmation.
create function authz.nexloop_sim_world_files_pending(p_limit integer) returns setof text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;
begin
 if session_user is distinct from 'nexloop_scheduler' then raise exception 'sim world files unavailable' using errcode='42501';end if;
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  return query select p.sim_world from runtime.nexloop_sim_world_file_purges p where p.tenant_id=t and p.confirmed_at is null
   order by p.requested_at limit greatest(1,least(coalesce(p_limit,20),200));
 end loop;
end $$;
create function authz.nexloop_sim_world_files_confirm(p_world text,p_outcome text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if session_user is distinct from 'nexloop_scheduler' or p_outcome is null or p_outcome not in ('purged','absent') then
  raise exception 'sim world files unavailable' using errcode='42501';end if;
 v_tenant:=control.nexloop_sim_world_tenant(p_world);
 if v_tenant is null then return false;end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 update runtime.nexloop_sim_world_file_purges set outcome=p_outcome,confirmed_at=clock_timestamp() where sim_world=p_world and confirmed_at is null;
 return found;
end $$;
-- The tenant of a registered world (for the purger, which has no world credential: the world's own was revoked at deletion).
create function control.nexloop_sim_world_tenant(p_world text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;
begin
 for t in select tenant_id from control.nexloop_tenants loop
  if control.nexloop_sim_world_status(t,p_world) is not null then return t;end if;
 end loop;
 return null;
end $$;
do $$declare f text;begin
 foreach f in array array['authz.nexloop_sim_world_files_pending(integer)','authz.nexloop_sim_world_files_confirm(text,text)','control.nexloop_sim_world_tenant(text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
 end loop;
end $$;
grant execute on function authz.nexloop_sim_world_files_pending(integer) to nexloop_scheduler;
grant execute on function authz.nexloop_sim_world_files_confirm(text,text) to nexloop_scheduler;

-- 4. The stand-in intent's time is the world's time (S5) --------------------------------------------------------------------------
create function runtime.nexloop_sim_world_time(p_tenant text,p_world text) returns timestamptz
 language plpgsql volatile set search_path=pg_catalog,pg_temp as $$
begin
 return runtime.nexloop_world_now(p_tenant,p_world);
exception when undefined_function then
 return clock_timestamp();  -- S5 (runtime.nexloop_world_now) not integrated yet
end $$;
alter function runtime.nexloop_sim_world_time(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_world_time(text,text) from public;
create function runtime.nexloop_sim_effect_world_time() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 new.submitted_at:=runtime.nexloop_sim_world_time(new.tenant_id,new.world);
 return new;
end $$;
alter function runtime.nexloop_sim_effect_world_time() owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_effect_world_time() from public;
create trigger nx041_sim_effect_world_time before insert on runtime.nexloop_sim_effect_intents for each row execute function runtime.nexloop_sim_effect_world_time();
