-- NX-043 / NX-041 AT-046 (L6 AT-046 batch-12 fix review, dispatcher 2026-10-22): the acceptance's synthetic consumer twin (at046_stage.py
-- prepare publishes it as at046-synthetic-<name>; cleanup retires it) must not back a real experiment. The migration number is
-- temporary; it lives only in the file name and the catalog. Published migrations are not edited: both bodies are redefined here from
-- their latest definitions, each with one change (registered in tests/test_function_copy_guard.py).
-- 1. runtime.nexloop_experiment_twin_ok (0309's body): the online stage still waives the twin's calibration (owner 2026-10-21), but no
--    longer its withdrawal - a selected twin must not be retired (runtime.nexloop_consumer_twin_unretired: its latest status is not
--    'retired'; an unknown twin fails; never null).
--    Without this a retired twin passed every online gate (start, assign, strategy_for, guardrails), so retiring the synthetic twin
--    did not keep it out of an online experiment.
-- 2. control.nexloop_governed_register_experiment (0299's body): an online or shadow registration naming a twin with the synthetic
--    prefix at046-synthetic- is refused (twin_synthetic), retired or not - its evidence would be the acceptance's synthetic data.

-- The withdrawal check online needs: true only for a known version whose latest status is not retired - never null (a missing tenant
-- context would otherwise read no row under row security and make the gate's `if not ...` pass), in the tenant's own context.
create function runtime.nexloop_consumer_twin_unretired(p_tenant text,p_twin text,p_version integer) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v text;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v:=runtime.nexloop_consumer_twin_status(p_tenant,p_twin,p_version);
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return coalesce(v<>'retired',false);
end $$;
alter function runtime.nexloop_consumer_twin_unretired(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_twin_unretired(text,text,integer) from public;

create or replace function runtime.nexloop_experiment_twin_ok(p_tenant text,r jsonb) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select r->'twin'='null'::jsonb or (r->>'stage'='online' and runtime.nexloop_consumer_twin_unretired(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer))
  or runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer)
$$;

create or replace function control.nexloop_governed_register_experiment(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r jsonb:=body->'registration';x control.nexloop_experiments;v_code text;v_frozen jsonb;v_digest text;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','registration'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200 then
  raise exception 'experiment: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'experiment: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment-request:'||p_tenant||':'||(body->>'request_id'),0));
 select * into x from control.nexloop_experiments where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  -- A retry replays the frozen registration; the same request with another registration is refused.
  begin v_frozen:=runtime.nexloop_experiment_registration_frozen(r);exception when others then v_frozen:=null;end;
  if v_frozen is distinct from x.registration then raise exception 'experiment: request_reused' using errcode='22023';end if;
  return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'registration',x.registration,
   'status',runtime.nexloop_experiment_status(p_tenant,x.experiment_id),'registered',false,'notice',runtime.nexloop_experiment_notice(x.registration));
 end if;
 -- L6 AT-046 batch-12 fix review: the acceptance's synthetic twin never backs a real experiment's online or shadow registration.
 if r->>'stage' in ('online','shadow') and coalesce(r->'twin'->>'twin_id','') like 'at046-synthetic-%' then
  raise exception 'experiment: twin_synthetic' using errcode='22023';end if;
 v_code:=runtime.nexloop_experiment_registration_problem(p_tenant,r);
 if v_code is not null then raise exception 'experiment: %',v_code using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||(r->>'experiment_id'),0));
 -- D3: registered once; a changed experiment is a new experiment.
 if exists(select 1 from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=r->>'experiment_id') then
  raise exception 'experiment: experiment_exists' using errcode='22023';end if;
 v_frozen:=runtime.nexloop_experiment_registration_frozen(r);
 v_digest:=encode(sha256(convert_to(v_frozen::text,'UTF8')),'hex');
 insert into control.nexloop_experiments(tenant_id,experiment_id,registration,registration_digest,request_id,registered_by)
  values(p_tenant,r->>'experiment_id',v_frozen,v_digest,body->>'request_id',p_principal) returning * into x;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment:register:'||x.experiment_id||':'||md5(p_tenant),'nexloop.experiment.registered',p_tenant,p_principal,'experiment',p_intent,
   jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'stage',v_frozen->>'stage'),clock_timestamp());
 return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'registration',x.registration,
  'status','registered','registered',true,'notice',runtime.nexloop_experiment_notice(x.registration));
end $$;
