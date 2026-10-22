-- NX-054 P2 (ADR-038 §2.2, decision 5 option a; design docs/design/relation-types-design.md §4.1, N-1 to N-3; L6 review of
-- 321948ad I-1, I-2, I-5, P-0 to P-2): relations are derived from the objects' reference properties, in the same transaction, at
-- commit. Temporary number; the dispatcher renumbers. P2 derives only: no backfill of existing objects (P3), no reader switched
-- (P4) - the read side keeps 0315's property rule.
--
-- 1. control.nexloop_relation_derivations: the derivation map, one row per tenant and relation name for its LATEST published
--    version (I-1: a newer version without source_property stops the derivation - it never falls back to an older version),
--    with the source type, target type, property and cardinality copied from the definition so the trigger looks a type up by
--    an index (P-0). Kept by a trigger on control.nexloop_relation_type_profiles (the P1 publication) and filled once here.
-- 2. ontology.nexloop_relation_derive: DEFERRABLE INITIALLY DEFERRED constraint triggers on ontology.objects - after insert, and
--    after update when the properties actually changed (P-1: a WHEN clause, so an update that leaves them equal queues nothing).
--    At commit (N-2: targets written later in the transaction are seen; the row's final properties count), for an object type
--    without any derivation row the function returns at once (P-0); otherwise, per derivation of its type:
--      source_property names a 64-hex id of an existing object of the target type in the same tenant and world -> the relation
--        is put with metadata {"derived": true} (idempotent, the 0016 relation id);
--      on update, derived relations of that name from this object that no longer match are dropped (P-2: an insert has none);
--      a value naming nothing is counted in runtime.nexloop_relation_derivation_dangling, except an erasure pseudonym
--        ('pseudonym:' prefix, I-5), which is only dropped;
--      a latest version without source_property drops the derived relations of that name from the object (I-1).
--    Only relations marked derived are ever dropped by derivation (I-2): a relation written through the governed link entry
--    (0016) is left alone, and a derived relation removed through the governed unlink comes back with the next change of the
--    property, which still names its target.
--    Cardinality: a one_to_one or one_to_many type whose target already has another source refuses the commit (42501 relation
--    cardinality conflict) - never skipped (the first batch is all many_to_one). The check relies on READ COMMITTED (the
--    advisory lock then a fresh snapshot per statement); see docs/implementation/NX-054-P2.md §1 for the isolation note (I-4).
-- 3. ontology.nexloop_relation_endpoint_delete: before delete on ontology.objects, the relations with the object at either end are
--    deleted (N-1: 0129's CommercialRecord retention, a whole sim/shadow world, an erasure - none is stopped by the end-point keys).
-- Objects written with session_replication_role=replica (restores, seeds) derive nothing; P3's backfill covers what exists.
-- Published migrations are not edited.

create table control.nexloop_relation_derivations (
 tenant_id text not null references control.nexloop_tenants(tenant_id),relation_name text not null,version integer not null,
 source_type text not null,target_type text not null,source_property text,cardinality text not null,
 primary key(tenant_id,relation_name));
create index nexloop_relation_derivations_source on control.nexloop_relation_derivations(tenant_id,source_type);
create table runtime.nexloop_relation_derivation_dangling (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,relation_name text not null,
 dangling bigint not null default 0 check(dangling>=0),last_source_type text,last_seen_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,relation_name));
do $$declare t text;begin
 foreach t in array array['control.nexloop_relation_derivations','runtime.nexloop_relation_derivation_dangling'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public',t);
 end loop;
end $$;
create index ontology_relations_source_world on ontology.relations(tenant_id,world,source_type,source_object_id,relation_name);

-- 1. The map follows the publication: a profile of a newer (or the first) version replaces the name's row.
create function control.nexloop_relation_derivation_map() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare d jsonb;
begin
 select definition into d from ontology.relation_type_versions where tenant_id=new.tenant_id and relation_name=new.relation_name and version=new.version;
 insert into control.nexloop_relation_derivations(tenant_id,relation_name,version,source_type,target_type,source_property,cardinality)
  values(new.tenant_id,new.relation_name,new.version,d->>'source_type',d->>'target_type',new.source_property,d->>'cardinality')
  on conflict(tenant_id,relation_name) do update set version=excluded.version,source_type=excluded.source_type,target_type=excluded.target_type,
   source_property=excluded.source_property,cardinality=excluded.cardinality
   where control.nexloop_relation_derivations.version<=excluded.version;
 return null;
end $$;
alter function control.nexloop_relation_derivation_map() owner to nexloop_owner;
revoke all on function control.nexloop_relation_derivation_map() from public;
create trigger nexloop_relation_derivation_map after insert on control.nexloop_relation_type_profiles for each row execute function control.nexloop_relation_derivation_map();
-- Fill once from what P1 already published (the latest version per name).
set local row_security=off;
insert into control.nexloop_relation_derivations(tenant_id,relation_name,version,source_type,target_type,source_property,cardinality)
select distinct on (f.tenant_id,f.relation_name) f.tenant_id,f.relation_name,f.version,t.definition->>'source_type',t.definition->>'target_type',
  f.source_property,t.definition->>'cardinality'
 from control.nexloop_relation_type_profiles f join ontology.relation_type_versions t using(tenant_id,relation_name,version)
 order by f.tenant_id,f.relation_name,f.version desc;
set local row_security=on;

-- 2. Derivation at commit.
create function ontology.nexloop_relation_derive() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);o ontology.objects;p record;v text;v_id text;v_ok boolean;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 -- P-0: a type nothing derives from costs one index probe.
 if not exists(select 1 from control.nexloop_relation_derivations m where m.tenant_id=new.tenant_id and m.source_type=new.type_name) then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return null;
 end if;
 -- The row as committed (a later statement of the transaction may have changed or deleted it).
 select * into o from ontology.objects where tenant_id=new.tenant_id and type_name=new.type_name and object_id=new.object_id;
 if found then
  for p in select m.relation_name,m.version,m.source_property,m.target_type,m.cardinality from control.nexloop_relation_derivations m
    where m.tenant_id=o.tenant_id and m.source_type=o.type_name loop
   v:=case when p.source_property is null then null else o.properties->>p.source_property end;
   v_ok:=v~'^[a-f0-9]{64}$' and exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world=o.world and t.type_name=p.target_type and t.object_id=v);
   if v_ok then
    v_id:=encode(sha256(convert_to(jsonb_build_array(o.tenant_id,o.world,p.relation_name,o.type_name,o.object_id,p.target_type,v)::text,'UTF8')),'hex');
    if p.cardinality in ('one_to_one','one_to_many') then
     perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(o.tenant_id,o.world,p.relation_name)::text,0));
     if exists(select 1 from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world and r.relation_name=p.relation_name
        and r.target_type=p.target_type and r.target_object_id=v and r.source_object_id<>o.object_id) then
      raise exception 'relation cardinality conflict' using errcode='42501',detail=p.relation_name;end if;
    end if;
    insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,metadata,created_at)
     values(v_id,o.tenant_id,o.world,p.relation_name,p.version,o.type_name,o.object_id,p.target_type,v,'{"derived":true}'::jsonb,clock_timestamp())
     on conflict do nothing;
   elsif v is not null and v<>'' and v not like 'pseudonym:%' then
    insert into runtime.nexloop_relation_derivation_dangling(tenant_id,world,relation_name,dangling,last_source_type)
     values(o.tenant_id,o.world,p.relation_name,1,o.type_name)
     on conflict(tenant_id,world,relation_name) do update set dangling=runtime.nexloop_relation_derivation_dangling.dangling+1,
      last_source_type=excluded.last_source_type,last_seen_at=clock_timestamp();
   end if;
   -- P-2: an inserted object has no earlier relation. On update, the derived relations of this name that no longer match go;
   -- a relation written through the governed link entry (not marked derived) stays (I-2).
   if tg_op='UPDATE' then
    delete from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world and r.source_type=o.type_name and r.source_object_id=o.object_id
     and r.relation_name=p.relation_name and r.metadata @> '{"derived":true}' and (not coalesce(v_ok,false) or r.target_object_id is distinct from v);
   end if;
  end loop;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function ontology.nexloop_relation_derive() owner to nexloop_owner;
revoke all on function ontology.nexloop_relation_derive() from public;
create constraint trigger nexloop_relation_derive after insert on ontology.objects
 deferrable initially deferred for each row execute function ontology.nexloop_relation_derive();
create constraint trigger nexloop_relation_derive_update after update of properties on ontology.objects
 deferrable initially deferred for each row when (old.properties is distinct from new.properties) execute function ontology.nexloop_relation_derive();

-- 3. Either end deleted: its relations go first.
create function ontology.nexloop_relation_endpoint_delete() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',old.tenant_id,true);  -- the relations' tenant policy
 delete from ontology.relations r where r.tenant_id=old.tenant_id
  and ((r.source_type=old.type_name and r.source_object_id=old.object_id) or (r.target_type=old.type_name and r.target_object_id=old.object_id));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return old;
end $$;
alter function ontology.nexloop_relation_endpoint_delete() owner to nexloop_owner;
revoke all on function ontology.nexloop_relation_endpoint_delete() from public;
create trigger nexloop_relation_endpoint_delete before delete on ontology.objects for each row execute function ontology.nexloop_relation_endpoint_delete();

-- Read only: the dangling references derivation met, per relation type (P2 acceptance: 0 after the production write paths). No
-- grant: when a reader is granted, the tenant must come from its server-side context, not this parameter.
create function runtime.nexloop_relation_dangling(p_tenant text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);out jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select coalesce(jsonb_object_agg(world||':'||relation_name,dangling),'{}'::jsonb) into out from runtime.nexloop_relation_derivation_dangling where tenant_id=p_tenant;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return out;
end $$;
alter function runtime.nexloop_relation_dangling(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_relation_dangling(text) from public;
