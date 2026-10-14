-- NX-035 slice 3 (temporary number 0450; renumbered at merge): the file backup executor's inventory.
-- nexloop-file-backup (APP_HOST) copies Artifacts and finished Pi Run directories into a restic repository and records a
-- files_artifact / files_runtime row through authz.nexloop_record_backup (0146, unchanged). It decides nothing itself:
-- this function is the owner-side list of what may enter a new snapshot (design §4.3), and the only thing it returns is
-- references, hashes and sizes.
--   files_artifact: Artifacts that are available and inside their retention; deleted, deleting, pending or expired ones
--                   never enter a new snapshot. ref = object_key (<64 hex>/<32 hex>, relative to the artifact root).
--   files_runtime:  Pi Runs whose every bound task is terminal (the Host has closed the store) and that are not queued
--                   for, nor done with, an NX-029 purge. ref = run_id (the Host's directory name, lower case).
-- World 'real' only: simulation and shadow worlds are disposable by design (NX-041 D2) and are not backed up.
-- Called only by nexloop_backup_recorder (session_user, like the other recorder functions); keyset paging by ref.
create function authz.nexloop_file_backup_inventory(p_kind text,p_after text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);t text;v jsonb:='[]'::jsonb;part jsonb;
begin
 if session_user is distinct from 'nexloop_backup_recorder' then raise exception 'file backup inventory unavailable' using errcode='42501';end if;
 if p_kind is null or p_kind not in ('files_artifact','files_runtime') or p_limit is null or p_limit not between 1 and 1000
  or (p_after is not null and p_after!~'^[a-f0-9/-]{1,97}$') then
  raise exception 'file backup inventory invalid' using errcode='22023';
 end if;
 -- Every tenant-bound table here is FORCE RLS: read one tenant at a time; the global first p_limit refs are inside the union
 -- of each tenant's first p_limit.
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  if p_kind='files_artifact' then
   select coalesce(jsonb_agg(jsonb_build_object('ref',x.object_key,'sha256',x.sha256,'bytes',x.size_bytes) order by x.object_key),'[]'::jsonb) into part
    from (select a.object_key,a.sha256,a.size_bytes from runtime.nexloop_local_artifacts a
     where a.tenant_id=t and a.world='real' and a.status='available' and a.retention_until>clock_timestamp()
      and a.object_key~'^[a-f0-9]{64}/[a-f0-9]{32}$' and (p_after is null or a.object_key>p_after)
     order by a.object_key limit p_limit) x;
  else
   select coalesce(jsonb_agg(jsonb_build_object('ref',x.ref) order by x.ref),'[]'::jsonb) into part
    from (select b.run_id::text ref from authz.nexloop_runtime_run_bindings b
     where b.tenant_id=t and b.world='real' and (p_after is null or b.run_id::text>p_after)
      and not exists(select 1 from runtime.jobs j where j.tenant_id=b.tenant_id and j.job_id=b.task_id
       and j.status not in ('succeeded','failed','dead_lettered','cancelled'))
      and not exists(select 1 from runtime.nexloop_run_purge_items i where i.tenant_id=b.tenant_id and i.world=b.world and i.run_id=b.run_id)
     order by b.run_id::text limit p_limit) x;
  end if;
  v:=v||part;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 select coalesce(jsonb_agg(s.item order by s.item->>'ref'),'[]'::jsonb) into v
  from (select x.item from jsonb_array_elements(v) as x(item) order by x.item->>'ref' limit p_limit) s;
 return jsonb_build_object('kind',p_kind,'entries',v);
end $$;
alter function authz.nexloop_file_backup_inventory(text,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_file_backup_inventory(text,text,integer) from public;
grant execute on function authz.nexloop_file_backup_inventory(text,text,integer) to nexloop_backup_recorder;
