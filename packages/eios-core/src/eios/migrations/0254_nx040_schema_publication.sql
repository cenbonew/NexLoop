-- NX-040 F3, migration B: the Schema publication transaction and its permission proof (docs/implementation/NX-040-design.md §4, §5,
-- §6, §7; D21-D27, owner 2026-10-20). The migration number lives only in the file name and the catalog; nothing below repeats it.
--
-- 1. authz.nexloop_schema_proposal_publish (nexloop_api, protocol nexloop-schema-publication-v1): a governed human decision, never a
--    service or Agent - an actual browser Human session whose current authority grants eios:action:ontology.schema.review:1 (D22:
--    publication is by hand only), signed claims verified as the review decision verifies them (0082). Two kinds:
--      publish  a frozen proposal (F2) whose change is on the whitelist (§4.1), against the Parent it was frozen on (else
--               parent_moved: the proposal is withdrawn and must be frozen again);
--      hide     the rollback of a published add_optional_property (§6, divergence 1 decided 2026-10-20): the next Schema version
--               with that property's visibility hidden, through the same transaction - a new Schema version, never an SQL migration.
--    The reviewer's backend builds the next version and the successor Actions with the EIOS models and the F1 compatibility library
--    (nexloop_eios.evolution.schema_compat); this function re-derives the expected version from the proposal and refuses anything
--    else, so the backend's build is checked, not trusted.
-- 2. Gates (ontology.nexloop_schema_publication_gates, codes shared with F1): parent_moved, value_type_not_allowed,
--    property_exists, schema_not_the_proposal, breaking_primary_key, edit_mode_open, authority_content (a new type publishes no
--    Action; a successor without a predecessor is a new Action), successor_not_one_to_one, tool_signature_changed (a successor
--    differing from its predecessor in anything but its version and the bound Schema reference - capability, scopes, risk, input),
--    property_security_bypass (a new property or a new type's property named like a property of an owner-restricted group, AT-024),
--    not_closed_vocabulary, vocabulary_value_exists, migration_over_limit (D25: 10 000 objects), not_published_property.
-- 3. Permission proof (§5), in the same transaction: the tenant authority fingerprint (0082), the workbench roles of the current
--    configuration, the contact restrictions and every Action definition that existed before are digested before and after;
--    any difference raises. Both digests are recorded with the publication.
-- 4. ontology.nexloop_schema_publications: one row per publication (append only; tenant FK, row security). The proposal moves to
--    published through F2's runtime.nexloop_schema_proposal_mark_published with the publication as its permit reference.

create table ontology.nexloop_schema_publications (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),publication_id uuid not null,
 proposal_id uuid not null,kind text not null check(kind in ('publish','hide')),type_name text not null,
 version_before integer not null check(version_before>=0),version_after integer not null check(version_after=version_before+1),
 successor_actions text[] not null default '{}',objects_migrated integer not null default 0 check(objects_migrated>=0),
 permission_digest_before text not null check(permission_digest_before~'^[0-9a-f]{64}$'),
 permission_digest_after text not null check(permission_digest_after~'^[0-9a-f]{64}$'),
 compatibility_report_digest text not null check(compatibility_report_digest~'^[0-9a-f]{64}$'),
 published_by text not null,rationale text not null check(char_length(rationale) between 1 and 2000),published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,publication_id),
 foreign key(tenant_id,world,proposal_id) references ontology.nexloop_schema_proposals(tenant_id,world,proposal_id),
 check(permission_digest_before=permission_digest_after)
);
alter table ontology.nexloop_schema_publications owner to nexloop_owner;
alter table ontology.nexloop_schema_publications enable row level security;
alter table ontology.nexloop_schema_publications force row level security;
create policy tenant_boundary on ontology.nexloop_schema_publications to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_schema_publications from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_evo_writer;
create function ontology.nexloop_schema_publications_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'schema publications are append-only' using errcode='42501';end $$;
alter function ontology.nexloop_schema_publications_append_only() owner to nexloop_owner;
revoke all on function ontology.nexloop_schema_publications_append_only() from public;
create trigger schema_publications_append_only before update or delete on ontology.nexloop_schema_publications for each row
 execute function ontology.nexloop_schema_publications_append_only();

-- 3. The permission proof ------------------------------------------------------------------------------------------------------------
create function authz.nexloop_schema_permission_digest(p_tenant text,p_exclude text[]) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 -- The tenant's own rows whoever asks (row security keys on eios.tenant_id).
 perform set_config('eios.tenant_id',p_tenant,true);
 return encode(sha256(convert_to(concat_ws(E'\n',
   authz.nexloop_tenant_authority_fingerprint(p_tenant),
   (select coalesce(c.roles::text,'')||'|'||coalesce(c.members::text,'') from control.nexloop_workbench_configurations c where c.tenant_id=p_tenant
     order by c.manifest_version desc limit 1),
   (select coalesce(string_agg(r::text,E'\n' order by r.consumer_id,r.control_revision),'') from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant),
   (select coalesce(string_agg(a.resource_id||'|'||a.definition::text||'|'||a.capability::text||'|'||a.active::text,E'\n' order by a.world,a.resource_id),'')
     from control.nexloop_action_definitions a where a.tenant_id=p_tenant and not a.resource_id=any(coalesce(p_exclude,'{}'::text[])))),'UTF8')),'hex');
end $$;
alter function authz.nexloop_schema_permission_digest(text,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_schema_permission_digest(text,text[]) from public;

-- A property name the owner restricted on any type of the tenant (property_group_restriction): never re-exposed through a new
-- property or a new type (AT-024: no bypass of property security).
create function ontology.nexloop_schema_restricted_names(p_tenant text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(array_agg(distinct pn order by pn),'{}') from (
  select distinct on (v.type_name) v.type_name,v.definition from ontology.object_type_versions v where v.tenant_id=p_tenant order by v.type_name,v.version desc) d
  join authz.nexloop_authority_facts f on f.tenant_id=p_tenant and f.fact_kind='property_group_restriction' and f.entity_key=array[d.type_name]
  cross join lateral jsonb_array_elements(coalesce(d.definition->'property_groups','[]'::jsonb)) g
  cross join lateral jsonb_array_elements_text(coalesce(g->'property_names','[]'::jsonb)) pn
  where f.payload->'restricted_groups' ? (g->>'group_name')
$$;
alter function ontology.nexloop_schema_restricted_names(text) owner to nexloop_owner;
revoke all on function ontology.nexloop_schema_restricted_names(text) from public;

-- 2. Gates -------------------------------------------------------------------------------------------------------------------------
create function ontology.nexloop_schema_publication_gates(p_tenant text,r ontology.nexloop_schema_proposals,p_kind text,p_pub jsonb,p_hide text)
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
  if r.change->>'value_type' not in ('string','integer','number','boolean','datetime') then f:=f||'value_type_not_allowed'::text;end if;
  if exists(select 1 from jsonb_array_elements(old->'properties') e where e->>'property_name'=r.change->>'property_name') then f:=f||'property_exists'::text;end if;
  if r.change->>'property_name'=any(restricted) then f:=f||'property_security_bypass'::text;end if;
  np:=jsonb_build_object('property_name',r.change->>'property_name','value_type',r.change->>'value_type','required',false,'nullable',true,'default_value',null,
   'description','','display_name','','render_hint','plain','visibility','normal','type_descriptor',null);
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
 if v_count>10000 then f:=f||'migration_over_limit'::text;end if;
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
alter function ontology.nexloop_schema_publication_gates(text,ontology.nexloop_schema_proposals,text,jsonb,text) owner to nexloop_owner;
revoke all on function ontology.nexloop_schema_publication_gates(text,ontology.nexloop_schema_proposals,text,jsonb,text) from public;

-- The reviewer's basis: the proposal, the latest version of its type and the active Actions bound to it (review read authority).
create function authz.nexloop_read_schema_publication_basis(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_assert_review_read(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;r ontology.nexloop_schema_proposals;
 v integer;s jsonb;
begin
 perform set_config('eios.tenant_id',t,true);
 if coalesce(c->>'proposal_id','')!~'^[0-9a-f-]{36}$' then raise exception 'schema publication basis invalid' using errcode='22023';end if;
 select * into r from ontology.nexloop_schema_proposals where tenant_id=t and world=p_world and proposal_id=(c->>'proposal_id')::uuid;
 if not found then return null;end if;
 select d.version,d.definition into v,s from ontology.object_type_versions d where d.tenant_id=t and d.type_name=r.type_name order by d.version desc limit 1;
 return jsonb_build_object('proposal_id',r.proposal_id,'status',r.status,'operation',r.operation,'type_name',r.type_name,'change',r.change,
  'parent_schema_version',r.parent_schema_version,'latest_version',v,'schema',s,'restricted_names',to_jsonb(ontology.nexloop_schema_restricted_names(t)),
  'object_count',(select count(*) from ontology.objects o where o.tenant_id=t and o.world='real' and o.type_name=r.type_name and o.schema_version=v),
  'actions',coalesce((select jsonb_agg(jsonb_build_object('resource_id',a.resource_id,'definition',a.definition,'capability',a.capability) order by a.resource_id)
   from control.nexloop_action_definitions a where a.tenant_id=t and a.world='real' and a.active and v is not null
    and exists(select 1 from jsonb_array_elements(a.definition->'object_types') x where x->>'stable_name'=r.type_name and (x->>'version')::integer=v)),'[]'::jsonb));
end $$;
alter function authz.nexloop_read_schema_publication_basis(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_schema_publication_basis(text,text,text,text,text) from public;
grant execute on function authz.nexloop_read_schema_publication_basis(text,text,text,text,text) to nexloop_api;

-- 1. The publication -------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_schema_proposal_publish(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;t text;principal text;r ontology.nexloop_schema_proposals;
 prior ontology.nexloop_schema_publications;gates text[];pub jsonb:=c->'publication';v_kind text:=c->>'kind';oldv integer;fp_before text;
 perm_before text;perm_after text;successors text[]:='{}';act jsonb;n integer:=0;v_report text;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>2097152
  or a->>'protocol' is distinct from 'nexloop-schema-publication-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:ontology.schema.review:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'schema publication unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-schema-publication-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'schema publication unavailable' using errcode='42501';end if;
 -- D22: a person, never a service, a Run or an Agent.
 if not exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest) then
  raise exception 'schema publication requires a human session' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'human' or ident->'binding'->>'tenant_id' is distinct from a->>'tenant_id'
  or ident->'binding'->>'subject_principal_id' is distinct from a->>'principal_id' then
  raise exception 'schema publication requires a human session' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 t:=a->>'tenant_id';principal:=a->>'principal_id';
 perform set_config('eios.tenant_id',t,true);
 if coalesce(c->>'publication_id','')!~'^[0-9a-f-]{36}$' or coalesce(c->>'proposal_id','')!~'^[0-9a-f-]{36}$' or v_kind not in ('publish','hide') or v_kind is null
  or char_length(coalesce(c->>'rationale',''))not between 1 and 2000 or jsonb_typeof(pub) is distinct from 'object'
  or jsonb_typeof(c->'compatibility_report') is distinct from 'object' then
  raise exception 'schema publication invalid' using errcode='22023';end if;
 select * into prior from ontology.nexloop_schema_publications where tenant_id=t and world=p_world and publication_id=(c->>'publication_id')::uuid;
 if found then
  if prior.proposal_id<>(c->>'proposal_id')::uuid or prior.kind<>v_kind then raise exception 'schema publication id reused' using errcode='40001';end if;
  return jsonb_build_object('published',true,'replayed',true,'publication_id',prior.publication_id,'version_after',prior.version_after,
   'permission_digest',prior.permission_digest_after);
 end if;
 select * into r from ontology.nexloop_schema_proposals where tenant_id=t and world=p_world and proposal_id=(c->>'proposal_id')::uuid for update;
 if not found then raise exception 'schema proposal unknown' using errcode='22023';end if;
 if (v_kind='publish' and r.status<>'frozen') or (v_kind='hide' and r.status<>'published') then
  raise exception 'schema proposal % cannot be %ed',r.status,v_kind using errcode='22023';end if;
 -- The backend's F1 verdict must allow it; SQL then re-derives the version itself.
 if (c->'compatibility_report'->>'allowed')::boolean is not true or c->'compatibility_report'->>'op' is distinct from
   (case when v_kind='hide' then 'hide_property' else r.operation end) or jsonb_array_length(coalesce(c->'compatibility_report'->'codes','[]'::jsonb))>0 then
  return jsonb_build_object('published',false,'codes',coalesce(c->'compatibility_report'->'codes','["breaking_change"]'::jsonb),'source','compatibility');
 end if;
 v_report:=encode(sha256(convert_to(runtime.nexloop_canonical_json(c->'compatibility_report'),'UTF8')),'hex');
 gates:=ontology.nexloop_schema_publication_gates(t,r,v_kind,pub,c->>'property_name');
 if cardinality(gates)>0 then
  if 'parent_moved'=any(gates) and r.status='frozen' then
   update ontology.nexloop_schema_proposals set status='withdrawn',withdraw_reason='parent_moved',finished_at=clock_timestamp()
    where tenant_id=t and world=p_world and proposal_id=r.proposal_id;
   insert into ontology.nexloop_schema_proposal_events(tenant_id,world,proposal_id,from_status,to_status,reason,recorded_by)
    values(t,p_world,r.proposal_id,'frozen','withdrawn','parent_moved','human:'||principal);
  end if;
  return jsonb_build_object('published',false,'codes',to_jsonb(gates),'source','gates');
 end if;
 select max(version) into oldv from ontology.object_type_versions where tenant_id=t and type_name=r.type_name;
 fp_before:=authz.nexloop_tenant_authority_fingerprint(t);
 perm_before:=authz.nexloop_schema_permission_digest(t,'{}');
 insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(t,r.type_name,(pub->'schema'->>'version')::integer,pub->'schema');
 for act in select value from jsonb_array_elements(coalesce(pub->'actions','[]'::jsonb)) loop
  insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability,active)
   values(t,'real','eios:action:'||(act->'definition'->>'stable_name')||':'||(act->'definition'->>'version'),act->'definition',act->'capability',true);
  successors:=successors||('eios:action:'||(act->'definition'->>'stable_name')||':'||(act->'definition'->>'version'));
 end loop;
 if oldv is not null then
  -- Additive: existing objects stay valid and only move to the published version (values untouched).
  update ontology.objects set schema_version=oldv+1,updated_at=clock_timestamp() where tenant_id=t and world='real' and type_name=r.type_name and schema_version=oldv;
  get diagnostics n=row_count;
 end if;
 if authz.nexloop_tenant_authority_fingerprint(t) is distinct from fp_before then
  raise exception 'publication must not change authority' using errcode='42501';end if;
 perm_after:=authz.nexloop_schema_permission_digest(t,successors);
 if perm_after is distinct from perm_before then raise exception 'publication must not change any permission' using errcode='42501';end if;
 insert into ontology.nexloop_schema_publications(tenant_id,world,publication_id,proposal_id,kind,type_name,version_before,version_after,successor_actions,
   objects_migrated,permission_digest_before,permission_digest_after,compatibility_report_digest,published_by,rationale)
  values(t,p_world,(c->>'publication_id')::uuid,r.proposal_id,v_kind,r.type_name,coalesce(oldv,0),(pub->'schema'->>'version')::integer,successors,n,
   perm_before,perm_after,v_report,'human:'||principal,c->>'rationale');
 if v_kind='publish' then
  perform runtime.nexloop_schema_proposal_mark_published(t,r.proposal_id,'publication:'||(c->>'publication_id'),'human:'||principal);
 end if;
 return jsonb_build_object('published',true,'replayed',false,'publication_id',c->>'publication_id','version_after',(pub->'schema'->>'version')::integer,
  'successor_actions',to_jsonb(successors),'objects_migrated',n,'permission_digest',perm_after);
end $$;
alter function authz.nexloop_schema_proposal_publish(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_schema_proposal_publish(text,text,text,text,text) from public;
grant execute on function authz.nexloop_schema_proposal_publish(text,text,text,text,text) to nexloop_api;
