-- UNREGISTERED CANDIDATE. Local delivery may materialize only a committed,
-- currently owned actual effect attempt. No business writes or credential rows.
create table runtime.nexloop_local_delivery_recoveries (
 intent_id uuid primary key,tenant_id text not null,world text not null,
 effect_fence bigint not null check(effect_fence>0),attempt_revision bigint not null check(attempt_revision>0),
 action_claim_revision bigint not null check(action_claim_revision>0),action_fencing_token text not null,
 reserved_at timestamptz not null,
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world)
);
alter table runtime.nexloop_local_delivery_recoveries owner to nexloop_owner;
alter table runtime.nexloop_local_delivery_recoveries enable row level security;
alter table runtime.nexloop_local_delivery_recoveries force row level security;
create policy local_delivery_recovery_tenant on runtime.nexloop_local_delivery_recoveries to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_local_delivery_recoveries from public,nexloop_api,nexloop_identity,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_runtime;
create function authz.nexloop_local_delivery_authority(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;tenant text;principal text;
 verb text:=p->>'verb';target text;terminal_read boolean:=false;i runtime.nexloop_effect_intents;o runtime.nexloop_effect_outbox;
 a runtime.nexloop_effect_attempts;r authz.nexloop_run_credentials;ac runtime.nexloop_action_claims;
 pb runtime.nexloop_effect_provider_bindings;published control.nexloop_action_definitions;plan jsonb;result jsonb;source_identity jsonb;
 link control.nexloop_effect_run_contexts;ctx control.nexloop_effect_contexts;consumer ontology.objects;binding control.nexloop_effect_plan_bindings;reserved jsonb;command jsonb;claim_wire jsonb;
begin
 if session_user<>'nexloop_action_worker' or p_world is distinct from 'real' or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>524288
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
  or verb is null or verb not in ('resolve','deliver','query','recover_claim','recover_reserve','recover_deliver')
  or c->>'protocol' is distinct from 'nexloop-local-json-delivery-v1'
  or c->>'operation' is distinct from 'execute'
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or jsonb_typeof(p->'intent_id') is distinct from 'string'
  or jsonb_typeof(p->'provider_profile_digest') is distinct from 'string'
  or p->>'provider_profile_digest' !~ '^[a-f0-9]{64}$'
 then raise exception 'local delivery denied' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-local-json-delivery-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
 then raise exception 'local delivery denied' using errcode='42501';end if;
 target:=case when verb in ('deliver','recover_reserve','recover_deliver') then 'eios:action:nexloop.service.request:1' else 'eios:action:nexloop.service.query:1' end;
 if c->>'resource_id' is distinct from target or c->>'action_resource' is distinct from target then raise exception 'local delivery denied' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service'
 then raise exception 'local delivery denied' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 select d.* into published from control.nexloop_action_definitions d where d.tenant_id=tenant and d.world=p_world and d.resource_id=target and d.active for update;
 if not found or published.definition is distinct from c->'definition' or published.capability is distinct from c->'capability'
  or published.definition->'governance'->>'approval_mode' is distinct from 'none'
  or published.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(published.definition->'governance'->'policy_refs') is distinct from 0
  or jsonb_array_length(coalesce(published.definition->'preconditions','[]'::jsonb))<>0
 then raise exception 'local delivery denied' using errcode='42501';end if;
 select ei.* into i from runtime.nexloop_effect_intents ei where ei.intent_id=(p->>'intent_id')::uuid and ei.tenant_id=tenant and ei.world=p_world;
 if not found or i.executor_principal is distinct from principal or i.action_name is distinct from 'nexloop.service.request'
 then raise exception 'local delivery denied' using errcode='42501';end if;
 if verb='recover_claim' then
  -- Exact existing intent only, never a generic scanner or a new provider attempt.
  select ea.* into a from runtime.nexloop_effect_attempts ea where ea.intent_id=i.intent_id and ea.tenant_id=tenant and ea.world=p_world;
  select eb.* into pb from runtime.nexloop_effect_provider_bindings eb where eb.intent_id=i.intent_id and eb.tenant_id=tenant and eb.world=p_world;
  if a.intent_id is null or pb.intent_id is null or pb.provider_profile_digest is distinct from p->>'provider_profile_digest'
   or a.state not in ('dispatching','unknown','provider_accepted') or i.governed_claim_finalized
   or jsonb_typeof(p->'execution_claim') is distinct from 'object'
  then raise exception 'local recovery denied' using errcode='42501';end if;
  -- A genuine Governor denies IN_PROGRESS. Do not acquire a long technical
  -- lease while waiting for that original Action claim to expire.
  select ca.* into ac from runtime.nexloop_action_claims ca where ca.tenant_id=tenant and ca.world=p_world
   and ca.action_name=i.action_name and ca.intent_id=i.intent_id::text;
  if not found or ac.principal_id is distinct from principal or ac.claim->>'state' is distinct from 'active'
   or ac.claim->>'lease_expires_at' is null or (ac.claim->>'lease_expires_at')::timestamptz>clock_timestamp()
  then raise exception 'local recovery claim not expired' using errcode='42501';end if;
  claim_wire:=p->'execution_claim';
  if (claim_wire->>'payload')::jsonb is distinct from jsonb_build_object('verb','claim','intent_id',i.intent_id,'lease_seconds',30)
  then raise exception 'local recovery denied' using errcode='42501';end if;
  result:=authz.nexloop_effect_execution_command(p_digest,p_world,claim_wire->>'text',claim_wire->>'signature',claim_wire->>'payload');
  select eb.* into pb from runtime.nexloop_effect_provider_bindings eb where eb.intent_id=i.intent_id and eb.tenant_id=tenant and eb.world=p_world for update;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
  if pb.provider_profile_digest is distinct from p->>'provider_profile_digest' or (c->>'expires_at')::timestamptz<=clock_timestamp()
  then raise exception 'local recovery denied' using errcode='42501';end if;
  return result;
 end if;
 -- Match 0042's control -> intent -> outbox -> attempt -> claim lock order.
 if verb in ('deliver','recover_reserve','recover_deliver') then
  if published.definition is distinct from i.action_definition or published.capability is distinct from i.capability then raise exception 'local delivery denied' using errcode='42501';end if;
  plan:=authz.nexloop_assert_effect_plan(i.context_id,tenant,p_world);
  if not exists(select 1 from runtime.nexloop_effect_control_reservations cr where cr.intent_id=i.intent_id and cr.tenant_id=tenant and cr.control_id=plan->>'control_id')
  then raise exception 'local delivery denied' using errcode='42501';end if;
 end if;
 select ei.* into i from runtime.nexloop_effect_intents ei where ei.intent_id=i.intent_id and ei.tenant_id=tenant and ei.world=p_world for update;
 select eo.* into o from runtime.nexloop_effect_outbox eo where eo.intent_id=i.intent_id and eo.tenant_id=tenant and eo.world=p_world for update;
 if not found then raise exception 'local delivery denied' using errcode='42501';end if;
 terminal_read:=verb='query' and i.governed_claim_finalized and i.state in ('fulfilled','confirmed');
 if not terminal_read and (o.state is distinct from 'leased' or o.lease_credential is distinct from p_digest or o.lease_principal is distinct from principal
  or o.lease_until is null or o.lease_until<=clock_timestamp()) then raise exception 'local delivery denied' using errcode='42501';end if;
 select ea.* into a from runtime.nexloop_effect_attempts ea where ea.intent_id=i.intent_id and ea.tenant_id=tenant and ea.world=p_world for update;
 if not found or a.executor_principal is distinct from principal or a.origin_run_id is distinct from i.origin_run_id
  or a.provider_payload_digest is distinct from i.provider_payload_digest then raise exception 'local delivery denied' using errcode='42501';end if;
 select eb.* into pb from runtime.nexloop_effect_provider_bindings eb where eb.intent_id=i.intent_id and eb.tenant_id=tenant and eb.world=p_world for update;
 if not found or pb.provider_profile_digest is distinct from p->>'provider_profile_digest' then raise exception 'local delivery denied' using errcode='42501';end if;
 if terminal_read then
  -- Narrow historical provider GET, no source grant/rebuild or generic lookup.
  select ca.* into ac from runtime.nexloop_action_claims ca where ca.tenant_id=tenant and ca.world=p_world and ca.action_name=i.action_name and ca.intent_id=i.intent_id::text for update;
  if not found or ac.principal_id is distinct from principal or ac.claim->>'state' is distinct from 'terminal'
   or ac.claim->'terminal_outcome'->>'status' is distinct from 'succeeded'
   or ac.claim->'terminal_outcome'->>'outcome_id' is distinct from i.receipt_id::text
   or a.state is distinct from 'fulfilled' or o.state is distinct from 'fulfilled'
  then raise exception 'local delivery denied' using errcode='42501';end if;
 end if;
 select rc.* into r from authz.nexloop_run_credentials rc where rc.run_id=i.origin_run_id;
 if not found then raise exception 'local delivery denied' using errcode='42501';end if;
 if verb in ('deliver','recover_reserve','recover_deliver') then
  select ca.* into ac from runtime.nexloop_action_claims ca where ca.tenant_id=tenant and ca.world=p_world and ca.action_name=i.action_name and ca.intent_id=i.intent_id::text for update;
  if not found or ac.principal_id is distinct from principal or (verb<>'recover_reserve' and ac.claim->>'state' is distinct from 'active')
   or ac.claim->>'lease_expires_at' is null or (verb<>'recover_reserve' and (ac.claim->>'lease_expires_at')::timestamptz<=clock_timestamp())
   or (ac.claim->>'claim_revision')::bigint is distinct from a.action_claim_revision
   or ac.claim->>'fencing_token' is distinct from a.action_fencing_token then raise exception 'local delivery denied' using errcode='42501';end if;
  if jsonb_typeof(p->'effect_fence') is distinct from 'number' or p->>'effect_fence' !~ '^[1-9][0-9]{0,18}$'
   or jsonb_typeof(p->'attempt_revision') is distinct from 'number' or p->>'attempt_revision' !~ '^[1-9][0-9]{0,18}$'
   or (p->>'effect_fence')::bigint is distinct from o.fence or (p->>'attempt_revision')::bigint is distinct from a.attempt_revision
   or (verb='deliver' and (a.state is distinct from 'dispatching' or a.effect_fence is distinct from o.fence))
   or (verb in ('recover_reserve','recover_deliver') and a.state not in ('dispatching','unknown','provider_accepted'))
   or (verb='recover_deliver' and a.effect_fence is distinct from o.fence) or r.status is distinct from 'active'
   or r.expires_at<=clock_timestamp() or jsonb_typeof(p->'origin_proof') is distinct from 'object'
   or p->'origin_proof'->>'resource_id' is distinct from target
   or p->'origin_proof'->>'expires_at' is null or (p->'origin_proof'->>'expires_at')::timestamptz<=clock_timestamp()
   or (p->'origin_proof'->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
   or p->'origin_proof'->>'tenant_id' is distinct from tenant or p->'origin_proof'->>'world' is distinct from p_world
   or p->>'payload_digest' is distinct from i.provider_payload_digest
   or jsonb_typeof(p->'parameters_text') is distinct from 'string'
   or (p->>'parameters_text')::jsonb is distinct from i.frozen_request->'parameters'
   or encode(sha256(convert_to(p->>'parameters_text','UTF8')),'hex') is distinct from i.provider_payload_digest
  then raise exception 'local delivery denied' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(r.token_digest,p_world,p->'origin_proof');
  source_identity:=authz.nexloop_service_identity_snapshot(r.token_digest,p_world);
  select rl.* into link from control.nexloop_effect_run_contexts rl where rl.run_id=r.run_id and rl.context_id=i.context_id for update;
  if not found or link.valid_until<=clock_timestamp() then raise exception 'local delivery denied' using errcode='42501';end if;
  select cx.* into ctx from control.nexloop_effect_contexts cx where cx.context_id=i.context_id and cx.tenant_id=tenant and cx.world=p_world;
  if not found or ctx.consumer_id is distinct from i.consumer_id then raise exception 'local delivery denied' using errcode='42501';end if;
  select co.* into consumer from ontology.objects co where co.tenant_id=tenant and co.world=p_world and co.object_id=i.consumer_id and co.type_name='Consumer' for update;
  if not found or consumer.nexloop_revision is distinct from ctx.consumer_revision then raise exception 'local delivery denied' using errcode='42501';end if;
  select bp.* into binding from control.nexloop_effect_plan_bindings bp where bp.context_id=i.context_id and bp.tenant_id=tenant and bp.world=p_world;
  if not found or not(binding.submitter_principals ? (source_identity->'binding'->>'subject_principal_id'))
   or source_identity->'binding'->>'tenant_id' is distinct from tenant then raise exception 'local delivery denied' using errcode='42501';end if;
  if verb='recover_reserve' then
   -- Governor supplied the genuine original reservation command. Never mint a
   -- permit from an in-progress replay and never insert a quota reservation.
   command:=authz.nexloop_effect_assert_claim(i,p->'action_claim','reserve',p->>'action_request_text');
   if command->>'lease_expires_at' is null or (command->>'lease_expires_at')::timestamptz>o.lease_until then raise exception 'local recovery lease invalid' using errcode='42501';end if;
   reserved:=authz.nexloop_action_claim_command(p_digest,p_world,p->'action_claim'->>'text',p->'action_claim'->>'signature',p->'action_claim'->>'payload');
   if reserved->>'disposition' is distinct from 'claimed' then raise exception 'local recovery claim busy' using errcode='42501';end if;
   update runtime.nexloop_effect_attempts ea set attempt_revision=ea.attempt_revision+1,effect_fence=o.fence,
    action_claim_revision=(reserved->'claim'->>'claim_revision')::bigint,action_fencing_token=reserved->'claim'->>'fencing_token'
    where ea.intent_id=i.intent_id and ea.tenant_id=tenant and ea.world=p_world returning ea.* into a;
   select ca.* into ac from runtime.nexloop_action_claims ca where ca.tenant_id=tenant and ca.world=p_world and ca.action_name=i.action_name and ca.intent_id=i.intent_id::text;
  end if;
  if verb='recover_reserve' then
   insert into runtime.nexloop_local_delivery_recoveries as lr (intent_id,tenant_id,world,effect_fence,attempt_revision,action_claim_revision,action_fencing_token,reserved_at)
    values(i.intent_id,tenant,p_world,o.fence,a.attempt_revision,a.action_claim_revision,a.action_fencing_token,clock_timestamp())
    on conflict(intent_id) do update set effect_fence=excluded.effect_fence,attempt_revision=excluded.attempt_revision,
     action_claim_revision=excluded.action_claim_revision,action_fencing_token=excluded.action_fencing_token,reserved_at=excluded.reserved_at
    where lr.tenant_id=tenant and lr.world=p_world;
  elsif verb='recover_deliver' and not exists(select 1 from runtime.nexloop_local_delivery_recoveries lr
   where lr.intent_id=i.intent_id and lr.tenant_id=tenant and lr.world=p_world and lr.effect_fence=o.fence
    and lr.attempt_revision=a.attempt_revision and lr.action_claim_revision=a.action_claim_revision and lr.action_fencing_token=a.action_fencing_token)
  then raise exception 'local recovery permit missing' using errcode='42501';end if;
  result:=jsonb_build_object('intent_id',i.intent_id,'parameters',i.frozen_request->'parameters','payload_digest',i.provider_payload_digest,
   'effect_fence',o.fence,'attempt_revision',a.attempt_revision,'action_claim_result',reserved);
 elsif verb='resolve' then
  -- Private trusted proof preparation only; never returned over HTTP.
  result:=jsonb_build_object('_run_digest',r.token_digest,'origin_run_id',r.run_id,'intent_id',i.intent_id,'effect_fence',o.fence,'attempt_revision',a.attempt_revision,'lease_until',o.lease_until,'frozen_request',i.frozen_request,'action_definition',i.action_definition,'capability',i.capability);
 else result:=jsonb_build_object('intent_id',i.intent_id,'payload_digest',i.provider_payload_digest);end if;
 if verb in ('deliver','recover_reserve','recover_deliver') then
  perform authz.nexloop_assert_effect_plan(i.context_id,tenant,p_world);
  perform authz.nexloop_assert_action_authority(r.token_digest,p_world,p->'origin_proof');
  if (ac.claim->>'lease_expires_at')::timestamptz<=clock_timestamp() or r.expires_at<=clock_timestamp() or link.valid_until<=clock_timestamp()
   or (p->'origin_proof'->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'local delivery denied' using errcode='42501';end if;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() or (not terminal_read and (o.lease_until is null or o.lease_until<=clock_timestamp())) then raise exception 'local delivery denied' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_local_delivery_authority(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_local_delivery_authority(text,text,text,text,text) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_scheduler,nexloop_runtime;
grant execute on function authz.nexloop_local_delivery_authority(text,text,text,text,text) to nexloop_action_worker;
