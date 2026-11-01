-- S89 (owner 2026-11-01, fix 1; temporary number, the dispatcher numbers it at batch time): a reevaluation Run denied at dispatch
-- (control_paused, the three stale codes, budget_exhausted, control_snapshot_missing) re-registered its plan as cause 'external',
-- which the precheck's self-trigger throttle (0106) never counts: with the model budget exhausted (not checked by the precheck)
-- every worker tick issued another Run, denied again. The denial of the plan's own Run is now cause 'self'. Side effect: a throttled
-- item is retried with max_attempts 20 (plan_reevaluation.py), so a budget exhausted for about 20 windows (3.3 h at 600 s)
-- dead-letters the item (feed_dead_letter, critical) until fix 2 (work_feed defer, S90). runtime.nexloop_plan_on_job_failed:
-- 0106's body with that cause; nothing else.
create or replace function runtime.nexloop_plan_on_job_failed() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);pr runtime.nexloop_plan_runs%rowtype;
begin
 if new.status<>'failed' or old.status='failed' or coalesce(new.result->>'code','') not in
   ('control_paused','goal_version_stale','control_revision_stale','object_revision_stale','budget_exhausted','control_snapshot_missing') then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select r.* into pr from runtime.nexloop_plan_runs r join authz.nexloop_runtime_run_bindings b on b.run_id=r.run_id
  where b.tenant_id=new.tenant_id and b.task_id=new.job_id and r.tenant_id=new.tenant_id;
 if found then
  perform authz.nexloop_plan_feed_touch(pr.tenant_id,pr.world,pr.plan_id,
   -- S89: the denied task is this plan's own reevaluation Run, so the trigger is self-caused and the 0106 self-trigger throttle
   -- (self_trigger_cap per self_window) bounds it; as 'external' it re-armed the plan at once, one Run per worker tick.
   jsonb_build_object('kind','dispatch_denied','cause','self','code',new.result->>'code','run_id',pr.run_id,'at',clock_timestamp()),null);
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;

