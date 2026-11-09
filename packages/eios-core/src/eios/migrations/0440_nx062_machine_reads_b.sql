-- NX-062 M4b (docs/implementation/NX-062-M4-projections.md; ADR-041 amendment A1; L6 M4-1 to M4-5, N1, N2): the read verbs
-- object.neighbors and commitment.list. The migration number is temporary; it lives only in the file name and the catalog.
--
-- * authz.nexloop_machine_read_gate(prefix, digest, verb, type): the checks of nx062_machine_reads in the same order - identity (the
--   machine snapshot, same function and error), the verb in the key's scope, current in the allowlist, the client's latest grant active,
--   the type in the grant and in the verb's projection, a non-empty projection - returning the tenant, world, granted types and the
--   type's registered properties. The object.get / object.list entry is not changed: authz.nexloop_machine_read is renamed once
--   (..._before_reads_b, its EXECUTE grantees moved to the wrapper) and wrapped; the wrapper answers the two new verbs and passes every
--   other verb on unchanged.
-- * object.neighbors: depth 1 around a visible object of a granted type. Edges are governed relations (ontology.relations, key's world)
--   and reference properties registered for the verb (Commitment.supersedes, CommercialRecord.related_record_id); a neighbour is shown
--   only when its type is granted for this verb and it is visible (authz.nexloop_machine_object_visible: world, NX-029/NX-054 erasure).
--   Groups carry at most 20 items and whether more visible ones exist; nothing is counted of what the key may not see (L6 N2) - no
--   invisible, collapsed or total count; a group without a visible neighbour is not returned.
-- * commitment.list: the client's visible Commitments of the key's world, open (default), ended or all, at most 50 a page in id order,
--   each with its registered properties only (no promised words, customer name, events, evidence or exceptions text); no total.
-- * Each read counts per day, key prefix, verb and type (T-12), as object.get / object.list do.
set local timezone = 'UTC';

insert into control.nexloop_machine_read_verbs(verb,projection,description,source_task) values
 ('object.neighbors','["type_name","object_id","depth","groups",
   {"type_name":"Commitment","properties":["supersedes"]},{"type_name":"CommercialRecord","properties":["related_record_id"]}]',
  'The depth-1 neighbours of one object through governed relations and registered reference properties: type and id only, visible granted types only, nothing counted of the rest.','NX-062'),
 ('commitment.list','["commitments","next_after",
   {"type_name":"Commitment","properties":["due_at","due_precision","fulfillment_basis","late","made_to","promised_at","related_goal_ref","status","supersedes"]}]',
  'List the visible commitments (open, ended or all), at most 50 a page, with the registered properties only (no promised words, names, events or evidence).','NX-062');
insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values
 ('object.neighbors',1,'active','read','The depth-1 neighbours of one object (type and id only).','migration:0440_nx062_machine_reads_b.sql','NX-062'),
 ('commitment.list',1,'active','read','List commitments (registered properties only).','migration:0440_nx062_machine_reads_b.sql','NX-062');

create function authz.nexloop_machine_read_gate(p_prefix text,p_digest text,p_verb text,p_type text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);v_tenant text;v_types text[];v_props text[];v_fields text[];
begin
 v_tenant:=ident->'binding'->>'tenant_id';
 if p_verb is null or not (ident->'scope' ? p_verb) then raise exception 'machine read: not_permitted' using errcode='42501';end if;
 if not control.nexloop_machine_capability_current(p_verb) then raise exception 'machine read: not_permitted' using errcode='42501';end if;
 v_types:=authz.nexloop_machine_grant_types(v_tenant,ident->>'client_id',p_verb);
 if v_types is null then raise exception 'machine read: not_permitted' using errcode='42501';end if;
 if coalesce(p_type,'')!~'^[A-Z][A-Za-z0-9_]{0,63}$' then raise exception 'machine read: invalid' using errcode='22023';end if;
 if not (p_type=any(v_types)) or not (p_type=any(control.nexloop_machine_verb_types(p_verb))) then raise exception 'machine read: not_found' using errcode='P0002';end if;
 select array_agg(x) into v_props from control.nexloop_machine_read_verbs v,jsonb_array_elements(v.projection) e,jsonb_array_elements_text(e->'properties') x
  where v.verb=p_verb and jsonb_typeof(e)='object' and e->>'type_name'=p_type;
 select array_agg(e#>>'{}') into v_fields from control.nexloop_machine_read_verbs v,jsonb_array_elements(v.projection) e where v.verb=p_verb and jsonb_typeof(e)='string';
 if coalesce(cardinality(v_props),0)=0 or coalesce(cardinality(v_fields),0)=0 then raise exception 'machine read: projection_unavailable' using errcode='55000';end if;
 return jsonb_build_object('tenant',v_tenant,'world',ident->>'world','key_prefix',ident->>'key_prefix','types',to_jsonb(v_types),'props',to_jsonb(v_props),
  'fields',to_jsonb(v_fields));
end $$;

create function runtime.nexloop_machine_read_count(p_tenant text,p_prefix text,p_verb text,p_type text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 insert into runtime.nexloop_machine_read_counts(day,tenant_id,key_prefix,verb,object_type,calls)
  values((clock_timestamp() at time zone 'UTC')::date,p_tenant,p_prefix,p_verb,p_type,1)
  on conflict(day,tenant_id,key_prefix,verb,object_type) do update set calls=runtime.nexloop_machine_read_counts.calls+1
$$;

-- The visible neighbours of one visible object, by (direction, via, type); a neighbour must be of a type granted for object.neighbors.
create function authz.nexloop_machine_neighbors(p_tenant text,p_world text,p_type text,p_object text,p_types text[]) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;edge text[];
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 -- The reference properties the verb registers, per type.
 with registered as (
   select e->>'type_name' type_name,x prop from control.nexloop_machine_read_verbs v,jsonb_array_elements(v.projection) e,jsonb_array_elements_text(e->'properties') x
   where v.verb='object.neighbors' and jsonb_typeof(e)='object'),
  edges as (
   select r.relation_name via,'outgoing' direction,r.target_type type_name,r.target_object_id object_id from ontology.relations r
    where r.tenant_id=p_tenant and r.world=p_world and r.source_type=p_type and r.source_object_id=p_object
   union select r.relation_name,'incoming',r.source_type,r.source_object_id from ontology.relations r
    where r.tenant_id=p_tenant and r.world=p_world and r.target_type=p_type and r.target_object_id=p_object
   union select 'property:'||g.prop,'outgoing',n.type_name,n.object_id from ontology.objects s join registered g on g.type_name=s.type_name
    cross join lateral jsonb_array_elements(case jsonb_typeof(s.properties->g.prop) when 'array' then s.properties->g.prop
      when 'string' then jsonb_build_array(s.properties->g.prop) else '[]'::jsonb end) val
    join ontology.objects n on n.tenant_id=s.tenant_id and n.world=s.world and jsonb_typeof(val)='string' and n.object_id=val#>>'{}'
     and not (n.type_name=s.type_name and n.object_id=s.object_id)
    where s.tenant_id=p_tenant and s.world=p_world and s.type_name=p_type and s.object_id=p_object
   union select 'property:'||g.prop,'incoming',n.type_name,n.object_id from ontology.objects n join registered g on g.type_name=n.type_name
    where n.tenant_id=p_tenant and n.world=p_world and not (n.type_name=p_type and n.object_id=p_object)
     and (n.properties->g.prop=to_jsonb(p_object) or (jsonb_typeof(n.properties->g.prop)='array' and n.properties->g.prop @> jsonb_build_array(p_object)))),
  shown as (
   select e.*,row_number() over (partition by e.direction,e.via,e.type_name order by e.object_id) rank from edges e
   where e.type_name=any(p_types) and authz.nexloop_machine_object_visible(p_tenant,p_world,e.type_name,e.object_id))
 select coalesce(jsonb_agg(jsonb_build_object('direction',g.direction,'via',g.via,'type_name',g.type_name,'items',g.items,'more',g.more)
   order by g.direction,g.via,g.type_name),'[]'::jsonb) into v
 from (select s.direction,s.via,s.type_name,
   jsonb_agg(jsonb_build_object('type_name',s.type_name,'object_id',s.object_id) order by s.object_id) filter (where s.rank<=20) items,
   count(*)>20 more from shown s group by s.direction,s.via,s.type_name) g;
 return v;
end $$;

alter function authz.nexloop_machine_read(text,text,text,jsonb) rename to nexloop_machine_read_before_reads_b;
create function authz.nexloop_machine_read(p_prefix text,p_digest text,p_verb text,p_params jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare gate jsonb;v_tenant text;v_world text;v_types text[];v_props text[];v_type text;v_result jsonb;o ontology.objects;
 v_after text;v_group text;v_items jsonb:='[]'::jsonb;v_scanned integer:=0;v_limit integer:=50;
begin
 if p_verb is distinct from 'object.neighbors' and p_verb is distinct from 'commitment.list' then
  return authz.nexloop_machine_read_before_reads_b(p_prefix,p_digest,p_verb,p_params);end if;
 if jsonb_typeof(p_params) is distinct from 'object' then
  -- Identity first, as for every verb (the gate refuses an unknown key before looking at the parameters).
  perform authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
  raise exception 'machine read: invalid' using errcode='22023';end if;
 v_type:=case when p_verb='commitment.list' then 'Commitment' else p_params->>'type_name' end;
 gate:=authz.nexloop_machine_read_gate(p_prefix,p_digest,p_verb,v_type);
 v_tenant:=gate->>'tenant';v_world:=gate->>'world';
 select array_agg(x) into v_types from jsonb_array_elements_text(gate->'types') x;
 select array_agg(x) into v_props from jsonb_array_elements_text(gate->'props') x;
 perform set_config('eios.tenant_id',v_tenant,true);
 if p_verb='object.neighbors' then
  if (select array_agg(k order by k) from jsonb_object_keys(p_params) k) is distinct from array['object_id','type_name']
   or coalesce(p_params->>'object_id','')!~'^[a-f0-9]{64}$' then raise exception 'machine read: invalid' using errcode='22023';end if;
  if not authz.nexloop_machine_object_visible(v_tenant,v_world,v_type,p_params->>'object_id') then raise exception 'machine read: not_found' using errcode='P0002';end if;
  v_result:=jsonb_build_object('type_name',v_type,'object_id',p_params->>'object_id','depth',1,
   'groups',authz.nexloop_machine_neighbors(v_tenant,v_world,v_type,p_params->>'object_id',v_types));
 else
  if exists(select 1 from jsonb_object_keys(p_params) k where k not in ('status_group','after'))
   or (p_params ? 'after' and coalesce(p_params->>'after','')!~'^[a-f0-9]{64}$')
   or coalesce(p_params->>'status_group','open') not in ('open','ended','all') then raise exception 'machine read: invalid' using errcode='22023';end if;
  v_group:=coalesce(p_params->>'status_group','open');v_after:=coalesce(p_params->>'after','');
  -- Visibility is part of the WHERE (L6 Q1): the page limit counts visible rows only, and next_after is the last visible id,
  -- set only when one more visible row exists. A status that is null or unknown counts as ended (L6 Q2).
  for o in select * from ontology.objects x where x.tenant_id=v_tenant and x.world=v_world and x.type_name='Commitment' and x.object_id>v_after
    and (v_group='all' or (v_group='open')=coalesce(x.properties->>'status' in ('conditional','open','in_progress','breached'),false))
    and authz.nexloop_machine_object_visible(v_tenant,v_world,'Commitment',x.object_id)
    order by x.object_id limit v_limit+1 loop
   v_scanned:=v_scanned+1;
   exit when v_scanned>v_limit;
   v_items:=v_items||jsonb_build_array(jsonb_build_object('commitment_id',o.object_id,'revision',o.nexloop_revision,'registered_at',to_jsonb(o.created_at),
    'properties',authz.nexloop_machine_object_properties(v_tenant,v_world,o,v_props)));
   v_after:=o.object_id;
  end loop;
  v_result:=jsonb_build_object('commitments',v_items,'next_after',case when v_scanned>v_limit then to_jsonb(v_after) else 'null'::jsonb end);
 end if;
 perform runtime.nexloop_machine_read_count(v_tenant,gate->>'key_prefix',p_verb,v_type);
 if exists(select 1 from jsonb_object_keys(v_result) k where not (k=any(array(select jsonb_array_elements_text(gate->'fields'))))) then
  raise exception 'machine read: projection_unavailable' using errcode='55000';end if;
 return v_result;
end $$;

do $$declare f text;g text;begin
 foreach f in array array['authz.nexloop_machine_read_gate(text,text,text,text)','runtime.nexloop_machine_read_count(text,text,text,text)',
  'authz.nexloop_machine_neighbors(text,text,text,text,text[])','authz.nexloop_machine_read(text,text,text,jsonb)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
 -- As 0434's fix: the wrapper takes over every EXECUTE grantee of the renamed function, which keeps none.
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='authz.nexloop_machine_read_before_reads_b(text,text,text,jsonb)'::regprocedure and a.privilege_type='EXECUTE'
    and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function authz.nexloop_machine_read(text,text,text,jsonb) to %I',g);
  execute format('revoke execute on function authz.nexloop_machine_read_before_reads_b(text,text,text,jsonb) from %I',g);
 end loop;
end $$;
