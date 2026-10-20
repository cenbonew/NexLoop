-- Workbench Object property values (L4 capability gap analysis D-G1, owner 2026-10-20 as recommended; ADR-025, ADR-035). Temporary
-- number; the dispatcher renumbers at merge.
--
-- The workbench verb `object_fields` (nexloop.workbench.read:1) says, for one object, which of its schema properties the member may
-- read: per property its name, whether a value is present, whether the member's role derives the READ (the existing ADR-025 /
-- ADR-035 member derivation, authz.nexloop_workbench_member_may_read, the one the audited object read checks again), and whether
-- the owner restricted its group (then it is withheld). It returns no value and writes nothing: the values come afterwards through
-- the existing audited object read (authz.nexloop_read_object, derivation workbench-member-v1, one audit row per object), which
-- re-checks the object and every requested property. An object the member may not read (including one whose Consumer is being
-- erased, NX-029: the derivation refuses it) answers readable=false and no field.
-- authz.nexloop_workbench_read keeps every other verb: it is renamed and called unchanged; this wrapper verifies the same signed read
-- claims (0130) for `object_fields` only.
alter function authz.nexloop_workbench_read(text,text,text,text,text) rename to nexloop_workbench_read_before_object_fields;
revoke all on function authz.nexloop_workbench_read_before_object_fields(text,text,text,text,text) from public,nexloop_api;

create function authz.nexloop_workbench_object_fields(p_tenant text,p_world text,p_role text,p_type text,p_object text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare o ontology.objects;def jsonb;restriction jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=p_type and object_id=p_object;
 if not found then return null;end if;
 if not authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,'eios:object:'||p_type||'/'||p_object) then
  return jsonb_build_object('type_name',p_type,'object_id',p_object,'readable',false,'fields','[]'::jsonb);end if;
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=o.schema_version;
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[p_type];
 return jsonb_build_object('type_name',p_type,'object_id',p_object,'readable',true,'schema_version',o.schema_version,'revision',o.nexloop_revision,
  'fields',coalesce((select jsonb_agg(jsonb_build_object('name',x.name,'present',x.present,'restricted',x.restricted,'erasing',x.erasing,
      'readable',not x.restricted and not x.erasing and authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,
        'eios:property:'||p_type||'/'||p_object||'/'||x.name)) order by x.ord)
    from (select p.value->>'property_name' as name,p.ordinality ord,
       (o.properties ? (p.value->>'property_name') and o.properties->(p.value->>'property_name')<>'null'::jsonb) present,
       coalesce(restriction->'restricted_groups' ? ontology.nexloop_property_group_of(def,p.value->>'property_name'),false) restricted,
       -- NX-029: a value that names a Consumer being erased (an id, or an array of ids) is never shown.
       exists(select 1 from jsonb_array_elements(case jsonb_typeof(o.properties->(p.value->>'property_name')) when 'array' then o.properties->(p.value->>'property_name')
          when 'string' then jsonb_build_array(o.properties->(p.value->>'property_name')) else '[]'::jsonb end) e
         where jsonb_typeof(e)='string' and e#>>'{}'~'^[a-f0-9]{64}$' and runtime.nexloop_consumer_erasing(p_tenant,p_world,e#>>'{}')) erasing
      from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) with ordinality p
      where coalesce(p.value->>'property_name','')~'^[A-Za-z][A-Za-z0-9_]{0,63}$') x),'[]'::jsonb));
end $$;
alter function authz.nexloop_workbench_object_fields(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_object_fields(text,text,text,text,text) from public;

create function authz.nexloop_workbench_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'object_fields' then
  return authz.nexloop_workbench_read_before_object_fields(p_digest,p_world,p_text,p_signature,p_payload);end if;
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
 if (select array_agg(k2 order by k2) from jsonb_object_keys(c) k2) is distinct from array['object_id','type_name','verb']
  or coalesce(c->>'type_name','')!~'^[A-Z][A-Za-z0-9_]{0,63}$' or coalesce(c->>'object_id','')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$' then
  raise exception 'workbench read invalid' using errcode='22023';end if;
 return authz.nexloop_workbench_object_fields(v_tenant,p_world,w->>'role',c->>'type_name',c->>'object_id');
end $$;
alter function authz.nexloop_workbench_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_read(text,text,text,text,text) to nexloop_api;
