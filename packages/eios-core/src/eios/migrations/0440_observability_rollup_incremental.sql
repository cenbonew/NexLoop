-- Alert evaluator statement timeout (dispatcher 2026-10-18; temporary number, renumbered at merge). authz.nexloop_alert_evaluate
-- calls runtime.nexloop_observability_maintain() on every tick (2 s). The 0133 body re-rolled every hour of the 7-day minute
-- samples on each call, with correlated subqueries per (kind,service,hour) that filtered on date_trunc('hour',window_start), which
-- no index serves: cost grew with groups x samples. guard_op (0186) and relay_step (0188, 22 steps, up to 117 histogram keys
-- per sample) multiplied both from 2026-10-13, and the call crossed the domain worker's 10 s statement_timeout.
-- This body rolls only hours from the newest stored rollup minus one hour (late samples of the previous hour are folded in; a
-- gap left by failed ticks is caught up in one pass), aggregates every key in a single grouped pass over a window_start range,
-- and purges by an indexed recorded_at. Rollup contents and the 7/90-day retention are unchanged.
create index nexloop_process_samples_window on runtime.nexloop_process_samples(window_start);
create index nexloop_process_samples_recorded on runtime.nexloop_process_samples(recorded_at);
create or replace function runtime.nexloop_observability_maintain() returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare v_rolled integer;v_minutes integer;v_hours integer;v_from timestamptz;v_to timestamptz:=date_trunc('hour',clock_timestamp());
begin
 select coalesce(max(hour)-interval '1 hour','-infinity'::timestamptz) into v_from from runtime.nexloop_process_sample_hours;
 with s as (select kind,service,date_trunc('hour',window_start) h,sample from runtime.nexloop_process_samples where window_start>=v_from and window_start<v_to),
  c as (select kind,service,h,count(*)::integer n from s group by kind,service,h),
  kv as (select kind,service,h,k,max((sample->>k)::numeric) mx,sum((sample->>k)::numeric) sm from s,jsonb_object_keys(s.sample) k
   where jsonb_typeof(s.sample->k)='number' group by kind,service,h,k),
  a as (select kind,service,h,jsonb_object_agg(k,mx) mx,jsonb_object_agg(k,sm) sm from kv group by kind,service,h)
 insert into runtime.nexloop_process_sample_hours(kind,service,hour,samples,summary)
 select c.kind,c.service,c.h,c.n,jsonb_build_object('max',coalesce(a.mx,'{}'::jsonb),'sum',coalesce(a.sm,'{}'::jsonb))
 from c left join a on a.kind=c.kind and a.service=c.service and a.h=c.h
 on conflict (kind,service,hour) do update set samples=excluded.samples,summary=excluded.summary;
 get diagnostics v_rolled=row_count;
 delete from runtime.nexloop_process_samples where recorded_at<clock_timestamp()-interval '7 days';get diagnostics v_minutes=row_count;
 delete from runtime.nexloop_process_sample_hours where hour<clock_timestamp()-interval '90 days';get diagnostics v_hours=row_count;
 -- D6 per-Run tool timings: operational counters of finished Runs, kept as long as the minute samples (D5).
 delete from runtime.nexloop_run_tool_timings where updated_at<clock_timestamp()-interval '7 days';
 return jsonb_build_object('rolled',v_rolled,'purged_minutes',v_minutes,'purged_hours',v_hours);
end $$;
