-- NX-037 A-class prep: two read-only, numbers-only views for the metrics role (nexloop_metrics), used by
-- the real tenant initialization check and the controlled interaction executor (docs/implementation/NX-037-prep-implementation.md).
-- Neither writes, neither returns text other than codes, ids, digests and times.
--
-- 1. authz.nexloop_nx037_tenant_state(tenant, world): the tenant's presence and status, the real dispatch switches (tenant layer
--    and open Action switches; the deployment layer is the dispatching process's own environment and is checked there), the
--    budget limits with their periods, the latest alert rules version and digest, and the count of effect category declarations.
-- 2. authz.nexloop_nx037_run_evidence(tenant, world, run): one Run's model calls (count, time, cost, currency, statuses), tool
--    time (0133), cost entries by kind, effect intents (origin_run_id) by state, and effect-path refusal codes of those intents.
-- Owner functions read FORCE RLS tables, so each sets eios.tenant_id for its own transaction and restores it.
create function runtime.nexloop_nx037_tenant_state(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object(
  'tenant_status',(select t.status from control.nexloop_tenants t where t.tenant_id=p_tenant),
  'tenant_switch_open',coalesce((select s.enabled from control.nexloop_real_dispatch_switches s
     where s.tenant_id=p_tenant and s.world=p_world and s.scope_kind='tenant'),false),
  'open_action_switches',(select count(*) from control.nexloop_real_dispatch_switches s
     where s.tenant_id=p_tenant and s.world=p_world and s.scope_kind='action_type' and s.enabled),
  'budgets',coalesce((select jsonb_object_agg(b.budget_kind,jsonb_build_object('limit',b.limit_amount::text,'unit',b.unit,
     'period_start',b.period_start,'period_end',b.period_end,'revision',b.revision))
     from control.nexloop_budget_limits b where b.tenant_id=p_tenant and b.world=p_world),'{}'::jsonb),
  'alert_rules',(select jsonb_build_object('version',r.version,'digest',r.digest) from control.nexloop_alert_rules r
     where r.tenant_id=p_tenant order by r.version desc limit 1),
  'effect_categories',(select count(*) from control.nexloop_action_effect_externality x where x.tenant_id=p_tenant and x.world=p_world)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_nx037_tenant_state(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_nx037_tenant_state(text,text) from public;
create function authz.nexloop_nx037_tenant_state(p_tenant text,p_world text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_nx037_tenant_state(p_tenant,p_world) $$;
alter function authz.nexloop_nx037_tenant_state(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_nx037_tenant_state(text,text) from public;
grant execute on function authz.nexloop_nx037_tenant_state(text,text) to nexloop_metrics;

create function runtime.nexloop_nx037_run_evidence(p_tenant text,p_world text,p_run uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object(
  'model',(select jsonb_build_object('calls',count(*),
     'ms',coalesce(round(sum(extract(epoch from q.completed_at-q.requested_at)*1000) filter (where q.completed_at is not null)),0),
     'cost',coalesce(sum(q.cost),0)::text,'currencies',coalesce(jsonb_agg(distinct q.currency) filter (where q.currency is not null),'[]'::jsonb),
     'unfinished',count(*) filter (where q.completed_at is null),'failed',count(*) filter (where q.result_status='failed'),
     'unknown',count(*) filter (where q.result_status='unknown'))
     from runtime.nexloop_model_requests q where q.tenant_id=p_tenant and q.world=p_world and q.run_id=p_run),
  'tool',coalesce((select jsonb_build_object('calls',t.calls,'ms',round(t.total_ms),'max_ms',round(t.max_ms)) from runtime.nexloop_run_tool_timings t
     where t.tenant_id=p_tenant and t.world=p_world and t.run_id=p_run),jsonb_build_object('calls',0,'ms',0,'max_ms',0)),
  'costs',coalesce((select jsonb_object_agg(k,jsonb_build_object('amount',a::text,'currencies',c)) from (
     select e.cost_kind k,coalesce(sum(e.amount),0) a,coalesce(jsonb_agg(distinct e.currency) filter (where e.currency is not null),'[]'::jsonb) c
     from runtime.nexloop_cost_entries e where e.tenant_id=p_tenant and e.world=p_world and e.run_id=p_run group by e.cost_kind) x),'{}'::jsonb),
  'intents',coalesce((select jsonb_object_agg(s,n) from (select i.state s,count(*) n from runtime.nexloop_effect_intents i
     where i.tenant_id=p_tenant and i.world=p_world and i.origin_run_id=p_run group by i.state) x),'{}'::jsonb),
  'intent_ids',coalesce((select jsonb_agg(i.intent_id order by i.intent_id) from runtime.nexloop_effect_intents i
     where i.tenant_id=p_tenant and i.world=p_world and i.origin_run_id=p_run),'[]'::jsonb),
  'refusals',coalesce((select jsonb_object_agg(c,n) from (select r.code c,count(*) n from runtime.nexloop_dispatch_refusals r
     join runtime.nexloop_effect_intents i on i.intent_id=r.intent_id and i.tenant_id=r.tenant_id and i.world=r.world
     where r.tenant_id=p_tenant and r.world=p_world and r.path='effect' and i.origin_run_id=p_run group by r.code) x),'{}'::jsonb)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_nx037_run_evidence(text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_nx037_run_evidence(text,text,uuid) from public;
create function authz.nexloop_nx037_run_evidence(p_tenant text,p_world text,p_run uuid) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_nx037_run_evidence(p_tenant,p_world,p_run) $$;
alter function authz.nexloop_nx037_run_evidence(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_nx037_run_evidence(text,text,uuid) from public;
grant execute on function authz.nexloop_nx037_run_evidence(text,text,uuid) to nexloop_metrics;
