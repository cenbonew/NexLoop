-- Timers the alert rules watch but no host reports (dispatcher 2026-11-08; NX-055 E3 found two on stage): a timer_stale_<name>
-- rule reads host_health.timer_success_age_seconds at its selector, and the evaluator (0131) yields nothing for an absent key, so
-- a timer that was never installed - and is therefore missing from its host's probe configuration - raised no alert at all.
-- Temporary number; the dispatcher renumbers. runtime.nexloop_metrics_snapshot is renamed once and wrapped: the host_health block
-- gains timers_not_reported (a number, the evaluator compares numbers) and timers_not_reported_names - the selectors of the
-- tenant's applied rules (latest control.nexloop_alert_rules version) whose rule_id starts with timer_stale_ and whose metric is
-- host_health.timer_success_age_seconds, with no number at that key. A tenant without applied rules watches nothing (0). Read only;
-- nothing else changes, and the evaluator is not changed.
set local timezone = 'UTC';

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_timers_not_reported;
revoke all on function runtime.nexloop_metrics_snapshot_before_timers_not_reported(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_timers_not_reported(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 hh jsonb:=v->'host_health';ages jsonb;names jsonb;
begin
 if jsonb_typeof(hh) is distinct from 'object' then return v;end if;
 ages:=coalesce(hh->'timer_success_age_seconds','{}'::jsonb);
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(s order by s),'[]'::jsonb) into names from (
  select distinct r->>'selector' s
  from (select rules from control.nexloop_alert_rules where tenant_id=p_tenant order by version desc limit 1) a
  cross join lateral jsonb_array_elements(a.rules) r
  where r->>'rule_id' like 'timer\_stale\_%' and r->>'metric'='host_health.timer_success_age_seconds'
   and coalesce(r->>'selector','') not in ('','*') and jsonb_typeof(ages->(r->>'selector')) is distinct from 'number') x;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('host_health',hh||jsonb_build_object('timers_not_reported',jsonb_array_length(names),'timers_not_reported_names',names));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
