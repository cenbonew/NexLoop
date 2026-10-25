-- NX-054 P2.1 (docs/design/relation-backfill-p3.md §2; L6 reviews of the P3 design v1-v3, S-7): the per-object, per-relation body
-- of the P2 derivation (0321) as one internal function, so that the commit-time triggers and the P3 backfill derive by the same
-- rules. Behaviour unchanged: every P2 test is the regression. Temporary number; the dispatcher renumbers.
--
-- ontology.nexloop_relation_derive_one(o, relation, marker, drop_stale)
--   o           the object row as committed;
--   relation    a relation name of control.nexloop_relation_derivations for o's type (the latest published version);
--   marker      the metadata of a relation it creates: the triggers pass {"derived":true}, the backfill
--               {"derived":true,"backfill":"<run id>"} (it must contain derived:true, or derivation could never drop it);
--   drop_stale  the triggers pass true on update: the derived relations of this name from o that no longer match go.
--   Returns 'created' (a relation was inserted), 'existing' (it was there), 'dangling' (a value that names no object of the target
--   type in o's world; counted as in 0321), 'none' (no value, a pseudonym, not a 64-hex id, no source_property) or 'unknown'
--   (no derivation of that name for o's type).
-- The trigger function becomes ontology.nexloop_relation_derive_each(): the same tenant setting, the P-0 short cut and the row as
-- committed, then derive_one per derivation of the type. The 0321 triggers keep their names and are re-created on it; the 0321
-- function is dropped. Neither function is granted to any role.

create function ontology.nexloop_relation_derive_one(o ontology.objects,p_relation text,p_marker jsonb,p_drop_stale boolean)
 returns text language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p record;v text;v_id text;v_ok boolean;v_result text;
begin
 if not coalesce(p_marker @> '{"derived":true}'::jsonb,false) then
  raise exception 'relation derivation marker' using errcode='22023';end if;
 select m.relation_name,m.version,m.source_property,m.target_type,m.cardinality into p from control.nexloop_relation_derivations m
  where m.tenant_id=o.tenant_id and m.relation_name=p_relation and m.source_type=o.type_name;
 if not found then return 'unknown';end if;
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
   values(v_id,o.tenant_id,o.world,p.relation_name,p.version,o.type_name,o.object_id,p.target_type,v,p_marker,clock_timestamp())
   on conflict do nothing;
  v_result:=case when found then 'created' else 'existing' end;
 elsif v is not null and v<>'' and v not like 'pseudonym:%' then
  insert into runtime.nexloop_relation_derivation_dangling(tenant_id,world,relation_name,dangling,last_source_type)
   values(o.tenant_id,o.world,p.relation_name,1,o.type_name)
   on conflict(tenant_id,world,relation_name) do update set dangling=runtime.nexloop_relation_derivation_dangling.dangling+1,
    last_source_type=excluded.last_source_type,last_seen_at=clock_timestamp();
  v_result:='dangling';
 else
  v_result:='none';
 end if;
 -- P-2: an inserted object has no earlier relation. On update, the derived relations of this name that no longer match go;
 -- a relation written through the governed link entry (not marked derived) stays (I-2).
 if p_drop_stale then
  delete from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world and r.source_type=o.type_name and r.source_object_id=o.object_id
   and r.relation_name=p.relation_name and r.metadata @> '{"derived":true}' and (not coalesce(v_ok,false) or r.target_object_id is distinct from v);
 end if;
 return v_result;
end $$;
alter function ontology.nexloop_relation_derive_one(ontology.objects,text,jsonb,boolean) owner to nexloop_owner;
revoke all on function ontology.nexloop_relation_derive_one(ontology.objects,text,jsonb,boolean) from public;

create function ontology.nexloop_relation_derive_each() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);o ontology.objects;p record;
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
  for p in select m.relation_name from control.nexloop_relation_derivations m where m.tenant_id=o.tenant_id and m.source_type=o.type_name loop
   perform ontology.nexloop_relation_derive_one(o,p.relation_name,'{"derived":true}'::jsonb,tg_op='UPDATE');
  end loop;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function ontology.nexloop_relation_derive_each() owner to nexloop_owner;
revoke all on function ontology.nexloop_relation_derive_each() from public;

drop trigger nexloop_relation_derive on ontology.objects;
drop trigger nexloop_relation_derive_update on ontology.objects;
create constraint trigger nexloop_relation_derive after insert on ontology.objects
 deferrable initially deferred for each row execute function ontology.nexloop_relation_derive_each();
create constraint trigger nexloop_relation_derive_update after update of properties on ontology.objects
 deferrable initially deferred for each row when (old.properties is distinct from new.properties) execute function ontology.nexloop_relation_derive_each();
drop function ontology.nexloop_relation_derive();
