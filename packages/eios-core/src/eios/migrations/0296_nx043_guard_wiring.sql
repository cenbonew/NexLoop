-- NX-043 X5 wiring (dispatcher 2026-10-21). The migration number lives only in the file name and the catalog.
-- 1. Model spend at reservation: trigger nx043_experiment_budget (before insert on control.nexloop_budget_consumption, beside the NX-041
--    S6 simulation trigger) attributes a Run's model reservation ('run:<uuid>') to a started experiment: in real, the Run's Consumer
--    (authz.nexloop_role_run_bindings) in the candidate group; in a simulation or shadow world, every Run of the experiment's world.
--    Within the cap the reservation is written to the experiment ledger (control.nexloop_experiment_spend); reaching the cap stops the
--    experiment; a reservation that would exceed it is not admitted to the experiment and stops it at once - the Run itself keeps
--    only the tenant's own model budget, and every later decision takes the control strategy (X4). Not raising here keeps the stop.
--    A reservation in another unit than CNY is not attributed.
-- 2. Incentives: the first batch has no incentive budget (D1, D5). An incentive reservation ('incentive:<intent>') for a Consumer
--    allocated to a started experiment (either group, so the groups stay comparable) is refused (NXB01; the incentive port reports
--    budget_exhausted).
-- 3. authz.nexloop_experiment_guard_tick: the alert evaluator's tick (service nexloop-alert-evaluator, nexloop_domain_worker, the
--    nexloop.alert.evaluate grant, protocol nexloop-experiment-guard-v1) runs runtime.nexloop_experiment_guard for each started
--    experiment of its tenant before the alerts are evaluated. Idempotent: a stopped experiment is never stopped again.
-- 4. The metrics snapshot gains an experiments block (running, auto_stopped_24h) for alert rule experiment_auto_stopped (alert rules v11).
-- Nothing here reads or writes a real dispatch switch.

create function control.nexloop_experiment_budget_gate() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_exp text;v_consumer text;r jsonb;v_total numeric;v_cap numeric;v_amount numeric:=round(new.amount,4);
begin
 if new.budget_kind not in ('model','incentive') then return new;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if new.budget_kind='incentive' and new.consumption_id like 'incentive:%' then
  select i.consumer_id into v_consumer from runtime.nexloop_incentive_issues i where i.tenant_id=new.tenant_id and i.world=new.world
   and i.intent_id=substr(new.consumption_id,11);
  if v_consumer is not null and exists(select 1 from control.nexloop_experiment_allocations a join control.nexloop_experiments e
    on e.tenant_id=a.tenant_id and e.experiment_id=a.experiment_id where a.tenant_id=new.tenant_id and a.consumer_id=v_consumer and a.arm is not null
    and e.registration->>'world'=new.world and runtime.nexloop_experiment_status(new.tenant_id,e.experiment_id)='started') then
   perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
   raise exception 'experiment incentive budget is 0 (D1, D5)' using errcode='NXB01';
  end if;
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return new;
 end if;
 if new.budget_kind<>'model' or new.consumption_id!~'^run:[0-9a-f-]{36}$' or new.unit<>'CNY' or round(new.amount,4)<=0 then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return new;end if;
 if new.world='real' then
  select a.experiment_id into v_exp from authz.nexloop_role_run_bindings b join control.nexloop_experiment_allocations a
   on a.tenant_id=b.tenant_id and a.consumer_id=b.consumer_id and a.arm='candidate'
   join control.nexloop_experiments e on e.tenant_id=a.tenant_id and e.experiment_id=a.experiment_id and e.registration->>'world'='real'
   where b.run_id=substr(new.consumption_id,5)::uuid and b.tenant_id=new.tenant_id and runtime.nexloop_experiment_status(new.tenant_id,a.experiment_id)='started'
   order by a.allocated_at limit 1;
 else
  select e.experiment_id into v_exp from control.nexloop_experiments e where e.tenant_id=new.tenant_id and e.registration->>'world'=new.world
   and runtime.nexloop_experiment_status(new.tenant_id,e.experiment_id)='started' order by e.registered_at limit 1;
 end if;
 if v_exp is not null then
  perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||new.tenant_id||':'||v_exp,0));
  select e.registration into r from control.nexloop_experiments e where e.tenant_id=new.tenant_id and e.experiment_id=v_exp;
  v_cap:=(r->'budget'->>'model_cny')::numeric;
  select coalesce(sum(amount_cny),0) into v_total from control.nexloop_experiment_spend where tenant_id=new.tenant_id and experiment_id=v_exp and category='model';
  if v_total+v_amount>v_cap then
   perform runtime.nexloop_experiment_autostop(new.tenant_id,v_exp,'budget_exhausted',runtime.nexloop_experiment_metrics(new.tenant_id,v_exp));
  else
   insert into control.nexloop_experiment_spend(tenant_id,experiment_id,category,amount_cny,spend_ref) values(new.tenant_id,v_exp,'model',v_amount,new.consumption_id)
    on conflict do nothing;
   if v_total+v_amount=v_cap then
    perform runtime.nexloop_experiment_autostop(new.tenant_id,v_exp,'budget_exhausted',runtime.nexloop_experiment_metrics(new.tenant_id,v_exp));end if;
  end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return new;
end $$;
alter function control.nexloop_experiment_budget_gate() owner to nexloop_owner;
revoke all on function control.nexloop_experiment_budget_gate() from public;
create trigger nx043_experiment_budget before insert on control.nexloop_budget_consumption for each row execute function control.nexloop_experiment_budget_gate();

create function authz.nexloop_experiment_guard_tick(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-experiment-guard-v1','eios:action:nexloop.alert.evaluate:1',
  array['nexloop_domain_worker']);
 x record;v jsonb;v_checked integer:=0;v_stopped integer:=0;
begin
 if p_world is distinct from 'real' then return jsonb_build_object('evaluated',false,'reason','real_world_only');end if;
 for x in select e.experiment_id from control.nexloop_experiments e where e.tenant_id=t
   and runtime.nexloop_experiment_status(t,e.experiment_id)='started' order by e.registered_at limit 200 loop
  v:=runtime.nexloop_experiment_guard(t,x.experiment_id);
  v_checked:=v_checked+1;
  if (v->>'stopped')::boolean then v_stopped:=v_stopped+1;end if;
 end loop;
 return jsonb_build_object('evaluated',true,'checked',v_checked,'stopped',v_stopped);
end $$;
alter function authz.nexloop_experiment_guard_tick(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_experiment_guard_tick(text,text,text,text,text) from public;
grant execute on function authz.nexloop_experiment_guard_tick(text,text,text,text,text) to nexloop_domain_worker;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_experiments;
revoke all on function runtime.nexloop_metrics_snapshot_before_experiments(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_experiments(p_tenant,p_world);v_prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select jsonb_build_object(
   'running',(select count(*) from control.nexloop_experiments e where e.tenant_id=p_tenant and runtime.nexloop_experiment_status(p_tenant,e.experiment_id)='started'),
   'auto_stopped_24h',(select count(*) from control.nexloop_experiment_events ev where ev.tenant_id=p_tenant and ev.status='stopped'
     and ev.recorded_by='nexloop:experiment-guard' and ev.recorded_at>=clock_timestamp()-interval '24 hours')) into v;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return s||jsonb_build_object('experiments',v);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
