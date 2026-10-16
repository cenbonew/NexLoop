-- NX-032 G-ROLE-READ (temporary number 0211; dispatcher 2026-10-17: option A, in the manner of 0161 relay_source_read_rule).
-- Design: docs/release/assembly-dryrun-gaps.md (G-ROLE-READ) and docs/release/run-g-plan-20261017.md.
--
-- A plan reevaluation Role Run is issued and its Context bound by the Role Source principal (plan_reevaluation.RoleRunLauncher),
-- which reads the plan's Role objects one by one. Configured per-object grants would change the Source credential's requested
-- scopes (credential rotation) and write per-object facts; instead a trusted-configuration rule role_source_read_rule [source
-- principal] (stage-principals section role_source_read_rules, compiled by stage_secrets register through 0050) lets SQL derive,
-- at each read, READ for that Source on exactly the objects of a plan that is current and active (runtime.nexloop_plans and its
-- latest event, 0106) and whose PlanStep (recipe step_id) lists the Source in submitter_principals:
--   * eios:object:Consumer/<plan consumer>, eios:object:Goal/<step goal_id>,
--   * the PlanStep, EffectControl (recipe control_id), ServiceOffering (recipe offering_id), ConsumerServiceOffering (recipe
--     binding_id), RoleDefinition (recipe role_id), ConsumerRoleLink (recipe link_id), RoleExecutionCeiling (role ceiling_ref) and
--     RoleAssignmentScope (link scope) objects and, for these, only the fixed properties the Role launch reads (measured on the
--     production launch path, tests/test_plan_role_launcher_pg.py);
-- and only when the recipe is consistent (link names the recipe role and the plan consumer, step and binding name the plan
-- consumer, binding names the recipe offering and this Source) and the consumer is not erasing. Nothing per object is written (no authority epoch
-- change), no scope is added to any credential. Each use re-reads the rule, the plan's current event, the step, role, link and
-- binding, the erasure state and the credential: a plan that is invalidated, closed, stale or superseded, a step no longer naming
-- the Source, a revoked rule or credential end the READ at the next read. Only an unrestricted service credential without a Run
-- context derives; configured grants of the principal on the exact target always win; owner-restricted property groups are never
-- derived. 0001..0210 are unchanged; the read entries are renamed and wrapped as 0161 did.

-- 1. The new fact kind and its exact shape in trusted configuration ---------------------------------------------------------
alter table authz.nexloop_authority_facts drop constraint nexloop_authority_facts_fact_kind_check;
alter table authz.nexloop_authority_facts add constraint nexloop_authority_facts_fact_kind_check
 check(fact_kind in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision','agent','agent_release','agent_application','message_read_rule','property_access_rule','property_group_restriction','message_purpose_rule','relay_source_read_rule','role_source_read_rule'));
-- Trusted configuration: the current body (0162) copied verbatim, plus the role_source_read_rule kind and shape.
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
  if v_kind is null or v_kind not in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision','message_read_rule','property_access_rule','property_group_restriction','message_purpose_rule','relay_source_read_rule','role_source_read_rule')
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
  -- 0211 (G-ROLE-READ): role_source_read_rule [source principal]: the Source of plan reevaluation Role Runs (a managed service principal).
  if v_kind='role_source_read_rule' and (cardinality(v_key)<>1 or d->>'principal_id' is distinct from v_key[1] or d->>'tenant_id' is distinct from p_tenant
   or (select count(*) from jsonb_object_keys(d))<>4 or not (d ?& array['tenant_id','principal_id','active','valid_until'])
   or jsonb_typeof(d->'active') is distinct from 'boolean' or jsonb_typeof(d->'valid_until') is distinct from 'string' or (d->>'valid_until')::timestamptz is null
   or not exists(select 1 from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='subject_authority'
    and af.entity_key=array[v_key[1]] and af.payload->>'subject_kind'='service')) then
   raise exception 'role source read rule rejected' using errcode='22023';end if;
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

-- 2. The derivation (grants nothing by itself) --------------------------------------------------------------------------------
create function authz.nexloop_role_source_derivation(p_tenant text,p_principal text,p_world text,p_resource text,p_operation text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m text[];t text;oid text;prop text;rule jsonb;restriction jsonb;pl record;cur_version integer;cur_status text;
 v_step ontology.objects;v_role ontology.objects;v_link ontology.objects;v_binding ontology.objects;v_object ontology.objects;def jsonb;grp text;
begin
 if p_world is distinct from 'real' or p_operation is distinct from 'read' then return null;end if;
 m:=regexp_match(p_resource,'^eios:object:(Consumer|Goal|PlanStep|EffectControl|ServiceOffering|ConsumerServiceOffering|RoleDefinition|ConsumerRoleLink|RoleExecutionCeiling|RoleAssignmentScope)/([a-f0-9]{64})$');
 if m is not null then t:=m[1];oid:=m[2];
 else
  m:=regexp_match(p_resource,'^eios:property:(PlanStep|EffectControl|ServiceOffering|ConsumerServiceOffering|RoleDefinition|ConsumerRoleLink|RoleExecutionCeiling|RoleAssignmentScope)/([a-f0-9]{64})/([a-z_]{1,64})$');
  if m is null then return null;end if;
  t:=m[1];oid:=m[2];prop:=m[3];
  -- The properties the Role launch reads (role_runs / role_policies / effect_contexts / service_offerings field lists), fixed here.
  if (t='PlanStep' and prop not in ('consumer_id','state','submitter_principals'))
   or (t='EffectControl' and prop not in ('allow_effect','budget_units','executor_principal','valid_until'))
   or (t='ServiceOffering' and prop not in ('active','allowed_discounts','allowed_guarantees','content_kind','currency','delivery_action','eligibility',
       'evidence_kind','price_amount','service_code','title','valid_until'))
   or (t='ConsumerServiceOffering' and prop not in ('active','consumer_id','offering_id','offering_revision','source_principal'))
   or (t='RoleDefinition' and prop not in ('active','ceiling_ref','name','responsibility','valid_from','valid_until'))
   or (t='ConsumerRoleLink' and prop not in ('active','consumer_id','role_id','scope','valid_from','valid_until'))
   or (t='RoleExecutionCeiling' and prop not in ('action_resources','active','budget','consumer_ids','effect_units','goal_ids','step_ids','valid_from','valid_until'))
   or (t='RoleAssignmentScope' and prop not in ('active','consumer_id','goal_ids','role_id','step_ids','valid_from','valid_until')) then return null;end if;
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select payload into rule from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='role_source_read_rule' and entity_key=array[p_principal] for share;
 if not found or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp() then return null;end if;
 -- The plans naming this object (directly in the recipe, or through the recipe's step, role or link).
 for pl in select p.plan_id,p.version,p.consumer_id,p.recipe from runtime.nexloop_plans p where p.tenant_id=p_tenant and p.world=p_world and (
     (t='Consumer' and p.consumer_id=oid) or (t='PlanStep' and p.recipe->>'step_id'=oid) or (t='EffectControl' and p.recipe->>'control_id'=oid)
     or (t='ServiceOffering' and p.recipe->>'offering_id'=oid) or (t='ConsumerServiceOffering' and p.recipe->>'binding_id'=oid)
     or (t='RoleDefinition' and p.recipe->>'role_id'=oid) or (t='ConsumerRoleLink' and p.recipe->>'link_id'=oid)
     or (t='Goal' and exists(select 1 from ontology.objects s where s.tenant_id=p_tenant and s.world=p_world and s.type_name='PlanStep'
        and s.object_id=p.recipe->>'step_id' and s.properties->>'goal_id'=oid))
     or (t='RoleExecutionCeiling' and exists(select 1 from ontology.objects r where r.tenant_id=p_tenant and r.world=p_world and r.type_name='RoleDefinition'
        and r.object_id=p.recipe->>'role_id' and r.properties->>'ceiling_ref'=oid))
     or (t='RoleAssignmentScope' and exists(select 1 from ontology.objects l where l.tenant_id=p_tenant and l.world=p_world and l.type_name='ConsumerRoleLink'
        and l.object_id=p.recipe->>'link_id' and l.properties->>'scope'=oid)))
   order by p.plan_id,p.version
 loop
  -- Only the plan's current version, and only while it is active (an invalidated / closed / stale / superseded plan derives nothing).
  select c.version,c.status into cur_version,cur_status from runtime.nexloop_plan_current(p_tenant,p_world,pl.plan_id) c;
  if cur_version is distinct from pl.version or cur_status is distinct from 'active' then continue;end if;
  if runtime.nexloop_consumer_erasing(p_tenant,p_world,pl.consumer_id) then continue;end if;
  perform set_config('eios.tenant_id',p_tenant,true);
  select * into v_step from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=pl.recipe->>'step_id' and type_name='PlanStep' for share;
  if v_step.object_id is null or jsonb_typeof(v_step.properties->'submitter_principals') is distinct from 'array'
   or not (v_step.properties->'submitter_principals' ? p_principal) or v_step.properties->>'consumer_id' is distinct from pl.consumer_id then continue;end if;
  select * into v_role from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=pl.recipe->>'role_id' and type_name='RoleDefinition' for share;
  select * into v_link from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=pl.recipe->>'link_id' and type_name='ConsumerRoleLink' for share;
  select * into v_binding from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=pl.recipe->>'binding_id' and type_name='ConsumerServiceOffering' for share;
  if v_role.object_id is null or v_link.object_id is null or v_binding.object_id is null
   or v_link.properties->>'role_id' is distinct from v_role.object_id or v_link.properties->>'consumer_id' is distinct from pl.consumer_id
   or v_binding.properties->>'consumer_id' is distinct from pl.consumer_id or v_binding.properties->>'offering_id' is distinct from pl.recipe->>'offering_id'
   or v_binding.properties->>'source_principal' is distinct from p_principal then continue;end if;
  if (t='Goal' and v_step.properties->>'goal_id' is distinct from oid) or (t='RoleExecutionCeiling' and v_role.properties->>'ceiling_ref' is distinct from oid)
   or (t='RoleAssignmentScope' and v_link.properties->>'scope' is distinct from oid) then continue;end if;
  select * into v_object from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=oid and type_name=t for share;
  if v_object.object_id is null then return null;end if;
  grp:=null;restriction:=null;
  if prop is not null then
   select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=t and version=v_object.schema_version for share;
   if def is null or not exists(select 1 from jsonb_array_elements(def->'properties') d where d->>'property_name'=prop) then return null;end if;
   grp:=ontology.nexloop_property_group_of(def,prop);
   -- ADR-019 §2: an owner-restricted group is never derived.
   select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[t] for share;
   if grp is not null and restriction->'restricted_groups' ? grp then return null;end if;
  end if;
  return jsonb_build_object('mode','derived','derivation','role-source-v1','type_name',t,'consumer_id',pl.consumer_id,'plan_id',pl.plan_id,'plan_version',pl.version,
   'rule_hash',encode(sha256(convert_to(rule::text,'UTF8')),'hex'),'rule_valid_until',rule->>'valid_until',
   'restriction_hash',case when restriction is null then null else encode(sha256(convert_to(restriction::text,'UTF8')),'hex') end);
 end loop;
 return null;
end $$;
alter function authz.nexloop_role_source_derivation(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_source_derivation(text,text,text,text,text) from public;

-- 3. Path selection (0161 basis, renamed and wrapped): configured grants win; then the earlier derivations; then this one.
alter function authz.nexloop_property_access_basis(text,text,text,text) rename to nexloop_property_access_basis_before_role_source_v0211;
revoke all on function authz.nexloop_property_access_basis_before_role_source_v0211(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_property_access_basis(p_digest text,p_world text,p_resource text,p_operation text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare b jsonb:=authz.nexloop_property_access_basis_before_role_source_v0211(p_digest,p_world,p_resource,p_operation);ident jsonb;tenant text;principal text;r jsonb;
begin
 if b->>'mode'='derived' then return b;end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',tenant,true);
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key=array[principal,p_resource])
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then return b;end if;
 r:=authz.nexloop_role_source_derivation(tenant,principal,p_world,p_resource,p_operation);
 return coalesce(r,b);
end $$;
alter function authz.nexloop_property_access_basis(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_property_access_basis(text,text,text,text) from public;
grant execute on function authz.nexloop_property_access_basis(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

-- 4. The derived proof, verified at each use.
create function authz.nexloop_assert_role_source_read(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;tenant text;principal text;basis jsonb:=p_claims->'derivation_basis';b jsonb;
begin
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);  -- raises for a revoked or expired credential
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 if p_claims->>'derivation' is distinct from 'role-source-v1' or jsonb_typeof(basis) is distinct from 'object'
  or p_claims->>'tenant_id' is distinct from tenant or p_claims->>'principal_id' is distinct from principal
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'world' is distinct from p_world or p_world is distinct from 'real'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or p_claims->'facts' is distinct from '[]'::jsonb
  or exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=p_digest)
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then
  raise exception 'derived role source read invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',tenant,true);
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key=array[principal,p_claims->>'resource_id']) then
  raise exception 'derived role source read superseded' using errcode='42501';end if;
 b:=authz.nexloop_role_source_derivation(tenant,principal,p_world,p_claims->>'resource_id','read');
 if b is null then raise exception 'derived role source read unavailable' using errcode='42501';end if;
 if b->>'rule_hash' is distinct from basis->>'rule_hash' or b->>'consumer_id' is distinct from basis->>'consumer_id'
  or b->>'plan_id' is distinct from basis->>'plan_id' or b->'plan_version' is distinct from basis->'plan_version' or b->>'type_name' is distinct from basis->>'type_name'
  or b->'restriction_hash' is distinct from basis->'restriction_hash' then raise exception 'derived role source read changed' using errcode='42501';end if;
 if (p_claims->>'expires_at')::timestamptz>(b->>'rule_valid_until')::timestamptz then raise exception 'derived role source read expired' using errcode='42501';end if;
 return ident->'binding';
end $$;
alter function authz.nexloop_assert_role_source_read(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_role_source_read(text,text,jsonb) from public;

-- 5. Both read entries (the public one and the inner one the message derivations call by name) dispatch this derivation.
alter function authz.nexloop_assert_read_authority(text,text,jsonb) rename to nexloop_assert_read_authority_before_role_source_v0211;
revoke all on function authz.nexloop_assert_read_authority_before_role_source_v0211(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if p_claims->>'derivation'='role-source-v1' then
  perform runtime.nexloop_assert_not_erasing(p_world,p_claims);
  return authz.nexloop_assert_role_source_read(p_digest,p_world,p_claims);
 end if;
 return authz.nexloop_assert_read_authority_before_role_source_v0211(p_digest,p_world,p_claims);
end $$;
alter function authz.nexloop_assert_read_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_read_authority(text,text,jsonb) from public;
alter function authz.nexloop_assert_read_authority_before_message_read_v0072(text,text,jsonb) rename to nexloop_assert_read_authority_inner_before_role_source_v0211;
revoke all on function authz.nexloop_assert_read_authority_inner_before_role_source_v0211(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_assert_read_authority_before_message_read_v0072(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if p_claims->>'derivation'='role-source-v1' then
  perform runtime.nexloop_assert_not_erasing(p_world,p_claims);
  return authz.nexloop_assert_role_source_read(p_digest,p_world,p_claims);
 end if;
 return authz.nexloop_assert_read_authority_inner_before_role_source_v0211(p_digest,p_world,p_claims);
end $$;
alter function authz.nexloop_assert_read_authority_before_message_read_v0072(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_read_authority_before_message_read_v0072(text,text,jsonb) from public;
