-- EIOS gap E4 (NX-061): API request metrics. The API process records, per minute and request class, one process sample of kind 'api'
-- (service read = GET/HEAD, write = every other method): calls, errors (5xx), slow (>= 500 ms, the PRD 8 read target), p50/p95/max and
-- the log-spaced histogram (h_<i>, as guard_op 0186), numbers only. The function body is 0277's with the api kind added; nothing else
-- changes. The snapshot is wrapped once more (the 0277/0318 pattern) and gains the 'api' block: the 5-minute process section with,
-- per class, the p50/p95 merged from every API process's histogram (scripts/load/report.py histogram_quantile, same caliber), the
-- error and slow rates. Per-route detail stays in the API process (eios.api.metrics, an optional loopback export).
alter table runtime.nexloop_process_samples drop constraint nexloop_process_samples_kind_check;
alter table runtime.nexloop_process_samples add constraint nexloop_process_samples_kind_check check(kind in ('guard','host','pool','guard_op','relay_step','clock','api'));
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
  when 'api' then array['calls','errors','slow','p50_ms','p95_ms','max_ms']
  end;
 if allowed is null or authz.nexloop_app_role_unscoped() not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler')
  or jsonb_typeof(p_sample) is distinct from 'object' or p_window_seconds not between 1 and 3600
  or p_window_start>clock_timestamp()+interval '1 minute' or p_window_start<clock_timestamp()-interval '1 day'
  or (p_kind='guard_op' and p_service not in ('start','resume','inspect','cancel','model','model_result','tool','effect_submit','effect_find','outcome','incentive','other'))
  or (p_kind='relay_step' and p_service not in ('backlog','claim','recover','lookup','goal_create','step_create','enterprise_step_create','assignment_create',
   'vault_prepare','source_proofs','issue','effect_bind','context_prepare','message_bind','own_route','route_authorize','runtime_accept','ack','release',
   'handoff','total','authenticate'))
  or (p_kind='api' and p_service not in ('read','write')) then
  raise exception 'process sample invalid' using errcode='22023';end if;
 for k in select jsonb_object_keys(p_sample) loop
  if not (k=any(allowed) or (p_kind in ('guard','guard_op','relay_step','api') and k ~ '^h_(0|[1-9][0-9]?|10[0-9]|11[0-6])$' and jsonb_typeof(p_sample->k)='number'))
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

-- The quantile of a merged h_<i> histogram ({h_<i>: count}): the rank ceil(q*n) as its bucket's geometric midpoint (bucket 0: 1 ms;
-- the last, open bucket 116: its lower bound), exactly scripts/load/report.py histogram_quantile. Null when empty.
create function runtime.nexloop_histogram_quantile(p_counts jsonb,p_q numeric) returns numeric
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 with b as (select substr(key,3)::integer i,value::numeric n from jsonb_each_text(coalesce(p_counts,'{}'::jsonb)) where key ~ '^h_[0-9]{1,3}$'),
  t as (select sum(n) total from b),
  r as (select i,sum(n) over (order by i) running from b)
 select case when i=0 then 1.0 when i>=116 then round(power(1.1::numeric,115),1) else round(power(1.1::numeric,i-0.5),1) end
  from r,t where t.total>0 and r.running>=greatest(1,ceil(p_q*t.total)) order by i limit 1 $$;
alter function runtime.nexloop_histogram_quantile(jsonb,numeric) owner to nexloop_owner;
revoke all on function runtime.nexloop_histogram_quantile(jsonb,numeric) from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_api;
revoke all on function runtime.nexloop_metrics_snapshot_before_api(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_api(p_tenant,p_world);api jsonb:=runtime.nexloop_process_section('api');
begin
 if api->>'status'='ok' then
  api:=api||jsonb_build_object('classes',(select jsonb_object_agg(c.key,jsonb_build_object(
    'calls',coalesce((c.value->'sum'->>'calls')::numeric,0),
    'p50_ms',runtime.nexloop_histogram_quantile(c.value->'sum',0.5),
    'p95_ms',runtime.nexloop_histogram_quantile(c.value->'sum',0.95),
    'error_rate',case when coalesce((c.value->'sum'->>'calls')::numeric,0)=0 then 0 else round(coalesce((c.value->'sum'->>'errors')::numeric,0)/(c.value->'sum'->>'calls')::numeric,4) end,
    'slow_rate',case when coalesce((c.value->'sum'->>'calls')::numeric,0)=0 then 0 else round(coalesce((c.value->'sum'->>'slow')::numeric,0)/(c.value->'sum'->>'calls')::numeric,4) end))
   from jsonb_each(api->'by_service') c));
 end if;
 return s||jsonb_build_object('api',api);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
