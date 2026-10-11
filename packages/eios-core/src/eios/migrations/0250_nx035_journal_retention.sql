-- NX-035 local remainder (temporary number 0250): the deletion records sweep stays below the exported restore journal, and the
-- restore journal gets its own retention. Closes the integration point recorded in 0140 (NX-029 completion, §2).
--
-- 1. runtime.nexloop_journal_exported_seq(): the highest journal sequence a verified journal export recorded
--    (runtime.nexloop_backup_manifests kind 'journal', journal_seq; NX-035 §6.2). Null while nothing was ever exported.
-- 2. records sweep (runtime.nexloop_retention_sweep_records, 0140 body kept as …_before_journal_v0250 for every other class):
--    a ledger entry leaves only when its days are over AND the journal entry that carries it is exported (journal seq at or
--    below the exported sequence; a journal entry already gone was exported, see 3). Nothing was exported: no ledger entry
--    leaves (fail closed; tombstones keep their own rule, they are not journaled). Never while a restore replay is open (as 0140).
-- 3. restore journal retention, in the same pass: journal entries of this tenant and world leave after deletion_ledger_days
--    (the records setting; longer than the backup retention, NX-029 §9 / NX-035 design §5) and only when exported. The
--    journal is append-only; the sweep deletes through a registered erasure scope (0129), like the ledger.
-- Published migrations are not edited.

create function runtime.nexloop_journal_exported_seq() returns bigint
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select max(journal_seq) from runtime.nexloop_backup_manifests where kind='journal' and verify_status='verified'
$$;
alter function runtime.nexloop_journal_exported_seq() owner to nexloop_owner;
revoke all on function runtime.nexloop_journal_exported_seq() from public;

alter function runtime.nexloop_retention_sweep_records(text,text,text,integer,text,text) rename to nexloop_retention_sweep_records_before_journal_v0250;
revoke all on function runtime.nexloop_retention_sweep_records_before_journal_v0250(text,text,text,integer,text,text) from public;
create function runtime.nexloop_retention_sweep_records(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r jsonb:=runtime.nexloop_retention_settings_current()->'records';v_exported bigint;n integer:=0;m integer:=0;j integer:=0;
begin
 if p_class<>'records' then
  return runtime.nexloop_retention_sweep_records_before_journal_v0250(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if not (runtime.nexloop_erasure_readiness()->>'ready')::boolean then
  return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts',jsonb_build_object('replay_open',true));
 end if;
 v_exported:=runtime.nexloop_journal_exported_seq();
 perform set_config('eios.tenant_id',p_tenant,true);
 perform runtime.nexloop_erasure_open('control.nexloop_deletion_ledger',null,true,p_ref);
 perform runtime.nexloop_erasure_open('runtime.nexloop_erasure_tombstones',null,true,p_ref);
 perform runtime.nexloop_erasure_open('control.nexloop_restore_journal',null,true,p_ref);
 if v_exported is not null then
  with due as (select l.seq from control.nexloop_deletion_ledger l where l.tenant_id=p_tenant and l.world=p_world
    and l.recorded_at<clock_timestamp()-make_interval(days=>(r->>'deletion_ledger_days')::integer)
    and not exists(select 1 from control.nexloop_restore_journal x where x.kind='deletion' and x.entry->>'subject'='deletion_ledger'
      and (x.entry->>'seq')::bigint=l.seq and x.seq>v_exported)
    order by l.seq limit p_batch for update skip locked)
  delete from control.nexloop_deletion_ledger l using due where l.seq=due.seq;
  get diagnostics n=row_count;
  -- The journal itself: exported entries of this tenant and world, after the same days.
  with due as (select x.seq from control.nexloop_restore_journal x where x.seq<=v_exported and x.entry->>'tenant_id'=p_tenant and x.entry->>'world'=p_world
    and x.recorded_at<clock_timestamp()-make_interval(days=>(r->>'deletion_ledger_days')::integer) order by x.seq limit p_batch for update skip locked)
  delete from control.nexloop_restore_journal x using due where x.seq=due.seq;
  get diagnostics j=row_count;
 end if;
 with due as (select tombstone_id from runtime.nexloop_erasure_tombstones where tenant_id=p_tenant and world=p_world
   and executed_at<clock_timestamp()-make_interval(days=>(r->>'tombstone_days')::integer) order by executed_at limit p_batch for update skip locked)
 delete from runtime.nexloop_erasure_tombstones t using due where t.tenant_id=p_tenant and t.world=p_world and t.tombstone_id=due.tombstone_id;
 get diagnostics m=row_count;
 perform runtime.nexloop_erasure_close();
 return jsonb_build_object('item_class',p_class,'processed',n+m+j,'more',greatest(n,m,j)>=p_batch,
  'counts',jsonb_build_object('ledger',n,'tombstones',m,'journal',j,'journal_exported',v_exported is not null));
end $$;
alter function runtime.nexloop_retention_sweep_records(text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_sweep_records(text,text,text,integer,text,text) from public;
