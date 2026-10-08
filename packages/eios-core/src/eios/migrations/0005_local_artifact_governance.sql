-- Narrow Artifact infrastructure, adapted from 0061/0313 permit invariants.
-- A signing key is held only by the trusted EIOS evaluator, never runtime/API clients.
create schema extensions authorization nexloop_owner;
revoke all on schema extensions from public;
create extension pgcrypto with schema extensions;
-- Default PUBLIC execution is revoked; grant only needed crypto to the owner.
grant execute on function extensions.hmac(bytea,bytea,text),extensions.gen_random_bytes(integer) to nexloop_owner;
create table authz.nexloop_authority_signing_keys (
 key_id text primary key,
 key_material bytea not null check(octet_length(key_material)=32),
 active boolean not null
);
alter table authz.nexloop_authority_signing_keys owner to nexloop_owner;
revoke all on authz.nexloop_authority_signing_keys from public;

create function authz.nexloop_service_identity_snapshot(p_digest text,p_world text)
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare v_identity jsonb;v_hash text;
begin
 v_identity:=authz.nexloop_service_identity(p_digest,p_world);
 select encode(sha256(convert_to((to_jsonb(c)||jsonb_build_object('tenant_revision',t.authority_revision,'tenant_status',t.status))::text,'UTF8')),'hex') into v_hash
 from authz.nexloop_service_credentials c join control.nexloop_tenants t on t.tenant_id=c.tenant_id where token_digest=p_digest;
 return v_identity||jsonb_build_object('directory_hash',v_hash);
end $$;
alter function authz.nexloop_service_identity_snapshot(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_service_identity_snapshot(text,text) from public;
grant execute on function authz.nexloop_service_identity_snapshot(text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

create function authz.nexloop_load_authority_fact_snapshot(p_digest text,p_world text,p_kind text,p_key text[])
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_payload jsonb;
begin
 v_payload:=authz.nexloop_load_authority_fact(p_digest,p_world,p_kind,p_key);
 if v_payload is null then return null;end if;
 return jsonb_build_object('payload',v_payload,'record_hash',encode(sha256(convert_to(v_payload::text,'UTF8')),'hex'));
end $$;
alter function authz.nexloop_load_authority_fact_snapshot(text,text,text,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_load_authority_fact_snapshot(text,text,text,text[]) from public;
grant execute on function authz.nexloop_load_authority_fact_snapshot(text,text,text,text[]) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

create table authz.nexloop_artifact_permits (
 tenant_id text not null,
 permit_id text not null,
 claims jsonb not null,
 used_count integer not null default 0 check(used_count between 0 and 1),
 created_at timestamptz not null default clock_timestamp(),
 consumed_at timestamptz,
 primary key(tenant_id,permit_id)
);
alter table authz.nexloop_artifact_permits owner to nexloop_owner;
alter table authz.nexloop_artifact_permits enable row level security;
alter table authz.nexloop_artifact_permits force row level security;
create policy artifact_permit_tenant on authz.nexloop_artifact_permits to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on authz.nexloop_artifact_permits from public;

create function authz.nexloop_assert_artifact_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_identity jsonb;v_entry jsonb;v_key text[];v_payload jsonb;v_snapshot jsonb;
begin
 -- Lock the credential through this short transaction, serializing revocation.
 perform 1 from authz.nexloop_service_credentials where token_digest=p_digest for share;
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
  or jsonb_array_length(p_claims->'facts')<>12
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
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash' then
  raise exception 'artifact authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
alter function authz.nexloop_assert_artifact_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_artifact_authority(text,text,jsonb) from public;

create function authz.nexloop_issue_artifact_permit(p_digest text,p_world text,p_text text,p_signature text)
 returns text language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_claims jsonb;v_key bytea;v_expected text;v_binding jsonb;
begin
 if octet_length(p_text)>262144 or p_signature !~ '^[a-f0-9]{64}$' then
  raise exception 'invalid signed artifact decision' using errcode='42501';end if;
 v_claims:=p_text::jsonb;
 select key_material into v_key from authz.nexloop_authority_signing_keys where key_id=v_claims->>'key_id' and active;
 if not found then raise exception 'artifact authority signer unavailable' using errcode='42501';end if;
 v_expected:=encode(extensions.hmac(convert_to('nexloop-artifact-permit-v1:'||p_text,'UTF8'),v_key,'sha256'),'hex');
 if p_signature is distinct from v_expected or v_claims->>'protocol' is distinct from 'nexloop-artifact-permit-v1'
  or v_claims->>'permit_id' !~ '^[a-f0-9]{32}$'
  or v_claims->>'parameters_digest' !~ '^[a-f0-9]{64}$'
  or (v_claims->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
 then raise exception 'signed artifact decision rejected' using errcode='42501';end if;
 v_binding:=authz.nexloop_assert_artifact_authority(p_digest,p_world,v_claims);
 insert into authz.nexloop_artifact_permits(tenant_id,permit_id,claims)
  values(v_claims->>'tenant_id',v_claims->>'permit_id',v_claims);
 return v_claims->>'permit_id';
end $$;
alter function authz.nexloop_issue_artifact_permit(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_issue_artifact_permit(text,text,text,text) from public;
grant execute on function authz.nexloop_issue_artifact_permit(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

create function authz.nexloop_consume_artifact_permit(p_digest text,p_world text,p_permit text,p_operation text,p_payload text)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_binding jsonb;v_permit authz.nexloop_artifact_permits%rowtype;
begin
 v_binding:=authz.nexloop_service_identity(p_digest,p_world)->'binding';
 perform set_config('eios.tenant_id',v_binding->>'tenant_id',true);
 select * into v_permit from authz.nexloop_artifact_permits where tenant_id=v_binding->>'tenant_id' and permit_id=p_permit for update;
 if not found or v_permit.used_count<>0 or v_permit.claims->>'operation' is distinct from p_operation
  or v_permit.claims->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
 then raise exception 'artifact permit already used or payload conflict' using errcode='42501';end if;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,v_permit.claims);
 update authz.nexloop_artifact_permits set used_count=1,consumed_at=clock_timestamp()
  where tenant_id=v_binding->>'tenant_id' and permit_id=p_permit;
 return v_permit.claims;
end $$;
alter function authz.nexloop_consume_artifact_permit(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_consume_artifact_permit(text,text,text,text,text) from public;

create table runtime.nexloop_local_artifacts (
 tenant_id text not null,
 world text not null,
 artifact_id text not null check(artifact_id ~ '^[a-f0-9]{32}$'),
 principal_id text not null,
 sha256 text not null check(sha256 ~ '^[a-f0-9]{64}$'),
 size_bytes bigint not null check(size_bytes between 0 and 16777216),
 media_type text not null check(length(media_type) between 1 and 255 and media_type !~ E'[\r\n]'),
 encryption text not null default 'none' check(encryption='none'),
 object_key text not null,
 retention_until timestamptz not null,
 status text not null check(status in ('pending','available','deleting','deleted')),
 payload_digest text not null,
 upload_worker text not null,
 upload_token text not null,
 upload_fence bigint not null check(upload_fence>=1),
 lease_until timestamptz not null,
 permit_id text not null,
 created_at timestamptz not null default clock_timestamp(),
 updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,artifact_id)
);
alter table runtime.nexloop_local_artifacts owner to nexloop_owner;
alter table runtime.nexloop_local_artifacts enable row level security;
alter table runtime.nexloop_local_artifacts force row level security;
create policy local_artifact_tenant on runtime.nexloop_local_artifacts to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_local_artifacts from public;

create function authz.nexloop_reserve_local_artifact(p_digest text,p_world text,p_permit text,p_payload text,p_worker text,p_lease integer)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_claims jsonb;v_input jsonb:=p_payload::jsonb;v_existing runtime.nexloop_local_artifacts%rowtype;v_namespace text;
begin
 if p_lease not between 1 and 30 or p_worker !~ '^[a-f0-9]{32}$' or v_input->>'artifact_id' !~ '^[a-f0-9]{32}$'
  or v_input->>'sha256' !~ '^[a-f0-9]{64}$' or (v_input->>'size_bytes')::bigint not between 0 and 16777216
  or (v_input->>'retention_until')::timestamptz is null
 then raise exception 'invalid artifact reservation' using errcode='22023';end if;
 v_claims:=authz.nexloop_consume_artifact_permit(p_digest,p_world,p_permit,'create',p_payload);
 perform pg_advisory_xact_lock(hashtextextended('nexloop-artifact:'||(v_claims->>'tenant_id')||':'||p_world||':'||(v_input->>'artifact_id'),0));
 select * into v_existing from runtime.nexloop_local_artifacts where tenant_id=v_claims->>'tenant_id' and world=p_world and artifact_id=v_input->>'artifact_id' for update;
 if found then
  if v_existing.principal_id is distinct from v_claims->>'principal_id' or v_existing.payload_digest is distinct from v_claims->>'parameters_digest' then
   raise exception 'artifact idempotency payload conflict' using errcode='22000';end if;
  if v_existing.status='available' then return to_jsonb(v_existing)-'upload_token';end if;
  if v_existing.status<>'pending' then raise exception 'artifact lifecycle conflict' using errcode='22000';end if;
  if v_existing.lease_until>clock_timestamp() and v_existing.upload_worker<>p_worker then
   raise exception 'artifact upload lease held' using errcode='40001';end if;
  update runtime.nexloop_local_artifacts set upload_worker=p_worker,upload_token=encode(extensions.gen_random_bytes(24),'hex'),upload_fence=upload_fence+1,
   lease_until=clock_timestamp()+make_interval(secs=>p_lease),permit_id=p_permit,updated_at=clock_timestamp()
   where tenant_id=v_existing.tenant_id and world=p_world and artifact_id=v_existing.artifact_id returning * into v_existing;
 else
  v_namespace:=encode(sha256(convert_to(v_claims->>'tenant_id','UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
  insert into runtime.nexloop_local_artifacts(tenant_id,world,artifact_id,principal_id,sha256,size_bytes,media_type,object_key,retention_until,status,payload_digest,upload_worker,upload_token,upload_fence,lease_until,permit_id)
   values(v_claims->>'tenant_id',p_world,v_input->>'artifact_id',v_claims->>'principal_id',v_input->>'sha256',(v_input->>'size_bytes')::bigint,v_input->>'media_type',v_namespace||'/'||(v_input->>'artifact_id'),(v_input->>'retention_until')::timestamptz,'pending',v_claims->>'parameters_digest',p_worker,encode(extensions.gen_random_bytes(24),'hex'),1,clock_timestamp()+make_interval(secs=>p_lease),p_permit)
   returning * into v_existing;
 end if;
 return to_jsonb(v_existing);
end $$;
alter function authz.nexloop_reserve_local_artifact(text,text,text,text,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_reserve_local_artifact(text,text,text,text,text,integer) from public;
grant execute on function authz.nexloop_reserve_local_artifact(text,text,text,text,text,integer) to nexloop_api,nexloop_domain_worker;

create function authz.nexloop_finalize_local_artifact(p_digest text,p_world text,p_id text,p_worker text,p_token text,p_fence bigint)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_binding jsonb;v_row runtime.nexloop_local_artifacts%rowtype;v_claims jsonb;
begin
 v_binding:=authz.nexloop_service_identity(p_digest,p_world)->'binding';
 perform set_config('eios.tenant_id',v_binding->>'tenant_id',true);
 select * into v_row from runtime.nexloop_local_artifacts where tenant_id=v_binding->>'tenant_id' and world=p_world and artifact_id=p_id for update;
 if not found or v_row.principal_id is distinct from v_binding->>'subject_principal_id' or v_row.upload_worker is distinct from p_worker
  or v_row.upload_token is distinct from p_token or v_row.upload_fence is distinct from p_fence or v_row.lease_until<=clock_timestamp()
 then raise exception 'artifact upload fenced' using errcode='40001';end if;
 select claims into v_claims from authz.nexloop_artifact_permits where tenant_id=v_row.tenant_id and permit_id=v_row.permit_id;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,v_claims);
 if v_row.status='available' then return to_jsonb(v_row)-'upload_token';end if;
 if v_row.status<>'pending' then raise exception 'artifact lifecycle conflict' using errcode='22000';end if;
 update runtime.nexloop_local_artifacts set status='available',updated_at=clock_timestamp()
  where tenant_id=v_row.tenant_id and world=p_world and artifact_id=p_id returning * into v_row;
 return to_jsonb(v_row)-'upload_token';
end $$;
alter function authz.nexloop_finalize_local_artifact(text,text,text,text,text,bigint) owner to nexloop_owner;
revoke all on function authz.nexloop_finalize_local_artifact(text,text,text,text,text,bigint) from public;
grant execute on function authz.nexloop_finalize_local_artifact(text,text,text,text,text,bigint) to nexloop_api,nexloop_domain_worker;

create function authz.nexloop_read_local_artifact(p_digest text,p_world text,p_permit text,p_payload text)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_claims jsonb;v_row runtime.nexloop_local_artifacts%rowtype;
begin
 v_claims:=authz.nexloop_consume_artifact_permit(p_digest,p_world,p_permit,'read',p_payload);
 select * into v_row from runtime.nexloop_local_artifacts where tenant_id=v_claims->>'tenant_id' and world=p_world
  and artifact_id=p_payload::jsonb->>'artifact_id' and principal_id=v_claims->>'principal_id' and status='available';
 if not found then raise exception 'artifact unavailable' using errcode='42501';end if;
 return to_jsonb(v_row)-'upload_token';
end $$;
alter function authz.nexloop_read_local_artifact(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_local_artifact(text,text,text,text) from public;
grant execute on function authz.nexloop_read_local_artifact(text,text,text,text) to nexloop_api,nexloop_domain_worker;
