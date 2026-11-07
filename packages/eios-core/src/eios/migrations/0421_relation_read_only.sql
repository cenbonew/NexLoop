-- NX-054 P5 (docs/design/relation-read-p4.md §1 P5, docs/design/relation-read-p5.md; owner confirmed 2026-11-07, L6 conditions C1/C2):
-- the erasing readers stop using the property rule as a fallback for an object whose ownership relation types are all backfilled,
-- and resolve its Consumers through the ownership relations only. The three readers - the read guard runtime.nexloop_assert_not_erasing,
-- the workbench object_fields authz.nexloop_workbench_object_fields (both 0357) and the workbench member read
-- authz.nexloop_workbench_member_may_read (0387) - call runtime.nexloop_object_consumers_read, so this migration changes that one
-- function and no reader body.
-- 1. control.nexloop_relation_backfill_coverage: per relation type version, the backfill runs that completed over it. A run reaches
--    completed only with every cursor done and nothing skipped (0349 complete), and only while the derivation map is the one it
--    started from (the gate stops it on catalog_changed): so at that moment every current derivation row is backfilled. A trigger
--    on the run's state records the rows; reverting or reverted removes that run's rows (its relations are being deleted). Rows of
--    completed runs already in place are recorded here when the derivation map still has the run's digest.
-- 2. The P4a union is kept as runtime.nexloop_object_consumers_read_p4union (rollback: docs/design/relation-read-p5-rollback.sql).
-- 3. The new runtime.nexloop_object_consumers_read: the Consumer itself; a tenant without a published relation catalog: the property
--    rule as before; else the relations (0319), united with the property rule (0315) while an ownership relation type on the
--    object's chain (its source type and every type an ownership derivation reaches from it) has a derivation with a source property
--    that no completed backfill covers - a type published after the backfill (C1: catalog version 2's assessment_evidence, any later
--    ownership type) keeps the fallback until its own backfill completes. A RelationshipAssessment adds its source_id and target_id by
--    property always (no relation in the first batch, ADR-038 §3 point 6), and the whole property rule while assessment_evidence
--    has no derivation (deferred on stage).
-- The purge keeps relations united with properties (0361, not touched); the daily gap check and its alert stay. Verify balance is
-- not recomputed here (a full scan per guarded read); the completed run's rescan with nothing skipped is the code-level condition,
-- the daily gap check the alarm. Temporary number; the dispatcher renumbers.

create table control.nexloop_relation_backfill_coverage (
 tenant_id text not null,relation_name text not null,version integer not null,backfill_run text not null,
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,relation_name,version,backfill_run),
 foreign key(tenant_id,backfill_run) references control.nexloop_relation_backfill_runs(tenant_id,backfill_run));
alter table control.nexloop_relation_backfill_coverage owner to nexloop_owner;
alter table control.nexloop_relation_backfill_coverage enable row level security;
alter table control.nexloop_relation_backfill_coverage force row level security;
create policy tenant_boundary on control.nexloop_relation_backfill_coverage to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_relation_backfill_coverage from public;

create function control.nexloop_relation_backfill_record_coverage() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if new.state='completed' and old.state is distinct from 'completed'
    and control.nexloop_relation_backfill_digest(new.tenant_id)=new.derivations_digest then
  insert into control.nexloop_relation_backfill_coverage(tenant_id,relation_name,version,backfill_run)
   select d.tenant_id,d.relation_name,d.version,new.backfill_run from control.nexloop_relation_derivations d
    where d.tenant_id=new.tenant_id and d.source_property is not null
   on conflict do nothing;
 elsif new.state in ('reverting','reverted') then
  delete from control.nexloop_relation_backfill_coverage where tenant_id=new.tenant_id and backfill_run=new.backfill_run;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function control.nexloop_relation_backfill_record_coverage() owner to nexloop_owner;
revoke all on function control.nexloop_relation_backfill_record_coverage() from public;
create trigger nexloop_relation_backfill_coverage after update of state on control.nexloop_relation_backfill_runs
 for each row execute function control.nexloop_relation_backfill_record_coverage();

-- Runs completed before this migration (stage: p3-2 on 2026-10-28), when the derivation map is still the run's.
do $$declare r record;begin
 for r in select tenant_id,backfill_run,derivations_digest from control.nexloop_relation_backfill_runs where state='completed' loop
  perform set_config('eios.tenant_id',r.tenant_id,true);
  if control.nexloop_relation_backfill_digest(r.tenant_id)=r.derivations_digest then
   insert into control.nexloop_relation_backfill_coverage(tenant_id,relation_name,version,backfill_run)
    select d.tenant_id,d.relation_name,d.version,r.backfill_run from control.nexloop_relation_derivations d
     where d.tenant_id=r.tenant_id and d.source_property is not null
    on conflict do nothing;
  end if;
 end loop;
 perform set_config('eios.tenant_id','',true);
end $$;

-- Whether the property rule must still be united for an object of this type (the caller has set eios.tenant_id, 0315).
create function runtime.nexloop_object_ownership_uncovered(p_tenant text,p_type text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 with recursive reach(type_name) as (
   select p_type
   union
   select d.target_type from reach r join control.nexloop_relation_derivations d on d.tenant_id=p_tenant and d.source_type=r.type_name
    join control.nexloop_relation_type_profiles p on p.tenant_id=d.tenant_id and p.relation_name=d.relation_name and p.version=d.version
     and p.consumer_ownership is not null)
 select exists(select 1 from reach r join control.nexloop_relation_derivations d on d.tenant_id=p_tenant and d.source_type=r.type_name
    join control.nexloop_relation_type_profiles p on p.tenant_id=d.tenant_id and p.relation_name=d.relation_name and p.version=d.version
     and p.consumer_ownership is not null
   where d.source_property is not null
    and not exists(select 1 from control.nexloop_relation_backfill_coverage c
     where c.tenant_id=d.tenant_id and c.relation_name=d.relation_name and c.version=d.version))
  or (p_type='RelationshipAssessment' and not exists(select 1 from control.nexloop_relation_derivations d
   where d.tenant_id=p_tenant and d.relation_name='assessment_evidence'))
$$;
alter function runtime.nexloop_object_ownership_uncovered(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_object_ownership_uncovered(text,text) from public;

alter function runtime.nexloop_object_consumers_read(text,text,text,text) rename to nexloop_object_consumers_read_p4union;
revoke all on function runtime.nexloop_object_consumers_read_p4union(text,text,text,text) from public;

create function runtime.nexloop_object_consumers_read(p_tenant text,p_world text,p_type text,p_object text) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare out text[];
begin
 if p_type='Consumer' then return array[p_object];end if;
 -- A tenant without a published relation catalog has nothing to read through.
 if not exists(select 1 from control.nexloop_relation_catalogs c where c.tenant_id=p_tenant) then
  return runtime.nexloop_object_consumers(p_tenant,p_world,p_type,p_object);
 end if;
 out:=runtime.nexloop_relation_object_consumers(p_tenant,p_world,p_type,p_object);
 if runtime.nexloop_object_ownership_uncovered(p_tenant,p_type) then
  -- C1: an ownership type on this object's chain is published but not yet backfilled: the property rule still counts.
  out:=out||runtime.nexloop_object_consumers(p_tenant,p_world,p_type,p_object);
 elsif p_type='RelationshipAssessment' then
  out:=out||coalesce((select array_remove(array[o.properties->>'source_id',o.properties->>'target_id'],null) from ontology.objects o
   where o.tenant_id=p_tenant and o.world=p_world and o.type_name=p_type and o.object_id=p_object),'{}');
 end if;
 return coalesce((select array_agg(distinct x order by x) from unnest(out) x where x~'^[a-f0-9]{64}$'),'{}');
end $$;
alter function runtime.nexloop_object_consumers_read(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_object_consumers_read(text,text,text,text) from public;
