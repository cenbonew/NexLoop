-- NX-043 review of the L5 fixes and X5 (L6, dispatcher 2026-10-21). The migration number lives only in the file name and the catalog.
-- 3.1 (confirmed, real erasure duty):
--   * runtime.nexloop_experiment_assign writes no row for a Consumer being erased (it answers excluded_reason erasing, allocated false);
--     runtime.nexloop_experiment_strategy_for answers null for them (3.3.b).
--   * control.nexloop_experiment_allocations joins the NX-029 erasure: when a Consumer's erasure enters purging (trigger on
--     control.nexloop_consumer_erasures), their allocation rows in that world keep arm and time under a random pseudonym (consumer_id
--     and cluster_key), and every cluster_key that was their consumer id is replaced too. control.nexloop_experiment_frozen lets only
--     that update through (runtime.nexloop_erasure_permits, as runtime.nexloop_sim_ledger_guard does for S2).
--   * control.nexloop_experiment_spend holds no Consumer: spend_ref is a Run reservation id or a caller reference (checked pattern,
--     never derived from a Consumer by the experiment code).
-- 2.1.a: an automatic stop records every guardrail hit at that moment (guardrails_hit in the audit metrics); the reason stays the first.
-- 2.1.c: the guard's stop has a request id no owner request can take (a fresh uuid; at most one stop is still the status check's).
-- 2.2.a: a spend is rounded to the ledger's 4 decimals before it is decided.
-- 2.3.a: reconciliation anomalies count unknown effects and determinations; a determination is only ever confirmed_not_delivered (0210
--   check), so nothing else is counted.
-- Re-created functions take their latest bodies (0290, 0294, 0295) with only these changes (registered in tests/test_function_copy_guard.py).

alter table control.nexloop_experiment_allocations drop constraint nexloop_experiment_allocations_consumer_id_check;
alter table control.nexloop_experiment_allocations add constraint nexloop_experiment_allocations_consumer_id_check
 check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$');

create or replace function control.nexloop_experiment_frozen() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='UPDATE' and runtime.nexloop_erasure_permits(current_user::text,tg_table_schema||'.'||tg_table_name,'UPDATE',to_jsonb(old),to_jsonb(new)) then return new;end if;  -- X1-X5 review 3.1: the erasure propagation only
 raise exception '% is append-only (a strategy version or an experiment registration is frozen; change it by a new one)',tg_table_name using errcode='42501';
end $$;

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

create or replace function runtime.nexloop_experiment_strategy_for(p_tenant text,p_world text,p_consumer text,p_cluster text default null) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);x record;v jsonb;v_ref jsonb;v_status text;out jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 if runtime.nexloop_consumer_erasing(p_tenant,p_world,p_consumer) then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;  -- review 3.3.b
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


-- 2.1.a: every guardrail hit at this moment (the guard stops on the first).
create function runtime.nexloop_experiment_guardrails_hit(p_tenant text,p_experiment text,m jsonb) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);r jsonb;h text[]:='{}';
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.registration into r from control.nexloop_experiments e where e.tenant_id=p_tenant and e.experiment_id=p_experiment;
 if (r->>'ends_at')::timestamptz<=clock_timestamp() then h:=h||'ended'::text;end if;
 if r->'twin'<>'null'::jsonb and not runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer) then h:=h||'twin_withdrawn'::text;end if;
 if exists(select 1 from (select r->'candidate' ref union all select r->'control') x,control.nexloop_strategy_versions s,
   jsonb_object_keys(coalesce(s.definition->'levers'->'outreach_purposes','{}'::jsonb)) k
   where x.ref<>'null'::jsonb and s.tenant_id=p_tenant and s.strategy_id=x.ref->>'strategy_id' and s.version=(x.ref->>'version')::integer
    and not runtime.nexloop_strategy_purpose_allowed(k)) then h:=h||'purpose_not_allowed'::text;end if;
 if ((m->>'model_cny')::numeric>=(r->'budget'->>'model_cny')::numeric and (r->'budget'->>'model_cny')::numeric>0)
   or (m->>'incentive_cny')::numeric>(r->'budget'->>'incentive_cny')::numeric then h:=h||'budget_exhausted'::text;end if;
 if (m->>'reconciliation_anomalies')::integer>(r->'guardrails'->>'reconciliation_anomalies')::integer then h:=h||'reconciliation_anomaly'::text;end if;
 if (m->>'candidate')::integer>0 and (m->>'control')::integer>0
   and (m->>'candidate_refused')::numeric/(m->>'candidate')::numeric-(m->>'control_refused')::numeric/(m->>'control')::numeric
    >(r->'guardrails'->>'refusal_or_unsubscribe_rate_delta')::numeric then h:=h||'refusal_rate'::text;end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return to_jsonb(h);
end $$;
alter function runtime.nexloop_experiment_guardrails_hit(text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_guardrails_hit(text,text,jsonb) from public;

-- 3.1: the erasure propagation into the allocations (purging: pseudonym, arm and time kept).
create function control.nexloop_experiment_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_pseudonym text:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
begin
 if new.state<>'purging' or old.state='purging' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 perform runtime.nexloop_erasure_open('control.nexloop_experiment_allocations',array['consumer_id','cluster_key'],false,'consumer-erasure:'||new.request_id);
 update control.nexloop_experiment_allocations a set consumer_id=v_pseudonym,cluster_key=v_pseudonym
  where a.tenant_id=new.tenant_id and a.consumer_id=new.consumer_id
   and exists(select 1 from control.nexloop_experiments e where e.tenant_id=a.tenant_id and e.experiment_id=a.experiment_id and e.registration->>'world'=new.world);
 update control.nexloop_experiment_allocations a set cluster_key=v_pseudonym
  where a.tenant_id=new.tenant_id and a.cluster_key=new.consumer_id
   and exists(select 1 from control.nexloop_experiments e where e.tenant_id=a.tenant_id and e.experiment_id=a.experiment_id and e.registration->>'world'=new.world);
 -- Only this table's scope is closed: the erasure step may hold others open in the same transaction.
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name='control.nexloop_experiment_allocations';
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function control.nexloop_experiment_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_experiment_erasure() from public;
create trigger nx043_experiment_erasure after update of state on control.nexloop_consumer_erasures for each row execute function control.nexloop_experiment_erasure();

create or replace function runtime.nexloop_experiment_autostop(p_tenant text,p_experiment text,p_code text,p_metrics jsonb) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 if runtime.nexloop_experiment_status(p_tenant,p_experiment) is distinct from 'started' then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return false;end if;
 insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,reason,recorded_by,request_id)
  values(p_tenant,p_experiment,'stopped','guardrail: '||p_code,'nexloop:experiment-guard','guard:'||p_experiment||':'||gen_random_uuid());
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment:autostop:'||p_experiment||':'||md5(p_tenant),'nexloop.experiment.auto_stopped',p_tenant,'nexloop:experiment-guard','experiment',
   'guard:'||p_experiment,jsonb_build_object('experiment_id',p_experiment,'guardrail',p_code,'metrics',coalesce(p_metrics,'{}'::jsonb)),clock_timestamp());
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return true;
end $$;

create or replace function runtime.nexloop_experiment_guard(p_tenant text,p_experiment text) returns jsonb
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
 if v_code is not null then perform runtime.nexloop_experiment_autostop(p_tenant,p_experiment,v_code,m||jsonb_build_object('guardrails_hit',runtime.nexloop_experiment_guardrails_hit(p_tenant,p_experiment,m)));end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return jsonb_build_object('experiment_id',p_experiment,'status',case when v_code is null then 'started' else 'stopped' end,'stopped',v_code is not null,
  'guardrail',v_code,'metrics',m);
end $$;

create or replace function runtime.nexloop_experiment_spend(p_tenant text,p_experiment text,p_category text,p_amount numeric,p_ref text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);r jsonb;v_cap numeric;v_total numeric;v_stopped boolean:=false;s control.nexloop_experiment_spend;
begin
 p_amount:=round(p_amount,4);  -- review 2.2.a: decide on the amount the ledger keeps
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
