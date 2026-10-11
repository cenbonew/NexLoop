-- NX-052: the tenant row lock of the configuration and initial-identity entries no longer blocks tenant foreign key checks.
-- Both bodies are copied unchanged from their current definitions (configure_manifest: 0142; initial_identity_create: 0050)
-- except one lock each: FOR UPDATE -> FOR NO KEY UPDATE. Neither function changes tenant_id or deletes a tenant, so NO KEY
-- UPDATE still serializes them against each other and against FOR SHARE authorization reads, but not against the foreign key
-- check's FOR KEY SHARE. create or replace keeps owner and grants. initial_identity_create also gets pg_temp listed last
-- (its 0050 search_path was pg_catalog alone, which the definer check refuses for a newly defined function). Review: docs/implementation/NX-052-tenant-lock-review.md.
create or replace function control.nexloop_configure_manifest(p_tenant text,p_text text,p_digest text,p_key_id text,p_key bytea,p_credentials jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
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
 select authority_revision into v_revision from control.nexloop_tenants where tenant_id=p_tenant for no key update;
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
  if v_kind is null or v_kind not in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision','message_read_rule','property_access_rule','property_group_restriction','message_purpose_rule','relay_source_read_rule')
   or d->>'tenant_id' is distinct from p_tenant or cardinality(v_key) not between 1 and 3 then raise exception 'configuration rejected' using errcode='22023';end if;
  -- Accepted-Message READ derivation rule: one per service principal, exact shape only.
  if v_kind='message_read_rule' and (cardinality(v_key)<>1 or d->>'principal_id' is distinct from v_key[1]
   or (select count(*) from jsonb_object_keys(d))<>6 or not (d ?& array['tenant_id','principal_id','type_name','fields','active','valid_until'])
   or d->>'type_name' is distinct from 'Message' or jsonb_typeof(d->'fields') is distinct from 'array'
   or not (d->'fields' @> '["actor","body"]'::jsonb)
   or exists(select 1 from jsonb_array_elements(d->'fields') f where jsonb_typeof(f) is distinct from 'string' or f#>>'{}' not in ('accepted_at','actor','body','conversation_id','sequence'))
   or d->'fields' is distinct from (select jsonb_agg(distinct f order by f) from jsonb_array_elements_text(d->'fields') f)
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
  -- Purpose-bound Message READ rule (plan B): one per (service principal, purpose), exact shape only.
  if v_kind='message_purpose_rule' and (cardinality(v_key)<>2 or d->>'principal_id' is distinct from v_key[1] or d->>'purpose' is distinct from v_key[2]
   or (select count(*) from jsonb_object_keys(d))<>7 or not (d ?& array['tenant_id','principal_id','purpose','type_name','fields','active','valid_until'])
   or d->>'purpose' not in ('claim_extraction','claim_matching') or d->>'type_name' is distinct from 'Message'
   or jsonb_typeof(d->'fields') is distinct from 'array' or not (d->'fields' @> '["body"]'::jsonb)
   or exists(select 1 from jsonb_array_elements(d->'fields') f where jsonb_typeof(f) is distinct from 'string' or f#>>'{}' not in ('accepted_at','actor','body','conversation_id','sequence'))
   or d->'fields' is distinct from (select jsonb_agg(distinct f order by f) from jsonb_array_elements_text(d->'fields') f)
   or jsonb_typeof(d->'active') is distinct from 'boolean' or jsonb_typeof(d->'valid_until') is distinct from 'string'
   or (d->>'valid_until')::timestamptz is null
   or not exists(select 1 from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='subject_authority'
    and af.entity_key=array[v_key[1]] and af.payload->>'subject_kind'='service')) then
   raise exception 'message purpose rule rejected' using errcode='22023';end if;
  -- 0161: relay_source_read_rule [source principal]: the relay it prepares Contexts for (a managed service principal).
  if v_kind='relay_source_read_rule' and (cardinality(v_key)<>1 or d->>'principal_id' is distinct from v_key[1] or d->>'tenant_id' is distinct from p_tenant
   or (select count(*) from jsonb_object_keys(d))<>5 or not (d ?& array['tenant_id','principal_id','relay_principal','active','valid_until'])
   or jsonb_typeof(d->'relay_principal') is distinct from 'string' or length(d->>'relay_principal') not between 1 and 320 or d->>'relay_principal'=d->>'principal_id'
   or jsonb_typeof(d->'active') is distinct from 'boolean' or jsonb_typeof(d->'valid_until') is distinct from 'string' or (d->>'valid_until')::timestamptz is null
   or not exists(select 1 from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='subject_authority'
    and af.entity_key=array[d->>'relay_principal'] and af.payload->>'subject_kind'='service')) then
   raise exception 'relay source read rule rejected' using errcode='22023';end if;
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
create or replace function control.nexloop_initial_identity_create(p_tenant text,p_application text,p_operator text,p_request text,p_operator_label text,
 p_subject jsonb,p_membership jsonb,p_account jsonb,p_hash text,p_password_fingerprint text,p_seal_key bytea)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare allowance control.nexloop_initial_identity_allowances;prior control.nexloop_initial_identity_receipts;
 v_request uuid;v_public_digest text;v_now timestamptz:=clock_timestamp();v_subject text;v_principal text;v_account text;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user or p_request is null
  or p_tenant is null or p_application is null or p_operator_label is null
  or jsonb_typeof(p_subject) is distinct from 'object' or jsonb_typeof(p_membership) is distinct from 'object' or jsonb_typeof(p_account) is distinct from 'object'
  or octet_length(p_subject::text)+octet_length(p_membership::text)+octet_length(p_account::text)>1048576
  or p_password_fingerprint is null or p_password_fingerprint !~ '^[a-f0-9]{64}$'
  or p_seal_key is null or octet_length(p_seal_key)<>32
  or p_hash is null or p_hash not like '$argon2id$%' or octet_length(p_hash)>1024
 then raise exception 'initial identity rejected' using errcode='42501';end if;
 v_request:=p_request::uuid;
 v_public_digest:=encode(sha256(convert_to(jsonb_build_object('tenant',p_tenant,'application',p_application,'operator',p_operator_label,
  'subject',p_subject,'membership',p_membership,'account',p_account)::text,'UTF8')),'hex');
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active' for no key update;
 if not found then raise exception 'initial identity rejected' using errcode='42501';end if;
 perform 1 from control.nexloop_browser_applications where tenant_id=p_tenant and application_id=p_application and active for share;
 if not found then raise exception 'initial identity rejected' using errcode='42501';end if;
 select * into allowance from control.nexloop_initial_identity_allowances where tenant_id=p_tenant and application_id=p_application for update;
 if not found or allowance.operator_label is distinct from p_operator_label or allowance.idempotency_key_digest is distinct from encode(sha256(p_seal_key),'hex') then raise exception 'initial identity rejected' using errcode='42501';end if;
 select * into prior from control.nexloop_initial_identity_receipts where tenant_id=p_tenant and application_id=p_application and request_id=v_request;
 if found then
  if prior.public_digest is distinct from v_public_digest or prior.password_fingerprint is distinct from p_password_fingerprint then
   raise exception 'initial identity conflict' using errcode='40001';end if;
  -- Replay after closed allowance does not create/account/grant/session again.
  return jsonb_build_object('created',false,'subject_id',prior.subject_id,'principal_id',prior.principal_id,'account_id',prior.account_id);
 end if;
 if not allowance.enabled or allowance.created_accounts>=allowance.maximum_accounts then raise exception 'initial identity closed' using errcode='42501';end if;
 v_subject:=p_subject->>'subject_id';v_principal:=p_membership->>'principal_id';v_account:=p_account->>'local_account_id';
 if v_subject is null or v_principal is null or v_account is null
  or p_subject->>'kind' is distinct from 'human' or p_subject->>'status' is distinct from 'active' or p_subject->'revision' is distinct from '1'::jsonb
  or p_membership->>'tenant_id' is distinct from p_tenant or p_membership->>'subject_id' is distinct from v_subject
  or p_membership->'trusted_attributes' is distinct from '{}'::jsonb or p_membership->'valid_until' is null
  or p_membership->>'status' is distinct from 'active' or p_membership->'revision' is distinct from '1'::jsonb
  or p_membership->>'valid_from' is null or (p_membership->>'valid_from')::timestamptz>clock_timestamp()
  or p_membership->'valid_until' is distinct from 'null'::jsonb and (p_membership->>'valid_until')::timestamptz<=clock_timestamp()
  or p_account->>'tenant_id' is distinct from p_tenant or p_account->>'subject_id' is distinct from v_subject
  or p_account->>'username' is null or p_account->>'status' is distinct from 'active'
  or p_account->'revision' is distinct from '1'::jsonb or p_account->'session_epoch' is distinct from '1'::jsonb
  or p_account->'failed_attempts' is distinct from '0'::jsonb or p_account->'lockout_level' is distinct from '0'::jsonb
  or p_account->'locked_until' is distinct from 'null'::jsonb or p_account ? 'password_hash' or p_account ? 'password_history'
  or p_account->>'created_at' is null or p_account->>'updated_at' is null
  or (p_account->>'created_at')::timestamptz>clock_timestamp() or (p_account->>'updated_at')::timestamptz>clock_timestamp()
 then raise exception 'initial identity rejected' using errcode='42501';end if;
 insert into control.nexloop_initial_identity_receipts values(p_tenant,p_application,v_request,v_public_digest,p_password_fingerprint,v_subject,v_principal,v_account,p_operator_label,clock_timestamp());
 insert into control.nexloop_browser_subjects values(v_subject,p_subject);
 insert into control.nexloop_browser_memberships values(p_tenant,v_principal,v_subject,p_membership);
 insert into control.nexloop_browser_accounts values(p_tenant,v_account,v_subject,p_account->>'username',p_hash,'{}',p_account);
 update control.nexloop_initial_identity_allowances set created_accounts=created_accounts+1,
  enabled=case when created_accounts+1>=maximum_accounts then false else enabled end where tenant_id=p_tenant and application_id=p_application;
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,v_subject,'initial_identity','created',session_user,p_request,p_request,
   jsonb_build_object('account_id',v_account,'principal_id',v_principal,'operator_label',p_operator_label,'business_grant_created',false),clock_timestamp());
 update control.nexloop_tenants set authority_revision=authority_revision+1 where tenant_id=p_tenant;
 return jsonb_build_object('created',true,'subject_id',v_subject,'principal_id',v_principal,'account_id',v_account);
end $$;
