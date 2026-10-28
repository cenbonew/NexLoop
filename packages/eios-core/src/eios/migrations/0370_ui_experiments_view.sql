-- Workbench experiment space, slice 1 (owner 2026-10-28): the owner's read-only experiments view. Temporary number; the dispatcher renumbers.
-- authz.nexloop_workbench_observe_read gains the verb 'experiments' (every other verb unchanged, as 0356 added 'members'), for the owner only.
-- runtime.nexloop_workbench_experiments_view(tenant): the tenant's latest 200 experiments, each with its frozen registration's fields (world
-- and its kind, stage, hypothesis, candidate and control strategy, primary metric, guardrails, sample size, budget, end, twin), the
-- registration digest, its status and the times and reason of its latest start and stop (and whether the guard stopped it), the spend so far
-- (runtime.nexloop_experiment_metrics: numbers only), the allocation counts per arm and of exclusions, the latest frozen report's digest and
-- its conclusion, the budget_drill flag with the notice (runtime.nexloop_experiment_notice), and the twin's status and calibration now.
-- Counts only: no Consumer id, cluster key or allocation row leaves this function; an erased Consumer's allocation is already a pseudonym
-- (0295) and is only counted. Nothing here writes.
set local timezone = 'UTC';

create function runtime.nexloop_workbench_experiments_view(p_tenant text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(x.v order by x.registered_at desc,x.experiment_id),'[]'::jsonb) into v from (
  select e.experiment_id,e.registered_at,jsonb_build_object(
   'experiment_id',e.experiment_id,'world',r->>'world','world_kind',runtime.nexloop_world_kind(r->>'world'),'stage',r->>'stage',
   'hypothesis',r->>'hypothesis','candidate',r->'candidate','control',r->'control','primary_metric',r->'primary_metric',
   'guardrails',coalesce(r->'guardrails','{}'::jsonb),'sample_size',r->'sample_size','budget',r->'budget','ends_at',r->>'ends_at',
   'registration_digest',e.registration_digest,'registered_at',runtime.nexloop_commercial_ts(e.registered_at),
   'status',runtime.nexloop_experiment_status(p_tenant,e.experiment_id),
   'started_at',runtime.nexloop_commercial_ts(st.recorded_at),'stopped_at',runtime.nexloop_commercial_ts(sp.recorded_at),
   'stop_reason',sp.reason,'stopped_by_guard',coalesce(sp.reason like 'guardrail: %',false),
   'spent',(select jsonb_build_object('model_cny',m->'model_cny','incentive_cny',m->'incentive_cny')
     from runtime.nexloop_experiment_metrics(p_tenant,e.experiment_id) m),
   'allocation',(select jsonb_build_object('candidate',count(*) filter (where a.arm='candidate'),'control',count(*) filter (where a.arm='control'),
     'excluded',count(*) filter (where a.arm is null)) from control.nexloop_experiment_allocations a
     where a.tenant_id=p_tenant and a.experiment_id=e.experiment_id),
   'report',(select jsonb_build_object('as_of',runtime.nexloop_commercial_ts(p.as_of),'created_at',runtime.nexloop_commercial_ts(p.created_at),
     'report_digest',p.report_digest,'conclusion',p.report->'conclusion','reason',p.report->'reason',
     'candidate_matured',p.candidate_matured,'control_matured',p.control_matured)
     from control.nexloop_experiment_reports p where p.tenant_id=p_tenant and p.experiment_id=e.experiment_id order by p.created_at desc,p.report_id limit 1),
   'budget_drill',coalesce(r->'budget_drill'='true'::jsonb,false),'notice',runtime.nexloop_experiment_notice(r),
   'twin',case when jsonb_typeof(r->'twin')='object' then jsonb_build_object('twin_id',r->'twin'->>'twin_id','version',r->'twin'->'version',
     'status',runtime.nexloop_consumer_twin_status(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer),
     'calibration',runtime.nexloop_consumer_twin_calibration(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer)) end) v
  from (select x.*,x.registration r from control.nexloop_experiments x where x.tenant_id=p_tenant order by x.registered_at desc,x.experiment_id limit 200) e
  left join lateral (select s.recorded_at from control.nexloop_experiment_events s where s.tenant_id=p_tenant and s.experiment_id=e.experiment_id
    and s.status='started' order by s.event_id desc limit 1) st on true
  left join lateral (select s.recorded_at,s.reason from control.nexloop_experiment_events s where s.tenant_id=p_tenant and s.experiment_id=e.experiment_id
    and s.status='stopped' order by s.event_id desc limit 1) sp on true) x;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return jsonb_build_object('experiments',v);
end $$;
alter function runtime.nexloop_workbench_experiments_view(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_workbench_experiments_view(text) from public;

alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_experiments;
revoke all on function authz.nexloop_workbench_observe_read_before_experiments(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'experiments' then
  return authz.nexloop_workbench_observe_read_before_experiments(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as 0356 for members).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 -- The owner only (registering, starting and stopping experiments are the owner's, NX-043 D3).
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or w->>'role' is distinct from 'owner'
   or not (cfg.roles->(w->>'role') ? v_action) then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 return runtime.nexloop_workbench_experiments_view(v_tenant);
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
