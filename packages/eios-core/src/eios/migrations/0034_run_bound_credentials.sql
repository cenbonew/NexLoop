-- Short-lived Run authority. No business objects or grants are created.
create table authz.nexloop_run_credentials (
 token_digest text primary key check(token_digest ~ '^[a-f0-9]{64}$'),
 run_id uuid not null unique,source_digest text not null references authz.nexloop_service_credentials(token_digest),
 source_directory_hash text not null,world text not null,
 audience text not null check(audience='nexloop-agent-host'),
 allowed_resources text[] not null check(cardinality(allowed_resources) between 1 and 32),
 status text not null check(status in ('active','revoked')),
 issued_at timestamptz not null,expires_at timestamptz not null,
 check(expires_at>issued_at and expires_at<=issued_at+interval '300 seconds')
);
alter table authz.nexloop_run_credentials owner to nexloop_owner;
revoke all on authz.nexloop_run_credentials from public;

-- Preserve root lookup as an owner-only helper. Existing ports use the wrapper.
alter function authz.nexloop_service_identity_snapshot(text,text) rename to nexloop_root_identity_snapshot;
revoke all on function authz.nexloop_root_identity_snapshot(text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
alter function authz.nexloop_service_identity(text,text) rename to nexloop_root_identity;
revoke all on function authz.nexloop_root_identity(text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create or replace function authz.nexloop_root_identity_snapshot(p_digest text,p_world text)
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare v_identity jsonb;v_hash text;
begin
 v_identity:=authz.nexloop_root_identity(p_digest,p_world);
 select encode(sha256(convert_to((to_jsonb(c)||jsonb_build_object('tenant_revision',t.authority_revision,'tenant_status',t.status))::text,'UTF8')),'hex') into v_hash
 from authz.nexloop_service_credentials c join control.nexloop_tenants t on t.tenant_id=c.tenant_id where token_digest=p_digest;
 return v_identity||jsonb_build_object('directory_hash',v_hash);
end $$;

create function authz.nexloop_service_identity(p_digest text,p_world text)
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on as $$
declare r authz.nexloop_run_credentials%rowtype;v jsonb;
begin
 if exists(select 1 from authz.nexloop_service_credentials where token_digest=p_digest) then
  return authz.nexloop_root_identity(p_digest,p_world)||jsonb_build_object('run_context',null);
 end if;
 select * into r from authz.nexloop_run_credentials where token_digest=p_digest
  and world=p_world and status='active' and expires_at>clock_timestamp();
 if not found then raise exception 'Run authentication denied' using errcode='42501';end if;
 v:=authz.nexloop_root_identity_snapshot(r.source_digest,p_world);
 if v->>'directory_hash' is distinct from r.source_directory_hash then
  raise exception 'Run source authority changed' using errcode='42501';end if;
 return v||jsonb_build_object('expires_at',r.expires_at,'run_context',jsonb_build_object(
  'run_id',r.run_id,'audience',r.audience,'allowed_resources',r.allowed_resources));
end $$;
alter function authz.nexloop_service_identity(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_service_identity(text,text) from public;
grant execute on function authz.nexloop_service_identity(text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

create function authz.nexloop_service_identity_snapshot(p_digest text,p_world text)
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare v jsonb;r jsonb;
begin
 v:=authz.nexloop_service_identity(p_digest,p_world);
 if v->'run_context' is null or v->'run_context'='null'::jsonb then
  return v||authz.nexloop_root_identity_snapshot(p_digest,p_world);
 end if;
 select to_jsonb(c) into r from authz.nexloop_run_credentials c where token_digest=p_digest;
 return v||jsonb_build_object('directory_hash',encode(sha256(convert_to(
  (r||jsonb_build_object('source_hash',v->>'directory_hash'))::text,'UTF8')),'hex'));
end $$;
alter function authz.nexloop_service_identity_snapshot(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_service_identity_snapshot(text,text) from public;
grant execute on function authz.nexloop_service_identity_snapshot(text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

create function authz.nexloop_lock_credential(p_digest text,p_world text,p_target text)
 returns void language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare r authz.nexloop_run_credentials%rowtype;
begin
 select * into r from authz.nexloop_run_credentials where token_digest=p_digest for share;
 if found then
  if not p_target=any(r.allowed_resources) then raise exception 'Run target denied' using errcode='42501';end if;
  perform 1 from authz.nexloop_service_credentials where token_digest=r.source_digest for share;
 else
  perform 1 from authz.nexloop_service_credentials where token_digest=p_digest for share;
 end if;
 perform authz.nexloop_service_identity_snapshot(p_digest,p_world);
end $$;
alter function authz.nexloop_lock_credential(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_lock_credential(text,text,text) from public;

create function authz.nexloop_issue_run_credential(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;proof jsonb;k bytea;v jsonb;raw text;ts timestamptz;expiry timestamptz;resources text[];
begin
 if octet_length(p_text)>1048576 then raise exception 'Run issuance denied' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or c->>'protocol' is distinct from 'nexloop-run-credential-v1' or c->>'audience' is distinct from 'nexloop-agent-host'
  or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-run-credential-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (c->>'ttl_seconds')::integer not between 1 and 300
  or jsonb_typeof(c->'proofs') is distinct from 'array' or jsonb_array_length(c->'proofs') not between 1 and 32
 then raise exception 'Run issuance denied' using errcode='42501';end if;
 perform 1 from authz.nexloop_service_credentials where token_digest=p_digest for share;
 if not found then raise exception 'nested Run issuance denied' using errcode='42501';end if;
 v:=authz.nexloop_root_identity_snapshot(p_digest,p_world);
 for proof in select value from jsonb_array_elements(c->'proofs') order by value->>'resource_id' loop
  perform authz.nexloop_assert_action_authority(p_digest,p_world,proof);
 end loop;
 select array_agg(value->>'resource_id' order by value->>'resource_id') into resources from jsonb_array_elements(c->'proofs');
 if cardinality(resources)<>(select count(distinct value->>'resource_id') from jsonb_array_elements(c->'proofs')) then
  raise exception 'duplicate Run resource' using errcode='42501';end if;
 ts:=clock_timestamp();expiry:=least((v->>'expires_at')::timestamptz,ts+make_interval(secs=>(c->>'ttl_seconds')::integer));
 raw:=encode(extensions.gen_random_bytes(48),'hex');
 insert into authz.nexloop_run_credentials values(encode(sha256(convert_to(raw,'UTF8')),'hex'),
  (c->>'run_id')::uuid,p_digest,v->>'directory_hash',p_world,'nexloop-agent-host',resources,'active',ts,expiry);
 return jsonb_build_object('run_id',c->>'run_id','token',raw,'expires_at',expiry);
end $$;
alter function authz.nexloop_issue_run_credential(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_issue_run_credential(text,text,text,text) from public;
grant execute on function authz.nexloop_issue_run_credential(text,text,text,text) to nexloop_api;
create or replace function authz.nexloop_assert_artifact_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
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
  or p_claims->>'resource_id' is distinct from 'eios:artifact:local_'||p_world
  or p_claims->>'operation' not in ('create','read','delete')
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or authz.nexloop_fact_coverage(v_identity,p_claims) is distinct from true
 then raise exception 'artifact authority binding stale or invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_claims->>'tenant_id',true);
 -- Fixed ordering prevents cross-request lock-order inversion.
 for v_entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  select array_agg(value) into v_key from jsonb_array_elements_text(v_entry->'key');
  select payload into v_payload from authz.nexloop_authority_facts
   where tenant_id=p_claims->>'tenant_id' and fact_kind=v_entry->>'kind' and entity_key=v_key for share;
  if not found then raise exception 'artifact authority dependency missing' using errcode='42501';end if;
  v_snapshot:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,v_entry->>'kind',v_key);
  if v_snapshot->>'record_hash' is distinct from v_entry->>'record_hash' then
   raise exception 'artifact authority revision changed' using errcode='42501';
  end if;
 end loop;
 -- Tenant changes and remove/restore cycles must not resurrect an old permit.
 -- Lock this realm row after fact locks (matching publisher lock ordering).
 perform authz.nexloop_assert_agent_parents(v_identity,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash' then
  raise exception 'artifact authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
create or replace function authz.nexloop_assert_action_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
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
  or p_claims->>'resource_id' is distinct from p_claims->>'action_resource'
  or p_claims->>'operation' is distinct from 'execute'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or authz.nexloop_fact_coverage(v_identity,p_claims) is distinct from true
 then raise exception 'action authority binding stale or invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_claims->>'tenant_id',true);
 -- Fixed ordering prevents cross-request lock-order inversion.
 for v_entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  select array_agg(value) into v_key from jsonb_array_elements_text(v_entry->'key');
  select payload into v_payload from authz.nexloop_authority_facts
   where tenant_id=p_claims->>'tenant_id' and fact_kind=v_entry->>'kind' and entity_key=v_key for share;
  if not found then raise exception 'action authority dependency missing' using errcode='42501';end if;
  v_snapshot:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,v_entry->>'kind',v_key);
  if v_snapshot->>'record_hash' is distinct from v_entry->>'record_hash' then
   raise exception 'action authority revision changed' using errcode='42501';
  end if;
 end loop;
 -- Tenant changes and remove/restore cycles must not resurrect an old permit.
 -- Lock this realm row after fact locks (matching publisher lock ordering).
 perform authz.nexloop_assert_agent_parents(v_identity,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'action authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
create or replace function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
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
end $$;
create or replace function authz.nexloop_assert_edit_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
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
  or p_claims->>'operation' is distinct from 'edit'
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
end $$;
create or replace function authz.nexloop_assert_relation_create_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
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
  or p_claims->>'operation' is distinct from 'create'
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
end $$;
create or replace function authz.nexloop_assert_orphan_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
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
  or p_claims->>'operation' is distinct from 'delete'
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
end $$;

create or replace function authz.nexloop_load_authority_fact(p_digest text,p_world text,p_kind text,p_key text[])
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_identity jsonb;v_invocation jsonb;v_binding jsonb;v_payload jsonb;v_principal text;v_valid boolean:=false;
begin
 v_identity:=authz.nexloop_service_identity(p_digest,p_world);
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
end $$;
