-- NX-043 X4 review fixes (L6 review, dispatcher 2026-10-21). The migration number lives only in the file name and the catalog.
-- 3.1 (confirmed): a member joining a cluster already in the candidate group is checked like a new cluster - the candidate group
--     stays at most its cap and at most the control group's size; a member that would break either is excluded
--     (candidate_cluster_full) rather than moved to control, so a session cluster is never split across groups (D2, AT-048).
-- 1.1: a Consumer being erased is excluded (erasing).
-- 4.1: at decision a candidate gets the candidate strategy only while that version is still published and a selected twin still
--     selectable, else the control strategy; a retired control strategy gives null (the current policy).
-- runtime.nexloop_experiment_assign and runtime.nexloop_experiment_strategy_for are re-created from 0292 with only these changes
-- (registered in tests/test_function_copy_guard.py).

alter table control.nexloop_experiment_allocations drop constraint nexloop_experiment_allocations_excluded_reason_check;
alter table control.nexloop_experiment_allocations add constraint nexloop_experiment_allocations_excluded_reason_check check(excluded_reason in
 ('persona_not_real','contact_restricted','in_takeover','erasing','in_other_experiment','candidate_cluster_full','sample_full'));

create or replace function runtime.nexloop_experiment_assign(p_tenant text,p_experiment text,p_consumer text,p_cluster text default null) returns jsonb
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
 elsif runtime.nexloop_consumer_erasing(p_tenant,r->>'world',p_consumer) then v_reason:='erasing';  -- X4 review 1.1
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
  if v_arm='candidate' and (v_cand+1>v_cap_cand or v_cand+1>v_ctrl) then v_arm:=null;v_reason:='candidate_cluster_full';
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

create or replace function runtime.nexloop_experiment_strategy_for(p_tenant text,p_world text,p_consumer text,p_cluster text default null) returns jsonb
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
 if x.arm='candidate' and v_status='started' and (x.registration->>'ends_at')::timestamptz>clock_timestamp() and runtime.nexloop_strategy_status(p_tenant,x.registration->'candidate'->>'strategy_id',(x.registration->'candidate'->>'version')::integer)='published' and (x.registration->'twin'='null'::jsonb or runtime.nexloop_consumer_twin_selectable(p_tenant,x.registration->'twin'->>'twin_id',(x.registration->'twin'->>'version')::integer)) then v_ref:=x.registration->'candidate';
 else v_ref:=x.registration->'control';end if;
 if v_ref<>'null'::jsonb and v_ref=x.registration->'control' and runtime.nexloop_strategy_status(p_tenant,v_ref->>'strategy_id',(v_ref->>'version')::integer) is distinct from 'published' then v_ref:='null'::jsonb;end if;  -- X4 review 4.1
 out:=jsonb_build_object('experiment_id',x.experiment_id,'arm',x.arm,'status',v_status,
  'applied',case when v_ref=x.registration->'candidate' then 'candidate' else 'control' end,
  'strategy',case when v_ref='null'::jsonb then null else (select jsonb_build_object('strategy_id',s.strategy_id,'version',s.version,'levers',s.definition->'levers')
   from control.nexloop_strategy_versions s where s.tenant_id=p_tenant and s.strategy_id=v_ref->>'strategy_id' and s.version=(v_ref->>'version')::integer) end);
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return out;
end $$;
