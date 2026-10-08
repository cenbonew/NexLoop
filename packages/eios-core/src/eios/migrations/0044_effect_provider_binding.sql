-- Append-only durable provider identity binding for effect recovery.
create table runtime.nexloop_effect_provider_bindings (
 intent_id uuid primary key,tenant_id text not null,world text not null,
 provider_profile_digest text not null check(provider_profile_digest ~ '^[a-f0-9]{64}$'),
 created_at timestamptz not null,
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world)
);
alter table runtime.nexloop_effect_provider_bindings owner to nexloop_owner;
alter table runtime.nexloop_effect_provider_bindings enable row level security;
alter table runtime.nexloop_effect_provider_bindings force row level security;
create policy effect_provider_tenant on runtime.nexloop_effect_provider_bindings to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_effect_provider_bindings from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;
create function authz.nexloop_effect_provider_binding_immutable() returns trigger
language plpgsql set search_path=pg_catalog as $$
begin raise exception 'effect provider binding immutable' using errcode='42501';end $$;
alter function authz.nexloop_effect_provider_binding_immutable() owner to nexloop_owner;
revoke all on function authz.nexloop_effect_provider_binding_immutable() from public;
create trigger effect_provider_binding_immutable before update or delete on runtime.nexloop_effect_provider_bindings
 for each row execute function authz.nexloop_effect_provider_binding_immutable();
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) rename to nexloop_effect_execution_command_v0042;
revoke all on function authz.nexloop_effect_execution_command_v0042(text,text,text,text,text)
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;
create function authz.nexloop_effect_execution_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;v text:=p->>'verb';identity jsonb;
 tenant text;profile text;bound text;result jsonb;i runtime.nexloop_effect_intents;
 old_lease timestamptz;old_action_lease timestamptz;r authz.nexloop_run_credentials;technical_read boolean;
begin
 if session_user<>'nexloop_action_worker' or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>524288
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
 then raise exception 'effect provider authority unavailable' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-execution-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-execution-v1'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
 then raise exception 'effect provider authority unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);tenant:=identity->'binding'->>'tenant_id';
 if v in ('admit','query','observe','finalize') then
  technical_read:=v='query' and p->'terminal_only'='true'::jsonb;
  profile:=p->>'provider_profile_digest';
  if not coalesce(technical_read,false) and (jsonb_typeof(p->'provider_profile_digest') is distinct from 'string' or profile !~ '^[a-f0-9]{64}$') then
   raise exception 'effect provider profile unavailable' using errcode='42501';end if;
  select ei.* into i from runtime.nexloop_effect_intents ei where ei.intent_id=(p->>'intent_id')::uuid and ei.tenant_id=tenant and ei.world=p_world;
  if not found then raise exception 'effect provider profile unavailable' using errcode='42501';end if;
  select pb.provider_profile_digest into bound from runtime.nexloop_effect_provider_bindings pb
   where pb.intent_id=i.intent_id and pb.tenant_id=tenant and pb.world=p_world;
  if v<>'admit' and (not found or not coalesce(technical_read,false) and bound is distinct from profile) or v='admit' and bound is not null then
   raise exception 'effect provider profile unavailable' using errcode='42501';end if;
  -- Read without new locks. The original command acquires and verifies the
  -- authoritative intent/outbox/attempt locks and ownership in its old order.
  select eo.lease_until into old_lease from runtime.nexloop_effect_outbox eo where eo.intent_id=i.intent_id and eo.tenant_id=tenant and eo.world=p_world;
  if v in ('admit','finalize') then
   select (ac.claim->>'lease_expires_at')::timestamptz into old_action_lease from runtime.nexloop_action_claims ac
    where ac.intent_id=i.intent_id::text and ac.tenant_id=tenant and ac.world=p_world and ac.action_name=i.action_name;
  end if;
 end if;
 result:=authz.nexloop_effect_execution_command_v0042(p_digest,p_world,p_text,p_signature,p_payload);
 if v='admit' then
  insert into runtime.nexloop_effect_provider_bindings values(i.intent_id,tenant,p_world,profile,clock_timestamp());
 end if;
 if v in ('admit','query','observe','finalize') then
  select pb.provider_profile_digest into bound from runtime.nexloop_effect_provider_bindings pb
   where pb.intent_id=i.intent_id and pb.tenant_id=tenant and pb.world=p_world;
  if not found or not coalesce(technical_read,false) and bound is distinct from profile then raise exception 'effect provider profile unavailable' using errcode='42501';end if;
  if not coalesce(technical_read,false) and (old_lease is null or old_lease<=clock_timestamp()) then
   raise exception 'effect provider lease expired' using errcode='42501';end if;
  if v in ('admit','finalize') then
   select rc.* into r from authz.nexloop_run_credentials rc where rc.run_id=i.origin_run_id;
   if not found or r.expires_at<=clock_timestamp() then raise exception 'effect provider source expired' using errcode='42501';end if;
   perform authz.nexloop_assert_action_authority(r.token_digest,p_world,p->'origin_proof');
   perform authz.nexloop_assert_effect_plan(i.context_id,tenant,p_world);
   -- finalize reserve may reacquire an expired Action lease. Its original
   -- function already checks the newly reserved lease; re-read its current row.
   select (ac.claim->>'lease_expires_at')::timestamptz into old_action_lease from runtime.nexloop_action_claims ac
    where ac.intent_id=i.intent_id::text and ac.tenant_id=tenant and ac.world=p_world and ac.action_name=i.action_name;
   if old_action_lease is null or old_action_lease<=clock_timestamp() then raise exception 'effect provider Action lease expired' using errcode='42501';end if;
  end if;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect provider proof expired' using errcode='42501';end if;
 if v in ('admit','query','observe','finalize') and not coalesce(technical_read,false)
  and (old_lease is null or old_lease<=clock_timestamp()) then
  raise exception 'effect provider lease expired' using errcode='42501';end if;
 if v in ('admit','finalize') and (old_action_lease is null or old_action_lease<=clock_timestamp()
  or r.expires_at<=clock_timestamp() or (p->'origin_proof'->>'expires_at')::timestamptz<=clock_timestamp()) then
  raise exception 'effect provider final permit expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_execution_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_effect_execution_command(text,text,text,text,text) to nexloop_action_worker;
