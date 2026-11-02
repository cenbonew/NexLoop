-- NX-064 (design docs/implementation/NX-064-effect-timeout-design.md v2, ADR-029c; temporary number, the dispatcher numbers it at
-- batch time): the Agent Host's effect request counters as process samples, and the snapshot block the effect alerts read.
-- * kind 'host' also accepts effect_calls, effect_ambiguous, effect_unavailable (the increase over the window, every operation): the
--   runtime worker reads them from the Host's metrics endpoint with the concurrency counters, so a Host that answers but predates
--   them, or a sampler that stopped, shows as no data.
-- * new kind 'host_op' (service effect_submit, enterprise_submit, effect_find, incentive): calls, ambiguous (the first request was
--   ambiguous: timeout, connection error, 503), unavailable (still ambiguous after the one resend, or any other unavailable end) and
--   the guard histogram h_<i> of every HTTP request's Host-side elapsed time. Written only for an operation with requests.
--   authz.nexloop_record_process_sample is 0323's body with these two kinds' keys added; nothing else.
-- * runtime.nexloop_metrics_snapshot is renamed once and wrapped (world real): effect_requests over the last 24 hours, three items
--   judged apart - effect_submit and effect_find (guard_op samples: calls, the calls at or above the 2 s deadline the guard counts as
--   timeouts, their ratio) and host (the 'host' samples carrying the effect counters). An item without a sample says present false
--   and no_data 1 (never a zero that reads as healthy). enterprise_unavailable: the failed Runs of the tenant whose tool
--   nexloop.enterprise.deliver ended runtime_effect_unavailable (runtime.jobs result, codes only), their count and up to 16 run ids
--   for the owner to check against the intent list: (a) the order was placed but the customer was not told, (b) there is no order.
--   Numbers, codes and run ids only.
alter table runtime.nexloop_process_samples drop constraint nexloop_process_samples_kind_check;
alter table runtime.nexloop_process_samples add constraint nexloop_process_samples_kind_check check(kind in ('guard','host','pool','guard_op','relay_step','clock','api','host_op'));
create or replace function authz.nexloop_record_process_sample(p_kind text,p_service text,p_instance text,p_window_start timestamptz,p_window_seconds integer,p_sample jsonb)
 returns void language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare allowed text[];k text;
begin
 allowed:=case p_kind
  when 'guard' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'guard_op' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'relay_step' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'host' then array['active_runs','waiting','max_active_runs','admission_timeouts','busy_refusals','runs_started','available','effect_calls','effect_ambiguous','effect_unavailable']
  when 'host_op' then array['calls','ambiguous','unavailable']
  when 'pool' then array['pool_size','pool_available','requests_waiting','requests_num','requests_errors','requests_wait_ms','usage_ms','backend_busy','max_size']
  when 'clock' then array['skew_ms','skew_abs_ms','round_trip_ms']
  when 'api' then array['calls','errors','slow','p50_ms','p95_ms','max_ms']
  end;
 if allowed is null or authz.nexloop_app_role_unscoped() not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler')
  or jsonb_typeof(p_sample) is distinct from 'object' or p_window_seconds not between 1 and 3600
  or p_window_start>clock_timestamp()+interval '1 minute' or p_window_start<clock_timestamp()-interval '1 day'
  or (p_kind='guard_op' and p_service not in ('start','resume','inspect','cancel','model','model_result','tool','effect_submit','effect_find','outcome','incentive','other'))
  or (p_kind='relay_step' and p_service not in ('backlog','claim','recover','lookup','goal_create','step_create','enterprise_step_create','assignment_create',
   'vault_prepare','source_proofs','issue','effect_bind','context_prepare','message_bind','own_route','route_authorize','runtime_accept','ack','release',
   'handoff','total','authenticate'))
  or (p_kind='api' and p_service not in ('read','write'))
  or (p_kind='host_op' and p_service not in ('effect_submit','enterprise_submit','effect_find','incentive')) then
  raise exception 'process sample invalid' using errcode='22023';end if;
 for k in select jsonb_object_keys(p_sample) loop
  if not (k=any(allowed) or (p_kind in ('guard','guard_op','relay_step','api','host_op') and k ~ '^h_(0|[1-9][0-9]?|10[0-9]|11[0-6])$' and jsonb_typeof(p_sample->k)='number'))
   or jsonb_typeof(p_sample->k) not in ('number','boolean') then raise exception 'process sample invalid' using errcode='22023';end if;
 end loop;
 -- Review 1.5: a simulation role's samples never enter the real samples (snapshot, rollups, alerts read those only).
 if session_user::text in ('nexloop_sim_worker','nexloop_sim_guard','nexloop_sim_api') then
  insert into runtime.nexloop_sim_process_samples(role,kind,service,instance,window_start,window_seconds,sample)
   values(session_user::text,p_kind,p_service,p_instance,p_window_start,p_window_seconds,p_sample);
  delete from runtime.nexloop_sim_process_samples where recorded_at<clock_timestamp()-interval '7 days';  -- as the real minute samples (D5)
  return;
 end if;
 insert into runtime.nexloop_process_samples(kind,service,instance,window_start,window_seconds,sample) values(p_kind,p_service,p_instance,p_window_start,p_window_seconds,p_sample);
end $$;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_effect_requests;
revoke all on function runtime.nexloop_metrics_snapshot_before_effect_requests(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_effect_requests(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 since timestamptz:=clock_timestamp()-interval '24 hours';o jsonb;ent jsonb;
begin
 if p_world is distinct from 'real' then return v;end if;
 with g as (
   select x.service,count(s.sample_id) n,coalesce(sum((s.sample->>'calls')::bigint),0) calls,coalesce(sum(coalesce((s.sample->>'timeouts')::bigint,0)),0) over_2s
     from (values ('effect_submit'),('effect_find')) x(service)
     left join runtime.nexloop_process_samples s on s.kind='guard_op' and s.service=x.service and s.window_start>since
    group by x.service),
  h as (
   select count(*) n,coalesce(sum((s.sample->>'effect_calls')::bigint),0) calls,coalesce(sum((s.sample->>'effect_ambiguous')::bigint),0) ambiguous,
          coalesce(sum((s.sample->>'effect_unavailable')::bigint),0) unavailable
     from runtime.nexloop_process_samples s where s.kind='host' and s.window_start>since and s.sample ? 'effect_calls'),
  ops as (
   select s.service,jsonb_build_object('calls',sum((s.sample->>'calls')::bigint),'ambiguous',sum((s.sample->>'ambiguous')::bigint),
          'unavailable',sum((s.sample->>'unavailable')::bigint)) value
     from runtime.nexloop_process_samples s where s.kind='host_op' and s.window_start>since group by s.service)
 select jsonb_object_agg(g.service,case when g.n=0 then jsonb_build_object('present',false,'no_data',1)
          else jsonb_build_object('present',true,'no_data',0,'samples',g.n,'calls',g.calls,'over_2s',g.over_2s)
               ||case when g.calls>0 then jsonb_build_object('over_2s_ratio',round(g.over_2s::numeric/g.calls,4)) else '{}'::jsonb end end)
        ||jsonb_build_object('host',case when h.n=0 then jsonb_build_object('present',false,'no_data',1)
          else jsonb_build_object('present',true,'no_data',0,'samples',h.n,'calls',h.calls,'ambiguous',h.ambiguous,'unavailable',h.unavailable,
               'operations',coalesce((select jsonb_object_agg(ops.service,ops.value) from ops),'{}'::jsonb)) end)
   into o from g,h group by h.n,h.calls,h.ambiguous,h.unavailable;
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object('runs_24h',count(*),'run_ids',coalesce((array_to_json((array_agg(j.result->>'run_id' order by j.updated_at desc,j.job_id))[1:16]))::jsonb,'[]'::jsonb))
   into ent from runtime.jobs j
  where j.tenant_id=p_tenant and j.world=p_world and j.status='failed' and j.updated_at>since
    and j.result->>'failure_tool'='nexloop.enterprise.deliver' and j.result->>'failure_detail'='runtime_effect_unavailable';
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('effect_requests',o||jsonb_build_object('enterprise_unavailable',ent));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
