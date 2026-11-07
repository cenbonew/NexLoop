-- NX-054 P5 (DRAFT; docs/design/relation-read-p4.md §1 P5, docs/design/relation-read-p5.md): the erasing read guard
-- (runtime.nexloop_assert_not_erasing) and the workbench's object_fields (authz.nexloop_workbench_object_fields) stop using the
-- property rule as a fallback and resolve an object's Consumers through the ownership relations only. Both readers call
-- runtime.nexloop_object_consumers_read (0357), so this migration changes that one function and no reader body:
-- * the P4a union is kept under the name runtime.nexloop_object_consumers_read_p4union (rollback: drop the new function and rename
--   the union back; docs/design/relation-read-p5.md §4);
-- * the new function: the Consumer itself; else, while the tenant has no published relation catalog, the property rule as before
--   (nothing to read through: never fewer than 0315 for a tenant that never published relations); else the relations
--   (runtime.nexloop_relation_object_consumers, 0319), plus a RelationshipAssessment's source_id and target_id by property - the
--   first relation batch has no relation for them (ADR-038 §3 point 6); its evidence Message goes through the relations.
-- The purge keeps relations united with properties for good (ADR-038 §2.3, 0361: not touched). The daily gap check (0357
-- authz.nexloop_relation_read_gaps) and its alert stay.
-- Gate (one way; owner confirmation): gaps 0 for 7 days in a row after P4a, daily verify 0, the whole-database erasure test in
-- relation-only mode. Draft: not in a batch before that. Temporary number; the dispatcher renumbers.

alter function runtime.nexloop_object_consumers_read(text,text,text,text) rename to nexloop_object_consumers_read_p4union;
revoke all on function runtime.nexloop_object_consumers_read_p4union(text,text,text,text) from public;

create function runtime.nexloop_object_consumers_read(p_tenant text,p_world text,p_type text,p_object text) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare out text[];
begin
 if p_type='Consumer' then return array[p_object];end if;
 -- The caller has set eios.tenant_id (0315); a tenant without a published relation catalog has nothing to read through.
 if not exists(select 1 from control.nexloop_relation_catalogs c where c.tenant_id=p_tenant) then
  return runtime.nexloop_object_consumers(p_tenant,p_world,p_type,p_object);
 end if;
 out:=runtime.nexloop_relation_object_consumers(p_tenant,p_world,p_type,p_object);
 if p_type='RelationshipAssessment' then
  out:=out||coalesce((select array_remove(array[o.properties->>'source_id',o.properties->>'target_id'],null) from ontology.objects o
   where o.tenant_id=p_tenant and o.world=p_world and o.type_name=p_type and o.object_id=p_object),'{}');
 end if;
 return coalesce((select array_agg(distinct x order by x) from unnest(out) x where x~'^[a-f0-9]{64}$'),'{}');
end $$;
alter function runtime.nexloop_object_consumers_read(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_object_consumers_read(text,text,text,text) from public;
