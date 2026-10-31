-- I35 read-only diagnosis: where a workbench member's object/property READ comes from, and what the owner can read per type.
-- Run as an owner-capable read-only session (it calls owner-only functions; it writes nothing):
--   psql ... -v tenant=<TENANT_ID> -f scripts/ops/i35_owner_property_read.sql
-- Workbench reads are derived (workbench-member-v1, 0123/0224) from the live role in control.nexloop_workbench_configurations;
-- there are no per-member grants, so the absence of eios:object_type:* / eios:property:* grants for humans is expected.
begin transaction read only;
select set_config('eios.tenant_id', :'tenant', true);

\echo '1. workbench members and their roles (current configuration) - the only basis of a member read'
select c.manifest_version, m.key principal_id, m.value #>> '{}' role
  from control.nexloop_workbench_current(:'tenant') c, jsonb_each(c.members) m order by 3, 2;

\echo '2. role history of those principals (was any role removed or changed?)'
select principal_id, role, event, source, recorded_at from control.nexloop_workbench_role_history
 where tenant_id = :'tenant' order by principal_id, entry_id;

\echo '3. rule facts: property_access_rule (service principals only), property_group_restriction'
select fact_kind, entity_key, payload->>'principal_id' principal, payload->'operations' ops, payload->'property_groups' groups,
       payload->'restricted_groups' restricted, payload->>'active' active, payload->>'valid_until' valid_until
  from authz.nexloop_authority_facts where tenant_id = :'tenant'
   and fact_kind in ('property_access_rule','property_group_restriction','definition_read_rule','message_read_rule','role_source_read_rule')
 order by 1, 2;

\echo '4. per published type: latest version, properties, properties inside a group (0123 derives a Consumer property only inside a group)'
select v.type_name, v.version, jsonb_array_length(coalesce(v.definition->'properties','[]')) properties,
       (select count(distinct n) from jsonb_array_elements(coalesce(v.definition->'property_groups','[]')) g, jsonb_array_elements_text(g->'property_names') n) grouped
  from ontology.object_type_versions v
 where v.tenant_id = :'tenant' and v.version = (select max(x.version) from ontology.object_type_versions x where x.tenant_id = v.tenant_id and x.type_name = v.type_name)
 order by 1;

\echo '5. owner READ per type and property on one real object of each type (true = readable in the workbench)'
with sample as (
  select distinct on (o.type_name) o.type_name, o.object_id, o.schema_version from ontology.objects o
   where o.tenant_id = :'tenant' and o.world = 'real' order by o.type_name, o.updated_at desc nulls last)
select s.type_name,
       authz.nexloop_workbench_member_may_read(:'tenant','real','owner','eios:object:'||s.type_name||'/'||s.object_id) object_readable,
       count(*) filter (where authz.nexloop_workbench_member_may_read(:'tenant','real','owner','eios:property:'||s.type_name||'/'||s.object_id||'/'||(p->>'property_name'))) readable,
       count(*) properties,
       string_agg(p->>'property_name', ',' order by p->>'property_name') filter (where not authz.nexloop_workbench_member_may_read(:'tenant','real','owner','eios:property:'||s.type_name||'/'||s.object_id||'/'||(p->>'property_name'))) not_readable
  from sample s join ontology.object_type_versions v on v.tenant_id = :'tenant' and v.type_name = s.type_name and v.version = s.schema_version,
       jsonb_array_elements(coalesce(v.definition->'properties','[]')) p
 group by s.type_name, s.object_id order by 1;
rollback;
