-- NX-035 deployment gap D1 (stage deployment, 2026-10-14): pgBackRest needs to read protected server settings.
-- 0146 gave nexloop_backup REPLICATION and EXECUTE on four backup functions only. The real pgBackRest 2.59.3 run as
-- nexloop_backup refused stanza-create and check with "unable to select some rows from pg_settings": pgBackRest reads settings
-- such as data_directory, which only superusers and members of pg_read_all_settings may see. Measured on a disposable cluster
-- on the CI host: with this grant added, stanza-create, check, a full backup and verify all pass as nexloop_backup, which stays
-- a non-superuser. nexloop_backup is NOINHERIT (0146), so the membership is granted WITH INHERIT TRUE: without it the role
-- would have to SET ROLE, which pgBackRest does not do. Nothing else changes: no new role attribute, no table or function
-- privilege; pg_read_all_settings only reads configuration (no data, no file access).
grant pg_read_all_settings to nexloop_backup with inherit true;
