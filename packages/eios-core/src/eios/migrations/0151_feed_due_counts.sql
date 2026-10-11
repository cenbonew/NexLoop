-- NX-031 slice 3 (temporary number 0151): work-feed counts split into due and scheduled.
-- The snapshot's feeds.<feed>.pending counted every pending item, including items scheduled for later (commitment due stages,
-- reply-due before its start time, plan reassessments): a feed looked stuck while nothing was due. Each feed now also reports
-- `due` (pending, available now) and `scheduled` (pending, available later) and `oldest_due_seconds`; `pending` keeps its meaning
-- (due + scheduled) for existing readers and alert rules. runtime.nexloop_metrics_snapshot is renamed once and wrapped, as 0146
-- did; counts only, no payload.
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_feed_due_v0151;
revoke all on function runtime.nexloop_metrics_snapshot_before_feed_due_v0151(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_feed_due_v0151(p_tenant,p_world);split jsonb;now_at timestamptz:=clock_timestamp();
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_object_agg(feed,jsonb_build_object('due',due,'scheduled',scheduled,'oldest_due_seconds',oldest)),'{}'::jsonb) into split
  from (select feed,count(*) filter (where available_at<=now_at) due,count(*) filter (where available_at>now_at) scheduled,
         floor(extract(epoch from now_at-min(changed_at) filter (where available_at<=now_at)))::bigint oldest
        from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and status='pending' group by feed) x;
 return jsonb_set(v,'{feeds}',coalesce((select jsonb_object_agg(f.key,f.value||coalesce(split->f.key,jsonb_build_object('due',0,'scheduled',0,'oldest_due_seconds',null)))
  from jsonb_each(coalesce(v->'feeds','{}'::jsonb)) f),'{}'::jsonb));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
