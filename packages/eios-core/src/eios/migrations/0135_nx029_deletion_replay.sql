-- NX-029 slice 4 (temporary number 0140): deletion ledger, deletion replay after a restore, erasure readiness and the
-- retention doctor. docs/implementation/NX-029-design.md §9; interface aligned with NX-035 §5–§6.2 (restore journal).
--
-- 1. control.nexloop_deletion_ledger: every deletion as an append-only entry without content (watermark from the global
--    sequence control.nexloop_deletion_watermark): a Consumer erasure once it is no longer pending confirmation, a Message
--    erasure, and each expiry pass of a data class with its cutoff. NX-035 reads it into its restore journal (its own
--    triggers and export) and, after a restore, hands the entries newer than the restored database back to
--    runtime.nexloop_replay_deletions.
-- 2. Replay (owner only; idempotent; by original ids): a Consumer entry erases the Consumer again through the same
--    governed handler (blocking at once, purge by the keeper); a Message entry erases the Message; a class entry makes
--    that class due at once (the sweep recomputes its cutoff, idempotent). runtime.nexloop_replay_begin / _finish
--    bracket a restore's replay; runtime.nexloop_erasure_readiness() is false while a replay is open (NX-035 readiness).
-- 3. Settings v2 (retention.v1.json): records — ledger and tombstones outlive the maximum backup retention (NX-035 D4,
--    35 days); a version that breaks this is refused.
-- 4. Doctor: runtime.nexloop_retention_warnings (body replaced in place) also reports erasures waiting long for unknown
--    effects, Run file purges that keep failing, Artifacts whose deletion keeps failing and classes overdue.
-- Published migrations are not edited.

-- 3. Settings v2 ------------------------------------------------------------------------------------------------------
create function control.nexloop_retention_records_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare r jsonb:=new.definition->'records';
begin
 if new.version>=2 and (jsonb_typeof(r) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(r) k)
   is distinct from array['backup_max_retention_days','deletion_ledger_days','tombstone_days']
   or (r->>'deletion_ledger_days')!~'^[0-9]{1,5}$' or (r->>'tombstone_days')!~'^[0-9]{1,5}$' or (r->>'backup_max_retention_days')!~'^[0-9]{1,4}$'
   or (r->>'backup_max_retention_days')::integer<1
   or (r->>'backup_max_retention_days')::integer>=least((r->>'deletion_ledger_days')::integer,(r->>'tombstone_days')::integer)) then
  raise exception 'retention records invalid: ledger and tombstones must outlive every backup' using errcode='22023';end if;
 return new;
end $$;
alter function control.nexloop_retention_records_check() owner to nexloop_owner;
create trigger nx029_records_check before insert on control.nexloop_retention_settings for each row execute function control.nexloop_retention_records_check();
insert into control.nexloop_retention_settings(version,definition,definition_digest,published_by)
 values(2,'{"classes":{"claim_quote":{"days":90,"source":"message_text"},"commercial_receipt":{"days":90,"source":null},"job_input":{"days":30,"source":"message_text"},"message_text":{"days":90,"source":null},"metric_observation":{"days":365,"source":null},"prompt_text":{"days":30,"source":"message_text"}},"decision":"NX-029 slice 1 (design docs/implementation/NX-029-design.md §3, owner decisions D1–D7 ''all as recommended'' 2026-10-10, dispatcher D8/D9): tenant-level retention (D9) for the classes the retention keeper executes in slice 1. Product defaults, not legal periods (03:96, 13:31); enterprises adjust by a new version. A derived class never outlives its source (prompt text, Claim quotes and job inputs follow the message text). Financial records and cost entries use financial_retention_days of the commercial settings (NX-027 D6), not a second value here. Expired message text, Claim quotes, prompt text and job inputs are redacted in place (rows, digests and order stay); commercial receipts, metric observations, commercial records and cost entries are deleted; every pass leaves a tombstone without content. v2 (NX-029 slice 4, NX-035 D4 confirmed by the owner 2026-10-10): records — deletion ledger and tombstones are kept 365 days, longer than the 35-day maximum backup retention, so every deletion still in a backup can be replayed after a restore (NX-035 §5, §6.2); a version with backup_max_retention_days not below both is refused.","records":{"backup_max_retention_days":35,"deletion_ledger_days":365,"tombstone_days":365},"schema":"nexloop-retention/1","sweep":{"batch":200,"interval_seconds":3600,"max_passes":20},"version":2}'::jsonb,'6429c94bcc14cfdcc5b78e2c3a572689c2b275757632096152362aa6e3b18ae6','deploy/configuration/retention.v1.json');

-- 1. Ledger -----------------------------------------------------------------------------------------------------------
create table control.nexloop_deletion_ledger (
 seq bigint primary key,tenant_id text not null,world text not null,
 kind text not null check(kind in ('consumer','message','item_class')),
 subject_ref text not null check(char_length(subject_ref) between 1 and 200),request_ref text not null check(char_length(request_ref) between 1 and 200),
 cutoff timestamptz,recorded_at timestamptz not null default clock_timestamp(),
 check((kind='item_class')=(cutoff is not null)),unique(tenant_id,world,kind,subject_ref,request_ref)
);
create table control.nexloop_deletion_replays (
 restore_ref text primary key check(restore_ref~'^[A-Za-z0-9._:-]{1,190}$'),from_seq bigint not null,
 state text not null check(state in ('replaying','replayed')),entries integer not null default 0,
 started_at timestamptz not null default clock_timestamp(),finished_at timestamptz,check((state='replayed')=(finished_at is not null))
);
alter table control.nexloop_deletion_ledger owner to nexloop_owner;
-- Owner-only and cross-tenant by design (the restore journal of the whole deployment, NX-035): no application role
-- reaches it, so it carries no tenant policy.
create trigger nx029_append_only before update or delete on control.nexloop_deletion_ledger for each row execute function control.nexloop_nx022_append_only();
alter table control.nexloop_deletion_replays owner to nexloop_owner;
create trigger nx029_no_delete before delete on control.nexloop_deletion_replays for each row execute function control.nexloop_nx022_append_only();
do $revoke$
declare t text;
begin
 foreach t in array array['control.nexloop_deletion_ledger','control.nexloop_deletion_replays'] loop
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $revoke$;

create function runtime.nexloop_deletion_ledger_add(p_tenant text,p_world text,p_kind text,p_subject text,p_request text,p_cutoff timestamptz) returns void
 language sql security definer set search_path=pg_catalog,pg_temp as $$
 insert into control.nexloop_deletion_ledger(seq,tenant_id,world,kind,subject_ref,request_ref,cutoff)
  values(nextval('control.nexloop_deletion_watermark'),p_tenant,p_world,p_kind,p_subject,p_request,p_cutoff) on conflict do nothing
$$;
alter function runtime.nexloop_deletion_ledger_add(text,text,text,text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_deletion_ledger_add(text,text,text,text,text,timestamptz) from public;

-- A Consumer erasure enters the ledger once it is no longer waiting for confirmation (D1: a Consumer's own unconfirmed
-- request deletes nothing and is not replayed).
create function control.nexloop_ledger_on_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if new.state<>'pending_confirmation' and (tg_op='INSERT' or old.state='pending_confirmation') then
  perform runtime.nexloop_deletion_ledger_add(new.tenant_id,new.world,'consumer',new.consumer_id,new.request_id,null);
 end if;
 return null;
end $$;
alter function control.nexloop_ledger_on_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_ledger_on_erasure() from public;
create trigger nx029_ledger after insert or update of state on control.nexloop_consumer_erasures for each row execute function control.nexloop_ledger_on_erasure();
-- Message erasures and expiry passes enter it from their tombstones (written in the same transaction as the deletion).
create function runtime.nexloop_ledger_on_tombstone() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if new.reason='message_erasure' and new.subject_kind='message' then
  perform runtime.nexloop_deletion_ledger_add(new.tenant_id,new.world,'message',new.subject_key,new.request_ref,null);
 elsif new.reason='retention_expiry' and new.action in ('redacted','deleted') and new.item_class=any(runtime.nexloop_retention_classes()) then
  perform runtime.nexloop_deletion_ledger_add(new.tenant_id,new.world,'item_class',new.item_class,new.request_ref,
   new.executed_at-make_interval(days=>runtime.nexloop_retention_days(new.item_class)));
 end if;
 return null;
end $$;
alter function runtime.nexloop_ledger_on_tombstone() owner to nexloop_owner;
revoke all on function runtime.nexloop_ledger_on_tombstone() from public;
create trigger nx029_ledger after insert on runtime.nexloop_erasure_tombstones for each row execute function runtime.nexloop_ledger_on_tombstone();

-- 2. Replay -----------------------------------------------------------------------------------------------------------
create function runtime.nexloop_replay_begin(p_restore text,p_from_seq bigint) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if coalesce(p_restore,'')!~'^[A-Za-z0-9._:-]{1,190}$' or p_from_seq is null or p_from_seq<0 then raise exception 'deletion replay invalid' using errcode='22023';end if;
 insert into control.nexloop_deletion_replays(restore_ref,from_seq,state) values(p_restore,p_from_seq,'replaying') on conflict do nothing;
 return (select jsonb_build_object('restore_ref',restore_ref,'state',state,'from_seq',from_seq) from control.nexloop_deletion_replays where restore_ref=p_restore);
end $$;
create function runtime.nexloop_replay_finish(p_restore text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 update control.nexloop_deletion_replays set state='replayed',finished_at=clock_timestamp() where restore_ref=p_restore and state='replaying';
 if not found and not exists(select 1 from control.nexloop_deletion_replays where restore_ref=p_restore) then
  raise exception 'deletion replay unavailable' using errcode='42501';end if;
 return (select jsonb_build_object('restore_ref',restore_ref,'state',state,'entries',entries) from control.nexloop_deletion_replays where restore_ref=p_restore);
end $$;
-- Entries: [{seq, tenant_id, world, kind consumer|message|item_class, subject_ref, request_ref, cutoff?}] as the
-- restore journal carries them (NX-035 §6.2 'deletion'). Idempotent: an entry already applied changes nothing.
create function runtime.nexloop_replay_deletions(p_restore text,p_entries jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare x jsonb;n_consumer integer:=0;n_message integer:=0;n_class integer:=0;n_skipped integer:=0;r jsonb;
begin
 if not exists(select 1 from control.nexloop_deletion_replays where restore_ref=p_restore and state='replaying') then
  raise exception 'deletion replay not begun' using errcode='42501';end if;
 if jsonb_typeof(p_entries) is distinct from 'array' or jsonb_array_length(p_entries)>1000 then raise exception 'deletion replay invalid' using errcode='22023';end if;
 for x in select value from jsonb_array_elements(p_entries) loop
  if jsonb_typeof(x) is distinct from 'object' or x->>'kind' not in ('consumer','message','item_class') or coalesce(x->>'tenant_id','')=''
   or coalesce(x->>'world','')='' or coalesce(x->>'subject_ref','')='' or coalesce(x->>'request_ref','')!~'^[A-Za-z0-9._:-]{1,200}$' then
   raise exception 'deletion replay entry invalid' using errcode='22023';end if;
  perform set_config('eios.tenant_id',x->>'tenant_id',true);
  if x->>'kind'='consumer' then
   if coalesce(x->>'subject_ref','')!~'^[a-f0-9]{64}$' then raise exception 'deletion replay entry invalid' using errcode='22023';end if;
   if exists(select 1 from ontology.objects where tenant_id=x->>'tenant_id' and world=x->>'world' and type_name='Consumer' and object_id=x->>'subject_ref')
    and not runtime.nexloop_consumer_erasing(x->>'tenant_id',x->>'world',x->>'subject_ref') then
    -- The same governed handler as the owner's Action: blocking at once (contact, plans), then the keeper purges.
    r:=control.nexloop_governed_erase_consumer(x->>'tenant_id',x->>'world','restore:'||p_restore,'service',x->>'request_ref',
     jsonb_build_object('operation','erase_consumer','consumer_id',x->>'subject_ref','reason','deletion replayed after restore '||p_restore));
    n_consumer:=n_consumer+1;
   else n_skipped:=n_skipped+1;end if;
  elsif x->>'kind'='message' then
   if exists(select 1 from runtime.nexloop_conversation_messages where tenant_id=x->>'tenant_id' and world=x->>'world' and message_id=x->>'subject_ref' and record->>'body'<>'')
    or exists(select 1 from ontology.objects where tenant_id=x->>'tenant_id' and world=x->>'world' and type_name='Message' and object_id=x->>'subject_ref') then
    perform runtime.nexloop_erase_message(x->>'tenant_id',x->>'world',x->>'subject_ref',x->>'request_ref','message_erasure','restore:'||p_restore);
    n_message:=n_message+1;
   else n_skipped:=n_skipped+1;end if;
  else
   -- The class is due at once; its sweep recomputes the cutoff from now, which covers the journal's cutoff.
   update runtime.nexloop_retention_sweeps set last_completed_at=null where tenant_id=x->>'tenant_id' and world=x->>'world' and item_class=x->>'subject_ref';
   n_class:=n_class+1;
  end if;
 end loop;
 update control.nexloop_deletion_replays set entries=entries+jsonb_array_length(p_entries) where restore_ref=p_restore;
 return jsonb_build_object('restore_ref',p_restore,'consumers',n_consumer,'messages',n_message,'classes',n_class,'already_applied',n_skipped);
end $$;
-- Ledger entries after a sequence, for the restore journal export (NX-035) and for checks; no content.
create function runtime.nexloop_deletion_ledger_since(p_seq bigint,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if p_seq is null or p_limit is null or p_limit not between 1 and 1000 then raise exception 'deletion ledger read invalid' using errcode='22023';end if;
 return (select coalesce(jsonb_agg(jsonb_build_object('seq',seq,'tenant_id',tenant_id,'world',world,'kind',kind,'subject_ref',subject_ref,
   'request_ref',request_ref,'cutoff',runtime.nexloop_commercial_ts(cutoff)) order by seq),'[]'::jsonb)
  from (select * from control.nexloop_deletion_ledger where seq>p_seq order by seq limit p_limit) l);
end $$;
create function runtime.nexloop_erasure_readiness() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('ready',not exists(select 1 from control.nexloop_deletion_replays where state='replaying'),
  'replaying',(select coalesce(jsonb_agg(restore_ref order by started_at),'[]'::jsonb) from control.nexloop_deletion_replays where state='replaying'))
$$;
do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_replay_begin(text,bigint)','runtime.nexloop_replay_finish(text)','runtime.nexloop_replay_deletions(text,jsonb)',
   'runtime.nexloop_deletion_ledger_since(bigint,integer)','runtime.nexloop_erasure_readiness()'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;

-- 4. Doctor --------------------------------------------------------------------------------------------------------------
create or replace function runtime.nexloop_retention_warnings(p_tenant text,p_world text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce((select jsonb_agg(jsonb_build_object('code','financial_retention_shorter_than_maturity','metric_id',m.metric_id,'metric_version',m.version,
   'maturity_days',ceil(m.maturity_seconds/86400.0)::integer,'financial_retention_days',runtime.nexloop_retention_days('financial')) order by m.metric_id,m.version)
  from control.nexloop_metric_definitions m join control.nexloop_metric_sources s on s.tenant_id=m.tenant_id and s.world=m.world and s.metric_id=m.metric_id
   and s.metric_version=m.version
  where m.tenant_id=p_tenant and m.world=p_world and m.maturity_seconds>runtime.nexloop_retention_days('financial')*86400),'[]'::jsonb)
 ||coalesce((select jsonb_agg(jsonb_build_object('code','erasure_waiting_for_unknown_effects','request_id',e.request_id,'since',runtime.nexloop_commercial_ts(e.updated_at))
   order by e.updated_at) from control.nexloop_consumer_erasures e where e.tenant_id=p_tenant and e.world=p_world and e.state='waiting_unknown_effects'
   and e.updated_at<clock_timestamp()-interval '24 hours'),'[]'::jsonb)
 ||coalesce((select jsonb_agg(jsonb_build_object('code','run_purge_failing','run_id',i.run_id,'attempts',i.attempts) order by i.created_at)
   from runtime.nexloop_run_purge_items i where i.tenant_id=p_tenant and i.world=p_world and i.state='pending' and i.attempts>=3),'[]'::jsonb)
 ||coalesce((select jsonb_agg(jsonb_build_object('code','artifact_deletion_failing','artifact_id',a.artifact_id) order by a.artifact_id)
   from runtime.nexloop_local_artifacts a where a.tenant_id=p_tenant and a.world=p_world and a.status='deleting'
   and a.lease_until<clock_timestamp()-interval '1 hour'),'[]'::jsonb)
 ||coalesce((select jsonb_agg(jsonb_build_object('code','retention_class_overdue','item_class',w.item_class,
   'last_completed_at',runtime.nexloop_commercial_ts(w.last_completed_at)) order by w.item_class)
   from runtime.nexloop_retention_sweeps w where w.tenant_id=p_tenant and w.world=p_world
   and coalesce(w.last_completed_at,w.last_started_at)<clock_timestamp()-make_interval(secs=>3*(runtime.nexloop_retention_settings_current()->'sweep'->>'interval_seconds')::integer)),'[]'::jsonb)
$$;
