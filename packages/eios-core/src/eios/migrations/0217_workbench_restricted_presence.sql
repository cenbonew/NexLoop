-- Walkthrough 2026-10-18 F04 / D2 (owner 2026-10-18, as recommended): a workbench member learns nothing about a Consumer property it
-- may not read. authz.nexloop_workbench_consumer_fields (0123) returned `present` for every declared property, readable or not, so a
-- member could tell whether an owner-restricted property (e.g. display_name) was recorded. Now `present` is null for a property the
-- member's role does not derive (readable=false); readable properties are unchanged. The 0123 body is copied with that one change;
-- create or replace keeps owner and grants. Nothing else changes; no data is written.
create or replace function authz.nexloop_workbench_consumer_fields(p_digest text,p_world text,p_consumer text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w jsonb:=authz.nexloop_workbench_member(p_digest,p_world);o ontology.objects;def jsonb;
begin
 if w->>'role' not in ('owner','operator') or coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' then raise exception 'workbench member read forbidden' using errcode='42501';end if;
 perform set_config('eios.tenant_id',w->>'tenant_id',true);
 select * into o from ontology.objects where tenant_id=w->>'tenant_id' and world=p_world and type_name='Consumer' and object_id=p_consumer;
 if not found then return null;end if;
 select definition into def from ontology.object_type_versions where tenant_id=o.tenant_id and type_name='Consumer' and version=o.schema_version;
 return coalesce((select jsonb_agg(jsonb_build_object('name',x.name,'present',case when x.readable then o.properties ? x.name end,'readable',x.readable) order by x.name)
   from (select p->>'property_name' as name,
     authz.nexloop_workbench_member_may_read(o.tenant_id,p_world,w->>'role','eios:property:Consumer/'||p_consumer||'/'||(p->>'property_name')) as readable
     from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) p) x),'[]'::jsonb);
end $$;
