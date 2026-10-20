-- Enterprise world model W1 (docs/design/workbench-world-model-design.md; owner 2026-10-19 D1-D7 as recommended; temporary number (0260, was 0450),
-- renumbered at merge): the workbench verb `world_model`, a read-only snapshot of the tenant's published model metadata for every
-- workbench member whose role carries nexloop.workbench.read (D1): the current version of each object type (its properties),
-- relation type and event type, the tenant's active Actions and Functions of the read world, and instance counts per type.
-- Metadata only: no instance content, no Message, nothing personal. A property in a group the owner restricted
-- (authz.nexloop_authority_facts property_group_restriction, as authz.nexloop_workbench_member_may_read) is listed by name with
-- withheld=true and nothing else. Nothing is written; there is no edit path (D5: schema changes stay candidate review and
-- trusted configuration).
-- authz.nexloop_workbench_read keeps every other verb: it is renamed and called unchanged; this wrapper verifies the same signed
-- read claims (0130) for `world_model` only.
alter function authz.nexloop_workbench_read(text,text,text,text,text) rename to nexloop_workbench_read_before_world_model_v0260;
revoke all on function authz.nexloop_workbench_read_before_world_model_v0260(text,text,text,text,text) from public,nexloop_api;

create function authz.nexloop_workbench_world_model(p_tenant text,p_world text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 with ot as (select distinct on (v.type_name) v.type_name,v.version,v.definition from ontology.object_type_versions v
   where v.tenant_id=p_tenant order by v.type_name,v.version desc),
  rt as (select distinct on (v.relation_name) v.relation_name,v.version,v.definition from ontology.relation_type_versions v
   where v.tenant_id=p_tenant order by v.relation_name,v.version desc),
  et as (select distinct on (v.event_name) v.event_name,v.version,v.definition from ontology.event_type_versions v
   where v.tenant_id=p_tenant order by v.event_name,v.version desc),
  counts as (select o.type_name,count(*) n from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world group by o.type_name)
 select jsonb_build_object('world',p_world,'computed_at',clock_timestamp(),
  'object_types',coalesce((select jsonb_agg(jsonb_build_object('name',ot.type_name,'version',ot.version,
     'display_name',coalesce(ot.definition->>'display_name',''),'description',coalesce(ot.definition->>'description',''),
     'title_property',coalesce(ot.definition->>'title_property',''),'primary_key',coalesce(ot.definition->'primary_key','[]'::jsonb),
     'objects',coalesce((select c.n from counts c where c.type_name=ot.type_name),0),
     'properties',coalesce((select jsonb_agg(case when x.withheld then jsonb_build_object('name',x.name,'withheld',true)
        else jsonb_build_object('name',x.name,'withheld',false,'value_type',x.p->>'value_type','required',coalesce((x.p->>'required')::boolean,false),
         'nullable',coalesce((x.p->>'nullable')::boolean,false),'group',x.grp,'visibility',coalesce(x.p->>'visibility','normal'),
         'description',coalesce(x.p->>'description','')) end order by x.ord)
       from (select p.value p,p.ordinality ord,p.value->>'property_name' as name,ontology.nexloop_property_group_of(ot.definition,p.value->>'property_name') grp,
         coalesce((select f.payload->'restricted_groups' ? ontology.nexloop_property_group_of(ot.definition,p.value->>'property_name')
           from authz.nexloop_authority_facts f where f.tenant_id=p_tenant and f.fact_kind='property_group_restriction' and f.entity_key=array[ot.type_name]),false) withheld
         from jsonb_array_elements(coalesce(ot.definition->'properties','[]'::jsonb)) with ordinality p) x),'[]'::jsonb))
    order by ot.type_name) from ot),'[]'::jsonb),
  'relation_types',coalesce((select jsonb_agg(jsonb_build_object('name',rt.relation_name,'version',rt.version,
     'source_type',rt.definition->>'source_type','target_type',rt.definition->>'target_type',
     'cardinality',coalesce(rt.definition->>'cardinality','many_to_many'),'inverse_name',coalesce(rt.definition->>'inverse_name',''),
     'description',coalesce(rt.definition->>'description',''),'property_count',jsonb_array_length(coalesce(rt.definition->'properties','[]'::jsonb)))
    order by rt.relation_name) from rt),'[]'::jsonb),
  'event_types',coalesce((select jsonb_agg(jsonb_build_object('name',et.event_name,'version',et.version,'object_type',et.definition->>'object_type',
     'description',coalesce(et.definition->>'description',''),'payload_property_count',jsonb_array_length(coalesce(et.definition->'payload_properties','[]'::jsonb)))
    order by et.event_name) from et),'[]'::jsonb),
  'actions',coalesce((select jsonb_agg(jsonb_build_object('resource_id',a.resource_id,'name',a.definition->>'stable_name','version',(a.definition->>'version')::integer,
     'risk_level',a.definition->'governance'->>'risk_level','approval_mode',a.definition->'governance'->>'approval_mode',
     'change_scope',a.definition->'governance'->>'change_scope','has_side_effects',coalesce((a.capability->>'has_side_effects')::boolean,false),
     'object_types',coalesce((select jsonb_agg(r->>'stable_name' order by r->>'stable_name') from jsonb_array_elements(coalesce(a.definition->'object_types','[]'::jsonb)) r),'[]'::jsonb),
     'required_scopes',coalesce(a.definition->'required_scopes','[]'::jsonb),'contract_digest',a.definition->>'contract_digest')
    order by a.definition->>'stable_name',(a.definition->>'version')::integer)
    from control.nexloop_action_definitions a where a.tenant_id=p_tenant and a.world=p_world and a.active),'[]'::jsonb),
  'functions',coalesce((select jsonb_agg(jsonb_build_object('resource_id',f.resource_id,'name',f.definition->>'stable_name','version',(f.definition->>'version')::integer,
     'applies_to',coalesce((select jsonb_agg(coalesce(t->'object_type'->>'stable_name',t->'object_set'->>'stable_name')) from jsonb_array_elements(coalesce(f.definition->'applies_to','[]'::jsonb)) t),'[]'::jsonb),
     'required_scopes',coalesce(f.definition->'required_scopes','[]'::jsonb),'contract_digest',f.definition->>'contract_digest')
    order by f.definition->>'stable_name',(f.definition->>'version')::integer)
    from control.nexloop_function_definitions f where f.tenant_id=p_tenant and f.world=p_world and f.active),'[]'::jsonb))
$$;
alter function authz.nexloop_workbench_world_model(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_world_model(text,text) from public;

create function authz.nexloop_workbench_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'world_model' then
  return authz.nexloop_workbench_read_before_world_model_v0260(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0130 verification, for this verb.
 if session_user is distinct from 'nexloop_api' or p_world is distinct from 'real'
  or octet_length(p_text)>1048576 or octet_length(p_payload)>8192
  or a->>'protocol' is distinct from 'nexloop-workbench-read-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 -- The live member (browser Human of the workbench application, not a customer principal) and its role's Action.
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action) then
  raise exception 'workbench read forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench read invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return authz.nexloop_workbench_world_model(v_tenant,p_world);
end $$;
alter function authz.nexloop_workbench_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_read(text,text,text,text,text) to nexloop_api;
