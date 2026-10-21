-- Clock skew between APP_HOST and the database as a metric (L6; L2's post-deploy check runs once per deployment, the skew drifts).
-- The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. Process sample kind 'clock' (numbers only): the alert evaluator (APP_HOST) estimates, before each evaluation, the database clock
--    minus its own clock at the midpoint of the shortest of a few round trips (skew_ms, skew_abs_ms, round_trip_ms).
-- 2. The metrics snapshot gains clock = the process section of kind 'clock' (by_service.<service>.max.skew_abs_ms ...); alert rules
--    v10 alert above 2 s. Signed commands carry 5 s of tolerance (signing TTL 20 s under the SQL 25 s bound): 2 s is the early warning.
-- The alert evaluation stays real world only (0131). Simulation roles' samples stay apart (0268).
alter table runtime.nexloop_process_samples drop constraint nexloop_process_samples_kind_check;
alter table runtime.nexloop_process_samples add constraint nexloop_process_samples_kind_check check(kind in ('guard','host','pool','guard_op','relay_step','clock'));

-- authz.nexloop_record_process_sample (latest body 0268): the kind 'clock' and its three numbers; the body is otherwise unchanged.
create or replace function authz.nexloop_record_process_sample(p_kind text,p_service text,p_instance text,p_window_start timestamptz,p_window_seconds integer,p_sample jsonb)
 returns void language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare allowed text[];k text;
begin
 allowed:=case p_kind
  when 'guard' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'guard_op' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'relay_step' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'host' then array['active_runs','waiting','max_active_runs','admission_timeouts','busy_refusals','runs_started','available']
  when 'pool' then array['pool_size','pool_available','requests_waiting','requests_num','requests_errors','requests_wait_ms','usage_ms','backend_busy','max_size']
  when 'clock' then array['skew_ms','skew_abs_ms','round_trip_ms']
  end;
 if allowed is null or authz.nexloop_app_role_unscoped() not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler')
  or jsonb_typeof(p_sample) is distinct from 'object' or p_window_seconds not between 1 and 3600
  or p_window_start>clock_timestamp()+interval '1 minute' or p_window_start<clock_timestamp()-interval '1 day'
  or (p_kind='guard_op' and p_service not in ('start','resume','inspect','cancel','model','model_result','tool','effect_submit','effect_find','outcome','incentive','other'))
  or (p_kind='relay_step' and p_service not in ('backlog','claim','recover','lookup','goal_create','step_create','enterprise_step_create','assignment_create',
   'vault_prepare','source_proofs','issue','effect_bind','context_prepare','message_bind','own_route','route_authorize','runtime_accept','ack','release',
   'handoff','total','authenticate')) then
  raise exception 'process sample invalid' using errcode='22023';end if;
 for k in select jsonb_object_keys(p_sample) loop
  if not (k=any(allowed) or (p_kind in ('guard','guard_op','relay_step') and k ~ '^h_(0|[1-9][0-9]?|10[0-9]|11[0-6])$' and jsonb_typeof(p_sample->k)='number'))
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

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_clock_skew;
revoke all on function runtime.nexloop_metrics_snapshot_before_clock_skew(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_clock_skew(p_tenant,p_world);
begin
 return s||jsonb_build_object('clock',runtime.nexloop_process_section('clock'));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
