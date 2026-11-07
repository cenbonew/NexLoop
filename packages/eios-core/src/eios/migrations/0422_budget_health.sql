-- Budget health (dispatcher 2026-11-07, after the stage model budget's period ended on 11-05 16:00Z unnoticed for two days:
-- budget_near_limit reads the consumed ratio of the period and never sees an end, refusal_budget_exhausted needs a live refusal).
-- Temporary number; the dispatcher renumbers. runtime.nexloop_metrics_snapshot is renamed once and wrapped with the block
-- budget_health (numbers, so the evaluator can compare them; the 7-day caliber of relay_readiness.dependencies_expiring_7d):
-- * kinds: one entry per required kind (model, evolution) and per other configured kind: configured (0/1), period_end,
--   expires_in_seconds (negative once passed), expired (0/1: now outside [period_start, period_end)), ratio (consumed / limit of
--   the period, null when the limit is 0 or the kind is unconfigured);
-- * expiring_7d: configured kinds inside their period that end within 7 days; expired: configured kinds outside their period (a
--   budget not yet started refuses as one that ended, NXB01 'budget period closed'); unconfigured: required kinds without a
--   budget row - never a silent pass (an unconfigured model budget refuses every Run, NXB03); exhausted: kinds whose period
--   consumption reached the limit; exhausted_refusals_1h: NXB01 dispatch refusals in the last hour (the 5-minute
--   refusals.last_5m window opens and closes with sparse traffic).
-- Read only; nothing else in the snapshot changes.
set local timezone = 'UTC';

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_budget_health;
revoke all on function runtime.nexloop_metrics_snapshot_before_budget_health(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_budget_health(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 now_at timestamptz:=clock_timestamp();kinds jsonb;h jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with required(kind) as (values ('model'),('evolution')),
 named as (select kind from required union select b.budget_kind from control.nexloop_budget_limits b where b.tenant_id=p_tenant and b.world=p_world),
 k as (select n.kind,n.kind in ('model','evolution') is_required,b.period_start,b.period_end,b.limit_amount,
   (select coalesce(sum(u.amount),0) from control.nexloop_budget_consumption u where u.tenant_id=b.tenant_id and u.world=b.world
     and u.budget_kind=b.budget_kind and u.recorded_at>=b.period_start and u.recorded_at<b.period_end) consumed
  from named n left join control.nexloop_budget_limits b on b.tenant_id=p_tenant and b.world=p_world and b.budget_kind=n.kind)
 select jsonb_object_agg(kind,jsonb_build_object('configured',case when period_end is null then 0 else 1 end,'period_end',period_end,
   'expires_in_seconds',case when period_end is null then null else floor(extract(epoch from period_end-now_at))::bigint end,
   'expired',case when period_end is not null and (now_at<period_start or now_at>=period_end) then 1 else 0 end,
   'ratio',case when period_end is null or limit_amount=0 then null else round(consumed/limit_amount,4) end)),
  jsonb_build_object(
   'expiring_7d',count(*) filter (where period_end is not null and now_at>=period_start and now_at<period_end and period_end<=now_at+interval '7 days'),
   'expired',count(*) filter (where period_end is not null and (now_at<period_start or now_at>=period_end)),
   'unconfigured',count(*) filter (where period_end is null and is_required),
   'exhausted',count(*) filter (where period_end is not null and limit_amount>0 and consumed>=limit_amount))
 into kinds,h from k;
 h:=h||jsonb_build_object('kinds',kinds,'required',jsonb_build_array('model','evolution'),
  'exhausted_refusals_1h',(select count(*) from runtime.nexloop_dispatch_refusals r where r.tenant_id=p_tenant and r.world=p_world
   and r.code='NXB01' and r.recorded_at>now_at-interval '1 hour'));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('budget_health',h);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
