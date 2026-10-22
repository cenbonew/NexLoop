-- NX-054 P2 (ADR-038 §2.2, decision 5 option a; design docs/design/relation-types-design.md §4.1, N-1 to N-3): relations are
-- derived from the objects' reference properties, in the same transaction, at commit. Temporary number; the dispatcher renumbers.
-- P2 derives only: no backfill of existing objects (P3), no reader switched (P4) - the read side keeps 0315's property rule.
--
-- 1. ontology.nexloop_relation_derive: a DEFERRABLE INITIALLY DEFERRED constraint trigger after insert or update of properties on
--    ontology.objects (N-2: at commit it sees targets written later in the same transaction, and the row's final properties). For
--    every published relation type whose source type is the object's type and which names a source_property (the latest version
--    per name, control.nexloop_relation_type_profiles):
--      the property holds a 64-hex id of an existing object of the target type in the same tenant and world -> that relation is
--        put (idempotent, the 0016 relation id) and any other relation of that name from this object is dropped;
--      the property is empty, changed to something else, or names nothing that exists -> the relation of that name from this
--        object is dropped; a value naming nothing (dangling) is counted in runtime.nexloop_relation_derivation_dangling.
--    Cardinality: a one_to_one or one_to_many type whose target already has another source refuses the commit (42501 relation
--    cardinality conflict) - never skipped, a missing relation would be a missed read (the first batch is all many_to_one).
-- 2. ontology.nexloop_relation_endpoint_delete: before delete on ontology.objects, the relations with the object at either end are
--    deleted (N-1: a delete path that does not know relations - 0129's CommercialRecord retention, a whole sim/shadow world - is
--    never stopped by the end-point foreign keys).
-- Objects written with session_replication_role=replica (restores, seeds) derive nothing; P3's backfill covers what exists.
-- Published migrations are not edited.

create table runtime.nexloop_relation_derivation_dangling (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,relation_name text not null,
 dangling bigint not null default 0 check(dangling>=0),last_source_type text,last_seen_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,relation_name));
alter table runtime.nexloop_relation_derivation_dangling owner to nexloop_owner;
alter table runtime.nexloop_relation_derivation_dangling enable row level security;
alter table runtime.nexloop_relation_derivation_dangling force row level security;
create policy tenant_boundary on runtime.nexloop_relation_derivation_dangling to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_relation_derivation_dangling from public;
create index nexloop_relation_type_profiles_source on control.nexloop_relation_type_profiles(tenant_id,relation_name,version) where source_property is not null;
create index ontology_relations_source_world on ontology.relations(tenant_id,world,source_type,source_object_id,relation_name);

create function ontology.nexloop_relation_derive() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);o ontology.objects;p record;v text;v_id text;v_kept integer;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 -- The row as committed (a later statement of the transaction may have changed or deleted it).
 select * into o from ontology.objects where tenant_id=new.tenant_id and type_name=new.type_name and object_id=new.object_id;
 if found then
  for p in select distinct on (f.relation_name) f.relation_name,f.version,f.source_property,t.definition->>'target_type' target_type,
     t.definition->>'cardinality' cardinality
    from control.nexloop_relation_type_profiles f join ontology.relation_type_versions t on t.tenant_id=f.tenant_id and t.relation_name=f.relation_name and t.version=f.version
    where f.tenant_id=o.tenant_id and f.source_property is not null and t.definition->>'source_type'=o.type_name
    order by f.relation_name,f.version desc loop
   v:=o.properties->>p.source_property;
   v_kept:=0;
   if v~'^[a-f0-9]{64}$' and exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world=o.world and t.type_name=p.target_type and t.object_id=v) then
    v_id:=encode(sha256(convert_to(jsonb_build_array(o.tenant_id,o.world,p.relation_name,o.type_name,o.object_id,p.target_type,v)::text,'UTF8')),'hex');
    if p.cardinality in ('one_to_one','one_to_many') then
     perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(o.tenant_id,o.world,p.relation_name)::text,0));
     if exists(select 1 from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world and r.relation_name=p.relation_name
        and r.target_type=p.target_type and r.target_object_id=v and r.source_object_id<>o.object_id) then
      raise exception 'relation cardinality conflict' using errcode='42501',detail=p.relation_name;end if;
    end if;
    insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,metadata,created_at)
     values(v_id,o.tenant_id,o.world,p.relation_name,p.version,o.type_name,o.object_id,p.target_type,v,'{}'::jsonb,clock_timestamp())
     on conflict do nothing;
    v_kept:=1;
   elsif v is not null and v<>'' then
    insert into runtime.nexloop_relation_derivation_dangling(tenant_id,world,relation_name,dangling,last_source_type)
     values(o.tenant_id,o.world,p.relation_name,1,o.type_name)
     on conflict(tenant_id,world,relation_name) do update set dangling=runtime.nexloop_relation_derivation_dangling.dangling+1,
      last_source_type=excluded.last_source_type,last_seen_at=clock_timestamp();
   end if;
   -- Every other relation of this name from this object goes (the property changed, was cleared or names nothing).
   delete from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world and r.source_type=o.type_name and r.source_object_id=o.object_id
    and r.relation_name=p.relation_name and (v_kept=0 or r.target_object_id is distinct from v);
  end loop;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function ontology.nexloop_relation_derive() owner to nexloop_owner;
revoke all on function ontology.nexloop_relation_derive() from public;
create constraint trigger nexloop_relation_derive after insert or update of properties on ontology.objects
 deferrable initially deferred for each row execute function ontology.nexloop_relation_derive();

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

-- Read only: the dangling references derivation met, per relation type (P2 acceptance: 0 after the production write paths).
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
