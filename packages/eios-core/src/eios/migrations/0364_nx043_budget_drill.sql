-- NX-043 X7 item 10 (owner 2026-10-28, option A): a budget drill - a screening experiment in a simulation world whose only purpose is
-- to show on stage that an exhausted experiment budget blocks new spend. The migration number is temporary; it lives only in the
-- file name and the catalog. Published migrations are not edited.
-- 1. Registration: an explicit "budget_drill": true is accepted only together with all of: stage screening, a simulation world, a
--    model cap above 0 and at most 1 CNY, an end at most one hour away, and a selected twin that is published (not retired) and NOT
--    calibrated (a calibrated twin needs no drill). Anything else naming budget_drill is refused (budget_drill_not_allowed). The flag
--    stays in the frozen registration (and so in its digest), and registering one writes the audit event nexloop.experiment.budget_drill.
-- 2. runtime.nexloop_experiment_twin_ok (0311's body) also passes a registration for which the drill holds now - the one waiver of
--    the twin's calibration (D9) besides online. A screening registration without the flag is refused twin_not_calibrated at start as
--    before (item 4). Start, the guard and strategy_for all read twin_ok, so the drill is not stopped as twin_withdrawn.
-- 3. Dispatch-time recheck (D5): a model reservation of a Run ('run:<uuid>') in a simulation or shadow world is refused with NXB01
--    (the runtime worker's dispatch control reports budget_exhausted) once an experiment of the world was stopped by the guard for
--    budget_exhausted, until an experiment of the world is started after that stop (any other later event does not lift it). The reservation that reaches or crosses the cap is still admitted (its
--    stop has to commit with it, 0297); every later one is refused. Real and test worlds are not touched.
-- 4. A report of a drill experiment carries budget_drill: true and the notice below, in the stored report and its digest; the
--    registration's notice says the same. Pooled reports are online and real only (0299), so a drill is never pooled.
-- Nothing here reads or writes a real dispatch switch, and no credential of any world changes.

create function runtime.nexloop_experiment_budget_drill_notice() returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select 'budget drill: the twin is not calibrated; the result does not represent the experiment''s effect'::text
$$;
alter function runtime.nexloop_experiment_budget_drill_notice() owner to nexloop_owner;
grant execute on function runtime.nexloop_experiment_budget_drill_notice() to public;

-- null when the drill holds for registration r now, else the refusal code.
create function runtime.nexloop_experiment_budget_drill_problem(p_tenant text,r jsonb) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_ok boolean;
begin
 if r->'budget_drill' is distinct from 'true'::jsonb then return 'budget_drill_not_allowed';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 begin
  v_ok:=r->>'stage'='screening' and runtime.nexloop_world_kind(r->>'world')='simulation'
   and jsonb_typeof(r->'budget'->'model_cny')='number' and (r->'budget'->>'model_cny')::numeric>0 and (r->'budget'->>'model_cny')::numeric<=1
   and (r->>'ends_at')::timestamptz<=clock_timestamp()+interval '1 hour'
   and jsonb_typeof(r->'twin')='object'
   and runtime.nexloop_consumer_twin_status(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer)='published'
   and runtime.nexloop_consumer_twin_calibration(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer)='uncalibrated';
 exception when others then v_ok:=false;
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return case when coalesce(v_ok,false) then null else 'budget_drill_not_allowed' end;
end $$;
alter function runtime.nexloop_experiment_budget_drill_problem(text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_budget_drill_problem(text,jsonb) from public;

create function runtime.nexloop_experiment_budget_drill_ok(p_tenant text,r jsonb) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select r->'budget_drill'='true'::jsonb and runtime.nexloop_experiment_budget_drill_problem(p_tenant,r) is null
$$;
alter function runtime.nexloop_experiment_budget_drill_ok(text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_budget_drill_ok(text,jsonb) from public;

-- 1. The registration check: the earlier one sees the registration without the flag; the drill conditions come on top.
alter function runtime.nexloop_experiment_registration_problem(text,jsonb) rename to nexloop_experiment_registration_problem_before_budget_drill;
create function runtime.nexloop_experiment_registration_problem(p_tenant text,r jsonb) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v text;
begin
 -- In the tenant's own context (the earlier check reads the twin under row security; a definer is not exempt as a superuser is).
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 if jsonb_typeof(r)='object' and r ? 'budget_drill' then
  if r->'budget_drill' is distinct from 'true'::jsonb then v:='invalid';
  else
   v:=coalesce(runtime.nexloop_experiment_registration_problem_before_budget_drill(p_tenant,r-'budget_drill'),
    runtime.nexloop_experiment_budget_drill_problem(p_tenant,r));
  end if;
 else
  v:=runtime.nexloop_experiment_registration_problem_before_budget_drill(p_tenant,r);
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_experiment_registration_problem(text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_registration_problem(text,jsonb) from public;
revoke all on function runtime.nexloop_experiment_registration_problem_before_budget_drill(text,jsonb) from public;

create function control.nexloop_experiment_budget_drill_audit() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if new.registration->'budget_drill'='true'::jsonb then
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('experiment:budget-drill:'||new.experiment_id||':'||md5(new.tenant_id),'nexloop.experiment.budget_drill',new.tenant_id,new.registered_by,'experiment',
    new.request_id,jsonb_build_object('experiment_id',new.experiment_id,'registration_digest',new.registration_digest,'calibration_waived','budget_drill',
     'notice',runtime.nexloop_experiment_budget_drill_notice()),clock_timestamp());
 end if;
 return new;
end $$;
alter function control.nexloop_experiment_budget_drill_audit() owner to nexloop_owner;
revoke all on function control.nexloop_experiment_budget_drill_audit() from public;
create trigger nx043_budget_drill_audit after insert on control.nexloop_experiments for each row execute function control.nexloop_experiment_budget_drill_audit();

create or replace function runtime.nexloop_experiment_notice(p_registration jsonb) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_registration->'budget_drill'='true'::jsonb then runtime.nexloop_experiment_budget_drill_notice()
  when p_registration->>'stage'='online' and (p_registration->'sample_size'->>'candidate')::integer
   <coalesce((p_registration->'maturity'->>'min_samples_per_group')::integer,30)
  then 'single_round_below_minimum_sample: one round reports nothing (minimum '||coalesce(p_registration->'maturity'->>'min_samples_per_group','30')
   ||' per group); pool rounds of the same strategy comparison (nexloop.experiment.report kind pooled)' end
$$;

-- 2. The twin check.
create or replace function runtime.nexloop_experiment_twin_ok(p_tenant text,r jsonb) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select r->'twin'='null'::jsonb or (r->>'stage'='online' and runtime.nexloop_consumer_twin_unretired(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer))
  or runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer)
  or runtime.nexloop_experiment_budget_drill_ok(p_tenant,r)
$$;

-- 3. The dispatch-time recheck.
create function control.nexloop_experiment_budget_refusal() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_stop bigint;
begin
 if new.budget_kind<>'model' or new.consumption_id!~'^run:[0-9a-f-]{36}$'
  or coalesce(runtime.nexloop_world_kind(new.world),'') not in ('simulation','shadow') then return new;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 -- L6 S1 on f94107af: not the latest event's reason (any later event would lift it) but the latest budget_exhausted stop of an
 -- experiment of this world, refused until an experiment of this world is started after it.
 select max(x.event_id) into v_stop from control.nexloop_experiment_events x join control.nexloop_experiments e
   on e.tenant_id=x.tenant_id and e.experiment_id=x.experiment_id
  where x.tenant_id=new.tenant_id and e.registration->>'world'=new.world and x.status='stopped' and x.reason='guardrail: budget_exhausted';
 if v_stop is not null and not exists(select 1 from control.nexloop_experiment_events x join control.nexloop_experiments e
   on e.tenant_id=x.tenant_id and e.experiment_id=x.experiment_id
  where x.tenant_id=new.tenant_id and e.registration->>'world'=new.world and x.status='started' and x.event_id>v_stop) then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  raise exception 'experiment budget exhausted (D5): the world''s experiment stopped' using errcode='NXB01';
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return new;
end $$;
alter function control.nexloop_experiment_budget_refusal() owner to nexloop_owner;
revoke all on function control.nexloop_experiment_budget_refusal() from public;
-- Named to sort before nx043_experiment_budget (0297): BEFORE triggers fire by name, so the refusal reads the state before this row;
-- the reservation that stops the experiment is not refused by its own stop.
create trigger nx043_experiment_admit before insert on control.nexloop_budget_consumption for each row execute function control.nexloop_experiment_budget_refusal();

-- 4. The report.
create function control.nexloop_experiment_budget_drill_report() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if exists(select 1 from control.nexloop_experiments e where e.tenant_id=new.tenant_id and e.experiment_id=new.experiment_id
   and e.registration->'budget_drill'='true'::jsonb) then
  new.report:=new.report||jsonb_build_object('budget_drill',true,'notice',runtime.nexloop_experiment_budget_drill_notice());
  new.report_digest:=encode(sha256(convert_to(new.report::text,'UTF8')),'hex');
 end if;
 return new;
end $$;
alter function control.nexloop_experiment_budget_drill_report() owner to nexloop_owner;
revoke all on function control.nexloop_experiment_budget_drill_report() from public;
create trigger nx043_budget_drill_report before insert on control.nexloop_experiment_reports for each row execute function control.nexloop_experiment_budget_drill_report();
