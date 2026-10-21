-- NX-043 X6 wiring requirement (L3, dispatcher 2026-10-21): no cross-contamination between rounds of one comparison. A Consumer who
-- already has a group in another experiment of the same comparison - the same candidate and control strategy versions, tenant and world,
-- whatever that experiment's status - is left out of a new allocation (excluded_reason in_same_comparison) and, as for erasing, no row is
-- written. An erased Consumer's rows carry a pseudonym, so a tombstone never matches. runtime.nexloop_experiment_assign is re-created
-- from its 0295 body with only this check (registered in tests/test_function_copy_guard.py); L3's runtime.nexloop_experiment_leave_out
-- is to be aligned at integration. The migration number lives only in the file name and the catalog.

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
 -- Review 3.1: a Consumer being erased gets no row at all (nothing new is kept about them).
 if runtime.nexloop_consumer_erasing(p_tenant,r->>'world',p_consumer) then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return jsonb_build_object('experiment_id',p_experiment,'status',v_status,'arm',null,'excluded_reason','erasing','allocated',false);end if;
 -- X6 wiring (L3): a Consumer already in a group of another experiment of the same comparison (same candidate and control versions, tenant and world, any status) is left out, without a row.
 if exists(select 1 from control.nexloop_experiment_allocations o join control.nexloop_experiments e on e.tenant_id=o.tenant_id and e.experiment_id=o.experiment_id where o.tenant_id=p_tenant and o.consumer_id=p_consumer and o.arm is not null and o.experiment_id<>p_experiment and e.registration->>'world'=r->>'world' and e.registration->'candidate'=r->'candidate' and e.registration->'control'=r->'control') then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return jsonb_build_object('experiment_id',p_experiment,'status',v_status,'arm',null,'excluded_reason','in_same_comparison','allocated',false);end if;
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
