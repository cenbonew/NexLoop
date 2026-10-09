-- Deployment authorization plan B (temporary number): purpose-bound Message/Conversation
-- READ for NexLoop's own background services, derived in SQL from governed work state.
-- The versioned service manifest (deploy/authorization/service-grants.v<N>.json,
-- section message_read_rules) compiles into message_purpose_rule facts [principal,
-- purpose], written only through trusted configuration:
--   * claim_extraction: the Conversation and Messages referenced by an extraction task
--     of queue claim-extraction that this exact credential currently leases (running,
--     lease not expired, fence as when the proof was signed);
--   * claim_matching: the evidence Message of a Claim still to be matched (unresolved,
--     needs_resolution, awaiting_definition, or a hypothesis without a recorded match),
--     and that Claim's Conversation object (to read its Claims).
-- Each use re-reads, under share locks, the rule, the principal's current EXECUTE grant
-- on the purpose Action, the task lease or Claim state, and the stored Message,
-- acceptance and Conversation rows. Revoking the rule or the Action grant, deleting the
-- Message, moving it out of the referenced Conversation, finishing the task or resolving
-- the Claim ends the READ at the next use. Configured grants of the principal on the
-- exact target always win; nothing per Message is written, so accepting a Message never
-- advances the authority epoch. 0001..0098 are unchanged; the read-assertion memo (0096)
-- is untouched (these claims carry a derivation and always run the full check).

alter table authz.nexloop_authority_facts drop constraint nexloop_authority_facts_fact_kind_check;
alter table authz.nexloop_authority_facts add constraint nexloop_authority_facts_fact_kind_check
 check(fact_kind in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision','agent','agent_release','agent_application','message_read_rule','property_access_rule','property_group_restriction','message_purpose_rule'));

-- Trusted configuration (0086 body): adds the message_purpose_rule kind and its exact shape.
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
  if v_kind is null or v_kind not in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision','message_read_rule','property_access_rule','property_group_restriction','message_purpose_rule')
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

-- The purpose Action the principal must currently hold for its purpose READ.
create function authz.nexloop_message_purpose_action(p_purpose text) returns text
language sql immutable security definer set search_path=pg_catalog,pg_temp as $$
 select case p_purpose when 'claim_extraction' then 'eios:action:nexloop.claim.extract:1'
  when 'claim_matching' then 'eios:action:nexloop.claim.match:1' end
$$;

-- A Claim the matcher still has to match (or re-match after a definition decision).
create function authz.nexloop_claim_match_pending(p_tenant text,p_world text,p_claim text,p_state text) returns boolean
language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select p_state in ('unresolved','needs_resolution','awaiting_definition')
  or (p_state='hypothesis_only' and not exists(select 1 from ontology.nexloop_claim_matches x
   where x.tenant_id=p_tenant and x.world=p_world and x.claim_id=p_claim))
$$;

-- Basis lookup only (no locks, grants nothing): which purpose, task lease or Claim
-- currently covers this Message/Conversation for this credential. Null when none.
create function authz.nexloop_message_purpose_basis_for(p_digest text,p_world text,p_tenant text,p_principal text,p_kind text,p_id text) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_purpose text;rule jsonb;v_conversation text;j runtime.jobs;v_claim text;held jsonb;
begin
 if p_kind='Message' then
  select cm.conversation_id into v_conversation from runtime.nexloop_conversation_messages cm
   where cm.tenant_id=p_tenant and cm.world=p_world and cm.message_id=p_id;
  if not found then return null;end if;
 elsif p_kind='Conversation' then v_conversation:=p_id;
 else return null;end if;
 foreach v_purpose in array array['claim_extraction','claim_matching'] loop
  select payload into rule from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='message_purpose_rule' and entity_key=array[p_principal,v_purpose];
  continue when not found or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp()
   or (p_kind='Conversation' and not (rule->'fields' ? 'conversation_id'));
  select payload into held from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='grants'
   and entity_key=array[p_principal,authz.nexloop_message_purpose_action(v_purpose)];
  continue when not found or jsonb_typeof(held->'grants') is distinct from 'array' or jsonb_array_length(held->'grants')=0;
  if v_purpose='claim_extraction' then
   select * into j from runtime.jobs x where x.tenant_id=p_tenant and x.world=p_world and x.queue='claim-extraction' and x.capability_name='NexLoop.event'
    and x.status='running' and x.lease_until>clock_timestamp() and x.lease_credential=p_digest
    and x.normalized_input->>'conversation_id'=v_conversation and (p_kind='Conversation' or x.normalized_input->'message_ids' ? p_id)
    order by x.lease_until desc,x.job_id limit 1;
   if found then
    return jsonb_build_object('mode','purpose','purpose',v_purpose,'fields',rule->'fields','conversation_id',v_conversation,
     'rule_hash',encode(sha256(convert_to(rule::text,'UTF8')),'hex'),'rule_valid_until',rule->>'valid_until',
     'job_id',j.job_id,'fence',j.fencing_token,'lease_until',j.lease_until);
   end if;
  else
   select c.claim_id into v_claim from ontology.nexloop_claims c where c.tenant_id=p_tenant and c.world=p_world and c.conversation_id=v_conversation
    and (p_kind='Conversation' or c.source_message_id=p_id) and authz.nexloop_claim_match_pending(c.tenant_id,c.world,c.claim_id,c.resolution_state)
    order by c.claim_id limit 1;
   if found then
    return jsonb_build_object('mode','purpose','purpose',v_purpose,'fields',rule->'fields','conversation_id',v_conversation,
     'rule_hash',encode(sha256(convert_to(rule::text,'UTF8')),'hex'),'rule_valid_until',rule->>'valid_until','claim_id',v_claim);
   end if;
  end if;
 end loop;
 return null;
end $$;

-- The "configured" answer, flagged while the principal has an active purpose rule.
create function authz.nexloop_message_purpose_configured(p_tenant text,p_principal text) returns jsonb
language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select case when exists(select 1 from authz.nexloop_authority_facts f where f.tenant_id=p_tenant and f.fact_kind='message_purpose_rule'
   and f.entity_key[1]=p_principal and f.payload->'active'='true'::jsonb)
  then jsonb_build_object('mode','configured','purpose_rules',true) else jsonb_build_object('mode','configured') end
$$;

-- Path selection (0086 bodies) plus the purpose fallback when no accepted-Message rule applies.
-- "configured" answers carry purpose_rules=true while the principal has an active purpose
-- rule, so a client never keeps such an answer beyond one unit of work.
create or replace function authz.nexloop_message_read_basis(p_digest text,p_world text,p_message text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;tenant text;principal text;rule jsonb;cm runtime.nexloop_conversation_messages;conv runtime.nexloop_conversations;purpose jsonb;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 if p_message is null or p_message !~ '^[a-f0-9]{64}$' then raise exception 'message read basis unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',tenant,true);
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key[1]=principal
   and entity_key[2] in ('eios:object:Message/'||p_message,'eios:property:Message/'||p_message||'/actor','eios:property:Message/'||p_message||'/body',
    'eios:property:Message/'||p_message||'/accepted_at','eios:property:Message/'||p_message||'/conversation_id','eios:property:Message/'||p_message||'/sequence')) then
  return jsonb_build_object('mode','configured');end if;
 if p_world is distinct from 'real' or ident->'binding'->>'subject_kind' is distinct from 'service'
  or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then return jsonb_build_object('mode','configured');end if;
 select payload into rule from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='message_read_rule' and entity_key=array[principal];
 if not found or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp() then
  purpose:=authz.nexloop_message_purpose_basis_for(p_digest,p_world,tenant,principal,'Message',p_message);
  return coalesce(purpose,authz.nexloop_message_purpose_configured(tenant,principal));
 end if;
 select * into cm from runtime.nexloop_conversation_messages where tenant_id=tenant and world=p_world and message_id=p_message;
 if not found then raise exception 'message read basis unavailable' using errcode='42501';end if;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=cm.conversation_id;
 return jsonb_build_object('mode','derived','consumer_id',conv.consumer_id,'conversation_id',cm.conversation_id,'fields',rule->'fields',
  'rule_hash',encode(sha256(convert_to(rule::text,'UTF8')),'hex'),'rule_valid_until',rule->>'valid_until');
end $$;

create or replace function authz.nexloop_conversation_read_basis(p_digest text,p_world text,p_conversation text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;tenant text;principal text;rule jsonb;conv runtime.nexloop_conversations;purpose jsonb;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 if p_conversation is null or p_conversation !~ '^[a-f0-9]{64}$' then raise exception 'conversation read basis unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',tenant,true);
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key[1]=principal
   and entity_key[2] in ('eios:object:Conversation/'||p_conversation,'eios:property:Conversation/'||p_conversation||'/consumer_id','eios:property:Conversation/'||p_conversation||'/owner_principal')) then
  return jsonb_build_object('mode','configured');end if;
 if p_world is distinct from 'real' or ident->'binding'->>'subject_kind' is distinct from 'service'
  or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then return jsonb_build_object('mode','configured');end if;
 select payload into rule from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='message_read_rule' and entity_key=array[principal];
 if not found or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp() or not (rule->'fields' ? 'conversation_id') then
  purpose:=authz.nexloop_message_purpose_basis_for(p_digest,p_world,tenant,principal,'Conversation',p_conversation);
  return coalesce(purpose,authz.nexloop_message_purpose_configured(tenant,principal));
 end if;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=p_conversation;
 if not found then raise exception 'conversation read basis unavailable' using errcode='42501';end if;
 return jsonb_build_object('mode','derived','consumer_id',conv.consumer_id,'conversation_id',conv.conversation_id,'fields',rule->'fields',
  'rule_hash',encode(sha256(convert_to(rule::text,'UTF8')),'hex'),'rule_valid_until',rule->>'valid_until');
end $$;

-- Purpose-bound claim verification: every condition is re-read under share locks at use.
create function authz.nexloop_assert_purpose_message_read(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;tenant text;principal text;basis jsonb:=p_claims->'derivation_basis';v_purpose text;rule jsonb;held jsonb;
 v_kind text;v_id text;v_field text;v_conversation text;m ontology.objects;cm runtime.nexloop_conversation_messages;j runtime.jobs;cl ontology.nexloop_claims;
 v_targets text[];
begin
 v_kind:=substring(p_claims->>'resource_id' from '^eios:(?:object|property):(Message|Conversation)/[a-f0-9]{64}(?:/[a-z_]{1,64})?$');
 v_id:=substring(p_claims->>'resource_id' from '^eios:(?:object|property):(?:Message|Conversation)/([a-f0-9]{64})(?:/[a-z_]{1,64})?$');
 v_field:=substring(p_claims->>'resource_id' from '^eios:property:(?:Message|Conversation)/[a-f0-9]{64}/([a-z_]{1,64})$');
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';v_purpose:=basis->>'purpose';
 if p_claims->>'derivation' is distinct from 'purpose-message-v1' or v_kind is null or v_id is null
  or (p_claims->>'resource_id' like 'eios:property:%') is distinct from (v_field is not null)
  or p_claims->>'tenant_id' is distinct from tenant or p_claims->>'principal_id' is distinct from principal
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'world' is distinct from p_world or p_world is distinct from 'real'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or p_claims->'facts' is distinct from '[]'::jsonb
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or jsonb_typeof(basis) is distinct from 'object' or v_purpose is null or v_purpose not in ('claim_extraction','claim_matching')
  or (v_purpose='claim_extraction' and (basis->>'job_id' is null or coalesce(basis->>'fence','') !~ '^[0-9]{1,18}$'))
  or (v_purpose='claim_matching' and coalesce(basis->>'claim_id','') !~ '^[0-9a-f]{64}$') then
  raise exception 'purpose message read invalid' using errcode='42501';end if;
 -- Message metadata of the rule; Conversation object (both purposes) and its two
 -- properties only while extracting. Nothing else is ever derived.
 if (v_kind='Message' and v_field is not null and v_field not in ('accepted_at','actor','body','conversation_id','sequence'))
  or (v_kind='Conversation' and v_field is not null and (v_field not in ('consumer_id','owner_principal') or v_purpose<>'claim_extraction')) then
  raise exception 'purpose message field unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',tenant,true);
 -- This principal's configured grants on the object or any of its fields win.
 v_targets:=case v_kind when 'Message' then array['eios:object:Message/'||v_id,'eios:property:Message/'||v_id||'/accepted_at','eios:property:Message/'||v_id||'/actor',
   'eios:property:Message/'||v_id||'/body','eios:property:Message/'||v_id||'/conversation_id','eios:property:Message/'||v_id||'/sequence']
  else array['eios:object:Conversation/'||v_id,'eios:property:Conversation/'||v_id||'/consumer_id','eios:property:Conversation/'||v_id||'/owner_principal'] end;
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key[1]=principal and entity_key[2]=any(v_targets)) then
  raise exception 'purpose message read superseded' using errcode='42501';end if;
 select payload into rule from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='message_purpose_rule' and entity_key=array[principal,v_purpose] for share;
 if not found or encode(sha256(convert_to(rule::text,'UTF8')),'hex') is distinct from basis->>'rule_hash'
  or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp()
  or (p_claims->>'expires_at')::timestamptz>(rule->>'valid_until')::timestamptz
  or (v_kind='Message' and v_field is not null and not (rule->'fields' ? v_field))
  or (v_kind='Conversation' and not (rule->'fields' ? 'conversation_id')) then
  raise exception 'purpose message read rule unavailable' using errcode='42501';end if;
 -- The principal still holds EXECUTE on the purpose Action (configured, revocable).
 select payload into held from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants'
  and entity_key=array[principal,authz.nexloop_message_purpose_action(v_purpose)] for share;
 if not found or jsonb_typeof(held->'grants') is distinct from 'array' or jsonb_array_length(held->'grants')=0 then
  raise exception 'purpose message read action revoked' using errcode='42501';end if;
 if v_kind='Message' then
  select * into m from ontology.objects where tenant_id=tenant and world=p_world and type_name='Message' and object_id=v_id for share;
  if not found then raise exception 'purpose message unavailable' using errcode='42501';end if;
  if v_field is not null and not (m.properties ? v_field) then raise exception 'purpose message field unavailable' using errcode='42501';end if;
  -- Accepted inbound (0046 outbox) or a materialized channel-accepted outbound reply (0079).
  perform 1 from runtime.nexloop_message_outbox where tenant_id=tenant and world=p_world and message_id=v_id for share;
  if not found then
   perform 1 from runtime.nexloop_outbound_messages where tenant_id=tenant and world=p_world and message_id=v_id for share;
   if not found then raise exception 'purpose message not accepted' using errcode='42501';end if;
  end if;
  select * into cm from runtime.nexloop_conversation_messages where tenant_id=tenant and world=p_world and message_id=v_id for share;
  if not found then raise exception 'purpose message conversation changed' using errcode='42501';end if;
  v_conversation:=cm.conversation_id;
 else
  perform 1 from ontology.objects where tenant_id=tenant and world=p_world and type_name='Conversation' and object_id=v_id for share;
  if not found then raise exception 'purpose conversation unavailable' using errcode='42501';end if;
  v_conversation:=v_id;
 end if;
 perform 1 from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=v_conversation for share;
 if not found or basis->>'conversation_id' is distinct from v_conversation then raise exception 'purpose message conversation changed' using errcode='42501';end if;
 if v_purpose='claim_extraction' then
  -- The extraction task this exact credential leases now, referencing this Conversation/Message.
  select * into j from runtime.jobs where tenant_id=tenant and job_id=basis->>'job_id' for share;
  if not found or j.world is distinct from p_world or j.queue is distinct from 'claim-extraction' or j.capability_name is distinct from 'NexLoop.event'
   or j.status is distinct from 'running' or j.lease_until is null or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
   or j.fencing_token is distinct from (basis->>'fence')::bigint
   or j.normalized_input->>'conversation_id' is distinct from v_conversation
   or (v_kind='Message' and not coalesce(j.normalized_input->'message_ids' ? v_id,false))
   or (p_claims->>'expires_at')::timestamptz>j.lease_until then
   raise exception 'purpose extraction task unavailable' using errcode='42501';end if;
 else
  -- A Claim of this Conversation still to be matched, citing this Message as its evidence.
  select * into cl from ontology.nexloop_claims where tenant_id=tenant and world=p_world and claim_id=basis->>'claim_id' for share;
  if not found or cl.conversation_id is distinct from v_conversation or (v_kind='Message' and cl.source_message_id is distinct from v_id)
   or not authz.nexloop_claim_match_pending(cl.tenant_id,cl.world,cl.claim_id,cl.resolution_state) then
   raise exception 'purpose matching claim unavailable' using errcode='42501';end if;
 end if;
 if (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'purpose message read expired' using errcode='42501';end if;
 return ident->'binding';
end $$;

-- Outermost layer on the derived-read chain (0077 → 0080 → 0086 unchanged beneath).
alter function authz.nexloop_assert_derived_message_read(text,text,jsonb) rename to nexloop_assert_derived_message_read_evidence_v0086;
revoke all on function authz.nexloop_assert_derived_message_read_evidence_v0086(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function authz.nexloop_assert_derived_message_read(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if p_claims->>'derivation'='purpose-message-v1' then return authz.nexloop_assert_purpose_message_read(p_digest,p_world,p_claims);end if;
 return authz.nexloop_assert_derived_message_read_evidence_v0086(p_digest,p_world,p_claims);
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['authz.nexloop_message_purpose_action(text)','authz.nexloop_claim_match_pending(text,text,text,text)',
  'authz.nexloop_message_purpose_basis_for(text,text,text,text,text,text)','authz.nexloop_message_purpose_configured(text,text)',
  'authz.nexloop_assert_purpose_message_read(text,text,jsonb)','authz.nexloop_assert_derived_message_read(text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
