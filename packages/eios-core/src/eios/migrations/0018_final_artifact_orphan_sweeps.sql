-- A durable maintenance ledger, not business-object or runtime authority.
create table runtime.nexloop_orphan_sweeps (
 tenant_id text not null,world text not null,sweep_id text not null check(sweep_id ~ '^[a-f0-9]{32}$'),principal_id text not null,
 plan jsonb not null,status text not null default 'planned' check(status in ('planned','finished')),outcomes jsonb,
 created_at timestamptz not null default clock_timestamp(),finished_at timestamptz,
 primary key(tenant_id,world,sweep_id)
);
alter table runtime.nexloop_orphan_sweeps owner to nexloop_owner;
alter table runtime.nexloop_orphan_sweeps enable row level security;
alter table runtime.nexloop_orphan_sweeps force row level security;
create policy orphan_sweep_tenant on runtime.nexloop_orphan_sweeps to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_orphan_sweeps from public;

-- Service object/property reads using actual frozen EIOS authority facts.
create function authz.nexloop_assert_orphan_authority(p_digest text,p_world text,p_claims jsonb)
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
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource'
  or p_claims->>'operation' is distinct from 'delete'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or jsonb_array_length(p_claims->'facts')<>12
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
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'read authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
alter function authz.nexloop_assert_orphan_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_orphan_authority(text,text,jsonb) from public;


create function authz.nexloop_orphan_sweep_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;r runtime.nexloop_orphan_sweeps%rowtype;
 item jsonb;ids jsonb:='[]'::jsonb;outcome jsonb;verb text:=a->>'verb';
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>65536 then raise exception 'orphan command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or a->>'protocol' is distinct from 'nexloop-orphan-sweep-v1'
  or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-orphan-sweep-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'target_resource' is distinct from 'eios:artifact:orphans_'||p_world
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or verb not in ('plan','load','lock','finish') or body->>'sweep_id' !~ '^[a-f0-9]{32}$' then
  raise exception 'orphan command binding rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_orphan_authority(p_digest,p_world,a);
 if verb='plan' then
  if jsonb_typeof(body->'candidates') is distinct from 'array' or jsonb_array_length(body->'candidates')>50
   or jsonb_typeof(body->'older_than') is distinct from 'string' or (body->>'older_than')::timestamptz>clock_timestamp()-interval '60 seconds' then
   raise exception 'invalid orphan plan' using errcode='22023';end if;
  for item in select value from jsonb_array_elements(body->'candidates') loop
   if jsonb_typeof(item->'artifact_id') is distinct from 'string' or jsonb_typeof(item->'sha256') is distinct from 'string' or item->>'artifact_id' !~ '^[a-f0-9]{32}$' or item->>'sha256' !~ '^[a-f0-9]{64}$'
    or jsonb_typeof(item->'size_bytes') is distinct from 'number' or (item->>'size_bytes')::bigint not between 0 and 16777216
    or jsonb_typeof(item->'inode') is distinct from 'number' or jsonb_typeof(item->'mtime_ns') is distinct from 'number' then
    raise exception 'invalid orphan candidate' using errcode='22023';end if;
  end loop;
  insert into runtime.nexloop_orphan_sweeps(tenant_id,world,sweep_id,principal_id,plan)
   values(a->>'tenant_id',p_world,body->>'sweep_id',a->>'principal_id',body);
  return body;
 end if;
 select * into r from runtime.nexloop_orphan_sweeps where tenant_id=a->>'tenant_id' and world=p_world and sweep_id=body->>'sweep_id' for update;
 if not found or r.principal_id is distinct from a->>'principal_id' then raise exception 'orphan plan unavailable' using errcode='42501';end if;
 if verb='load' then return to_jsonb(r);end if;
 if r.status='finished' then
  if verb='finish' and r.outcomes is distinct from body->'outcomes' then raise exception 'orphan result conflict' using errcode='22000';end if;
  return jsonb_build_object('finished',true,'outcomes',r.outcomes,'eligible','[]'::jsonb);
 end if;
 -- Reservation inserts and runtime references cannot appear during physical removal.
 lock table runtime.nexloop_local_artifacts,runtime.invocations,runtime.jobs in share mode;
 for item in select value from jsonb_array_elements(r.plan->'candidates') loop
  if not exists(select 1 from runtime.nexloop_local_artifacts m where m.tenant_id=r.tenant_id and m.world=p_world and m.artifact_id=item->>'artifact_id')
   and not exists(select 1 from runtime.invocations i where i.tenant_id=r.tenant_id and i.artifact_refs::text like '%'||(item->>'artifact_id')||'%')
   and not exists(select 1 from runtime.jobs j where j.tenant_id=r.tenant_id and to_jsonb(j)::text like '%'||(item->>'artifact_id')||'%') then
   ids:=ids||jsonb_build_array(item->>'artifact_id');
  end if;
 end loop;
 if verb='lock' then return jsonb_build_object('finished',false,'eligible',ids);end if;
 if jsonb_typeof(body->'outcomes') is distinct from 'array' or jsonb_array_length(body->'outcomes')<>jsonb_array_length(r.plan->'candidates') then
  raise exception 'invalid orphan results' using errcode='22023';end if;
 for outcome in select value from jsonb_array_elements(body->'outcomes') loop
  if not exists(select 1 from jsonb_array_elements(r.plan->'candidates') c where c->>'artifact_id'=outcome->>'artifact_id')
   or jsonb_typeof(outcome->'disposition') is distinct from 'string' or outcome->>'disposition' not in ('removed','absent','changed','protected')
   or (outcome->>'disposition' in ('removed','absent') and not ids ? (outcome->>'artifact_id')) then
   raise exception 'orphan result binding rejected' using errcode='42501';end if;
 end loop;
 if (select count(distinct value->>'artifact_id') from jsonb_array_elements(body->'outcomes'))<>jsonb_array_length(body->'outcomes') then
  raise exception 'duplicate orphan results' using errcode='22023';end if;
 update runtime.nexloop_orphan_sweeps set status='finished',outcomes=body->'outcomes',finished_at=clock_timestamp()
  where tenant_id=r.tenant_id and world=p_world and sweep_id=r.sweep_id;
 return jsonb_build_object('finished',true,'outcomes',body->'outcomes');
end $$;
alter function authz.nexloop_orphan_sweep_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_orphan_sweep_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_orphan_sweep_command(text,text,text,text,text) to nexloop_domain_worker;
