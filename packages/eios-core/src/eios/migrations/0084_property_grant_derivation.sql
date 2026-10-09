-- Governed per-object property READ/EDIT derivation for service principals
-- (docs/implementation/property-grant-derivation.md, approved 2026-10-09).
-- Trusted configuration publishes property_access_rule [principal,type] and the
-- owner's property_group_restriction [type]; nothing per object is written, so new
-- objects / new properties never advance the authority epoch. At each use the
-- typed derived proof (derivation='type-property-v1') is re-derived under share
-- locks: rule active and covering the operation and the property's group; group
-- not restricted (the restriction fact must exist: no owner decision, no
-- derivation); object present in real; property declared in the object's current
-- schema version; a property added after the rule's basis version only when the
-- rule includes review-published properties and a human review decision published
-- it in that group. Configured grants of the principal always win. 0054/0077/0081
-- bodies are unchanged; read/edit authority get an outermost dispatch wrapper.

alter table authz.nexloop_authority_facts drop constraint nexloop_authority_facts_fact_kind_check;
alter table authz.nexloop_authority_facts add constraint nexloop_authority_facts_fact_kind_check
 check(fact_kind in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision','agent','agent_release','agent_application','message_read_rule','property_access_rule','property_group_restriction'));

create or replace function control.nexloop_configure_manifest(p_tenant text,p_text text,p_digest text,p_key_id text,p_key bytea,p_credentials jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare m jsonb:=p_text::jsonb;item jsonb;d jsonb;cap jsonb;v_schemas jsonb;ref jsonb;schema_body jsonb;
 v_revision bigint;v_expected bigint;v_manifest uuid;prior control.nexloop_configuration_publications;
 v_kind text;v_id text;v_name text;v_version integer;v_digest text;v_secret_fingerprint text;old_body jsonb;v_key text[];
begin
 if session_user<>'nexloop_configurator' or p_tenant is null or p_tenant !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'
  or p_text is null or octet_length(p_text)>16777216 or jsonb_typeof(m) is distinct from 'object'
  or p_digest is distinct from encode(sha256(convert_to(p_text,'UTF8')),'hex')
  or m->>'schema_version' is distinct from '1.0' or m->>'tenant_id' is distinct from p_tenant
  or jsonb_typeof(m->'expected_revision') is distinct from 'number' or m->>'expected_revision' !~ '^[0-9]+$'
  or m->>'tenant_status' is null or m->>'tenant_status' not in ('active','suspended')
  or (select count(*) from jsonb_object_keys(m))<>14
  or exists(select 1 from jsonb_object_keys(m) key where key not in ('schema_version','manifest_id','tenant_id','expected_revision','tenant_status','object_types','actions','functions','authority_facts','service_credentials','browser_applications','browser_business_applications','browser_rate_policies','identity_allowances'))
  or jsonb_typeof(p_credentials) is distinct from 'object' or p_key_id is null or p_key_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'
  or p_key is null or octet_length(p_key)<>32
 then raise exception 'configuration rejected' using errcode='42501';end if;
 foreach v_kind in array array['object_types','actions','functions','authority_facts','service_credentials','browser_applications','browser_business_applications','browser_rate_policies','identity_allowances'] loop
  if jsonb_typeof(m->v_kind) is distinct from 'array' or jsonb_array_length(m->v_kind)>4096 then raise exception 'configuration rejected' using errcode='22023';end if;
 end loop;
 v_secret_fingerprint:=encode(sha256(p_key||convert_to(p_credentials::text,'UTF8')),'hex');
 v_manifest:=(m->>'manifest_id')::uuid;v_expected:=(m->>'expected_revision')::bigint;
 -- Serialize bootstrap before tenant exists; all incremental writes lock tenant.
 perform pg_advisory_xact_lock(hashtextextended('nexloop-configure:'||p_tenant,0));
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into prior from control.nexloop_configuration_publications where tenant_id=p_tenant and manifest_id=v_manifest;
 if found then
  if prior.manifest_digest is distinct from p_digest or prior.secret_fingerprint is distinct from v_secret_fingerprint then raise exception 'configuration conflict' using errcode='40001';end if;
  return jsonb_build_object('configured',true,'manifest_digest',p_digest,'authority_revision',prior.authority_revision);
 end if;
 select authority_revision into v_revision from control.nexloop_tenants where tenant_id=p_tenant for update;
 if not found then
  if v_expected<>0 then raise exception 'configuration conflict' using errcode='40001';end if;
  insert into control.nexloop_tenants(tenant_id,status) values(p_tenant,m->>'tenant_status');
 else
  if v_expected<>v_revision then raise exception 'configuration conflict' using errcode='40001';end if;
  update control.nexloop_tenants set status=m->>'tenant_status' where tenant_id=p_tenant and status is distinct from m->>'tenant_status';
 end if;
 -- Global signing-key version is immutable; secrets are parameters, never audit.
 insert into authz.nexloop_authority_signing_keys values(p_key_id,p_key,true) on conflict(key_id) do nothing;
 if not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=p_key_id and key_material=p_key and active) then
  raise exception 'configuration conflict' using errcode='40001';end if;
 for item in select value from jsonb_array_elements(m->'object_types') loop
  v_name:=item->>'type_name';v_version:=(item->>'version')::integer;
  if v_name is null or v_name='' or v_version is null or v_version<1 or jsonb_typeof(item) is distinct from 'object' then raise exception 'configuration rejected' using errcode='22023';end if;
  select definition into old_body from ontology.object_type_versions where tenant_id=p_tenant and type_name=v_name and version=v_version;
  if found then
   if old_body is distinct from item then raise exception 'configuration conflict' using errcode='40001';end if;
  else
   -- Initial minimal slice publishes version1 only. No silently breaking schema
   -- increment; actual frozen assert_object_compatible port is still required.
   if v_version<>1 then raise exception 'schema increment not supported' using errcode='42501';end if;
   insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(p_tenant,v_name,v_version,item);
  end if;
 end loop;
 foreach v_kind in array array['actions','functions'] loop
  for item in select value from jsonb_array_elements(m->v_kind) loop
   d:=item->'definition';cap:=item->'capability';v_name:=d->>'stable_name';v_version:=(d->>'version')::integer;
   if jsonb_typeof(d) is distinct from 'object' or jsonb_typeof(cap) is distinct from 'object' or d->>'tenant_id' is distinct from p_tenant
    or d->>'status' is distinct from 'published' or v_name is null or v_name='' or v_version is null or v_version<1
    or d->>'definition_type' is distinct from (case v_kind when 'actions' then 'action' else 'function' end)
    or d->'capability_binding'->>'capability_name' is null
    or d->'capability_binding'->>'capability_name' is distinct from cap->>'capability_name'
    or d->'capability_binding'->>'capability_version' is distinct from cap->>'capability_version'
    or d->'capability_binding'->>'schema_hash' is null or d->'capability_binding'->>'schema_hash' is distinct from cap->>'schema_hash'
   then raise exception 'configuration rejected' using errcode='22023';end if;
   if v_kind='actions' then
    if cap->'has_side_effects' is distinct from 'true'::jsonb then raise exception 'configuration rejected' using errcode='22023';end if;
    v_id:='eios:action:'||v_name||':'||v_version;
    insert into control.nexloop_action_definitions values(p_tenant,'real',v_id,d,cap,true) on conflict do nothing;
    if not exists(select 1 from control.nexloop_action_definitions where tenant_id=p_tenant and world='real' and resource_id=v_id and definition=d and capability=cap and active) then raise exception 'configuration conflict' using errcode='40001';end if;
   else
    if cap->'has_side_effects' is distinct from 'false'::jsonb or cap->>'kind' is distinct from 'atomic' then raise exception 'configuration rejected' using errcode='22023';end if;
    v_schemas:='[]';
    for ref in select value->'object_type' from jsonb_array_elements(d->'applies_to') loop
     if ref->>'tenant_id' is distinct from p_tenant or ref->>'schema_type' is distinct from 'object_type' then raise exception 'configuration rejected' using errcode='22023';end if;
     select definition into schema_body from ontology.object_type_versions where tenant_id=p_tenant and type_name=ref->>'stable_name' and version=(ref->>'version')::integer;
     if not found then raise exception 'configuration rejected' using errcode='22023';end if;
     v_schemas:=v_schemas||jsonb_build_array(jsonb_build_object('reference',ref,'definition',schema_body));
    end loop;
    v_id:='eios:function:'||v_name||':'||v_version;
    insert into control.nexloop_function_definitions values(p_tenant,'real',v_id,d,cap,v_schemas,true) on conflict do nothing;
    if not exists(select 1 from control.nexloop_function_definitions where tenant_id=p_tenant and world='real' and resource_id=v_id and definition=d and capability=cap and control.nexloop_function_definitions.schemas=v_schemas and active) then raise exception 'configuration conflict' using errcode='40001';end if;
   end if;
  end loop;
 end loop;
 for item in select value from jsonb_array_elements(m->'authority_facts') loop
  v_kind:=item->>'kind';d:=item->'payload';
  select array_agg(value order by ordinal) into v_key from jsonb_array_elements_text(item->'key') with ordinality as x(value,ordinal);
  if v_kind is null or v_kind not in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision','message_read_rule','property_access_rule','property_group_restriction')
   or d->>'tenant_id' is distinct from p_tenant or cardinality(v_key) not between 1 and 3 then raise exception 'configuration rejected' using errcode='22023';end if;
  -- Accepted-Message READ derivation rule: one per service principal, exact shape only.
  if v_kind='message_read_rule' and (cardinality(v_key)<>1 or d->>'principal_id' is distinct from v_key[1]
   or (select count(*) from jsonb_object_keys(d))<>6 or not (d ?& array['tenant_id','principal_id','type_name','fields','active','valid_until'])
   or d->>'type_name' is distinct from 'Message' or d->'fields' is distinct from '["actor","body"]'::jsonb
   or jsonb_typeof(d->'active') is distinct from 'boolean' or jsonb_typeof(d->'valid_until') is distinct from 'string'
   or (d->>'valid_until')::timestamptz is null
   or not exists(select 1 from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='subject_authority'
    and af.entity_key=array[v_key[1]] and af.payload->>'subject_kind'='service')) then
   raise exception 'message read rule rejected' using errcode='22023';end if;
  -- Typed per-object property access derivation (property-grant-derivation.md): exact shapes only.
  if v_kind='property_access_rule' and (cardinality(v_key)<>2 or d->>'principal_id' is distinct from v_key[1] or d->>'type_name' is distinct from v_key[2]
   or (select count(*) from jsonb_object_keys(d))<>9
   or not (d ?& array['tenant_id','principal_id','type_name','operations','property_groups','include_review_published','basis_schema_version','active','valid_until'])
   or d->>'type_name' !~ '^[A-Za-z][A-Za-z0-9_]{0,63}$'
   or jsonb_typeof(d->'operations') is distinct from 'array' or jsonb_array_length(d->'operations') not between 1 and 2
   or exists(select 1 from jsonb_array_elements_text(d->'operations') o where o not in ('read','edit'))
   or jsonb_typeof(d->'property_groups') is distinct from 'array' or jsonb_array_length(d->'property_groups') not between 1 and 32
   or exists(select 1 from jsonb_array_elements(d->'property_groups') g where jsonb_typeof(g) is distinct from 'string' or g#>>'{}' !~ '^[a-z][a-z0-9_]{0,63}$')
   or jsonb_typeof(d->'include_review_published') is distinct from 'boolean' or jsonb_typeof(d->'basis_schema_version') is distinct from 'number'
   or (d->>'basis_schema_version')::numeric<1 or jsonb_typeof(d->'active') is distinct from 'boolean'
   or jsonb_typeof(d->'valid_until') is distinct from 'string' or (d->>'valid_until')::timestamptz is null
   or not exists(select 1 from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='subject_authority'
    and af.entity_key=array[v_key[1]] and af.payload->>'subject_kind'='service')) then
   raise exception 'property access rule rejected' using errcode='22023';end if;
  if v_kind='property_group_restriction' and (cardinality(v_key)<>1 or d->>'type_name' is distinct from v_key[1]
   or (select count(*) from jsonb_object_keys(d))<>4 or not (d ?& array['tenant_id','type_name','restricted_groups','decision'])
   or jsonb_typeof(d->'restricted_groups') is distinct from 'array' or jsonb_array_length(d->'restricted_groups')>32
   or exists(select 1 from jsonb_array_elements(d->'restricted_groups') g where jsonb_typeof(g) is distinct from 'string' or g#>>'{}' !~ '^[a-z][a-z0-9_]{0,63}$')
   or jsonb_typeof(d->'decision') is distinct from 'string' or char_length(d->>'decision') not between 1 and 500) then
   raise exception 'property group restriction rejected' using errcode='22023';end if;
  -- Configurator may grant an existing Human, but cannot invent a Human actor
  -- or overwrite canonical identity directory through an authority-fact proxy.
  if v_kind='subject' and (d->>'kind' is null or d->>'kind' not in ('service','agent')) then
   raise exception 'Human directory needs explicit identity operator' using errcode='42501';
  end if;
  if v_kind='membership' and not exists(select 1 from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='subject'
   and af.entity_key=array[d->>'subject_id'] and af.payload->>'kind' in ('service','agent')) then
   raise exception 'service subject must already be configured' using errcode='42501';
  end if;
  if v_kind in ('actor','subject_authority','grants') and d->>'kind'='human' or v_kind in ('subject_authority','grants') and d->>'subject_kind'='human' then
   if not exists(select 1 from control.nexloop_browser_memberships bm where bm.tenant_id=p_tenant and bm.principal_id=coalesce(d->>'principal_id',d->>'actor_principal_id')) then
    raise exception 'existing Human directory required' using errcode='42501';end if;
  end if;
  insert into authz.nexloop_authority_facts values(p_tenant,v_kind,v_key,d) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload
   where nexloop_authority_facts.payload is distinct from excluded.payload;
 end loop;
 if (select count(*) from jsonb_object_keys(p_credentials))<>jsonb_array_length(m->'service_credentials') then raise exception 'configuration rejected' using errcode='22023';end if;
 for item in select value from jsonb_array_elements(m->'service_credentials') loop
  d:=item->'binding';v_digest:=p_credentials->>(item->>'reference');
  if v_digest is null or v_digest !~ '^[a-f0-9]{64}$' or d->>'tenant_id' is distinct from p_tenant or d->>'credential_tenant_id' is distinct from p_tenant
   or d->>'credential_kind' is distinct from 'api_key' or d->>'subject_kind' is null or d->>'subject_kind' not in ('service','agent')
   or d->>'credential_id' is null or d->>'subject_id' is null or d->>'subject_principal_id' is null
   or jsonb_typeof(item->'worlds') is distinct from 'array' or jsonb_array_length(item->'worlds') not between 1 and 16
   or item->>'expires_at' is null or (item->>'expires_at')::timestamptz<=clock_timestamp() or item->>'status' is null or item->>'status' not in ('active','revoked') then raise exception 'configuration rejected' using errcode='22023';end if;
  insert into authz.nexloop_service_credentials values(v_digest,p_tenant,d->>'credential_id',d,
   array(select jsonb_array_elements_text(item->'worlds')),'nexloop-core',item->>'status',(item->>'expires_at')::timestamptz)
   on conflict(token_digest) do nothing;
  if not exists(select 1 from authz.nexloop_service_credentials where token_digest=v_digest and tenant_id=p_tenant and credential_id=d->>'credential_id'
   and binding=d and worlds=array(select jsonb_array_elements_text(item->'worlds')) and status=item->>'status' and expires_at=(item->>'expires_at')::timestamptz) then raise exception 'configuration conflict' using errcode='40001';end if;
 end loop;
 for item in select value from jsonb_array_elements(m->'browser_applications') loop
  if item->>'application_id' is null or jsonb_typeof(item->'active') is distinct from 'boolean' then raise exception 'configuration rejected' using errcode='22023';end if;
  insert into control.nexloop_browser_applications values(p_tenant,item->>'application_id',1,(item->>'active')::boolean)
   on conflict(tenant_id,application_id) do update set revision=nexloop_browser_applications.revision+1,active=excluded.active
   where nexloop_browser_applications.active is distinct from excluded.active;
 end loop;
 for item in select value from jsonb_array_elements(m->'browser_business_applications') loop
  if item->>'application_id' is null or item->>'caller_application_id' is null or item->>'caller_application_id' not like 'eios:application:%'
   or item->>'application_version' is null or jsonb_typeof(item->'requested_scopes') is distinct from 'array' or jsonb_array_length(item->'requested_scopes') not between 1 and 64
   or not exists(select 1 from control.nexloop_browser_applications where tenant_id=p_tenant and application_id=item->>'application_id')
   or not exists(select 1 from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='application' and af.entity_key=array[item->>'caller_application_id',item->>'application_version']) then raise exception 'configuration rejected' using errcode='22023';end if;
  insert into control.nexloop_browser_business_applications values(p_tenant,item->>'application_id',item->>'caller_application_id',item->>'application_version',item->'requested_scopes')
   on conflict(tenant_id,application_id) do update set caller_application_id=excluded.caller_application_id,application_version=excluded.application_version,requested_scopes=excluded.requested_scopes;
 end loop;
 for item in select value from jsonb_array_elements(m->'browser_rate_policies') loop
  if item->>'action' is null or item->>'operator_principal_id' is distinct from 'nexloop_identity' or jsonb_typeof(item->'active') is distinct from 'boolean'
   or (item->>'maximum_attempts')::integer not between 1 and 1000000 or (item->>'window_seconds')::integer not between 1 and 3600 then raise exception 'configuration rejected' using errcode='22023';end if;
  insert into control.nexloop_browser_rate_policies values(p_tenant,item->>'action','nexloop_identity',(item->>'maximum_attempts')::integer,(item->>'window_seconds')::integer,(item->>'active')::boolean)
   on conflict(tenant_id,action,operator_principal_id) do update set maximum_attempts=excluded.maximum_attempts,window_seconds=excluded.window_seconds,active=excluded.active;
 end loop;
 for item in select value from jsonb_array_elements(m->'identity_allowances') loop
  if item->>'application_id' is null or item->>'operator_label' is null or jsonb_typeof(item->'enabled') is distinct from 'boolean'
   or item->>'idempotency_key_digest' is null or item->>'idempotency_key_digest' !~ '^[a-f0-9]{64}$'
   or (item->>'maximum_accounts')::integer not between 1 and 100
   or not exists(select 1 from control.nexloop_browser_applications where tenant_id=p_tenant and application_id=item->>'application_id' and active) then raise exception 'configuration rejected' using errcode='22023';end if;
  insert into control.nexloop_initial_identity_allowances(tenant_id,application_id,enabled,maximum_accounts,operator_label,idempotency_key_digest)
   values(p_tenant,item->>'application_id',(item->>'enabled')::boolean,(item->>'maximum_accounts')::integer,item->>'operator_label',item->>'idempotency_key_digest') on conflict do nothing;
  -- Initial allowance is never reset/reopened, nor quota replenished by revision.
  if exists(select 1 from control.nexloop_initial_identity_allowances a where a.tenant_id=p_tenant and a.application_id=item->>'application_id'
   and (a.maximum_accounts is distinct from (item->>'maximum_accounts')::integer or a.operator_label is distinct from item->>'operator_label' or a.idempotency_key_digest is distinct from item->>'idempotency_key_digest' or not a.enabled and (item->>'enabled')::boolean)) then raise exception 'configuration conflict' using errcode='40001';end if;
  if (item->>'enabled')::boolean is false then update control.nexloop_initial_identity_allowances set enabled=false where tenant_id=p_tenant and application_id=item->>'application_id';end if;
 end loop;
 -- Explicit increment covers new schema/config that lacks a legacy epoch trigger.
 update control.nexloop_tenants set authority_revision=authority_revision+1 where tenant_id=p_tenant returning authority_revision into v_revision;
 insert into control.nexloop_configuration_publications values(p_tenant,v_manifest,p_digest,session_user,clock_timestamp(),v_revision,m,v_secret_fingerprint);
 return jsonb_build_object('configured',true,'manifest_digest',p_digest,'authority_revision',v_revision);
end $$;

alter function control.nexloop_configure_manifest(text,text,text,text,bytea,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_manifest(text,text,text,text,bytea,jsonb) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime;
grant execute on function control.nexloop_configure_manifest(text,text,text,text,bytea,jsonb) to nexloop_configurator;

-- Group of a property in a type definition (first group by name; null = ungrouped, never derived).
create function ontology.nexloop_property_group_of(p_definition jsonb,p_property text) returns text
 language sql immutable set search_path=pg_catalog as $$
 select g->>'group_name' from jsonb_array_elements(coalesce(p_definition->'property_groups','[]'::jsonb)) g
 where g->'property_names' ? p_property order by g->>'group_name' limit 1
$$;

-- Core derivation (internal). Returns the basis, or null with no grant. Share-locks what it reads.
create function authz.nexloop_property_access_derivation(p_tenant text,p_principal text,p_world text,p_resource text,p_operation text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare m text[];t text;oid text;prop text;rule jsonb;restriction jsonb;v_version integer;def jsonb;basis_def jsonb;grp text;decision text;
begin
 if p_world is distinct from 'real' or p_operation not in ('read','edit') then return null;end if;
 m:=regexp_match(p_resource,'^eios:object:([A-Za-z][A-Za-z0-9_]{0,63})/([A-Za-z0-9][A-Za-z0-9._:-]{0,159})$');
 if m is not null then t:=m[1];oid:=m[2];
 else
  m:=regexp_match(p_resource,'^eios:property:([A-Za-z][A-Za-z0-9_]{0,63})/([A-Za-z0-9][A-Za-z0-9._:-]{0,159})/([A-Za-z][A-Za-z0-9_]{0,63})$');
  if m is not null then t:=m[1];oid:=m[2];prop:=m[3];
  else
   m:=regexp_match(p_resource,'^eios:property:([A-Za-z][A-Za-z0-9_]{0,63})/([A-Za-z][A-Za-z0-9_]{0,63})$');
   if m is null or p_operation<>'read' then return null;end if;  -- definition-level: READ only
   t:=m[1];prop:=m[2];
  end if;
 end if;
 select payload into rule from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_access_rule' and entity_key=array[p_principal,t] for share;
 if not found or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp()
  or not (rule->'operations' ? p_operation) then return null;end if;
 -- The owner's restriction decision must exist for the type: none recorded, nothing derived.
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[t] for share;
 if not found then return null;end if;
 if oid is not null then
  select schema_version into v_version from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=t and object_id=oid for share;
  if not found then return null;end if;
 else
  select max(v.version) into v_version from ontology.object_type_versions v where v.tenant_id=p_tenant and v.type_name=t;
  if v_version is null then return null;end if;
 end if;
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=t and version=v_version for share;
 if not found then return null;end if;
 if prop is not null then
  if not exists(select 1 from jsonb_array_elements(def->'properties') e where e->>'property_name'=prop) then return null;end if;
  grp:=ontology.nexloop_property_group_of(def,prop);
  if grp is null or not (rule->'property_groups' ? grp) or restriction->'restricted_groups' ? grp then return null;end if;
  select definition into basis_def from ontology.object_type_versions where tenant_id=p_tenant and type_name=t and version=(rule->>'basis_schema_version')::integer;
  if not found then return null;end if;
  if not exists(select 1 from jsonb_array_elements(basis_def->'properties') e where e->>'property_name'=prop) then
   -- Added after the rule: only a human-reviewed publication into this very group.
   if rule->'include_review_published' is distinct from 'true'::jsonb then return null;end if;
   select d.decision_id into decision from ontology.nexloop_review_decisions d join ontology.nexloop_candidate_definitions c
     on c.tenant_id=d.tenant_id and c.world=d.world and c.candidate_id=d.candidate_id
    where d.tenant_id=p_tenant and d.outcome='published' and d.publication->'published_refs' ? ('eios:property:'||t||'/'||prop)
     and c.kind='property' and c.candidate->'proposed'->>'property_group'=grp
    order by d.decided_at,d.decision_id limit 1;
   if decision is null then return null;end if;
  end if;
 end if;
 return jsonb_build_object('mode','derived','type_name',t,'schema_version',v_version,'property_group',grp,'review_decision_id',decision,
  'rule_hash',encode(sha256(convert_to(rule::text,'UTF8')),'hex'),'rule_valid_until',rule->>'valid_until',
  'restriction_hash',encode(sha256(convert_to(restriction::text,'UTF8')),'hex'));
end $$;

-- Path selection for the backend: grants nothing.
create function authz.nexloop_property_access_basis(p_digest text,p_world text,p_resource text,p_operation text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;tenant text;principal text;b jsonb;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',tenant,true);
 -- Configured grants of this principal (even empty) always win.
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key=array[principal,p_resource]) then
  return jsonb_build_object('mode','configured');end if;
 if ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then
  return jsonb_build_object('mode','configured');end if;
 b:=authz.nexloop_property_access_derivation(tenant,principal,p_world,p_resource,p_operation);
 return coalesce(b,jsonb_build_object('mode','configured'));
end $$;

-- Derived proof verification at use (read and edit).
create function authz.nexloop_assert_derived_property_access(p_digest text,p_world text,p_claims jsonb,p_operation text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;tenant text;principal text;basis jsonb:=p_claims->'derivation_basis';b jsonb;type_read jsonb;
begin
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 if p_claims->>'derivation' is distinct from 'type-property-v1' or jsonb_typeof(basis) is distinct from 'object'
  or p_claims->>'tenant_id' is distinct from tenant or p_claims->>'principal_id' is distinct from principal
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'world' is distinct from p_world or p_world is distinct from 'real'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource' or p_claims->>'operation' is distinct from p_operation
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or p_claims->'facts' is distinct from '[]'::jsonb
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then
  raise exception 'derived property access invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',tenant,true);
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key=array[principal,p_claims->>'resource_id']) then
  raise exception 'derived property access superseded' using errcode='42501';end if;
 b:=authz.nexloop_property_access_derivation(tenant,principal,p_world,p_claims->>'resource_id',p_operation);
 if b is null then raise exception 'derived property access unavailable' using errcode='42501';end if;
 if b->>'rule_hash' is distinct from basis->>'rule_hash' or b->>'restriction_hash' is distinct from basis->>'restriction_hash'
  or b->>'type_name' is distinct from basis->>'type_name' or b->'schema_version' is distinct from basis->'schema_version'
  or b->'property_group' is distinct from basis->'property_group' or b->'review_decision_id' is distinct from basis->'review_decision_id' then
  raise exception 'derived property access changed' using errcode='42501';end if;
 if (p_claims->>'expires_at')::timestamptz>(b->>'rule_valid_until')::timestamptz then raise exception 'derived property access expired' using errcode='42501';end if;
 -- The principal's current configured READ on the type itself (revocable through trusted configuration).
 type_read:=basis->'type_read';
 if jsonb_typeof(type_read) is distinct from 'object' or type_read->>'resource_id' is distinct from 'eios:object_type:'||(b->>'type_name')
  or type_read->>'operation' is distinct from 'read' or type_read ? 'derivation'
  or (p_claims->>'expires_at')::timestamptz>(type_read->>'expires_at')::timestamptz then
  raise exception 'derived property access requires type READ' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority_before_message_read_v0072(p_digest,p_world,type_read);
 return ident->'binding';
end $$;

alter function authz.nexloop_assert_read_authority(text,text,jsonb) rename to nexloop_assert_read_authority_before_property_access_v0083;
revoke all on function authz.nexloop_assert_read_authority_before_property_access_v0083(text,text,jsonb) from public;
create function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 if p_claims->>'derivation'='type-property-v1' then return authz.nexloop_assert_derived_property_access(p_digest,p_world,p_claims,'read');end if;
 return authz.nexloop_assert_read_authority_before_property_access_v0083(p_digest,p_world,p_claims);
end $$;
alter function authz.nexloop_assert_edit_authority(text,text,jsonb) rename to nexloop_assert_edit_authority_before_property_access_v0083;
revoke all on function authz.nexloop_assert_edit_authority_before_property_access_v0083(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker;
create function authz.nexloop_assert_edit_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 if p_claims->>'derivation'='type-property-v1' then return authz.nexloop_assert_derived_property_access(p_digest,p_world,p_claims,'edit');end if;
 return authz.nexloop_assert_edit_authority_before_property_access_v0083(p_digest,p_world,p_claims);
end $$;

-- Review workbench: before approve, which service principals would automatically read/write
-- the new property, its group and whether that group is restricted (approved design §4).
create function authz.nexloop_read_review_derivation_impact(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=authz.nexloop_assert_review_read(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 x ontology.nexloop_candidate_definitions%rowtype;owner text;grp text;restriction jsonb;
begin
 select * into x from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=c->>'candidate_id';
 if not found then return null;end if;
 if x.kind<>'property' then return jsonb_build_object('applies',false);end if;
 owner:=substring(x.candidate->'proposed'->>'owner_type_ref' from '^eios:object_type:([A-Za-z][A-Za-z0-9_]*)$');grp:=x.candidate->'proposed'->>'property_group';
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=t and fact_kind='property_group_restriction' and entity_key=array[owner];
 return jsonb_build_object('applies',true,'type_name',owner,'property_group',grp,'restriction_configured',restriction is not null,
  'restricted',coalesce(restriction->'restricted_groups' ? grp,false),
  'auto_access',case when restriction is null or restriction->'restricted_groups' ? grp then '[]'::jsonb else coalesce((select jsonb_agg(jsonb_build_object(
    'principal_id',f.payload->>'principal_id','operations',f.payload->'operations') order by f.payload->>'principal_id')
   from authz.nexloop_authority_facts f where f.tenant_id=t and f.fact_kind='property_access_rule' and f.payload->>'type_name'=owner
    and f.payload->'active'='true'::jsonb and (f.payload->>'valid_until')::timestamptz>clock_timestamp()
    and f.payload->'include_review_published'='true'::jsonb and f.payload->'property_groups' ? grp),'[]'::jsonb) end);
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['ontology.nexloop_property_group_of(jsonb,text)','authz.nexloop_property_access_derivation(text,text,text,text,text)',
  'authz.nexloop_property_access_basis(text,text,text,text)','authz.nexloop_assert_derived_property_access(text,text,jsonb,text)',
  'authz.nexloop_assert_read_authority(text,text,jsonb)','authz.nexloop_assert_edit_authority(text,text,jsonb)',
  'authz.nexloop_read_review_derivation_impact(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_property_access_basis(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
grant execute on function authz.nexloop_read_review_derivation_impact(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
