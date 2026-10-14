-- NX-035 deploy slice (dispatcher 2026-10-14): the backup manifest records the tool that made a backup and its verification
-- evidence (manifest schema nexloop-backup-manifest/2, docs/implementation/NX-035-pgbackrest.md). Three nullable columns
-- (rows written before keep null); authz.nexloop_record_backup copied from 0146 with the marked additions only:
--   * tool ('pgbackrest' | 'pg_basebackup' | 'restic' | 'nexloop') and tool_version on the pending row of a PG backup (the
--     completed row inherits them) and on single-row entries (journal, files);
--   * verification: one object of schema nexloop-backup-verification/1 (at most 16 KiB) on the completed row;
--   * a pgBackRest row cannot be recorded verified without its verification evidence.
-- The migration gate is unchanged: it takes the latest completed PG row; a failed verify is recorded 'failed' and refuses.
alter table runtime.nexloop_backup_manifests
 add column tool text check(tool in ('pgbackrest','pg_basebackup','restic','nexloop')),
 add column tool_version text check(tool_version~'^[0-9A-Za-z.+_-]{1,32}$'),
 add column verification jsonb check(verification is null or (jsonb_typeof(verification)='object' and verification->>'schema'='nexloop-backup-verification/1'));

create or replace function authz.nexloop_record_backup(p_entry jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v_kind text:=p_entry->>'kind';v_status text:=p_entry->>'verify_status';v_allowed text[];v_pg boolean;
 base runtime.nexloop_backup_manifests;cat jsonb;ident jsonb;v_completed timestamptz;v_started timestamptz;v_id bigint;
begin
 if session_user is distinct from 'nexloop_backup_recorder' then raise exception 'backup recorder unavailable' using errcode='42501';end if;
 if jsonb_typeof(p_entry) is distinct from 'object' or v_kind not in ('pg_full','pg_diff','pg_incr','files_artifact','files_runtime','journal')
  or v_status not in ('pending','verified','failed') then raise exception 'backup manifest invalid' using errcode='22023';end if;
 v_pg:=v_kind in ('pg_full','pg_diff','pg_incr');
 v_allowed:=case
  when v_pg and v_status='pending' then array['backup_id','repo','kind','verify_status','release_id','key_ref','tool','tool_version']
  when v_pg then array['backup_id','repo','kind','verify_status','completed_at','system_identifier','timeline','start_lsn','stop_lsn','verification']
  when v_kind='journal' then array['backup_id','repo','kind','verify_status','started_at','completed_at','journal_seq','key_ref','tool','tool_version','verification']
  else array['backup_id','repo','kind','verify_status','started_at','completed_at','file_manifest_digest','file_count','bytes','key_ref','release_id','tool','tool_version','verification'] end;
 if exists(select 1 from jsonb_object_keys(p_entry) k where not k=any(v_allowed))
  or exists(select 1 from jsonb_each(p_entry) e where e.key<>'verification' and jsonb_typeof(e.value) not in ('string','number'))
  or (p_entry ? 'verification' and (jsonb_typeof(p_entry->'verification')<>'object' or p_entry->'verification'->>'schema' is distinct from 'nexloop-backup-verification/1'
      or octet_length((p_entry->'verification')::text)>16384))
  or (p_entry ? 'tool' and p_entry->>'tool' not in ('pgbackrest','pg_basebackup','restic','nexloop'))
  or (p_entry ? 'tool_version' and p_entry->>'tool_version'!~'^[0-9A-Za-z.+_-]{1,32}$')
  or coalesce(p_entry->>'backup_id','')!~'^[A-Za-z0-9._:-]{1,64}$' or p_entry->>'repo' not in ('repo1','repo2')
  or (p_entry ? 'release_id' and p_entry->>'release_id'!~'^[0-9]{8}-[a-f0-9]{12}-[0-9]{1,4}$')
  or (p_entry ? 'key_ref' and p_entry->>'key_ref'!~'^[A-Za-z0-9._:-]{1,64}$')
  or (p_entry ? 'system_identifier' and p_entry->>'system_identifier'!~'^[0-9]{1,20}$')
  or (p_entry ? 'timeline' and (jsonb_typeof(p_entry->'timeline')<>'number' or p_entry->>'timeline'!~'^[1-9][0-9]{0,9}$'))
  or (p_entry ? 'start_lsn' and p_entry->>'start_lsn'!~'^[0-9A-F]{1,8}/[0-9A-F]{1,8}$')
  or (p_entry ? 'stop_lsn' and p_entry->>'stop_lsn'!~'^[0-9A-F]{1,8}/[0-9A-F]{1,8}$')
  or (p_entry ? 'file_manifest_digest' and p_entry->>'file_manifest_digest'!~'^[a-f0-9]{64}$')
  or (p_entry ? 'file_count' and (jsonb_typeof(p_entry->'file_count')<>'number' or p_entry->>'file_count'!~'^[0-9]{1,15}$'))
  or (p_entry ? 'bytes' and (jsonb_typeof(p_entry->'bytes')<>'number' or p_entry->>'bytes'!~'^[0-9]{1,18}$'))
  or (p_entry ? 'journal_seq' and (jsonb_typeof(p_entry->'journal_seq')<>'number' or p_entry->>'journal_seq'!~'^[0-9]{1,18}$'))
  or (p_entry ? 'started_at' and p_entry->>'started_at'!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|\+00:00)$')
  or (p_entry ? 'completed_at' and p_entry->>'completed_at'!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|\+00:00)$') then
  raise exception 'backup manifest invalid' using errcode='22023';end if;
 select manifest_id into v_id from runtime.nexloop_backup_manifests where repo=p_entry->>'repo' and backup_id=p_entry->>'backup_id' and kind=v_kind and verify_status=v_status;
 if found then return jsonb_build_object('recorded',true,'replay',true,'manifest_id',v_id);end if;
 if p_entry ? 'completed_at' then
  v_completed:=(p_entry->>'completed_at')::timestamptz;
  if v_completed>clock_timestamp()+interval '1 minute' then raise exception 'backup manifest invalid' using errcode='22023';end if;
 end if;
 cat:=runtime.nexloop_catalog_state();ident:=runtime.nexloop_cluster_identity();
 if v_pg and v_status='pending' then
  insert into runtime.nexloop_backup_manifests(backup_id,repo,kind,started_at,system_identifier,timeline,catalog_revision,catalog_digest,release_id,
    deletion_watermark,journal_seq,key_ref,verify_status,tool,tool_version)
   values(p_entry->>'backup_id',p_entry->>'repo',v_kind,clock_timestamp(),ident->>'system_identifier',(ident->>'timeline')::integer,cat->>'revision',cat->>'digest',
    p_entry->>'release_id',(select coalesce(max(seq),0) from control.nexloop_deletion_ledger),(select coalesce(max(seq),0) from control.nexloop_restore_journal),
    p_entry->>'key_ref','pending',p_entry->>'tool',p_entry->>'tool_version') returning manifest_id into v_id;
 elsif v_pg then
  -- The completed row copies what the server recorded at the start; a backup of another cluster is refused.
  select * into base from runtime.nexloop_backup_manifests where repo=p_entry->>'repo' and backup_id=p_entry->>'backup_id' and kind=v_kind and verify_status='pending';
  if not found or v_completed is null or v_completed<base.started_at or p_entry->>'system_identifier' is distinct from base.system_identifier
   or not (p_entry ? 'start_lsn' and p_entry ? 'stop_lsn' and p_entry ? 'timeline') or (p_entry->>'stop_lsn')::pg_lsn<(p_entry->>'start_lsn')::pg_lsn then
   raise exception 'backup manifest invalid' using errcode='22023';end if;
  -- A pgBackRest row is verified only with its verification evidence (NX-035 deploy slice, manifest schema nexloop-backup-manifest/2).
  if base.tool='pgbackrest' and v_status='verified' and not (p_entry ? 'verification') then raise exception 'backup manifest invalid' using errcode='22023';end if;
  insert into runtime.nexloop_backup_manifests(backup_id,repo,kind,started_at,completed_at,system_identifier,timeline,start_lsn,stop_lsn,catalog_revision,
    catalog_digest,release_id,deletion_watermark,journal_seq,key_ref,verify_status,verified_at,tool,tool_version,verification)
   values(base.backup_id,base.repo,v_kind,base.started_at,v_completed,base.system_identifier,(p_entry->>'timeline')::integer,(p_entry->>'start_lsn')::pg_lsn,
    (p_entry->>'stop_lsn')::pg_lsn,base.catalog_revision,base.catalog_digest,base.release_id,base.deletion_watermark,base.journal_seq,base.key_ref,v_status,clock_timestamp(),
    base.tool,base.tool_version,p_entry->'verification')
   returning manifest_id into v_id;
 else
  if v_status='pending' or v_completed is null or not (p_entry ? 'started_at') then raise exception 'backup manifest invalid' using errcode='22023';end if;
  v_started:=(p_entry->>'started_at')::timestamptz;
  if v_started>v_completed or (v_kind='journal' and not (p_entry ? 'journal_seq'))
   or (v_kind='journal' and (p_entry->>'journal_seq')::bigint>(select coalesce(max(seq),0) from control.nexloop_restore_journal))
   or (v_kind<>'journal' and not (p_entry ?& array['file_manifest_digest','file_count','bytes'])) then
   raise exception 'backup manifest invalid' using errcode='22023';end if;
  insert into runtime.nexloop_backup_manifests(backup_id,repo,kind,started_at,completed_at,system_identifier,timeline,catalog_revision,catalog_digest,release_id,
    deletion_watermark,journal_seq,file_manifest_digest,file_count,bytes,key_ref,verify_status,verified_at,tool,tool_version,verification)
   values(p_entry->>'backup_id',p_entry->>'repo',v_kind,v_started,v_completed,ident->>'system_identifier',(ident->>'timeline')::integer,cat->>'revision',cat->>'digest',
    p_entry->>'release_id',(select coalesce(max(seq),0) from control.nexloop_deletion_ledger),
    coalesce((p_entry->>'journal_seq')::bigint,(select coalesce(max(seq),0) from control.nexloop_restore_journal)),
    p_entry->>'file_manifest_digest',(p_entry->>'file_count')::bigint,(p_entry->>'bytes')::bigint,p_entry->>'key_ref',v_status,clock_timestamp(),
    p_entry->>'tool',p_entry->>'tool_version',p_entry->'verification')
   returning manifest_id into v_id;
 end if;
 return jsonb_build_object('recorded',true,'replay',false,'manifest_id',v_id);
end $$;
