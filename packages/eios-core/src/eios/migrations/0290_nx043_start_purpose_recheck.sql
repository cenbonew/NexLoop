-- NX-043 X1 review fix (L6 review 2.1, dispatcher 2026-10-21): start rechecks every outreach purpose of the candidate and control
-- strategies against the current purpose list, as it does the persona: a purpose reclassified as marketing (or needing consent) after
-- the strategy was published blocks the start (purpose_not_allowed). control.nexloop_governed_start_experiment is re-created from
-- its 0285 body with only that check added (registered in tests/test_function_copy_guard.py). The migration number lives only in the
-- file name and the catalog.

create or replace function control.nexloop_governed_start_experiment(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare x control.nexloop_experiments;e control.nexloop_experiment_events;r jsonb;v_status text;v_ref jsonb;v_persona jsonb;
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
 -- X1 has no allocation and no stopping rules yet: an online experiment cannot start until X4 and X5.
 if r->>'stage'='online' then raise exception 'experiment: online_not_available' using errcode='22023';end if;
 -- D9 (the part that still holds): a selected twin must be published, not retired and calibrated now.
 if r->'twin'<>'null'::jsonb and not runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer) then
  raise exception 'experiment: twin_not_calibrated' using errcode='22023';end if;
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
   jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest),clock_timestamp());
 return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'status','started','started',true);
end $$;
