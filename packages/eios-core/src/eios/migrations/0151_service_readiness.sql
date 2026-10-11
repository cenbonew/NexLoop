-- Deployment readiness (temporary number 0200; NX-032 / NX-035): the facts a service's readiness endpoint and doctor --backup
-- need, through two definer functions that return fixed-shape status only (no rows, no identifiers beyond restore refs).
--
-- 1. authz.nexloop_service_readiness(): the NX-035 restore state (runtime.nexloop_restore_readiness: restored/replayed not
--    ready, verified_readonly reads only, open or never restored normal) and the NX-029 deletion replay
--    (runtime.nexloop_erasure_readiness: not ready while a replay is open). Granted to the application service roles and the
--    read-only metrics role; the existing runtime functions stay owner-only.
-- 2. authz.nexloop_backup_status(): the M12 backup block (runtime.nexloop_backup_metrics, 'unavailable' while no PG backup is
--    recorded), the restore state and whether a deletion replay is open, for doctor --backup. nexloop_metrics only.
create function authz.nexloop_service_readiness() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('restore',runtime.nexloop_restore_readiness(),
  'erasure',jsonb_build_object('ready',(runtime.nexloop_erasure_readiness()->>'ready')::boolean))
$$;
create function authz.nexloop_backup_status() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('backup',runtime.nexloop_backup_metrics(),'restore',runtime.nexloop_restore_readiness(),
  'erasure_ready',(runtime.nexloop_erasure_readiness()->>'ready')::boolean)
$$;
do $owners$
declare f text;
begin
 foreach f in array array['authz.nexloop_service_readiness()','authz.nexloop_backup_status()'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
grant execute on function authz.nexloop_service_readiness() to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_metrics;
grant execute on function authz.nexloop_backup_status() to nexloop_metrics;
