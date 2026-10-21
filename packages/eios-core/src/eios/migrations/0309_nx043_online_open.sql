-- NX-043 online opened (owner 2026-10-21: "this is still a test stage, so it can be opened"): an online experiment (world real) can be
-- started. The migration number lives only in the file name and the catalog.
-- 1. control.nexloop_governed_start_experiment (0290) loses only online_not_available. Every other start check stays: ended, the
--    strategies published, the persona published, the purpose recheck (marketing or consent purposes refused; renewal_reminder is
--    marketing), the world.
-- 2. D9 waived online (owner 2026-10-21: the test stage opens online, D9 calibration is not a precondition for now): a twin selected by
--    an online registration need not be calibrated - runtime.nexloop_experiment_twin_ok(tenant, registration) is true when no twin is
--    selected, when the stage is online, or when the twin is selectable. start, assign, strategy_for, guardrails_hit, guard and levers
--    (re-created from their latest bodies, only that expression replaced; tests/test_function_copy_guard.py) read it. A start that
--    waived it carries notice calibration_waived in its result and its audit. Screening and shadow keep D9.
-- 3. The inbound allocation trigger (0304) also takes a record that says direction 'inbound' (not only one without direction).
-- Everything else holds: X4 allocation exclusions (erasing, in_same_comparison, ...), X5 guard / budget / reconciliation / refusal and
-- unsubscribe rates, levers only switch off, the decision's strategy frozen at acceptance. The real dispatch switches are read, never
-- written: an online experiment sends only where they are open (real_dispatch_closed otherwise).

create function runtime.nexloop_experiment_twin_ok(p_tenant text,r jsonb) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select r->'twin'='null'::jsonb or r->>'stage'='online'
  or runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer)
$$;
alter function runtime.nexloop_experiment_twin_ok(text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_twin_ok(text,jsonb) from public;

create or replace function control.nexloop_governed_start_experiment(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare x control.nexloop_experiments;e control.nexloop_experiment_events;r jsonb;v_status text;v_ref jsonb;v_persona jsonb;v_waived boolean;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','experiment_id'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'experiment_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' then
  raise exception 'experiment: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'experiment: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||(body->>'experiment_id'),0));
 select * into e from control.nexloop_experiment_events where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (e.experiment_id,e.status) is distinct from (body->>'experiment_id','started') then raise exception 'experiment: request_reused' using errcode='22023';end if;
  return jsonb_build_object('experiment_id',e.experiment_id,'status',runtime.nexloop_experiment_status(p_tenant,e.experiment_id),'started',false);
 end if;
 select * into x from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=body->>'experiment_id';
 if not found then raise exception 'experiment: not_found' using errcode='22023';end if;
 v_status:=runtime.nexloop_experiment_status(p_tenant,x.experiment_id);
 if v_status<>'registered' then raise exception 'experiment: not_registered_state' using errcode='22023';end if;
 r:=x.registration;
 if (r->>'ends_at')::timestamptz<=clock_timestamp() then raise exception 'experiment: ended' using errcode='22023';end if;
 -- D9 (the part that still holds): a selected twin must be published, not retired and calibrated now - except online, where the
 -- owner waived it for the test stage (2026-10-21): the start then carries calibration_waived, in its result and its audit.
 if not runtime.nexloop_experiment_twin_ok(p_tenant,r) then raise exception 'experiment: twin_not_calibrated' using errcode='22023';end if;
 v_waived:=r->>'stage'='online' and r->'twin'<>'null'::jsonb
  and not runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer);
 foreach v_ref in array array[r->'candidate',r->'control'] loop
  if v_ref<>'null'::jsonb then
   if runtime.nexloop_strategy_status(p_tenant,v_ref->>'strategy_id',(v_ref->>'version')::integer) is distinct from 'published' then
    raise exception 'experiment: strategy_not_published' using errcode='22023';end if;
   v_persona:=(select s.definition->'levers'->'persona' from control.nexloop_strategy_versions s where s.tenant_id=p_tenant
    and s.strategy_id=v_ref->>'strategy_id' and s.version=(v_ref->>'version')::integer);
   if v_persona is not null and not runtime.nexloop_strategy_persona_published(p_tenant,v_persona->>'persona_id',(v_persona->>'version')::integer) then
    raise exception 'experiment: persona_not_published' using errcode='22023';end if;
   -- X1 review 2.1: a purpose reclassified as marketing (or needing consent) after publication blocks the start.
   if exists(select 1 from control.nexloop_strategy_versions s,jsonb_object_keys(coalesce(s.definition->'levers'->'outreach_purposes','{}'::jsonb)) k where s.tenant_id=p_tenant and s.strategy_id=v_ref->>'strategy_id' and s.version=(v_ref->>'version')::integer and not runtime.nexloop_strategy_purpose_allowed(k)) then raise exception 'experiment: purpose_not_allowed' using errcode='22023';end if;
  end if;
 end loop;
 if r->>'world'<>'real' and not exists(select 1 from control.nexloop_sim_worlds w where w.world=r->>'world' and w.tenant_id=p_tenant
   and w.status='active' and w.expires_at>clock_timestamp()) then
  raise exception 'experiment: world_not_active' using errcode='22023';end if;
 insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,recorded_by,request_id)
  values(p_tenant,x.experiment_id,'started',p_principal,body->>'request_id');
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment:start:'||x.experiment_id||':'||md5(p_tenant),'nexloop.experiment.started',p_tenant,p_principal,'experiment',p_intent,
   jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest)
    ||case when v_waived then jsonb_build_object('notice','calibration_waived') else '{}'::jsonb end,clock_timestamp());
 return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'status','started','started',true)
  ||case when v_waived then jsonb_build_object('notice','calibration_waived') else '{}'::jsonb end;
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
 if not runtime.nexloop_experiment_twin_ok(p_tenant,r) then
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
 if x.arm='candidate' and v_status='started' and (x.registration->>'ends_at')::timestamptz>clock_timestamp() and runtime.nexloop_strategy_status(p_tenant,x.registration->'candidate'->>'strategy_id',(x.registration->'candidate'->>'version')::integer)='published' and runtime.nexloop_experiment_twin_ok(p_tenant,x.registration) then v_ref:=x.registration->'candidate';
 else v_ref:=x.registration->'control';end if;
 if v_ref<>'null'::jsonb and v_ref=x.registration->'control' and runtime.nexloop_strategy_status(p_tenant,v_ref->>'strategy_id',(v_ref->>'version')::integer) is distinct from 'published' then v_ref:='null'::jsonb;end if;  -- X4 review 4.1
 out:=jsonb_build_object('experiment_id',x.experiment_id,'arm',x.arm,'status',v_status,
  'applied',case when v_ref=x.registration->'candidate' then 'candidate' else 'control' end,
  'strategy',case when v_ref='null'::jsonb then null else (select jsonb_build_object('strategy_id',s.strategy_id,'version',s.version,'levers',s.definition->'levers')
   from control.nexloop_strategy_versions s where s.tenant_id=p_tenant and s.strategy_id=v_ref->>'strategy_id' and s.version=(v_ref->>'version')::integer) end);
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return out;
end $$;

create or replace function runtime.nexloop_experiment_guardrails_hit(p_tenant text,p_experiment text,m jsonb) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);r jsonb;h text[]:='{}';
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.registration into r from control.nexloop_experiments e where e.tenant_id=p_tenant and e.experiment_id=p_experiment;
 if (r->>'ends_at')::timestamptz<=clock_timestamp() then h:=h||'ended'::text;end if;
 if not runtime.nexloop_experiment_twin_ok(p_tenant,r) then h:=h||'twin_withdrawn'::text;end if;
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
 elsif not runtime.nexloop_experiment_twin_ok(p_tenant,r) then v_code:='twin_withdrawn';
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

create or replace function runtime.nexloop_experiment_levers(p_tenant text,p_world text,p_consumer text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);x record;out jsonb;
begin
 if p_world is distinct from 'real' or p_consumer is null then return null;end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.experiment_id,e.registration,o.arm into x from control.nexloop_experiment_allocations o
  join control.nexloop_experiments e on e.tenant_id=o.tenant_id and e.experiment_id=o.experiment_id
  where o.tenant_id=p_tenant and o.consumer_id=p_consumer and o.arm is not null and e.registration->>'world'='real' and e.registration->>'stage'='online'
   and runtime.nexloop_experiment_status(p_tenant,e.experiment_id)='started' and (e.registration->>'ends_at')::timestamptz>clock_timestamp()
  order by o.allocated_at desc limit 1;
 if found and x.arm='candidate' and not runtime.nexloop_consumer_erasing(p_tenant,'real',p_consumer)
  and runtime.nexloop_strategy_status(p_tenant,x.registration->'candidate'->>'strategy_id',(x.registration->'candidate'->>'version')::integer)='published'
  and runtime.nexloop_experiment_twin_ok(p_tenant,x.registration) then
  select jsonb_build_object('experiment_id',x.experiment_id,'strategy_id',s.strategy_id,'version',s.version,'levers',s.definition->'levers') into out
   from control.nexloop_strategy_versions s where s.tenant_id=p_tenant and s.strategy_id=x.registration->'candidate'->>'strategy_id'
    and s.version=(x.registration->'candidate'->>'version')::integer;
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return out;
end $$;

create or replace function runtime.nexloop_experiment_allocate_on_inbound() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_consumer text;v_lock text;v_prior text:=current_setting('eios.tenant_id',true);
begin
 if new.world<>'real' or coalesce(new.record->>'direction','inbound')<>'inbound' then return null;end if;  -- inbound: no direction (0046) or 'inbound'
 if not runtime.nexloop_experiment_online_running(new.tenant_id) then return null;end if;  -- no subtransaction while none runs
 v_lock:=current_setting('lock_timeout');
 begin
  perform set_config('lock_timeout','200ms',true);
  perform set_config('eios.tenant_id',new.tenant_id,true);
  select cv.consumer_id into v_consumer from runtime.nexloop_conversations cv where cv.tenant_id=new.tenant_id and cv.world=new.world and cv.conversation_id=new.conversation_id;
  if v_consumer is not null then perform runtime.nexloop_experiment_strategy_for(new.tenant_id,'real',v_consumer,null);end if;
  perform set_config('lock_timeout',v_lock,true);
 exception when others then
  begin
   perform set_config('eios.tenant_id',new.tenant_id,true);
   insert into runtime.nexloop_experiment_accounting_failures(tenant_id,world,consumption_id,sqlstate) values(new.tenant_id,'real','allocation:inbound',sqlstate);
  exception when others then null;
  end;
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
