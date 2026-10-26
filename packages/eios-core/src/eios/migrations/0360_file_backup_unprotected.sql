-- NX-035 D6 (owner 2026-10-26): backupable files that no verified file snapshot can contain. The migration number is temporary;
-- it lives only in the file name and the catalog.
--
-- backup.file_backup_age_seconds (0146) is the age of the latest verified files_artifact / files_runtime row and is absent while
-- there is none: a file backup that was never enabled (stage until 10-26, NX-061 B9) never fires file_backup_age, however many
-- Artifacts and finished Runs wait. backup.file_backup_unprotected_seconds closes that: for the snapshot's tenant, world 'real'
-- only, the oldest object the file backup inventory (0194) lists - an available Artifact inside its retention, a Run whose bound
-- task is terminal and that is not queued for an NX-029 purge - that became backupable after the start of the latest verified
-- snapshot of its kind (or with no verified snapshot of its kind at all); the value is the seconds since it became backupable. Absent
-- when every such object is covered, or none exists.
-- When it became backupable is a time no later update moves (L6 S5: a retention change or a result written after the end must not
-- make a covered object unprotected again):
--   Artifact: least(updated_at, lease_until). Finalize (0005) turns pending into available only while the upload lease holds, and a
--     reservation sets that lease at most 30 s ahead, so lease_until is within 30 s after the moment it became available, and nothing
--     moves lease_until while it stays available (leaving available - deleting - is the only later write today). updated_at is that
--     moment itself as long as nothing else rewrites the row; least() keeps the bound if something later does.
--   Run: the latest terminal event of its task (runtime.job_events, insert-only: one row per transition; a requeued task that ends
--     again is genuinely new data), else the task's created_at (fixed; a path writing no terminal event can only make the value
--     older, never move it). A verified snapshot covers what was listed when it started, so the start time is the
-- bound. Stale backups with nothing new to back up stay file_backup_age's alert; new data a stopped (or never started) backup misses
-- is this one's (alert rules v20, file_backup_unprotected, above 1 hour). Read as the snapshot's tenant (the tables are FORCE RLS).
-- runtime.nexloop_metrics_snapshot is renamed once and wrapped, as 0354 did.
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_file_backup_unprotected;
revoke all on function runtime.nexloop_metrics_snapshot_before_file_backup_unprotected(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_file_backup_unprotected(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 now_at timestamptz:=clock_timestamp();artifacts_from timestamptz;runs_from timestamptz;oldest timestamptz;
begin
 if p_world is distinct from 'real' or jsonb_typeof(v->'backup') is distinct from 'object' then return v;end if;
 select max(started_at) into artifacts_from from runtime.nexloop_backup_manifests where kind='files_artifact' and verify_status='verified';
 select max(started_at) into runs_from from runtime.nexloop_backup_manifests where kind='files_runtime' and verify_status='verified';
 perform set_config('eios.tenant_id',p_tenant,true);
 select min(x.since) into oldest from (
  select min(a.since) since from (select least(a.updated_at,a.lease_until) since from runtime.nexloop_local_artifacts a
    where a.tenant_id=p_tenant and a.world='real' and a.status='available' and a.retention_until>now_at
     and a.object_key~'^[a-f0-9]{64}/[a-f0-9]{32}$') a
   where artifacts_from is null or a.since>artifacts_from
  union all
  select min(r.since) from (select coalesce((select max(e.created_at) from runtime.job_events e where e.tenant_id=j.tenant_id and e.job_id=j.job_id
     and e.status in ('succeeded','failed','dead_lettered','cancelled')),j.created_at) since
    from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
    where b.tenant_id=p_tenant and b.world='real' and j.status in ('succeeded','failed','dead_lettered','cancelled')
     and not exists(select 1 from runtime.nexloop_run_purge_items i where i.tenant_id=b.tenant_id and i.world=b.world and i.run_id=b.run_id)) r
   where runs_from is null or r.since>runs_from) x;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if oldest is null then return v;end if;
 return jsonb_set(v,'{backup,file_backup_unprotected_seconds}',to_jsonb(greatest(0,round(extract(epoch from now_at-oldest)))::bigint));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
