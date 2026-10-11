-- NX-035 local part (temporary number 0180): backup manifests, the restore journal, drill restore points and the M12 backup
-- metrics. Design docs/implementation/NX-035-design.md §4, §6.2, §7.1, §8 (D1–D8 settled); local-part rulings of the
-- dispatcher 2026-10-11 (Q1: journal segments are hash-chained and 0600, encrypted at rest by the file backup repository at
-- deployment; Q3: no migration-gate exemption of any kind).
--
-- 1. Roles. nexloop_backup_recorder (login) executes only the recorder functions below and reads no table.
--    nexloop_backup (login, REPLICATION) takes base backups over the replication protocol and may run the built-in backup
--    functions; it is not a superuser and has no privilege on any NexLoop table. Neither credential enters the runtime, an
--    Agent or a Run.
-- 2. runtime.nexloop_backup_manifests: deployment level (no tenant column), append-only, FORCE RLS, owner only. Labels,
--    hashes, LSNs, numbers and times only; a later verification state is a new row. At the start of a PG backup (state
--    pending) the server itself records the cluster identity, the migration catalog (revision and digest), the deletion
--    watermark and the restore-journal position; the completed row copies them, so a recorder cannot claim them.
-- 3. control.nexloop_restore_journal: one append-only sequence of what a point-in-time restore would otherwise lose —
--    deletion ledger entries (NX-029), contact restrictions (NX-025) and hashed erased contact keys (NX-029 D5) without the
--    customer's words, service credential revocations and workbench membership suspensions or revocations. Written by
--    triggers on those tables; exported by the recorder (authz.nexloop_restore_journal_since) into hash-chained segments.
-- 4. Drill restore points: authz.nexloop_drill_mark creates a named restore point and, in the same call, records the maximum
--    id, the row count and sampled row hashes of each append-only table (numbers and hashes only). A drill's measured result
--    (RPO, RTO, check outcomes) is appended by the recorder.
-- 5. M12: runtime.nexloop_metrics_snapshot is renamed once and kept (…_before_backup_v0180) and wrapped; the wrapper replaces
--    only the 'backup' block with runtime.nexloop_backup_metrics() (unavailable while no PG backup is recorded, never zero).
-- 6. Dispatch refusal code NXR01 restore_unverified (the restore fence itself is migration 0181).
-- Published migrations are not edited.

-- 1. Roles ----------------------------------------------------------------------------------------------------------------
create role nexloop_backup_recorder login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
create role nexloop_backup login replication nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
grant usage on schema authz to nexloop_backup_recorder;
-- The built-in backup functions (pgBackRest at deployment); pg_basebackup itself needs only REPLICATION.
grant execute on function pg_catalog.pg_backup_start(text,boolean) to nexloop_backup;
grant execute on function pg_catalog.pg_backup_stop(boolean) to nexloop_backup;
grant execute on function pg_catalog.pg_switch_wal() to nexloop_backup;
grant execute on function pg_catalog.pg_create_restore_point(text) to nexloop_backup;
-- The owner creates the drill's named restore point inside authz.nexloop_drill_mark.
grant execute on function pg_catalog.pg_create_restore_point(text) to nexloop_owner;

-- 2. Manifests -------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_backup_manifests (
 manifest_id bigint generated always as identity primary key,
 backup_id text not null check(backup_id~'^[A-Za-z0-9._:-]{1,64}$'),
 repo text not null check(repo in ('repo1','repo2')),
 kind text not null check(kind in ('pg_full','pg_diff','pg_incr','files_artifact','files_runtime','drill_restore_point','journal')),
 started_at timestamptz not null,completed_at timestamptz,
 system_identifier text not null check(system_identifier~'^[0-9]{1,20}$'),timeline integer not null check(timeline>=1),
 start_lsn pg_lsn,stop_lsn pg_lsn,
 catalog_revision text not null check(catalog_revision~'^[0-9]{4}$'),catalog_digest text not null check(catalog_digest~'^[a-f0-9]{64}$'),
 release_id text check(release_id~'^[0-9]{8}-[a-f0-9]{12}-[0-9]{1,4}$'),
 deletion_watermark bigint not null check(deletion_watermark>=0),journal_seq bigint not null check(journal_seq>=0),
 file_manifest_digest text check(file_manifest_digest~'^[a-f0-9]{64}$'),file_count bigint check(file_count>=0),bytes bigint check(bytes>=0),
 key_ref text check(key_ref~'^[A-Za-z0-9._:-]{1,64}$'),
 verify_status text not null check(verify_status in ('pending','verified','failed')),verified_at timestamptz,
 recorded_at timestamptz not null default clock_timestamp(),
 unique(repo,backup_id,kind,verify_status),
 check((verify_status='pending')=(verified_at is null) and (verify_status='pending')=(completed_at is null)),
 check(kind not in ('pg_full','pg_diff','pg_incr') or verify_status='pending' or (start_lsn is not null and stop_lsn is not null and stop_lsn>=start_lsn)),
 check(kind not in ('files_artifact','files_runtime') or verify_status='pending' or (file_manifest_digest is not null and file_count is not null and bytes is not null)),
 check(completed_at is null or completed_at>=started_at)
);
create index nexloop_backup_manifests_latest on runtime.nexloop_backup_manifests(kind,verify_status,completed_at desc);

-- 3. Restore journal -------------------------------------------------------------------------------------------------------
create table control.nexloop_restore_journal (
 seq bigint generated always as identity primary key,
 kind text not null check(kind in ('deletion','contact_restriction','revocation')),
 entry jsonb not null check(jsonb_typeof(entry)='object'),
 recorded_at timestamptz not null default clock_timestamp()
);

-- 4. Drills ----------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_restore_drills (
 drill_id text primary key check(drill_id~'^[a-z0-9][a-z0-9._-]{0,47}$'),restore_point text not null unique,
 restore_lsn pg_lsn not null,marked_at timestamptz not null,
 tables jsonb not null check(jsonb_typeof(tables)='object'),recorded_at timestamptz not null default clock_timestamp()
);
create table runtime.nexloop_restore_drill_results (
 result_id bigint generated always as identity primary key,
 drill_id text not null references runtime.nexloop_restore_drills(drill_id),
 outcome text not null check(outcome in ('passed','failed')),
 rto_seconds numeric not null check(rto_seconds>=0),rpo_seconds numeric not null check(rpo_seconds>=0),
 checks jsonb not null check(jsonb_typeof(checks)='object'),recorded_at timestamptz not null default clock_timestamp()
);

do $tables$
declare t text;
begin
 foreach t in array array['runtime.nexloop_backup_manifests','control.nexloop_restore_journal','runtime.nexloop_restore_drills',
   'runtime.nexloop_restore_drill_results'] loop
  -- Deployment level (no tenant column): forced RLS with an owner-only policy, no application privileges.
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy deployment_backup on %s to nexloop_owner using(true) with check(true)',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_backup_recorder,nexloop_backup',t);
  execute format('create trigger nx035_append_only before update or delete on %s for each row execute function control.nexloop_nx022_append_only()',t);
 end loop;
end $tables$;

-- The owner reads the applied migration list (manifests, the migration gate and the drill compare it); it never writes it.
grant select on control.schema_migrations to nexloop_owner;
-- Catalog state: the applied migration list as (version, checksum) lines, the same in a backup and in its restore.
create function runtime.nexloop_catalog_state() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('revision',coalesce(max(version),'0000'),'count',count(*),
  'digest',encode(sha256(convert_to(coalesce(string_agg(version||':'||checksum,E'\n' order by version),''),'UTF8')),'hex'))
 from control.schema_migrations
$$;
create function runtime.nexloop_cluster_identity() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('system_identifier',(select system_identifier::text from pg_control_system()),
  'timeline',(select timeline_id from pg_control_checkpoint()))
$$;

-- 5. Recorder: manifests ----------------------------------------------------------------------------------------------------
-- p_entry: {backup_id, repo, kind, verify_status, …}. A pg_* backup starts with a pending row (the server records cluster,
-- catalog, deletion watermark and journal position) and ends with one verified or failed row carrying the LSN range, the
-- backup's own system identifier and timeline, and the completion time. Other kinds are recorded in one row. A repeated
-- (repo, backup_id, kind, verify_status) is a replay. Every value is a label, hash, LSN, number or time; nothing else passes.
create function authz.nexloop_record_backup(p_entry jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v_kind text:=p_entry->>'kind';v_status text:=p_entry->>'verify_status';v_allowed text[];v_pg boolean;
 base runtime.nexloop_backup_manifests;cat jsonb;ident jsonb;v_completed timestamptz;v_started timestamptz;v_id bigint;
begin
 if session_user is distinct from 'nexloop_backup_recorder' then raise exception 'backup recorder unavailable' using errcode='42501';end if;
 if jsonb_typeof(p_entry) is distinct from 'object' or v_kind not in ('pg_full','pg_diff','pg_incr','files_artifact','files_runtime','journal')
  or v_status not in ('pending','verified','failed') then raise exception 'backup manifest invalid' using errcode='22023';end if;
 v_pg:=v_kind in ('pg_full','pg_diff','pg_incr');
 v_allowed:=case
  when v_pg and v_status='pending' then array['backup_id','repo','kind','verify_status','release_id','key_ref']
  when v_pg then array['backup_id','repo','kind','verify_status','completed_at','system_identifier','timeline','start_lsn','stop_lsn']
  when v_kind='journal' then array['backup_id','repo','kind','verify_status','started_at','completed_at','journal_seq','key_ref']
  else array['backup_id','repo','kind','verify_status','started_at','completed_at','file_manifest_digest','file_count','bytes','key_ref','release_id'] end;
 if exists(select 1 from jsonb_object_keys(p_entry) k where not k=any(v_allowed))
  or exists(select 1 from jsonb_each(p_entry) e where jsonb_typeof(e.value) not in ('string','number'))
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
    deletion_watermark,journal_seq,key_ref,verify_status)
   values(p_entry->>'backup_id',p_entry->>'repo',v_kind,clock_timestamp(),ident->>'system_identifier',(ident->>'timeline')::integer,cat->>'revision',cat->>'digest',
    p_entry->>'release_id',(select coalesce(max(seq),0) from control.nexloop_deletion_ledger),(select coalesce(max(seq),0) from control.nexloop_restore_journal),
    p_entry->>'key_ref','pending') returning manifest_id into v_id;
 elsif v_pg then
  -- The completed row copies what the server recorded at the start; a backup of another cluster is refused.
  select * into base from runtime.nexloop_backup_manifests where repo=p_entry->>'repo' and backup_id=p_entry->>'backup_id' and kind=v_kind and verify_status='pending';
  if not found or v_completed is null or v_completed<base.started_at or p_entry->>'system_identifier' is distinct from base.system_identifier
   or not (p_entry ? 'start_lsn' and p_entry ? 'stop_lsn' and p_entry ? 'timeline') or (p_entry->>'stop_lsn')::pg_lsn<(p_entry->>'start_lsn')::pg_lsn then
   raise exception 'backup manifest invalid' using errcode='22023';end if;
  insert into runtime.nexloop_backup_manifests(backup_id,repo,kind,started_at,completed_at,system_identifier,timeline,start_lsn,stop_lsn,catalog_revision,
    catalog_digest,release_id,deletion_watermark,journal_seq,key_ref,verify_status,verified_at)
   values(base.backup_id,base.repo,v_kind,base.started_at,v_completed,base.system_identifier,(p_entry->>'timeline')::integer,(p_entry->>'start_lsn')::pg_lsn,
    (p_entry->>'stop_lsn')::pg_lsn,base.catalog_revision,base.catalog_digest,base.release_id,base.deletion_watermark,base.journal_seq,base.key_ref,v_status,clock_timestamp())
   returning manifest_id into v_id;
 else
  if v_status='pending' or v_completed is null or not (p_entry ? 'started_at') then raise exception 'backup manifest invalid' using errcode='22023';end if;
  v_started:=(p_entry->>'started_at')::timestamptz;
  if v_started>v_completed or (v_kind='journal' and not (p_entry ? 'journal_seq'))
   or (v_kind='journal' and (p_entry->>'journal_seq')::bigint>(select coalesce(max(seq),0) from control.nexloop_restore_journal))
   or (v_kind<>'journal' and not (p_entry ?& array['file_manifest_digest','file_count','bytes'])) then
   raise exception 'backup manifest invalid' using errcode='22023';end if;
  insert into runtime.nexloop_backup_manifests(backup_id,repo,kind,started_at,completed_at,system_identifier,timeline,catalog_revision,catalog_digest,release_id,
    deletion_watermark,journal_seq,file_manifest_digest,file_count,bytes,key_ref,verify_status,verified_at)
   values(p_entry->>'backup_id',p_entry->>'repo',v_kind,v_started,v_completed,ident->>'system_identifier',(ident->>'timeline')::integer,cat->>'revision',cat->>'digest',
    p_entry->>'release_id',(select coalesce(max(seq),0) from control.nexloop_deletion_ledger),
    coalesce((p_entry->>'journal_seq')::bigint,(select coalesce(max(seq),0) from control.nexloop_restore_journal)),
    p_entry->>'file_manifest_digest',(p_entry->>'file_count')::bigint,(p_entry->>'bytes')::bigint,p_entry->>'key_ref',v_status,clock_timestamp())
   returning manifest_id into v_id;
 end if;
 return jsonb_build_object('recorded',true,'replay',false,'manifest_id',v_id);
end $$;

-- 6. Restore journal: writers (triggers) and export ---------------------------------------------------------------------
create function control.nexloop_restore_journal_append(p_kind text,p_entry jsonb) returns bigint
 language sql security definer set search_path=pg_catalog,pg_temp as $$
 insert into control.nexloop_restore_journal(kind,entry) values(p_kind,p_entry) returning seq
$$;
create function control.nexloop_journal_on_deletion() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 perform control.nexloop_restore_journal_append('deletion',jsonb_build_object('subject','deletion_ledger','seq',new.seq,'tenant_id',new.tenant_id,
  'world',new.world,'kind',new.kind,'subject_ref',new.subject_ref,'request_ref',new.request_ref,'cutoff',new.cutoff));
 return null;
end $$;
-- A contact restriction travels without the customer's words (matched_text) and without the release reason (free text).
create function control.nexloop_journal_on_contact_restriction() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 if tg_op='UPDATE' and (new.active,new.control_revision,new.released_at) is not distinct from (old.active,old.control_revision,old.released_at) then return null;end if;
 perform control.nexloop_restore_journal_append('contact_restriction',jsonb_build_object('subject','contact_restriction','tenant_id',new.tenant_id,
  'world',new.world,'consumer_id',new.consumer_id,'active',new.active,'control_revision',new.control_revision,'message_id',new.message_id,
  'conversation_id',new.conversation_id,'rule_version',new.rule_version,'rule_id',new.rule_id,'certainty',new.certainty,'restricted_at',new.restricted_at,
  'released_at',new.released_at,'released_by',new.released_by,'release_intent',new.release_intent));
 return null;
end $$;
create function control.nexloop_journal_on_erased_contact_key() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 if tg_op='UPDATE' and new.revoked_at is not distinct from old.revoked_at then return null;end if;
 perform control.nexloop_restore_journal_append('contact_restriction',jsonb_build_object('subject','erased_contact_key','tenant_id',new.tenant_id,
  'world',new.world,'key_digest',new.key_digest,'request_id',new.request_id,'created_at',new.created_at,'revoked_at',new.revoked_at,'revoked_by',new.revoked_by));
 return null;
end $$;
create function control.nexloop_journal_on_service_credential() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 if new.status='revoked' and old.status is distinct from 'revoked' then
  perform control.nexloop_restore_journal_append('revocation',jsonb_build_object('subject','service_credential','tenant_id',new.tenant_id,
   'credential_id',new.credential_id,'revoked_at',clock_timestamp()));
 end if;
 return null;
end $$;
create function control.nexloop_journal_on_browser_membership() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 if new.payload->>'status' in ('suspended','revoked') and (tg_op='INSERT' or old.payload->>'status' is distinct from new.payload->>'status') then
  perform control.nexloop_restore_journal_append('revocation',jsonb_build_object('subject','browser_membership','tenant_id',new.tenant_id,
   'principal_id',new.principal_id,'status',new.payload->>'status','revision',(new.payload->>'revision')::integer,'recorded_at',clock_timestamp()));
 end if;
 return null;
end $$;
create trigger nx035_restore_journal after insert on control.nexloop_deletion_ledger for each row execute function control.nexloop_journal_on_deletion();
create trigger nx035_restore_journal after insert or update on control.nexloop_contact_restrictions for each row execute function control.nexloop_journal_on_contact_restriction();
create trigger nx035_restore_journal after insert or update on control.nexloop_erased_contact_keys for each row execute function control.nexloop_journal_on_erased_contact_key();
create trigger nx035_restore_journal after update on authz.nexloop_service_credentials for each row execute function control.nexloop_journal_on_service_credential();
create trigger nx035_restore_journal after insert or update on control.nexloop_browser_memberships for each row execute function control.nexloop_journal_on_browser_membership();

create function authz.nexloop_restore_journal_since(p_seq bigint,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
begin
 if session_user is distinct from 'nexloop_backup_recorder' then raise exception 'restore journal unavailable' using errcode='42501';end if;
 if p_seq is null or p_seq<0 or p_limit is null or p_limit not between 1 and 10000 then raise exception 'restore journal read invalid' using errcode='22023';end if;
 return jsonb_build_object('after',p_seq,'max_seq',(select coalesce(max(seq),0) from control.nexloop_restore_journal),
  'entries',coalesce((select jsonb_agg(jsonb_build_object('seq',seq,'kind',kind,'entry',entry,'recorded_at',recorded_at) order by seq)
   from (select * from control.nexloop_restore_journal where seq>p_seq order by seq limit p_limit) j),'[]'::jsonb));
end $$;

-- 7. Drills ---------------------------------------------------------------------------------------------------------------
-- The append-only tables a drill compares (single increasing id): count and maximum id at the restore point, and the sha256 of
-- up to five rows chosen at random below that maximum (row_to_json text in UTC).
create function runtime.nexloop_drill_tables() returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select array['control.nexloop_deletion_ledger:seq','control.nexloop_restore_journal:seq','runtime.nexloop_dispatch_refusals:refusal_id',
  'control.nexloop_alert_events:event_id','runtime.nexloop_process_samples:sample_id','runtime.nexloop_backup_manifests:manifest_id']
$$;
create function runtime.nexloop_drill_row_hashes(p_table text,p_ids bigint[]) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v_rel text:=split_part(p_table,':',1);v_col text:=split_part(p_table,':',2);v jsonb;
begin
 if not p_table=any(runtime.nexloop_drill_tables()) then raise exception 'drill table invalid' using errcode='22023';end if;
 execute format('select coalesce(jsonb_object_agg(%1$I::text,encode(sha256(convert_to(row_to_json(t)::text,''UTF8'')),''hex'')),''{}''::jsonb) from %2$s t where %1$I=any($1)',
  v_col,v_rel) into v using p_ids;
 return v;
end $$;
create function runtime.nexloop_drill_table_counts(p_table text,p_max bigint) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_rel text:=split_part(p_table,':',1);v_col text:=split_part(p_table,':',2);v jsonb;
begin
 if not p_table=any(runtime.nexloop_drill_tables()) then raise exception 'drill table invalid' using errcode='22023';end if;
 if p_max is null then
  execute format('select jsonb_build_object(''max_id'',coalesce(max(%1$I),0),''count'',count(*)) from %2$s',v_col,v_rel) into v;
 else
  execute format('select jsonb_build_object(''max_id'',$1,''count'',count(*)) from %2$s where %1$I<=$1',v_col,v_rel) into v using p_max;
 end if;
 return v;
end $$;
create function authz.nexloop_drill_mark(p_drill text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare t text;c jsonb;ids bigint[];v_tables jsonb:='{}'::jsonb;v_lsn pg_lsn;v_point text;cat jsonb;
begin
 if session_user is distinct from 'nexloop_backup_recorder' then raise exception 'drill mark unavailable' using errcode='42501';end if;
 if coalesce(p_drill,'')!~'^[a-z0-9][a-z0-9._-]{0,47}$' then raise exception 'drill invalid' using errcode='22023';end if;
 if exists(select 1 from runtime.nexloop_restore_drills where drill_id=p_drill) then raise exception 'drill exists' using errcode='22023';end if;
 v_point:='drill-'||p_drill;
 -- Counts and samples are taken in this transaction; the restore point follows, so everything counted precedes it.
 foreach t in array runtime.nexloop_drill_tables() loop
  c:=runtime.nexloop_drill_table_counts(t,null);
  execute format('select coalesce(array_agg(id),''{}'') from (select %1$I id from %2$s order by random() limit 5) s',split_part(t,':',2),split_part(t,':',1)) into ids;
  v_tables:=v_tables||jsonb_build_object(t,c||jsonb_build_object('samples',runtime.nexloop_drill_row_hashes(t,ids)));
 end loop;
 cat:=runtime.nexloop_catalog_state();
 v_lsn:=pg_create_restore_point(v_point);
 insert into runtime.nexloop_restore_drills(drill_id,restore_point,restore_lsn,marked_at,tables)
  values(p_drill,v_point,v_lsn,clock_timestamp(),v_tables||jsonb_build_object('catalog',cat));
 return jsonb_build_object('drill_id',p_drill,'restore_point',v_point,'restore_lsn',v_lsn::text,'tables',v_tables,'catalog',cat,'marked_at',clock_timestamp());
end $$;
create function authz.nexloop_record_drill_result(p_drill text,p_outcome text,p_rto numeric,p_rpo numeric,p_checks jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_id bigint;
begin
 if session_user is distinct from 'nexloop_backup_recorder' then raise exception 'drill result unavailable' using errcode='42501';end if;
 -- Check outcomes are booleans or counts keyed by a code; nothing else is accepted.
 if not exists(select 1 from runtime.nexloop_restore_drills where drill_id=p_drill) or p_outcome not in ('passed','failed')
  or p_rto is null or p_rto<0 or p_rpo is null or p_rpo<0 or jsonb_typeof(p_checks) is distinct from 'object'
  or exists(select 1 from jsonb_each(p_checks) e where e.key!~'^[a-z][a-z0-9_]{0,63}$' or jsonb_typeof(e.value) not in ('boolean','number')) then
  raise exception 'drill result invalid' using errcode='22023';end if;
 insert into runtime.nexloop_restore_drill_results(drill_id,outcome,rto_seconds,rpo_seconds,checks) values(p_drill,p_outcome,p_rto,p_rpo,p_checks)
  returning result_id into v_id;
 return jsonb_build_object('recorded',true,'result_id',v_id);
end $$;
-- Run on the restored instance by its owner: the same counts and hashes for ids up to the marked maximum.
create function runtime.nexloop_drill_compare(p_mark jsonb) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare t text;m jsonb;c jsonb;h jsonb;v_counts integer:=0;v_samples integer:=0;v_count_bad integer:=0;v_sample_bad integer:=0;
begin
 foreach t in array runtime.nexloop_drill_tables() loop
  m:=p_mark->'tables'->t;
  if m is null then continue;end if;
  c:=runtime.nexloop_drill_table_counts(t,(m->>'max_id')::bigint);v_counts:=v_counts+1;
  if (c->>'count')::bigint is distinct from (m->>'count')::bigint then v_count_bad:=v_count_bad+1;end if;
  h:=runtime.nexloop_drill_row_hashes(t,(select coalesce(array_agg(k::bigint),'{}') from jsonb_object_keys(m->'samples') k));
  v_samples:=v_samples+(select count(*) from jsonb_object_keys(m->'samples'));
  v_sample_bad:=v_sample_bad+(select count(*) from jsonb_each_text(m->'samples') s where h->>s.key is distinct from s.value);
 end loop;
 return jsonb_build_object('tables_compared',v_counts,'count_mismatches',v_count_bad,'samples_compared',v_samples,'sample_mismatches',v_sample_bad,
  'catalog_equal',runtime.nexloop_catalog_state()->>'digest' is not distinct from p_mark->'catalog'->>'digest');
end $$;

-- 8. M12 backup metrics (numbers only; unavailable — never zero — while no PG backup is recorded) ---------------------------
create function runtime.nexloop_backup_metrics() returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare now_at timestamptz:=clock_timestamp();a pg_stat_archiver;v jsonb;
 last_pg runtime.nexloop_backup_manifests;last_journal runtime.nexloop_backup_manifests;v_journal_max bigint;
begin
 select * into last_pg from runtime.nexloop_backup_manifests where kind in ('pg_full','pg_diff','pg_incr') and verify_status<>'pending'
  order by completed_at desc,manifest_id desc limit 1;
 if not found then return jsonb_build_object('status','unavailable');end if;  -- the NX-030 shape (AT-045)
 select * into a from pg_stat_archiver;
 select * into last_journal from runtime.nexloop_backup_manifests where kind='journal' and verify_status='verified' order by journal_seq desc,manifest_id desc limit 1;
 select coalesce(max(seq),0) into v_journal_max from control.nexloop_restore_journal;
 v:=jsonb_build_object('status','ok',
  -- age_seconds: the latest verified PG backup (the key alert-rules v1/v2 'backup_age' already names).
  'age_seconds',(select round(extract(epoch from now_at-max(completed_at))) from runtime.nexloop_backup_manifests
    where kind in ('pg_full','pg_diff','pg_incr') and verify_status='verified'),
  'full_backup_age_seconds',(select round(extract(epoch from now_at-max(completed_at))) from runtime.nexloop_backup_manifests where kind='pg_full' and verify_status='verified'),
  'repo_backup_age_seconds',coalesce((select jsonb_object_agg(repo,age) from (select repo,round(extract(epoch from now_at-max(completed_at))) age
    from runtime.nexloop_backup_manifests where kind in ('pg_full','pg_diff','pg_incr') and verify_status='verified' group by repo) r),'{}'::jsonb),
  'verify_failed',case when last_pg.verify_status='failed' then 1 else 0 end,
  'wal_last_archived_age_seconds',case when a.last_archived_time is null then null else round(extract(epoch from now_at-a.last_archived_time)) end,
  'wal_archive_failing',case when a.last_failed_time is not null and (a.last_archived_time is null or a.last_failed_time>a.last_archived_time) then 1 else 0 end,
  -- Journal export lag: 0 while every entry is exported; otherwise the age of the oldest entry not yet exported.
  'journal_export_lag_seconds',coalesce((select round(extract(epoch from now_at-min(recorded_at))) from control.nexloop_restore_journal
    where seq>coalesce(last_journal.journal_seq,0)),0),
  'file_backup_age_seconds',(select round(extract(epoch from now_at-max(completed_at))) from runtime.nexloop_backup_manifests
    where kind in ('files_artifact','files_runtime') and verify_status='verified'),
  -- Since the last passed drill or, before any, since the first verified backup: backups without a drill also become overdue.
  'drill_age_seconds',round(extract(epoch from now_at-coalesce((select max(recorded_at) from runtime.nexloop_restore_drill_results where outcome='passed'),
    (select min(completed_at) from runtime.nexloop_backup_manifests where kind in ('pg_full','pg_diff','pg_incr') and verify_status='verified'),now_at))),
  'drill_rpo_seconds',(select rpo_seconds from runtime.nexloop_restore_drill_results order by result_id desc limit 1),
  'drill_rto_seconds',(select rto_seconds from runtime.nexloop_restore_drill_results order by result_id desc limit 1));
 -- A metric with no observation is left out (absent), never reported as zero.
 return jsonb_strip_nulls(v);
end $$;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_backup_v0180;
revoke all on function runtime.nexloop_metrics_snapshot_before_backup_v0180(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return runtime.nexloop_metrics_snapshot_before_backup_v0180(p_tenant,p_world)||jsonb_build_object('backup',runtime.nexloop_backup_metrics());
end $$;

-- 9. Refusal code (the restore fence of 0181 raises it at dispatch) ---------------------------------------------------------
insert into control.nexloop_dispatch_refusal_codes values ('NXR01','restore_unverified');

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_catalog_state()','runtime.nexloop_cluster_identity()','authz.nexloop_record_backup(jsonb)',
   'control.nexloop_restore_journal_append(text,jsonb)','control.nexloop_journal_on_deletion()','control.nexloop_journal_on_contact_restriction()',
   'control.nexloop_journal_on_erased_contact_key()','control.nexloop_journal_on_service_credential()','control.nexloop_journal_on_browser_membership()',
   'authz.nexloop_restore_journal_since(bigint,integer)','runtime.nexloop_drill_tables()','runtime.nexloop_drill_row_hashes(text,bigint[])',
   'runtime.nexloop_drill_table_counts(text,bigint)','authz.nexloop_drill_mark(text)','authz.nexloop_record_drill_result(text,text,numeric,numeric,jsonb)',
   'runtime.nexloop_drill_compare(jsonb)','runtime.nexloop_backup_metrics()','runtime.nexloop_metrics_snapshot(text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
grant execute on function authz.nexloop_record_backup(jsonb) to nexloop_backup_recorder;
grant execute on function authz.nexloop_restore_journal_since(bigint,integer) to nexloop_backup_recorder;
grant execute on function authz.nexloop_drill_mark(text) to nexloop_backup_recorder;
grant execute on function authz.nexloop_record_drill_result(text,text,numeric,numeric,jsonb) to nexloop_backup_recorder;
