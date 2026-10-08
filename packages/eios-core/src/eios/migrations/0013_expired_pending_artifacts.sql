-- Expired standalone pending uploads may retire only after their upload lease ends.
-- Retention deadline, ownership, live DELETE authority and runtime reference locks remain mandatory.
create or replace function authz.nexloop_claim_artifact_deletion(p_digest text,p_world text,p_permit text,p_payload text,p_worker text,p_lease integer)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare v_claims jsonb;v_row runtime.nexloop_local_artifacts%rowtype;
begin
 if p_worker !~ '^[a-f0-9]{32}$' or p_lease not between 1 and 30 then
  raise exception 'invalid deletion claim' using errcode='22023';end if;
 v_claims:=authz.nexloop_consume_artifact_permit(p_digest,p_world,p_permit,'delete',p_payload);
 select * into v_row from runtime.nexloop_local_artifacts where tenant_id=v_claims->>'tenant_id' and world=p_world
  and artifact_id=p_payload::jsonb->>'artifact_id' and principal_id=v_claims->>'principal_id' for update;
 if not found or v_row.status not in ('pending','available','deleting','deleted') or v_row.retention_until>clock_timestamp() then
  raise exception 'artifact retention protected' using errcode='42501';end if;
 if v_row.status='pending' and v_row.lease_until>clock_timestamp() then
  raise exception 'artifact upload lease held' using errcode='40001';end if;
 -- Conservatively protect ANY recorded invocation/job reference, including terminal
 -- ones: removal of those bindings requires later lifecycle/deletion integration.
 -- Prevent new Runtime references until this transaction completes.
 lock table runtime.invocations,runtime.jobs in share mode;
 if exists(select 1 from runtime.invocations where tenant_id=v_row.tenant_id and artifact_refs::text like '%'||v_row.artifact_id||'%')
  or exists(select 1 from runtime.jobs where tenant_id=v_row.tenant_id and to_jsonb(jobs)::text like '%'||v_row.artifact_id||'%') then
  raise exception 'artifact runtime binding protected' using errcode='42501';end if;
 if v_row.status='deleted' then return to_jsonb(v_row)-'upload_token';end if;
 if v_row.status='deleting' and v_row.lease_until>clock_timestamp() and v_row.upload_worker<>p_worker then
  raise exception 'artifact deletion lease held' using errcode='40001';end if;
 update runtime.nexloop_local_artifacts set status='deleting',upload_worker=p_worker,
  upload_token=encode(extensions.gen_random_bytes(24),'hex'),upload_fence=upload_fence+1,
  lease_until=clock_timestamp()+make_interval(secs=>p_lease),permit_id=p_permit,updated_at=clock_timestamp()
  where tenant_id=v_row.tenant_id and world=p_world and artifact_id=v_row.artifact_id returning * into v_row;
 return to_jsonb(v_row);
end $$;
alter function authz.nexloop_claim_artifact_deletion(text,text,text,text,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_claim_artifact_deletion(text,text,text,text,text,integer) from public;
grant execute on function authz.nexloop_claim_artifact_deletion(text,text,text,text,text,integer) to nexloop_api,nexloop_domain_worker;


-- Hold this row and live authority across physical fsync, then finalize in the same transaction.
create function authz.nexloop_lock_artifact_upload(p_digest text,p_world text,p_id text,p_worker text,p_token text,p_fence bigint)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_binding jsonb;v_row runtime.nexloop_local_artifacts%rowtype;v_claims jsonb;
begin
 v_binding:=authz.nexloop_service_identity(p_digest,p_world)->'binding';
 perform set_config('eios.tenant_id',v_binding->>'tenant_id',true);
 select * into v_row from runtime.nexloop_local_artifacts where tenant_id=v_binding->>'tenant_id' and world=p_world and artifact_id=p_id;
 if not found or v_row.principal_id is distinct from v_binding->>'subject_principal_id' or v_row.upload_worker is distinct from p_worker
  or v_row.upload_token is distinct from p_token or v_row.upload_fence is distinct from p_fence or v_row.lease_until<=clock_timestamp()
 then raise exception 'artifact upload fenced' using errcode='40001';end if;
 select claims into v_claims from authz.nexloop_artifact_permits where tenant_id=v_row.tenant_id and permit_id=v_row.permit_id;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,v_claims);
 -- Authority first, metadata second: match deletion-claim lock order.
 select * into v_row from runtime.nexloop_local_artifacts where tenant_id=v_binding->>'tenant_id' and world=p_world and artifact_id=p_id for update;
 if not found or v_row.principal_id is distinct from v_binding->>'subject_principal_id' or v_row.upload_worker is distinct from p_worker
  or v_row.upload_token is distinct from p_token or v_row.upload_fence is distinct from p_fence or v_row.lease_until<=clock_timestamp()
 then raise exception 'artifact upload fenced' using errcode='40001';end if;
 if v_row.status<>'pending' then raise exception 'artifact lifecycle conflict' using errcode='22000';end if;
 return to_jsonb(v_row)-'upload_token';
end $$;
alter function authz.nexloop_lock_artifact_upload(text,text,text,text,text,bigint) owner to nexloop_owner;
revoke all on function authz.nexloop_lock_artifact_upload(text,text,text,text,text,bigint) from public;
grant execute on function authz.nexloop_lock_artifact_upload(text,text,text,text,text,bigint) to nexloop_api,nexloop_domain_worker;
