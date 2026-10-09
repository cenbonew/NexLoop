-- Temporary number. Tighten temporary-schema exposure for the NexLoop lineage:
-- (a) only nexloop_owner may create temporary objects in this database;
-- (b) every application-schema routine that pins search_path lists pg_temp
--     explicitly and last, so names inside owner-run code never resolve to a
--     session's temporary schema. Other per-function settings are preserved.

do $temp$
declare r text;
begin
 execute format('revoke temporary on database %I from public',current_database());
 foreach r in array array['nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler',
  'nexloop_runtime','nexloop_identity','nexloop_configurator'] loop
  if exists(select 1 from pg_catalog.pg_roles where rolname=r) then
   execute format('revoke temporary on database %I from %I',current_database(),r);
  end if;
 end loop;
 execute format('grant temporary on database %I to nexloop_owner',current_database());
end $temp$;

do $path$
declare f record;
begin
 for f in
  select p.oid::regprocedure as signature
  from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid=p.pronamespace
  where n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg\_toast%' and n.nspname not like 'pg\_temp%'
   and not exists(select 1 from pg_catalog.pg_depend d where d.classid='pg_catalog.pg_proc'::regclass and d.objid=p.oid and d.deptype='e')
   and exists(select 1 from pg_catalog.unnest(p.proconfig) c where c like 'search\_path=%')
  order by p.oid::regprocedure::text
 loop
  -- ALTER ... SET replaces only this one setting; row_security etc. are kept.
  execute format('alter routine %s set search_path = pg_catalog, pg_temp',f.signature);
 end loop;
end $path$;
