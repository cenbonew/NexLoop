-- Run failure in the NX-037 evidence (temporary number 0204; 10/18 stage, Run 1ab1d785: a failed tool was reported as
-- tool_error with no tool named). runtime.nexloop_nx037_run_evidence (latest body: 0161, copied verbatim) also returns
-- `failure` for a failed message Run: its task result's code, failure_code, failure_detail and failure_tool (the Host's
-- canonical tool name or unregistered_tool), each only when it is a fixed code. Read-only, as before.
create or replace function runtime.nexloop_nx037_run_evidence(p_tenant text,p_world text,p_run uuid) returns jsonb
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
     where r.tenant_id=p_tenant and r.world=p_world and r.path='effect' and i.origin_run_id=p_run group by r.code) x),'{}'::jsonb),
  -- 0204: why a message Run failed, codes only, from its runtime task result (which tool, when a tool failed).
  'failure',(select jsonb_strip_nulls(jsonb_build_object(
      'code',case when j.result->>'code' ~ '^[a-z][a-z_]{0,63}$' then j.result->>'code' end,
      'failure_code',case when j.result->>'failure_code' ~ '^[a-z][a-z_]{0,63}$' then j.result->>'failure_code' end,
      'failure_detail',case when j.result->>'failure_detail' ~ '^[a-z][a-z_]{0,63}$' then j.result->>'failure_detail' end,
      'failure_tool',case when j.result->>'failure_tool' ~ '^(nexloop\.[a-z][a-z_.]{0,63}|unregistered_tool)$' then j.result->>'failure_tool' end))
     from runtime.nexloop_message_routes r join runtime.jobs j on j.job_id=r.task_id
     where r.tenant_id=p_tenant and r.world=p_world and r.run_id=p_run and j.status='failed' limit 1)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
