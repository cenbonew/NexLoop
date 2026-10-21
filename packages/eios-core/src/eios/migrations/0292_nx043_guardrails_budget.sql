-- NX-043 X5 (design §4, §5, D4, D5): stopping rules and the experiment budget. The migration number lives only in the file name and
-- the catalog.
-- 1. runtime.nexloop_experiment_autostop: the guard's stop - one appended 'stopped' event (recorded by nexloop:experiment-guard, reason
--    'guardrail: <code>') and an audit row with the metrics at that moment. Stopping only returns every consumer to the control
--    strategy (X4 strategy_for); nothing is published, withdrawn or resent: effects already dispatched are only reconciled.
-- 2. runtime.nexloop_experiment_guard(tenant, experiment): evaluates a started experiment and stops it on the first guardrail hit:
--    ended (its end time), twin_withdrawn (a selected twin no longer selectable, X1 review 4.1), purpose_not_allowed (a purpose of a
--    strategy reclassified), budget_exhausted, refusal_rate (D4: the candidate group's rate of contact refusals and marketing
--    withdrawals since allocation exceeds the control group's by more than the registered delta, default 2 points),
--    reconciliation_anomaly (D4: any effect intent of an allocated consumer since allocation in state unknown, or confirmed not
--    delivered).
-- 3. control.nexloop_experiment_spend: the experiment's own ledger per category (model, incentive; D5), beside the existing budget
--    accounts. runtime.nexloop_experiment_spend(tenant, experiment, category, amount, ref) admits a spend only within the registered
--    cap (budget.model_cny, budget.incentive_cny - 0 in the first batch); a spend that would exceed it is refused and stops the
--    experiment; reaching the cap exactly is admitted and stops it (D4: budget exhausted). Idempotent per ref.
-- Nothing here reads or writes a real dispatch switch.

create table control.nexloop_experiment_spend (
 event_id bigint generated always as identity primary key,
 tenant_id text not null,experiment_id text not null,
 category text not null check(category in ('model','incentive')),
 amount_cny numeric(12,4) not null check(amount_cny>0),
 spend_ref text not null check(spend_ref~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,experiment_id,spend_ref),
 foreign key(tenant_id,experiment_id) references control.nexloop_experiments(tenant_id,experiment_id));
alter table control.nexloop_experiment_spend owner to nexloop_owner;
alter table control.nexloop_experiment_spend enable row level security;
alter table control.nexloop_experiment_spend force row level security;
create policy tenant_boundary on control.nexloop_experiment_spend to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_experiment_spend from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api;
create trigger nx043_frozen before update or delete on control.nexloop_experiment_spend for each row execute function control.nexloop_experiment_frozen();
create trigger nx043_no_truncate before truncate on control.nexloop_experiment_spend for each statement execute function control.nexloop_experiment_frozen();

create function runtime.nexloop_experiment_autostop(p_tenant text,p_experiment text,p_code text,p_metrics jsonb) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 if runtime.nexloop_experiment_status(p_tenant,p_experiment) is distinct from 'started' then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return false;end if;
 insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,reason,recorded_by,request_id)
  values(p_tenant,p_experiment,'stopped','guardrail: '||p_code,'nexloop:experiment-guard','guard:'||p_experiment);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment:autostop:'||p_experiment||':'||md5(p_tenant),'nexloop.experiment.auto_stopped',p_tenant,'nexloop:experiment-guard','experiment',
   'guard:'||p_experiment,jsonb_build_object('experiment_id',p_experiment,'guardrail',p_code,'metrics',coalesce(p_metrics,'{}'::jsonb)),clock_timestamp());
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return true;
end $$;
alter function runtime.nexloop_experiment_autostop(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_autostop(text,text,text,jsonb) from public;

-- The guardrail metrics of an experiment (numbers only).
create function runtime.nexloop_experiment_metrics(p_tenant text,p_experiment text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);r jsonb;v jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.registration into r from control.nexloop_experiments e where e.tenant_id=p_tenant and e.experiment_id=p_experiment;
 with a as (select o.consumer_id,o.arm,o.allocated_at from control.nexloop_experiment_allocations o
   where o.tenant_id=p_tenant and o.experiment_id=p_experiment and o.arm is not null),
  refused as (select a.consumer_id,a.arm from a where exists(select 1 from control.nexloop_contact_restrictions c where c.tenant_id=p_tenant
    and c.world=r->>'world' and c.consumer_id=a.consumer_id and c.restricted_at>=a.allocated_at)
   or exists(select 1 from runtime.nexloop_marketing_consents m where m.tenant_id=p_tenant and m.world=r->>'world' and m.consumer_id=a.consumer_id
    and m.kind='withdrawn' and m.recorded_at>=a.allocated_at)),
  anomalies as (select count(*) n from runtime.nexloop_effect_intents i join a on a.consumer_id=i.consumer_id
   where i.tenant_id=p_tenant and i.world=r->>'world' and i.created_at>=a.allocated_at
    and (i.state='unknown' or exists(select 1 from runtime.nexloop_effect_determinations d where d.tenant_id=i.tenant_id and d.world=i.world and d.intent_id=i.intent_id)))
 select jsonb_build_object(
   'candidate',(select count(*) from a where arm='candidate'),'control',(select count(*) from a where arm='control'),
   'candidate_refused',(select count(*) from refused where arm='candidate'),'control_refused',(select count(*) from refused where arm='control'),
   'reconciliation_anomalies',(select n from anomalies),
   'model_cny',(select coalesce(sum(amount_cny),0) from control.nexloop_experiment_spend s where s.tenant_id=p_tenant and s.experiment_id=p_experiment and s.category='model'),
   'incentive_cny',(select coalesce(sum(amount_cny),0) from control.nexloop_experiment_spend s where s.tenant_id=p_tenant and s.experiment_id=p_experiment and s.category='incentive'))
  into v;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_experiment_metrics(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_metrics(text,text) from public;

create function runtime.nexloop_experiment_guard(p_tenant text,p_experiment text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);r jsonb;m jsonb;v_code text;v_ref jsonb;v_delta numeric;v_cand_rate numeric;v_ctrl_rate numeric;v_status text;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.registration into r from control.nexloop_experiments e where e.tenant_id=p_tenant and e.experiment_id=p_experiment;
 if r is null then raise exception 'experiment: not_found' using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||p_experiment,0));
 v_status:=runtime.nexloop_experiment_status(p_tenant,p_experiment);
 m:=runtime.nexloop_experiment_metrics(p_tenant,p_experiment);
 if v_status<>'started' then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('experiment_id',p_experiment,'status',v_status,'stopped',false,'guardrail',null,'metrics',m);
 end if;
 if (r->>'ends_at')::timestamptz<=clock_timestamp() then v_code:='ended';
 elsif r->'twin'<>'null'::jsonb and not runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer) then v_code:='twin_withdrawn';
 elsif exists(select 1 from (select r->'candidate' ref union all select r->'control') x,control.nexloop_strategy_versions s,
   jsonb_object_keys(coalesce(s.definition->'levers'->'outreach_purposes','{}'::jsonb)) k
   where x.ref<>'null'::jsonb and s.tenant_id=p_tenant and s.strategy_id=x.ref->>'strategy_id' and s.version=(x.ref->>'version')::integer
    and not runtime.nexloop_strategy_purpose_allowed(k)) then v_code:='purpose_not_allowed';
 elsif (m->>'model_cny')::numeric>=(r->'budget'->>'model_cny')::numeric and (r->'budget'->>'model_cny')::numeric>0
   or (m->>'incentive_cny')::numeric>(r->'budget'->>'incentive_cny')::numeric then v_code:='budget_exhausted';
 elsif (m->>'reconciliation_anomalies')::integer>(r->'guardrails'->>'reconciliation_anomalies')::integer then v_code:='reconciliation_anomaly';
 else
  v_delta:=(r->'guardrails'->>'refusal_or_unsubscribe_rate_delta')::numeric;
  if (m->>'candidate')::integer>0 and (m->>'control')::integer>0 then
   v_cand_rate:=(m->>'candidate_refused')::numeric/(m->>'candidate')::numeric;
   v_ctrl_rate:=(m->>'control_refused')::numeric/(m->>'control')::numeric;
   if v_cand_rate-v_ctrl_rate>v_delta then v_code:='refusal_rate';end if;
  end if;
 end if;
 if v_code is not null then perform runtime.nexloop_experiment_autostop(p_tenant,p_experiment,v_code,m);end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return jsonb_build_object('experiment_id',p_experiment,'status',case when v_code is null then 'started' else 'stopped' end,'stopped',v_code is not null,
  'guardrail',v_code,'metrics',m);
end $$;
alter function runtime.nexloop_experiment_guard(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_guard(text,text) from public;

-- D5: a spend inside the experiment's cap; beyond it the spend is refused (allowed false) and the experiment stops.
create function runtime.nexloop_experiment_spend(p_tenant text,p_experiment text,p_category text,p_amount numeric,p_ref text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);r jsonb;v_cap numeric;v_total numeric;v_stopped boolean:=false;s control.nexloop_experiment_spend;
begin
 if coalesce(p_category,'') not in ('model','incentive') or p_amount is null or p_amount<=0 or coalesce(p_ref,'')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then
  raise exception 'experiment: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.registration into r from control.nexloop_experiments e where e.tenant_id=p_tenant and e.experiment_id=p_experiment;
 if r is null then raise exception 'experiment: not_found' using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||p_experiment,0));
 select * into s from control.nexloop_experiment_spend where tenant_id=p_tenant and experiment_id=p_experiment and spend_ref=p_ref;
 if found then
  if (s.category,s.amount_cny) is distinct from (p_category,p_amount::numeric(12,4)) then raise exception 'experiment: request_reused' using errcode='22023';end if;
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('allowed',true,'replayed',true,'stopped',false);
 end if;
 if runtime.nexloop_experiment_status(p_tenant,p_experiment) is distinct from 'started' then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('allowed',false,'reason','not_running','stopped',false);
 end if;
 v_cap:=(r->'budget'->>(p_category||'_cny'))::numeric;
 select coalesce(sum(amount_cny),0) into v_total from control.nexloop_experiment_spend where tenant_id=p_tenant and experiment_id=p_experiment and category=p_category;
 if v_total+p_amount>v_cap then
  v_stopped:=runtime.nexloop_experiment_autostop(p_tenant,p_experiment,'budget_exhausted',runtime.nexloop_experiment_metrics(p_tenant,p_experiment));
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('allowed',false,'reason','budget_exhausted','stopped',v_stopped);
 end if;
 insert into control.nexloop_experiment_spend(tenant_id,experiment_id,category,amount_cny,spend_ref) values(p_tenant,p_experiment,p_category,p_amount,p_ref);
 if v_total+p_amount=v_cap then
  v_stopped:=runtime.nexloop_experiment_autostop(p_tenant,p_experiment,'budget_exhausted',runtime.nexloop_experiment_metrics(p_tenant,p_experiment));end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return jsonb_build_object('allowed',true,'replayed',false,'stopped',v_stopped);
end $$;
alter function runtime.nexloop_experiment_spend(text,text,text,numeric,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_spend(text,text,text,numeric,text) from public;
