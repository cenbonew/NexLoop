-- S6 r8/r6 verify with a low-privilege role (L6 follow-up; run-s6-stage-prep §1.2): bootstrap.verify reads only
-- control.schema_migrations (version, checksum: no tenant data), and roles.verify used the bootstrap DSN for it. nexloop_evo_reader
-- (0233/0234: login, noinherit, nobypassrls, local socket only, already enrolled with a DSN on DATA_HOST for S6) gains USAGE on
-- schema control and SELECT on that one table, nothing else: every other control table still needs its own grant, and its function
-- grants (the trace and semantic access reads) are unchanged. Temporary number; the dispatcher renumbers.
grant usage on schema control to nexloop_evo_reader;
grant select on control.schema_migrations to nexloop_evo_reader;
