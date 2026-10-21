-- NX-043 X4 (design §3, §4, D2, D9): online allocation. The migration number lives only in the file name and the catalog.
-- 1. Eligibility is part of the preregistration (D3): runtime.nexloop_experiment_eligibility_default() is the rule of design §3
--    (persona real, no active contact restriction, no active takeover), written into every frozen registration; a member may not be
--    loosened (eligibility_looser_than_default). runtime.nexloop_experiment_registration_problem and _frozen are re-created from
--    0285 with only that change (registered in tests/test_function_copy_guard.py). A registration frozen before this migration has no
--    eligibility member and is read with the default rule.
-- 2. control.nexloop_experiment_allocations: one frozen row per (experiment, consumer): its cluster, its arm (candidate, control) or
--    its exclusion reason. Written once, never changed (D2: a consumer stays in its group for the whole experiment).
-- 3. runtime.nexloop_experiment_assign(tenant, experiment, consumer, cluster): the arm of a consumer in a started experiment,
--    allocating on first sight (D2): a cluster (by default the consumer; the caller passes a session cluster key) always gets one arm;
--    a new cluster goes to the candidate group by a seeded hash at the registered share, only while the candidate group stays at most
--    sample_size.candidate (<= 20) and at most the control group's size (control >= 50% at every moment); otherwise control until
--    sample_size.control. A selected twin must still be selectable (D9). A consumer already allocated in another running experiment
--    of the same world is excluded. Exclusions are recorded and counted, never retried.
-- 4. runtime.nexloop_experiment_strategy_for(tenant, world, consumer, cluster): what a decision uses - the candidate strategy for a
--    candidate consumer of a started experiment, the control strategy (null: the current policy) for control consumers and for
--    every consumer of a stopped or ended experiment, null when the consumer is in no experiment. It only chooses a strategy version:
--    any external effect still passes every outreach gate and the three real dispatch layers, and nothing here writes a switch.

create function runtime.nexloop_experiment_eligibility_default() returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select '{"persona":"real","exclude_contact_restricted":true,"exclude_in_takeover":true}'::jsonb
$$;
alter function runtime.nexloop_experiment_eligibility_default() owner to nexloop_owner;

create or replace function runtime.nexloop_experiment_registration_problem(p_tenant text,r jsonb) returns text language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v_def jsonb:=runtime.nexloop_experiment_defaults();v_ref jsonb;v_kind text;
begin
 if jsonb_typeof(r) is distinct from 'object' then return 'invalid';end if;
 -- Design §4: an experiment never opens real dispatch; a registration that names it is refused before anything else.
 if r::text ~* 'real.?dispatch' then return 'real_dispatch_not_allowed';end if;
 if exists(select 1 from jsonb_object_keys(r) k where k not in ('schema','experiment_id','stage','world','hypothesis','candidate','control','twin',
   'primary_metric','guardrails','sample_size','maturity','budget','ends_at','allocation_seed','eligibility')) then return 'invalid';end if;
 -- D3: every preregistered field is required (control and twin explicitly, null when there is none).
 if exists(select 1 from unnest(array['schema','experiment_id','stage','world','hypothesis','candidate','control','twin','primary_metric','guardrails',
   'sample_size','maturity','budget','ends_at','allocation_seed']) k where not r ? k) then return 'registration_incomplete';end if;
 if r->>'schema' is distinct from 'nexloop-experiment-registration/1' or coalesce(r->>'experiment_id','')!~'^[a-z][a-z0-9_.-]{0,63}$'
  or coalesce(r->>'stage','') not in ('screening','shadow','online')
  or jsonb_typeof(r->'world')<>'string' or runtime.nexloop_world_kind(r->>'world') is null
  or jsonb_typeof(r->'hypothesis')<>'string' or char_length(btrim(r->>'hypothesis')) not between 1 and 500
  or jsonb_typeof(r->'allocation_seed')<>'number' or (r->>'allocation_seed')!~'^[0-9]{1,10}$' or (r->>'allocation_seed')::bigint>2147483647
  or jsonb_typeof(r->'ends_at')<>'string' or (r->>'ends_at')!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})$' then return 'invalid';end if;
 if runtime.nexloop_twin_text_looks_real(r->>'hypothesis') then return 'looks_real';end if;
 foreach v_ref in array array[r->'candidate',r->'control'] loop
  if v_ref is distinct from 'null'::jsonb and (jsonb_typeof(v_ref)<>'object' or (select array_agg(k order by k) from jsonb_object_keys(v_ref) k) is distinct from array['strategy_id','version']
   or coalesce(v_ref->>'strategy_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(v_ref->'version')<>'number' or (v_ref->>'version')!~'^[0-9]{1,9}$') then return 'invalid';end if;
 end loop;
 if r->'candidate'='null'::jsonb then return 'registration_incomplete';end if;
 if r->'twin'<>'null'::jsonb and (jsonb_typeof(r->'twin')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(r->'twin') k) is distinct from array['twin_id','version']
  or coalesce(r->'twin'->>'twin_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(r->'twin'->'version')<>'number' or (r->'twin'->>'version')!~'^[0-9]{1,9}$') then return 'invalid';end if;
 if jsonb_typeof(r->'primary_metric')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(r->'primary_metric') k) is distinct from array['direction','key']
  or coalesce(r->'primary_metric'->>'key','')!~'^[a-z][a-z0-9_.]{0,63}$' or coalesce(r->'primary_metric'->>'direction','') not in ('increase','decrease') then return 'invalid';end if;
 -- D4: the guardrails; a member left out takes the default, a given one may only be tighter.
 if jsonb_typeof(r->'guardrails')<>'object' or exists(select 1 from jsonb_object_keys(r->'guardrails') k where k not in
   ('refusal_or_unsubscribe_rate_delta','reconciliation_anomalies','stop_on_budget_exhausted')) then return 'invalid';end if;
 if (r->'guardrails' ? 'refusal_or_unsubscribe_rate_delta' and (jsonb_typeof(r->'guardrails'->'refusal_or_unsubscribe_rate_delta')<>'number'
    or (r->'guardrails'->>'refusal_or_unsubscribe_rate_delta')::numeric<=0
    or (r->'guardrails'->>'refusal_or_unsubscribe_rate_delta')::numeric>(v_def->'guardrails'->>'refusal_or_unsubscribe_rate_delta')::numeric))
  or (r->'guardrails' ? 'reconciliation_anomalies' and r->'guardrails'->'reconciliation_anomalies' is distinct from '0'::jsonb)
  or (r->'guardrails' ? 'stop_on_budget_exhausted' and r->'guardrails'->'stop_on_budget_exhausted' is distinct from 'true'::jsonb) then return 'guardrail_looser_than_default';end if;
 -- D6: the maturity window and the minimum sample per group; defaults when left out, never shorter or smaller.
 if jsonb_typeof(r->'maturity')<>'object' or exists(select 1 from jsonb_object_keys(r->'maturity') k where k not in ('window_days','min_samples_per_group'))
  or exists(select 1 from jsonb_each(r->'maturity') m where jsonb_typeof(m.value)<>'number' or (m.value#>>'{}')!~'^[0-9]{1,5}$') then return 'invalid';end if;
 if (r->'maturity' ? 'window_days' and ((r->'maturity'->>'window_days')::integer<(v_def->'maturity'->>'window_days')::integer or (r->'maturity'->>'window_days')::integer>90))
  or (r->'maturity' ? 'min_samples_per_group' and (r->'maturity'->>'min_samples_per_group')::integer<(v_def->'maturity'->>'min_samples_per_group')::integer) then
  return 'maturity_below_default';end if;
 -- X4 (design §3): the eligibility rule is frozen here; a member left out takes the default and none can be loosened.
 if r ? 'eligibility' and (jsonb_typeof(r->'eligibility')<>'object' or exists(select 1 from jsonb_each(r->'eligibility') e where not (runtime.nexloop_experiment_eligibility_default() ? e.key) or e.value<>runtime.nexloop_experiment_eligibility_default()->e.key)) then return 'eligibility_looser_than_default';end if;
 -- D2: candidate group at most 20 consumers, the control group at least as large (at least half); D6: each at least the minimum sample.
 if jsonb_typeof(r->'sample_size')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(r->'sample_size') k) is distinct from array['candidate','control']
  or exists(select 1 from jsonb_each(r->'sample_size') s where jsonb_typeof(s.value)<>'number' or (s.value#>>'{}')!~'^[0-9]{1,5}$') then return 'invalid';end if;
 if (r->'sample_size'->>'candidate')::integer not between 1 and (v_def->>'max_candidate_consumers')::integer then return 'candidate_group_too_large';end if;
 if (r->'sample_size'->>'control')::integer<(r->'sample_size'->>'candidate')::integer then return 'control_group_too_small';end if;
 -- D5: a model ceiling per experiment; D1: no incentive experiments in the first batch.
 if jsonb_typeof(r->'budget')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(r->'budget') k) is distinct from array['incentive_cny','model_cny']
  or jsonb_typeof(r->'budget'->'model_cny')<>'number' or jsonb_typeof(r->'budget'->'incentive_cny')<>'number'
  or (r->'budget'->>'model_cny')::numeric<0 or (r->'budget'->>'model_cny')::numeric>(v_def->>'max_model_cny')::numeric then return 'invalid';end if;
 if (r->'budget'->>'incentive_cny')::numeric<>0 then return 'incentive_not_allowed';end if;
 begin
  if (r->>'ends_at')::timestamptz<=clock_timestamp() or (r->>'ends_at')::timestamptz>clock_timestamp()+interval '180 days' then return 'ends_at_out_of_range';end if;
 exception when others then return 'invalid';
 end;
 -- The stage decides the world: screening in a simulation world with a twin, shadow in a shadow world, online in real.
 v_kind:=runtime.nexloop_world_kind(r->>'world');
 if (r->>'stage'='screening' and v_kind<>'simulation') or (r->>'stage'='shadow' and v_kind<>'shadow') or (r->>'stage'='online' and r->>'world'<>'real') then
  return 'world_mismatch';end if;
 if r->>'stage'='screening' and r->'twin'='null'::jsonb then return 'twin_required';end if;
 if r->'twin'<>'null'::jsonb and not exists(select 1 from control.nexloop_consumer_twins t where t.tenant_id=p_tenant and t.twin_id=r->'twin'->>'twin_id'
   and t.version=(r->'twin'->>'version')::integer) then return 'twin_unknown';end if;
 foreach v_ref in array array[r->'candidate',r->'control'] loop
  if v_ref<>'null'::jsonb and runtime.nexloop_strategy_status(p_tenant,v_ref->>'strategy_id',(v_ref->>'version')::integer) is distinct from 'published' then
   return 'strategy_not_published';end if;
 end loop;
 if r->'candidate'=r->'control' then return 'invalid';end if;
 return null;
end $$;

create or replace function runtime.nexloop_experiment_registration_frozen(r jsonb) returns jsonb language sql stable set search_path=pg_catalog,pg_temp as $$
 select r||jsonb_build_object('hypothesis',btrim(r->>'hypothesis'),
  'guardrails',(runtime.nexloop_experiment_defaults()->'guardrails')||(r->'guardrails'),
  'maturity',(runtime.nexloop_experiment_defaults()->'maturity')||(r->'maturity'),
  'eligibility',runtime.nexloop_experiment_eligibility_default()||coalesce(r->'eligibility','{}'::jsonb),
  'ends_at',to_char((r->>'ends_at')::timestamptz at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'))
$$;

create table control.nexloop_experiment_allocations (
 tenant_id text not null,experiment_id text not null,
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 cluster_key text not null check(char_length(cluster_key) between 1 and 200),
 arm text check(arm in ('candidate','control')),
 excluded_reason text check(excluded_reason in ('persona_not_real','contact_restricted','in_takeover','in_other_experiment','candidate_cluster_full','sample_full')),
 allocated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,experiment_id,consumer_id),
 check((arm is null)<>(excluded_reason is null)),
 foreign key(tenant_id,experiment_id) references control.nexloop_experiments(tenant_id,experiment_id));
create index nexloop_experiment_allocations_cluster on control.nexloop_experiment_allocations(tenant_id,experiment_id,cluster_key);
create index nexloop_experiment_allocations_consumer on control.nexloop_experiment_allocations(tenant_id,consumer_id);
alter table control.nexloop_experiment_allocations owner to nexloop_owner;
alter table control.nexloop_experiment_allocations enable row level security;
alter table control.nexloop_experiment_allocations force row level security;
create policy tenant_boundary on control.nexloop_experiment_allocations to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_experiment_allocations from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api;
create trigger nx043_frozen before update or delete on control.nexloop_experiment_allocations for each row execute function control.nexloop_experiment_frozen();
create trigger nx043_no_truncate before truncate on control.nexloop_experiment_allocations for each statement execute function control.nexloop_experiment_frozen();

-- The arm of one consumer in one experiment (allocating on first sight while the experiment runs).
create function runtime.nexloop_experiment_assign(p_tenant text,p_experiment text,p_consumer text,p_cluster text default null) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);x control.nexloop_experiments;a control.nexloop_experiment_allocations;r jsonb;
 v_status text;v_cluster text:=coalesce(nullif(p_cluster,''),p_consumer);v_elig jsonb;v_reason text;v_arm text;v_cand integer;v_ctrl integer;
 v_cap_cand integer;v_cap_ctrl integer;v_share numeric;v_draw numeric;
begin
 if coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' or char_length(v_cluster)>200 then raise exception 'experiment: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select * into x from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=p_experiment;
 if not found then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 r:=x.registration;
 v_status:=runtime.nexloop_experiment_status(p_tenant,p_experiment);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment-allocation:'||p_tenant||':'||p_experiment,0));
 select * into a from control.nexloop_experiment_allocations where tenant_id=p_tenant and experiment_id=p_experiment and consumer_id=p_consumer;
 if found then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('experiment_id',p_experiment,'status',v_status,'arm',a.arm,'excluded_reason',a.excluded_reason,'cluster_key',a.cluster_key,'allocated',false);
 end if;
 -- Only a started experiment before its end allocates; a stopped or ended one never takes a new consumer.
 if v_status<>'started' or (r->>'ends_at')::timestamptz<=clock_timestamp() then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('experiment_id',p_experiment,'status',v_status,'arm',null,'excluded_reason',null,'allocated',false);
 end if;
 -- D9: an experiment that selects a twin allocates only while that twin is selectable.
 if r->'twin'<>'null'::jsonb and not runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer) then
  raise exception 'experiment: twin_not_calibrated' using errcode='22023';end if;
 v_elig:=runtime.nexloop_experiment_eligibility_default()||coalesce(r->'eligibility','{}'::jsonb);
 -- Eligibility (design §3, frozen in the registration).
 if runtime.nexloop_consumer_persona(p_tenant,r->>'world',p_consumer)<>(v_elig->>'persona') and r->>'stage'='online' then v_reason:='persona_not_real';
 elsif (v_elig->>'exclude_contact_restricted')::boolean and exists(select 1 from control.nexloop_contact_restrictions c where c.tenant_id=p_tenant
   and c.world=r->>'world' and c.consumer_id=p_consumer and c.active) then v_reason:='contact_restricted';
 elsif (v_elig->>'exclude_in_takeover')::boolean and exists(select 1 from control.nexloop_takeovers t where t.tenant_id=p_tenant and t.world=r->>'world'
   and t.consumer_id=p_consumer and t.ended_at is null and t.expires_at>clock_timestamp()) then v_reason:='in_takeover';
 elsif exists(select 1 from control.nexloop_experiment_allocations o join control.nexloop_experiments e on e.tenant_id=o.tenant_id and e.experiment_id=o.experiment_id
   where o.tenant_id=p_tenant and o.consumer_id=p_consumer and o.arm is not null and o.experiment_id<>p_experiment and e.registration->>'world'=r->>'world'
   and runtime.nexloop_experiment_status(p_tenant,o.experiment_id)='started') then v_reason:='in_other_experiment';
 end if;
 if v_reason is null then
  v_cap_cand:=(r->'sample_size'->>'candidate')::integer;v_cap_ctrl:=(r->'sample_size'->>'control')::integer;
  select count(*) filter(where arm='candidate'),count(*) filter(where arm='control') into v_cand,v_ctrl
   from control.nexloop_experiment_allocations where tenant_id=p_tenant and experiment_id=p_experiment;
  -- D2: a session cluster has one arm.
  select o.arm into v_arm from control.nexloop_experiment_allocations o where o.tenant_id=p_tenant and o.experiment_id=p_experiment
   and o.cluster_key=v_cluster and o.arm is not null order by o.allocated_at limit 1;
  if v_arm='candidate' and v_cand+1>v_cap_cand then v_arm:=null;v_reason:='candidate_cluster_full';
  elsif v_arm='control' and v_ctrl+1>v_cap_ctrl then v_arm:=null;v_reason:='sample_full';
  elsif v_arm is null then
   -- A new cluster: the seeded draw at the registered share; the candidate group never exceeds its cap or the control group's size.
   v_share:=v_cap_cand::numeric/(v_cap_cand+v_cap_ctrl);
   v_draw:=(('x'||substr(encode(sha256(convert_to((r->>'allocation_seed')||':'||p_experiment||':'||v_cluster,'UTF8')),'hex'),1,12))::bit(48)::bigint)::numeric/281474976710656;
   if v_draw<v_share and v_cand+1<=v_cap_cand and v_cand+1<=v_ctrl then v_arm:='candidate';
   elsif v_ctrl+1<=v_cap_ctrl then v_arm:='control';
   elsif v_cand+1<=v_cap_cand and v_cand+1<=v_ctrl then v_arm:='candidate';
   else v_reason:='sample_full';end if;
  end if;
 end if;
 insert into control.nexloop_experiment_allocations(tenant_id,experiment_id,consumer_id,cluster_key,arm,excluded_reason)
  values(p_tenant,p_experiment,p_consumer,v_cluster,v_arm,v_reason) returning * into a;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return jsonb_build_object('experiment_id',p_experiment,'status',v_status,'arm',a.arm,'excluded_reason',a.excluded_reason,'cluster_key',a.cluster_key,'allocated',true);
end $$;
alter function runtime.nexloop_experiment_assign(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_assign(text,text,text,text) from public;

-- What a decision uses for one consumer in one world (design §3, §5): see the header. Never opens real dispatch.
create function runtime.nexloop_experiment_strategy_for(p_tenant text,p_world text,p_consumer text,p_cluster text default null) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);x record;v jsonb;v_ref jsonb;v_status text;out jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 -- A consumer already allocated: its experiment decides (the latest allocation with an arm).
 select e.experiment_id,e.registration,o.arm into x from control.nexloop_experiment_allocations o
  join control.nexloop_experiments e on e.tenant_id=o.tenant_id and e.experiment_id=o.experiment_id
  where o.tenant_id=p_tenant and o.consumer_id=p_consumer and o.arm is not null and e.registration->>'world'=p_world
  order by (runtime.nexloop_experiment_status(p_tenant,e.experiment_id)='started') desc,o.allocated_at desc limit 1;
 if not found then
  -- Otherwise the oldest started experiment of this world allocates it now.
  for x in select e.experiment_id,e.registration,null::text arm from control.nexloop_experiments e where e.tenant_id=p_tenant and e.registration->>'world'=p_world
    and runtime.nexloop_experiment_status(p_tenant,e.experiment_id)='started' order by e.registered_at loop
   v:=runtime.nexloop_experiment_assign(p_tenant,x.experiment_id,p_consumer,p_cluster);
   if v->>'arm' is not null then x.arm:=v->>'arm';exit;end if;
  end loop;
  if x.arm is null then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 end if;
 v_status:=runtime.nexloop_experiment_status(p_tenant,x.experiment_id);
 -- Stopped, or past its end: everyone returns to the control strategy.
 if x.arm='candidate' and v_status='started' and (x.registration->>'ends_at')::timestamptz>clock_timestamp() then v_ref:=x.registration->'candidate';
 else v_ref:=x.registration->'control';end if;
 out:=jsonb_build_object('experiment_id',x.experiment_id,'arm',x.arm,'status',v_status,
  'applied',case when v_ref=x.registration->'candidate' then 'candidate' else 'control' end,
  'strategy',case when v_ref='null'::jsonb then null else (select jsonb_build_object('strategy_id',s.strategy_id,'version',s.version,'levers',s.definition->'levers')
   from control.nexloop_strategy_versions s where s.tenant_id=p_tenant and s.strategy_id=v_ref->>'strategy_id' and s.version=(v_ref->>'version')::integer) end);
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return out;
end $$;
alter function runtime.nexloop_experiment_strategy_for(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_strategy_for(text,text,text,text) from public;
