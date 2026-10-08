-- NexLoop independent lineage. Adapted isolation pattern from EIOS 0007/0061.
-- No domain seeds, permissive API grants, or credential values.
create role nexloop_owner nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
create role nexloop_api login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
create role nexloop_domain_worker login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
create role nexloop_action_worker login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
create role nexloop_scheduler login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
create role nexloop_runtime login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
revoke create on schema public from public;
revoke all on schema control, runtime, ontology from public;
grant usage on schema control to nexloop_api, nexloop_domain_worker, nexloop_action_worker, nexloop_scheduler;
grant select on control.schema_migrations to nexloop_api, nexloop_domain_worker, nexloop_action_worker, nexloop_scheduler;
-- Runtime receives neither business schemas nor database credentials.
-- Object table writes stay owner-only. Governed write definers are added by NX-012.
do $isolation$
declare r record;
begin
  for r in select schemaname,tablename from pg_tables
    where schemaname in ('runtime','ontology') order by schemaname,tablename loop
    execute format('alter table %I.%I owner to nexloop_owner',r.schemaname,r.tablename);
    execute format('alter table %I.%I enable row level security',r.schemaname,r.tablename);
    execute format('alter table %I.%I force row level security',r.schemaname,r.tablename);
    execute format('create policy tenant_boundary on %I.%I to nexloop_owner using (tenant_id = current_setting(''eios.tenant_id'',true)) with check (tenant_id = current_setting(''eios.tenant_id'',true))',r.schemaname,r.tablename);
    execute format('revoke all on %I.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime',r.schemaname,r.tablename);
  end loop;
end $isolation$;
alter schema ontology owner to nexloop_owner;
alter schema runtime owner to nexloop_owner;
