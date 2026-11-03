-- Claim → Consumer phase two (docs/implementation/claim-consumer-delivery-windows-design.md v2 §2.2, §2.5, §3; L6 conditions C1-C3,
-- 2026-11-03): the NX-040 Schema publication accepts exactly one composite property shape - a closed-vocabulary struct array - so
-- that Consumer v2 can add delivery_windows ([{weekday, part}]). The migration number lives only in the file name and the catalog.
--
-- 1. runtime.nexloop_structured_descriptor_valid: the hard limits (C1). Top level {kind 'array', element, max_items 1-64}; element
--    {kind 'struct', fields}, 1-8 fields; each field {name ^[a-z][a-z0-9_]{0,31}$ unique, required true, descriptor {kind 'string',
--    enum}}; enum 1-16 unique ASCII values ^[a-z][a-z0-9_]{0,31}$. Any other key anywhere, a missing max_items, an empty enum or a
--    non-ASCII value: not valid (the gate answers value_type_not_allowed). The same cases drive nexloop_eios.evolution.schema_compat
--    (tests/support/structured_descriptor_cases.py).
-- 2. runtime.nexloop_structured_descriptor_expand: the EIOS dump of such a descriptor (PropertyTypeDescriptor.model_dump), so the
--    published definition equals what the Python side validates and the governed edit compares (0054: the Action's schema must equal
--    object_type_versions). max_items is the vendored model's additive field (PROVENANCE adaptation): kept only when set.
-- 3. runtime.nexloop_schema_change_valid (0252 body): add_optional_property may also carry type_descriptor (json only),
--    display_name and description (free text, 1-200 characters, no control characters). Free text from a proposal may come from a
--    model (NX-038 evolution candidates): the publication page shows it in full and the matcher passes it only inside its data payload.
-- 4. ontology.nexloop_schema_publication_gates (0257 body): the value type check reads runtime.nexloop_schema_property_type_allowed,
--    and the expected new property carries the proposal's display_name, description and expanded descriptor; nothing else.
-- 5. ontology.nexloop_property_value_valid and a trigger on ontology.nexloop_mutation_proposals: a proposal for a property with such a
--    descriptor must carry a canonical value (array, 1..max_items elements, exactly the declared fields, enum members, no duplicates,
--    ordered by the vocabularies). Recorded otherwise: reason value_not_normalized; moving it to applying: refused.

create function runtime.nexloop_structured_descriptor_valid(d jsonb) returns boolean
 language plpgsql immutable set search_path=pg_catalog,pg_temp as $$
declare f jsonb;e jsonb;
begin
 if jsonb_typeof(d) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(d) k) is distinct from array['element','kind','max_items']
  or d->>'kind' is distinct from 'array' or jsonb_typeof(d->'max_items') is distinct from 'number' or d->>'max_items'!~'^[1-9][0-9]?$'
  or (d->>'max_items')::integer>64 or jsonb_typeof(d->'element') is distinct from 'object' then return false;end if;
 if (select array_agg(k order by k) from jsonb_object_keys(d->'element') k) is distinct from array['fields','kind'] or d->'element'->>'kind' is distinct from 'struct'
  or jsonb_typeof(d->'element'->'fields') is distinct from 'array' or jsonb_array_length(d->'element'->'fields') not between 1 and 8 then return false;end if;
 for f in select value from jsonb_array_elements(d->'element'->'fields') loop
  if jsonb_typeof(f) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(f) k) is distinct from array['descriptor','name','required']
   or jsonb_typeof(f->'name') is distinct from 'string' or f->>'name'!~'^[a-z][a-z0-9_]{0,31}$' or octet_length(f->>'name')<>char_length(f->>'name')
   or f->'required' is distinct from 'true'::jsonb or jsonb_typeof(f->'descriptor') is distinct from 'object' then return false;end if;
  if (select array_agg(k order by k) from jsonb_object_keys(f->'descriptor') k) is distinct from array['enum','kind'] or f->'descriptor'->>'kind' is distinct from 'string'
   or jsonb_typeof(f->'descriptor'->'enum') is distinct from 'array' or jsonb_array_length(f->'descriptor'->'enum') not between 1 and 16 then return false;end if;
  for e in select value from jsonb_array_elements(f->'descriptor'->'enum') loop
   if jsonb_typeof(e) is distinct from 'string' or e#>>'{}'!~'^[a-z][a-z0-9_]{0,31}$' or octet_length(e#>>'{}')<>char_length(e#>>'{}') then return false;end if;
  end loop;
  if (select count(distinct x#>>'{}') from jsonb_array_elements(f->'descriptor'->'enum') x)<>jsonb_array_length(f->'descriptor'->'enum') then return false;end if;
 end loop;
 return (select count(distinct x->>'name') from jsonb_array_elements(d->'element'->'fields') x)=jsonb_array_length(d->'element'->'fields');
end $$;
alter function runtime.nexloop_structured_descriptor_valid(jsonb) owner to nexloop_owner;

create function runtime.nexloop_structured_descriptor_expand(d jsonb) returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when d is null or jsonb_typeof(d)='null' then null else jsonb_build_object('kind','array',
  'element',jsonb_build_object('kind','struct','element',null,
    'fields',(select jsonb_agg(jsonb_build_object('name',f->>'name','descriptor',jsonb_build_object('kind','string','element',null,'fields','[]'::jsonb,
      'enum',f->'descriptor'->'enum','format','','unit',''),'required',true) order by n) from jsonb_array_elements(d->'element'->'fields') with ordinality q(f,n)),
    'enum','[]'::jsonb,'format','','unit',''),
  'fields','[]'::jsonb,'enum','[]'::jsonb,'format','','unit','','max_items',d->'max_items') end
$$;
alter function runtime.nexloop_structured_descriptor_expand(jsonb) owner to nexloop_owner;

-- The value type of a new optional property: a scalar without a descriptor, or json with a valid structured descriptor.
create function runtime.nexloop_schema_property_type_allowed(c jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(case when c->>'value_type' in ('string','integer','number','boolean','datetime') then not (c ? 'type_descriptor')
  when c->>'value_type'='json' then runtime.nexloop_structured_descriptor_valid(c->'type_descriptor') else false end,false)
$$;
alter function runtime.nexloop_schema_property_type_allowed(jsonb) owner to nexloop_owner;

create function runtime.nexloop_schema_optional_property_valid(c jsonb) returns boolean
 language plpgsql immutable set search_path=pg_catalog,pg_temp as $$
declare k text;
begin
 if jsonb_typeof(c) is distinct from 'object' or not (c ? 'property_name' and c ? 'value_type') then return false;end if;
 for k in select jsonb_object_keys(c) loop
  if k not in ('property_name','value_type','type_descriptor','display_name','description') then return false;end if;
 end loop;
 if jsonb_typeof(c->'property_name') is distinct from 'string' or c->>'property_name'!~'^[a-z][a-z0-9_]{0,63}$' then return false;end if;
 foreach k in array array['display_name','description'] loop
  if c ? k and (jsonb_typeof(c->k) is distinct from 'string' or char_length(c->>k) not between 1 and 200 or c->>k~'[[:cntrl:]]') then return false;end if;
 end loop;
 return runtime.nexloop_schema_property_type_allowed(c);
end $$;
alter function runtime.nexloop_schema_optional_property_valid(jsonb) owner to nexloop_owner;

-- 3.
create or replace function runtime.nexloop_schema_change_valid(p_operation text,p_type text,p_change jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(case p_operation
  when 'add_optional_property' then runtime.nexloop_schema_optional_property_valid(p_change)
  when 'add_vocabulary_value' then (select array_agg(k order by k) from jsonb_object_keys(p_change) k)=array['property_name','value']
   and p_change->>'property_name'~'^[a-z][a-z0-9_]{0,63}$' and jsonb_typeof(p_change->'value')='string' and char_length(p_change->>'value') between 1 and 200
  when 'add_object_type' then (select array_agg(k order by k) from jsonb_object_keys(p_change) k)=array['properties']
   and jsonb_typeof(p_change->'properties')='array' and jsonb_array_length(p_change->'properties') between 1 and 64
   and not exists(select 1 from jsonb_array_elements(p_change->'properties') e where jsonb_typeof(e)<>'object'
     or (select array_agg(k order by k) from jsonb_object_keys(e) k) is distinct from array['property_name','value_type']
     or e->>'property_name'!~'^[a-z][a-z0-9_]{0,63}$' or e->>'value_type' not in ('string','integer','number','boolean','datetime'))
   and (select count(distinct e->>'property_name') from jsonb_array_elements(p_change->'properties') e)=jsonb_array_length(p_change->'properties')
  else false end,false)
$$;


-- 4.
create or replace function ontology.nexloop_schema_publication_gates(p_tenant text,r ontology.nexloop_schema_proposals,p_kind text,p_pub jsonb,p_hide text)
 returns text[] language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare f text[]:='{}';old jsonb;oldv integer;new jsonb:=p_pub->'schema';np jsonb;expected jsonb;prop jsonb;a jsonb;pred jsonb;i integer;
 restricted text[]:=ontology.nexloop_schema_restricted_names(p_tenant);v_count integer;
begin
 select d.version,d.definition into oldv,old from ontology.object_type_versions d where d.tenant_id=p_tenant and d.type_name=r.type_name order by d.version desc limit 1;
 if jsonb_typeof(new) is distinct from 'object' or new->>'type_name' is distinct from r.type_name then return array['schema_not_the_proposal'];end if;
 if p_kind='publish' and coalesce(oldv,0)<>r.parent_schema_version then return array['parent_moved'];end if;
 if r.operation='add_object_type' and p_kind='publish' then
  if oldv is not null then return array['parent_moved'];end if;
  if jsonb_array_length(coalesce(r.change->'primary_key','[]'::jsonb))>0 or jsonb_array_length(coalesce(new->'primary_key','[]'::jsonb))>0 then
   f:=f||'breaking_primary_key'::text;end if;
  if (new->>'only_edit_via_actions')::boolean is not true then f:=f||'edit_mode_open'::text;end if;
  if jsonb_array_length(coalesce(p_pub->'actions','[]'::jsonb))<>0 then f:=f||'authority_content'::text;end if;
  if exists(select 1 from jsonb_array_elements(coalesce(r.change->'properties','[]'::jsonb)) e where e->>'property_name'=any(restricted)) then
   f:=f||'property_security_bypass'::text;end if;
  if exists(select 1 from jsonb_array_elements(coalesce(r.change->'properties','[]'::jsonb)) e where e->>'value_type' not in ('string','integer','number','boolean','datetime')) then
   f:=f||'value_type_not_allowed'::text;end if;
  expected:=jsonb_build_object('type_name',r.type_name,'version',1,'only_edit_via_actions',true,'primary_key','[]'::jsonb,
   'properties',(select coalesce(jsonb_agg(jsonb_build_object('property_name',e->>'property_name','value_type',e->>'value_type','required',false,'nullable',true)
     order by n),'[]'::jsonb) from jsonb_array_elements(coalesce(r.change->'properties','[]'::jsonb)) with ordinality q(e,n)));
  if jsonb_build_object('type_name',new->>'type_name','version',(new->>'version')::integer,'only_edit_via_actions',new->'only_edit_via_actions','primary_key',coalesce(new->'primary_key','[]'::jsonb),
     'properties',(select coalesce(jsonb_agg(jsonb_build_object('property_name',e->>'property_name','value_type',e->>'value_type','required',coalesce((e->>'required')::boolean,false),
       'nullable',coalesce((e->>'nullable')::boolean,false)) order by n),'[]'::jsonb) from jsonb_array_elements(coalesce(new->'properties','[]'::jsonb)) with ordinality q(e,n)))
    is distinct from expected or exists(select 1 from jsonb_object_keys(new) k where k in ('relations','actions')) then
   f:=f||'schema_not_the_proposal'::text;end if;
  return f;
 end if;
 if oldv is null then return array['parent_moved'];end if;
 if (new->>'version')::integer is distinct from oldv+1 then f:=f||'schema_not_the_proposal'::text;end if;
 if p_kind='hide' then
  if r.operation<>'add_optional_property' or r.status<>'published' or not exists(select 1 from jsonb_array_elements(old->'properties') e
    where e->>'property_name'=p_hide and p_hide=r.change->>'property_name') then return array['not_published_property'];end if;
  expected:=old||jsonb_build_object('version',oldv+1,'properties',(select jsonb_agg(case when e->>'property_name'=p_hide then e||'{"visibility":"hidden"}'::jsonb else e end order by n)
   from jsonb_array_elements(old->'properties') with ordinality q(e,n)));
  if new is distinct from expected then f:=f||'schema_not_the_proposal'::text;end if;
 elsif r.operation='add_optional_property' then
  if not runtime.nexloop_schema_property_type_allowed(r.change) then f:=f||'value_type_not_allowed'::text;end if;
  if exists(select 1 from jsonb_array_elements(old->'properties') e where e->>'property_name'=r.change->>'property_name') then f:=f||'property_exists'::text;end if;
  if r.change->>'property_name'=any(restricted) then f:=f||'property_security_bypass'::text;end if;
  np:=jsonb_build_object('property_name',r.change->>'property_name','value_type',r.change->>'value_type','required',false,'nullable',true,'default_value',null,
   'description',coalesce(r.change->>'description',''),'display_name',coalesce(r.change->>'display_name',''),'render_hint','plain','visibility','normal',
   'type_descriptor',runtime.nexloop_structured_descriptor_expand(r.change->'type_descriptor'));
  expected:=old||jsonb_build_object('version',oldv+1,'properties',(old->'properties')||jsonb_build_array(np));
  if new is distinct from expected then f:=f||'schema_not_the_proposal'::text;end if;
 elsif r.operation='add_vocabulary_value' then
  select e into prop from jsonb_array_elements(old->'properties') e where e->>'property_name'=r.change->>'property_name';
  if prop is null or jsonb_typeof(prop->'type_descriptor'->'enum') is distinct from 'array' or jsonb_array_length(prop->'type_descriptor'->'enum')=0 then
   f:=f||'not_closed_vocabulary'::text;
  elsif prop->'type_descriptor'->'enum' @> jsonb_build_array(r.change->'value') then f:=f||'vocabulary_value_exists'::text;
  else
   expected:=old||jsonb_build_object('version',oldv+1,'properties',(select jsonb_agg(case when e->>'property_name'=r.change->>'property_name'
     then jsonb_set(e,'{type_descriptor,enum}',(e->'type_descriptor'->'enum')||jsonb_build_array(r.change->'value')) else e end order by n)
     from jsonb_array_elements(old->'properties') with ordinality q(e,n)));
   if new is distinct from expected then f:=f||'schema_not_the_proposal'::text;end if;
  end if;
 end if;
 select count(*) into v_count from ontology.objects o where o.tenant_id=p_tenant and o.world='real' and o.type_name=r.type_name and o.schema_version=oldv;
 -- D25: above 10 000 objects the publication is refused unless it is a batched one (then the objects move 10 000 per call).
 if v_count>10000 and coalesce((p_pub->>'batch')::boolean,false) is not true then f:=f||'migration_over_limit'::text;end if;
 -- Successor Actions (§5.3): exactly the active Actions bound to the old version, each changed only in its version and the bound
 -- Schema reference (0082's rule, with F1's codes).
 if jsonb_array_length(coalesce(p_pub->'actions','[]'::jsonb))<>(select count(*) from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.active
   and exists(select 1 from jsonb_array_elements(d.definition->'object_types') x where x->>'stable_name'=r.type_name and (x->>'version')::integer=oldv)) then
  f:=f||'successor_not_one_to_one'::text;
 end if;
 for a in select value from jsonb_array_elements(coalesce(p_pub->'actions','[]'::jsonb)) loop
  pred:=null;
  select jsonb_build_object('definition',d.definition,'capability',d.capability) into pred from control.nexloop_action_definitions d
   where d.tenant_id=p_tenant and d.world='real' and d.active and d.definition->>'stable_name'=a->'definition'->>'stable_name'
    and (d.definition->>'version')::integer=(a->'definition'->>'version')::integer-1
    and exists(select 1 from jsonb_array_elements(d.definition->'object_types') x where x->>'stable_name'=r.type_name and (x->>'version')::integer=oldv);
  if pred is null then f:=f||'authority_content'::text;continue;end if;
  if a->'capability' is distinct from pred->'capability'
   or exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.resource_id='eios:action:'||(a->'definition'->>'stable_name')||':'||(a->'definition'->>'version'))
   or ((a->'definition')-'version'-'object_types'-'governance'-'contract_digest'-'previous_version'-'created_at')
      is distinct from ((pred->'definition')-'version'-'object_types'-'governance'-'contract_digest'-'previous_version'-'created_at')
   or ((a->'definition'->'governance')-'change_scope') is distinct from ((pred->'definition'->'governance')-'change_scope')
   or ((a->'definition'->'governance'->'change_scope')-'object_types') is distinct from ((pred->'definition'->'governance'->'change_scope')-'object_types') then
   f:=f||'tool_signature_changed'::text;
  end if;
  for i in 0..greatest(jsonb_array_length(a->'definition'->'object_types'),jsonb_array_length(pred->'definition'->'object_types'))-1 loop
   if ((a->'definition'->'object_types'->i)-'version'-'schema_digest') is distinct from ((pred->'definition'->'object_types'->i)-'version'-'schema_digest')
    or (pred->'definition'->'object_types'->i->>'stable_name'<>r.type_name and a->'definition'->'object_types'->i is distinct from pred->'definition'->'object_types'->i)
    or (pred->'definition'->'object_types'->i->>'stable_name'=r.type_name and (a->'definition'->'object_types'->i->>'version')::integer<>oldv+1) then
    f:=f||'tool_signature_changed'::text;exit;
   end if;
  end loop;
 end loop;
 return array(select distinct x from unnest(f) x order by 1);
end $$;


-- 5. Canonical structured values.
create function ontology.nexloop_property_value_valid(d jsonb,v jsonb) returns boolean
 language plpgsql immutable set search_path=pg_catalog,pg_temp as $$
declare e jsonb;f jsonb;prev text:='';cur text;i integer;
begin
 if not runtime.nexloop_structured_descriptor_valid(d) then return false;end if;
 if jsonb_typeof(v) is distinct from 'array' or jsonb_array_length(v)<1 or jsonb_array_length(v)>(d->>'max_items')::integer then return false;end if;
 for e in select value from jsonb_array_elements(v) loop
  if jsonb_typeof(e) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(e) k)
    is distinct from (select array_agg(x->>'name' order by x->>'name') from jsonb_array_elements(d->'element'->'fields') x) then return false;end if;
  cur:='';
  for f in select value from jsonb_array_elements(d->'element'->'fields') loop
   if jsonb_typeof(e->(f->>'name')) is distinct from 'string' then return false;end if;
   select n into i from jsonb_array_elements_text(f->'descriptor'->'enum') with ordinality q(x,n) where x=e->>(f->>'name');
   if not found then return false;end if;
   cur:=cur||lpad(i::text,2,'0');
  end loop;
  if cur<=prev then return false;end if;  -- strictly increasing by the vocabularies: ordered, no duplicates
  prev:=cur;
 end loop;
 return true;
end $$;
alter function ontology.nexloop_property_value_valid(jsonb,jsonb) owner to nexloop_owner;

create function ontology.nexloop_mutation_proposal_value_check() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare d jsonb;prior text:=current_setting('eios.tenant_id',true);
begin
 if new.op is distinct from 'set_property' or new.property_name is null then return new;end if;
 if tg_op='UPDATE' and not (new.status='applying' and old.status<>'applying') then return new;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select p->'type_descriptor' into d from ontology.object_type_versions t,jsonb_array_elements(t.definition->'properties') p
  where t.tenant_id=new.tenant_id and t.type_name=new.type_name and p->>'property_name'=new.property_name order by t.version desc limit 1;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if d is null or jsonb_typeof(d) is distinct from 'object' or d->>'kind' is distinct from 'array' or d->'element'->>'kind' is distinct from 'struct' then return new;end if;
 if ontology.nexloop_property_value_valid(jsonb_build_object('kind','array','max_items',d->'max_items','element',jsonb_build_object('kind','struct',
    'fields',(select jsonb_agg(jsonb_build_object('name',f->>'name','required',true,'descriptor',jsonb_build_object('kind','string','enum',f->'descriptor'->'enum')) order by n)
     from jsonb_array_elements(d->'element'->'fields') with ordinality q(f,n)))),new.proposal->'operations'->0->'value') then return new;end if;
 if tg_op='INSERT' then new.reason:='value_not_normalized';return new;end if;
 raise exception 'property value not normalized' using errcode='22023';
end $$;
alter function ontology.nexloop_mutation_proposal_value_check() owner to nexloop_owner;
revoke all on function ontology.nexloop_mutation_proposal_value_check() from public;
create trigger nexloop_mutation_proposal_value_check before insert or update of status on ontology.nexloop_mutation_proposals
 for each row execute function ontology.nexloop_mutation_proposal_value_check();
