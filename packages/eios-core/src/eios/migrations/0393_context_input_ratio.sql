-- S86 step 1 (owner 2026-11-01; temporary number, the dispatcher numbers it at batch time): read-only monitoring of the provider's
-- input against the Context budget. The D1 calibration (deepseek-flash, n=25) showed provider input ~ 1.92 x budget_report.used +
-- 3622: the pack estimate counts item contents only, so a pack filled to its budget would be sent at ~27k provider tokens against
-- an input_token_budget of 16000. Nothing here changes a budget or an estimate.
--
-- runtime.nexloop_metrics_snapshot is renamed once and wrapped (world real): context_input_ratio, over the last 24 hours, of every
-- model request with a provider usage, the ratio provider input (usage input + cache_read + cache_write, as the Host records it) /
-- the strategy's input_token_budget (the pack's budget_report, bound to the strategy definition by 0092), its p95 and max overall
-- and per model and Run kind (fallback: a reply fallback issuance; message: a message route; plan: a plan Run, outreach included;
-- other). Numbers and codes only: no request, prompt or pack body is read.
-- Not silent: configured says whether the tenant has a published Context strategy, present whether any request was measured;
-- without a sample p95/max/groups are absent (never a 0 that would read as healthy).
-- M1 (L6): a succeeded model request of the 24 hours whose usage is missing or has no numeric input is counted in
-- requests_without_usage_24h (always a number), so "requests but no usage" never looks like "no request".
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_context_input_ratio;
revoke all on function runtime.nexloop_metrics_snapshot_before_context_input_ratio(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_context_input_ratio(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 o jsonb;
begin
 if p_world is distinct from 'real' then return v;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 with r as (
   select q.model_id model,
          case when exists(select 1 from authz.nexloop_message_fallback_issuances f where f.run_id=q.run_id) then 'fallback'
               when exists(select 1 from runtime.nexloop_message_routes m where m.run_id=q.run_id) then 'message'
               when exists(select 1 from runtime.nexloop_plan_runs pr where pr.tenant_id=q.tenant_id and pr.world=q.world and pr.run_id=q.run_id) then 'plan'
               else 'other' end kind,
          ((q.usage->>'input')::numeric+coalesce((q.usage->>'cache_read')::numeric,0)+coalesce((q.usage->>'cache_write')::numeric,0))
            /(p.budget_report->>'input_token_budget')::numeric ratio
     from runtime.nexloop_model_requests q
     join runtime.nexloop_context_packs p on p.tenant_id=q.tenant_id and p.world=q.world and p.context_id=q.context_id
    where q.tenant_id=p_tenant and q.world=p_world and q.requested_at>clock_timestamp()-interval '24 hours'
      and jsonb_typeof(q.usage->'input')='number' and (q.usage->'cache_read' is null or jsonb_typeof(q.usage->'cache_read')='number')
      and (q.usage->'cache_write' is null or jsonb_typeof(q.usage->'cache_write')='number')
      and jsonb_typeof(p.budget_report->'input_token_budget')='number' and (p.budget_report->>'input_token_budget')::numeric>0),
  g as (
   select model||'|'||kind k,jsonb_build_object('requests',count(*),'p95',round((percentile_cont(0.95) within group (order by ratio))::numeric,4),
          'max',round(max(ratio),4)) gv from r group by model,kind),
  t as (select count(*) n,percentile_cont(0.95) within group (order by ratio) p95,max(ratio) mx from r),
  u as (select count(*) n from runtime.nexloop_model_requests q where q.tenant_id=p_tenant and q.world=p_world
         and q.requested_at>clock_timestamp()-interval '24 hours' and q.result_status='succeeded'
         and (q.usage is null or jsonb_typeof(q.usage->'input') is distinct from 'number'))
 select jsonb_build_object('configured',exists(select 1 from control.nexloop_context_strategies s where s.tenant_id=p_tenant and s.world=p_world),
          'present',t.n>0,'requests_24h',t.n,'requests_without_usage_24h',u.n)
        ||case when t.n>0 then jsonb_build_object('p95',round(t.p95::numeric,4),'max',round(t.mx,4),'groups',(select jsonb_object_agg(g.k,g.gv) from g))
          else '{}'::jsonb end
   into o from t,u;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('context_input_ratio',o);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
