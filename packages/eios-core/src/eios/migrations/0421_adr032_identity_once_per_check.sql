-- ADR-032 (owner 2026-10-13): within one read check, the caller's identity is resolved once and passed to each fact load.
-- authz.nexloop_load_authority_fact resolved the identity (service_identity -> root_identity) again for every fact; a context
-- preparation made thousands of such resolutions per message. Only the two read checks change: the batch fact loader
-- authz.nexloop_load_authority_facts (STABLE: one statement snapshot already) and the read check
-- authz.nexloop_assert_read_authority_inner_before_erasure_v0142 (identity re-snapshotted after its fact loop, as before).
-- Checks guarding writes (action, edit, relation create, artifact, orphan) are unchanged. No GUC, temp table or persistent
-- state: the identity is a local variable of one function call. Previous bodies kept (owner only) for the equivalence
-- test and rollback.
CREATE FUNCTION authz.nexloop_load_authority_facts_before_identity_once_v0421(p_digest text, p_world text, p_keys jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare item jsonb;v_key text[];v jsonb;v_out jsonb:='[]'::jsonb;
begin
 if jsonb_typeof(p_keys) is distinct from 'array' or jsonb_array_length(p_keys)>512 then
  raise exception 'invalid authority fact batch' using errcode='22023';end if;
 for item in select value from jsonb_array_elements(p_keys) loop
  if jsonb_typeof(item) is distinct from 'object' or jsonb_typeof(item->'kind') is distinct from 'string'
   or jsonb_typeof(item->'key') is distinct from 'array' or jsonb_array_length(item->'key') not between 1 and 3 then
   raise exception 'invalid authority fact batch' using errcode='22023';end if;
  select array_agg(x order by o) into v_key from jsonb_array_elements_text(item->'key') with ordinality as t(x,o);
  begin
   v:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,item->>'kind',v_key);
   v_out:=v_out||jsonb_build_array(case when v is null then jsonb_build_object('status','missing')
     else jsonb_build_object('status','ok','value',v) end);
  exception when others then
   v_out:=v_out||jsonb_build_array(jsonb_build_object('status','error'));
  end;
 end loop;
 return v_out;
end $function$
;

CREATE FUNCTION authz.nexloop_assert_read_authority_inner_before_identity_once_v0421(p_digest text, p_world text, p_claims jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare v_identity jsonb;v_entry jsonb;v_key text[];v_payload jsonb;v_snapshot jsonb;
begin
 -- Lock the credential through this short transaction, serializing revocation.
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if p_claims->>'tenant_id' is distinct from v_identity->'binding'->>'tenant_id'
  or p_claims->>'credential_id' is distinct from v_identity->'binding'->>'credential_id'
  or p_claims->>'principal_id' is distinct from v_identity->'binding'->>'subject_principal_id'
  or p_claims->>'world' is distinct from p_world
  or p_claims->>'directory_hash' is distinct from v_identity->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource'
  or p_claims->>'operation' is distinct from 'read'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or authz.nexloop_fact_coverage(v_identity,p_claims) is distinct from true
 then raise exception 'read authority binding stale or invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_claims->>'tenant_id',true);
 -- Fixed ordering prevents cross-request lock-order inversion.
 for v_entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  select array_agg(value) into v_key from jsonb_array_elements_text(v_entry->'key');
  select payload into v_payload from authz.nexloop_authority_facts
   where tenant_id=p_claims->>'tenant_id' and fact_kind=v_entry->>'kind' and entity_key=v_key for share;
  if not found then raise exception 'read authority dependency missing' using errcode='42501';end if;
  v_snapshot:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,v_entry->>'kind',v_key);
  if v_snapshot->>'record_hash' is distinct from v_entry->>'record_hash' then
   raise exception 'read authority revision changed' using errcode='42501';
  end if;
 end loop;
 -- Tenant changes and remove/restore cycles must not resurrect an old permit.
 -- Lock this realm row after fact locks (matching publisher lock ordering).
 perform authz.nexloop_assert_agent_parents(v_identity,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'read authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $function$
;

CREATE FUNCTION authz.nexloop_load_authority_fact_with_identity(p_identity jsonb, p_digest text, p_world text, p_kind text, p_key text[])
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare v_identity jsonb;v_invocation jsonb;v_binding jsonb;v_payload jsonb;v_principal text;v_valid boolean:=false;
begin
 -- ADR-032: p_identity is authz.nexloop_service_identity(p_digest,p_world) resolved once by the calling read check.
 v_identity:=p_identity;
 if v_identity is null then raise exception 'service authentication denied' using errcode='42501';end if;
 v_binding:=v_identity->'binding';v_invocation:=v_identity->'agent_invocation';
 v_principal:=v_binding->>'subject_principal_id';
 if v_identity->'run_context' is not null and v_identity->'run_context'<>'null'::jsonb
  and ((p_kind='resource_graph' and not (v_identity->'run_context'->'allowed_resources')?p_key[1])
   or (p_kind in ('grants','scope','controls','policies') and not (v_identity->'run_context'->'allowed_resources')?p_key[2])) then
  raise exception 'Run resource denied' using errcode='42501';end if;
 if p_kind='agent_release' then perform authz.nexloop_assert_agent_parents(v_identity,false);end if;
 case p_kind
 when 'agent' then v_valid:=v_binding->>'subject_kind'='agent' and p_key=array[v_invocation->>'agent_id'];
 when 'agent_release' then v_valid:=v_binding->>'subject_kind'='agent' and p_key=array[v_invocation->>'release_id'];
 when 'agent_application' then v_valid:=v_binding->>'subject_kind'='agent' and p_key=array[v_invocation->>'agent_application_id',v_invocation->>'agent_application_version'];
 when 'subject' then v_valid:=p_key=array[v_binding->>'subject_id'];
 when 'membership' then v_valid:=p_key=array[v_binding->>'subject_id',v_principal];
 when 'actor' then v_valid:=p_key=array[v_principal];
 when 'authentication' then v_valid:=p_key=array[v_binding->>'credential_id'];
 when 'application' then v_valid:=p_key=array[v_binding->>'caller_application_id',v_binding->>'caller_application_version'];
 when 'subject_authority' then v_valid:=p_key=array[v_principal];
 when 'grants' then v_valid:=cardinality(p_key)=2 and p_key[1]=v_principal;
 when 'scope','controls','policies' then v_valid:=cardinality(p_key)=3 and p_key[1]=v_principal;
 when 'resource_graph' then v_valid:=cardinality(p_key)=1;
 when 'revision' then v_valid:=p_key=array['catalog'];
 else v_valid:=false;
 end case;
 if v_valid is distinct from true then raise exception 'authority fact binding denied' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_binding->>'tenant_id',true);
 select payload into v_payload from authz.nexloop_authority_facts
  where tenant_id=v_binding->>'tenant_id' and fact_kind=p_kind and entity_key=p_key;
 return v_payload;
end $function$
;

CREATE FUNCTION authz.nexloop_load_authority_fact_snapshot_with_identity(p_identity jsonb, p_digest text, p_world text, p_kind text, p_key text[])
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare v_payload jsonb;
begin
 v_payload:=authz.nexloop_load_authority_fact_with_identity(p_identity,p_digest,p_world,p_kind,p_key);
 if v_payload is null then return null;end if;
 return jsonb_build_object('payload',v_payload,'record_hash',encode(sha256(convert_to(v_payload::text,'UTF8')),'hex'));
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_load_authority_facts(p_digest text, p_world text, p_keys jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare item jsonb;v_key text[];v jsonb;v_out jsonb:='[]'::jsonb;v_identity jsonb;v_resolved boolean:=false;
begin
 if jsonb_typeof(p_keys) is distinct from 'array' or jsonb_array_length(p_keys)>512 then
  raise exception 'invalid authority fact batch' using errcode='22023';end if;
 for item in select value from jsonb_array_elements(p_keys) loop
  if jsonb_typeof(item) is distinct from 'object' or jsonb_typeof(item->'kind') is distinct from 'string'
   or jsonb_typeof(item->'key') is distinct from 'array' or jsonb_array_length(item->'key') not between 1 and 3 then
   raise exception 'invalid authority fact batch' using errcode='22023';end if;
  select array_agg(x order by o) into v_key from jsonb_array_elements_text(item->'key') with ordinality as t(x,o);
  begin
   -- ADR-032: the identity is resolved once per batch, at the first item, inside the item's own exception block (a failed
   -- resolution marks every item 'error', as resolving it again per item in this STABLE function's snapshot did).
   if not v_resolved then v_resolved:=true;v_identity:=authz.nexloop_service_identity(p_digest,p_world);end if;
   v:=authz.nexloop_load_authority_fact_snapshot_with_identity(v_identity,p_digest,p_world,item->>'kind',v_key);
   v_out:=v_out||jsonb_build_array(case when v is null then jsonb_build_object('status','missing')
     else jsonb_build_object('status','ok','value',v) end);
  exception when others then
   v_out:=v_out||jsonb_build_array(jsonb_build_object('status','error'));
  end;
 end loop;
 return v_out;
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_assert_read_authority_inner_before_erasure_v0142(p_digest text, p_world text, p_claims jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare v_identity jsonb;v_entry jsonb;v_key text[];v_payload jsonb;v_snapshot jsonb;v_fact_identity jsonb;
begin
 -- Lock the credential through this short transaction, serializing revocation.
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if p_claims->>'tenant_id' is distinct from v_identity->'binding'->>'tenant_id'
  or p_claims->>'credential_id' is distinct from v_identity->'binding'->>'credential_id'
  or p_claims->>'principal_id' is distinct from v_identity->'binding'->>'subject_principal_id'
  or p_claims->>'world' is distinct from p_world
  or p_claims->>'directory_hash' is distinct from v_identity->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource'
  or p_claims->>'operation' is distinct from 'read'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or authz.nexloop_fact_coverage(v_identity,p_claims) is distinct from true
 then raise exception 'read authority binding stale or invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_claims->>'tenant_id',true);
 -- Fixed ordering prevents cross-request lock-order inversion.
 for v_entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  select array_agg(value) into v_key from jsonb_array_elements_text(v_entry->'key');
  select payload into v_payload from authz.nexloop_authority_facts
   where tenant_id=p_claims->>'tenant_id' and fact_kind=v_entry->>'kind' and entity_key=v_key for share;
  if not found then raise exception 'read authority dependency missing' using errcode='42501';end if;
  -- ADR-032: resolved once per check at the first fact; the identity is re-snapshotted and compared after the loop.
  if v_fact_identity is null then v_fact_identity:=authz.nexloop_service_identity(p_digest,p_world);end if;
  v_snapshot:=authz.nexloop_load_authority_fact_snapshot_with_identity(v_fact_identity,p_digest,p_world,v_entry->>'kind',v_key);
  if v_snapshot->>'record_hash' is distinct from v_entry->>'record_hash' then
   raise exception 'read authority revision changed' using errcode='42501';
  end if;
 end loop;
 -- Tenant changes and remove/restore cycles must not resurrect an old permit.
 -- Lock this realm row after fact locks (matching publisher lock ordering).
 perform authz.nexloop_assert_agent_parents(v_identity,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'read authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $function$;

alter function authz.nexloop_load_authority_fact_with_identity(jsonb,text,text,text,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_load_authority_fact_with_identity(jsonb,text,text,text,text[]) from public;
alter function authz.nexloop_load_authority_fact_snapshot_with_identity(jsonb,text,text,text,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_load_authority_fact_snapshot_with_identity(jsonb,text,text,text,text[]) from public;
alter function authz.nexloop_load_authority_facts_before_identity_once_v0421(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_load_authority_facts_before_identity_once_v0421(text,text,jsonb) from public;
alter function authz.nexloop_assert_read_authority_inner_before_identity_once_v0421(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_read_authority_inner_before_identity_once_v0421(text,text,jsonb) from public;
