-- Generic Action claim adapter, no ontology/external write grants.
create function authz.nexloop_assert_action_authority(p_digest text,p_world text,p_claims jsonb)
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
  or p_claims->>'resource_id' is distinct from p_claims->>'action_resource'
  or p_claims->>'operation' is distinct from 'execute'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or jsonb_array_length(p_claims->'facts')<>12
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
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'action authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
alter function authz.nexloop_assert_action_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_action_authority(text,text,jsonb) from public;

create table runtime.nexloop_action_claims (
 tenant_id text not null,world text not null,action_name text not null,intent_id text not null,
 principal_id text not null,claim jsonb not null,
 primary key(tenant_id,world,action_name,intent_id)
);
create table authz.nexloop_action_decisions (
 tenant_id text not null,decision_id text not null,claims jsonb not null,
 created_at timestamptz not null default clock_timestamp(),primary key(tenant_id,decision_id)
);
alter table runtime.nexloop_action_claims owner to nexloop_owner;
alter table authz.nexloop_action_decisions owner to nexloop_owner;
alter table runtime.nexloop_action_claims enable row level security;
alter table runtime.nexloop_action_claims force row level security;
alter table authz.nexloop_action_decisions enable row level security;
alter table authz.nexloop_action_decisions force row level security;
create policy action_claim_tenant on runtime.nexloop_action_claims to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy action_decision_tenant on authz.nexloop_action_decisions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_action_claims,authz.nexloop_action_decisions from public;

create function authz.nexloop_action_claim_command(p_digest text,p_world text,p_claims_text text,p_signature text,p_command_text text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_claims_text::jsonb;c jsonb:=p_command_text::jsonb;r runtime.nexloop_action_claims%rowtype;
 k jsonb:=c->'key';b jsonb:=c->'binding';v_key bytea;v_binding jsonb;v_resource text;v_claim jsonb;v_verb text:=a->>'verb';v_now timestamptz:=clock_timestamp();
begin
 if octet_length(p_claims_text)>262144 or octet_length(p_command_text)>262144 then raise exception 'action command too large' using errcode='22023';end if;
 v_resource:='eios:action:'||(b->'action_reference'->>'stable_name')||':'||(b->'action_reference'->>'version');
 select key_material into v_key from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-action-command-v1:'||p_claims_text,'UTF8'),v_key,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-action-command-v1' or a->>'action_resource' is distinct from v_resource
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_command_text,'UTF8')),'hex')
  or a->>'permit_id' !~ '^[a-f0-9]{32}$' or v_verb not in ('reserve','retryable','finalize')
  or (a->>'expires_at')::timestamptz>v_now+interval '30 seconds'
  or k->>'tenant_id' is distinct from a->>'tenant_id'
  or b->'action_reference'->>'tenant_id' is distinct from a->>'tenant_id'
  or b->'action_reference'->>'definition_type' is distinct from 'action'
  or k->>'action_stable_name' is distinct from b->'action_reference'->>'stable_name'
  or coalesce(length(k->>'idempotency_key'),0) not between 1 and 200 then
  raise exception 'signed action command rejected' using errcode='42501';end if;
 v_binding:=authz.nexloop_assert_action_authority(p_digest,p_world,a);
 insert into authz.nexloop_action_decisions(tenant_id,decision_id,claims) values(a->>'tenant_id',a->>'permit_id',a);
 perform pg_advisory_xact_lock(hashtextextended('nexloop-action:'||(k->>'tenant_id')||':'||p_world||':'||(k->>'action_stable_name')||':'||(k->>'idempotency_key'),0));
 select * into r from runtime.nexloop_action_claims where tenant_id=k->>'tenant_id' and world=p_world and action_name=k->>'action_stable_name' and intent_id=k->>'idempotency_key' for update;
 -- Waiting for another owner must not extend a decision or lease lifetime.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_now:=clock_timestamp();
 if r.claim is not null and (r.principal_id is distinct from a->>'principal_id' or r.claim->'binding' is distinct from b) then
  if v_verb='reserve' then return jsonb_build_object('disposition','conflict');end if;
  raise exception 'action_claim_binding_conflict' using errcode='P0001';end if;
 if v_verb='reserve' then
  if r.claim->>'state'='terminal' then return jsonb_build_object('disposition','replay','terminal_outcome',r.claim->'terminal_outcome');end if;
  if (c->>'requested_at')::timestamptz>v_now or (c->>'lease_expires_at')::timestamptz<=v_now
   or (c->>'lease_expires_at')::timestamptz>v_now+interval '5 minutes' then raise exception 'action_claim_time_invalid' using errcode='P0001';end if;
  if r.claim->>'state'='active' and (r.claim->>'lease_expires_at')::timestamptz>v_now then return jsonb_build_object('disposition','in_progress');end if;
  v_claim:=jsonb_build_object('key',k,'binding',b,'state','active','claim_revision',coalesce((r.claim->>'claim_revision')::bigint,0)+1,
   'fencing_token',encode(extensions.gen_random_bytes(24),'hex'),'lease_expires_at',c->'lease_expires_at','terminal_outcome',null);
  insert into runtime.nexloop_action_claims values(k->>'tenant_id',p_world,k->>'action_stable_name',k->>'idempotency_key',a->>'principal_id',v_claim)
   on conflict(tenant_id,world,action_name,intent_id) do update set claim=excluded.claim;
  return jsonb_build_object('disposition','claimed','claim',v_claim);
 end if;
 if r.claim is null then raise exception 'action_claim_not_found' using errcode='P0001';end if;
 if (r.claim->>'claim_revision')::bigint is distinct from (c->>'expected_claim_revision')::bigint then raise exception 'action_claim_revision_conflict' using errcode='P0001';end if;
 if r.claim->>'fencing_token' is distinct from c->>'fencing_token' then raise exception 'action_claim_stale_fence' using errcode='P0001';end if;
 if v_verb='finalize' and r.claim->>'state'='terminal' then
  if r.claim->'terminal_outcome'=c->'outcome' then return r.claim;end if;
  raise exception 'action_claim_outcome_conflict' using errcode='P0001';end if;
 if r.claim->>'state'<>'active' then raise exception 'action_claim_transition_invalid' using errcode='P0001';end if;
 if (r.claim->>'lease_expires_at')::timestamptz<=v_now then raise exception 'action_claim_lease_expired' using errcode='P0001';end if;
 if v_verb='retryable' then
  if (c->>'marked_at')::timestamptz>v_now then raise exception 'action_claim_time_invalid' using errcode='P0001';end if;
  v_claim:=r.claim||jsonb_build_object('state','retryable');
 else
  if (c->'outcome'->>'finalized_at')::timestamptz>v_now then raise exception 'action_claim_time_invalid' using errcode='P0001';end if;
  v_claim:=r.claim||jsonb_build_object('state','terminal','terminal_outcome',c->'outcome');
 end if;
 update runtime.nexloop_action_claims set claim=v_claim where tenant_id=r.tenant_id and world=p_world and action_name=r.action_name and intent_id=r.intent_id;
 return v_claim;
end $$;
alter function authz.nexloop_action_claim_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_action_claim_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_action_claim_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
