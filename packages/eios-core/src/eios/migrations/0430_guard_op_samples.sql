-- AT-060 guard samples per operation (temporary number 0430; dispatcher 2026-10-13). The runtime guard keeps its overall
-- per-process sample (kind 'guard', unchanged: every existing reader, alert and the report's guard_p95_ms_max stay as they
-- are) and adds one sample per operation and window under the new kind 'guard_op'; the service is the operation name
-- (start, resume, inspect, cancel, model, model_result, tool, effect_submit, effect_find, outcome, incentive, other).
-- Numbers only: counts, latency figures and a log-spaced latency histogram (h_<i>, i in 0..116, ratio 1.1; owner 2026-10-13
-- 5A, ROADMAP §7) on both kinds, so that a window's p95 over all calls can be merged across processes within +-5%.
-- No authorization semantics change; the guard only labels and counts its calls.
alter table runtime.nexloop_process_samples drop constraint nexloop_process_samples_kind_check;
alter table runtime.nexloop_process_samples add constraint nexloop_process_samples_kind_check check(kind in ('guard','host','pool','guard_op'));
create or replace function authz.nexloop_record_process_sample(p_kind text,p_service text,p_instance text,p_window_start timestamptz,p_window_seconds integer,p_sample jsonb)
 returns void language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare allowed text[];k text;
begin
 allowed:=case p_kind
  when 'guard' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'guard_op' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'host' then array['active_runs','waiting','max_active_runs','admission_timeouts','busy_refusals','runs_started','available']
  when 'pool' then array['pool_size','pool_available','requests_waiting','requests_num','requests_errors','requests_wait_ms','usage_ms','backend_busy','max_size']
  end;
 if allowed is null or session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler')
  or jsonb_typeof(p_sample) is distinct from 'object' or p_window_seconds not between 1 and 3600
  or p_window_start>clock_timestamp()+interval '1 minute' or p_window_start<clock_timestamp()-interval '1 day'
  or (p_kind='guard_op' and p_service not in ('start','resume','inspect','cancel','model','model_result','tool','effect_submit','effect_find','outcome','incentive','other')) then
  raise exception 'process sample invalid' using errcode='22023';end if;
 for k in select jsonb_object_keys(p_sample) loop
  if not (k=any(allowed) or (p_kind in ('guard','guard_op') and k ~ '^h_(0|[1-9][0-9]?|10[0-9]|11[0-6])$' and jsonb_typeof(p_sample->k)='number'))
   or jsonb_typeof(p_sample->k) not in ('number','boolean') then raise exception 'process sample invalid' using errcode='22023';end if;
 end loop;
 insert into runtime.nexloop_process_samples(kind,service,instance,window_start,window_seconds,sample) values(p_kind,p_service,p_instance,p_window_start,p_window_seconds,p_sample);
end $$;
