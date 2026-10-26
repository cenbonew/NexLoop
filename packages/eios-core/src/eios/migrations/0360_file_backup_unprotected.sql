-- NX-035 D6 (owner 2026-10-26): backupable files that no verified file snapshot can contain. The migration number is temporary;
-- it lives only in the file name and the catalog.
--
-- backup.file_backup_age_seconds (0146) is the age of the latest verified files_artifact / files_runtime row and is absent while
-- there is none: a file backup that was never enabled (stage until 10-26, NX-061 B9) never fires file_backup_age, however many
-- Artifacts and finished Runs wait. backup.file_backup_unprotected_seconds closes that: for the snapshot's tenant, world 'real'
-- only, the oldest object the file backup inventory (0194) lists - an available Artifact inside its retention, a Run whose bound
-- task is terminal and that is not queued for an NX-029 purge - that became backupable after the start of the latest verified
-- snapshot of its kind (or with no verified snapshot of its kind at all); the value is the seconds since it became backupable
-- (an Artifact's updated_at, written when it turned available; the task's updated_at, written when it turned terminal). Absent when
-- every such object is covered, or none exists. A verified snapshot covers what was listed when it started, so the start time is the
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
  select min(a.updated_at) since from runtime.nexloop_local_artifacts a
   where a.tenant_id=p_tenant and a.world='real' and a.status='available' and a.retention_until>now_at
    and a.object_key~'^[a-f0-9]{64}/[a-f0-9]{32}$' and (artifacts_from is null or a.updated_at>artifacts_from)
  union all
  select min(j.updated_at) from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
   where b.tenant_id=p_tenant and b.world='real' and j.status in ('succeeded','failed','dead_lettered','cancelled')
    and not exists(select 1 from runtime.nexloop_run_purge_items i where i.tenant_id=b.tenant_id and i.world=b.world and i.run_id=b.run_id)
    and (runs_from is null or j.updated_at>runs_from)) x;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if oldest is null then return v;end if;
 return jsonb_set(v,'{backup,file_backup_unprotected_seconds}',to_jsonb(greatest(0,round(extract(epoch from now_at-oldest)))::bigint));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
