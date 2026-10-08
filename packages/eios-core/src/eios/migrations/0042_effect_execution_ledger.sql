-- DRAFT: append-only execution ledger for the future restricted effect Worker.
-- NOT registered, NOT applied. Protected commands below are candidate code;
-- actual PG/authority/provider fault tests are mandatory before catalog entry.
-- Provider observations are technical evidence, not automatic Action success.

alter table runtime.nexloop_effect_intents drop constraint nexloop_effect_intents_state_check;
alter table runtime.nexloop_effect_intents add constraint nexloop_effect_intents_state_check
 check(state in ('accepted','dispatching','unknown','observed_fulfilled','fulfilled','failed','confirmed'));
alter table runtime.nexloop_effect_intents add column governed_claim_finalized boolean not null default false;
alter table runtime.nexloop_effect_intents add constraint effect_governed_completion_state
 check(governed_claim_finalized = (state in ('fulfilled','confirmed')));
alter table runtime.nexloop_effect_outbox add column lease_principal text;
alter table runtime.nexloop_effect_outbox drop constraint nexloop_effect_outbox_state_check;
alter table runtime.nexloop_effect_outbox add constraint nexloop_effect_outbox_state_check
 check(state in ('pending','leased','unknown','query_pending','fulfilled','failed'));
alter table runtime.nexloop_effect_intents add constraint effect_intent_tenant_identity unique(intent_id,tenant_id,world);

create table runtime.nexloop_effect_attempts (
 intent_id uuid primary key,
 attempt_id uuid not null unique,tenant_id text not null,world text not null,
 provider_key text not null,provider_payload_digest text not null check(provider_payload_digest ~ '^[a-f0-9]{64}$'),
 executor_principal text not null,
 origin_run_id uuid not null references authz.nexloop_run_credentials(run_id),
 finalization_xid bigint,
 -- Action and effect lease are independent of a Run's inference lease. Both
 -- exact current revisions must be checked by every external admission/result.
 action_claim_revision bigint not null check(action_claim_revision>0),
 action_fencing_token text not null check(action_fencing_token ~ '^[a-f0-9]{48}$'),
 effect_fence bigint not null check(effect_fence>0),attempt_revision bigint not null check(attempt_revision>0),
 state text not null check(state in ('dispatching','unknown','provider_accepted','observed_fulfilled','fulfilled')),
 dispatched_at timestamptz not null,updated_at timestamptz not null,
 provider_reference text,
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world),
 unique(intent_id,tenant_id,world),
 check(provider_key=intent_id::text),
 check(provider_reference is null or provider_reference ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$')
);

create table runtime.nexloop_effect_observations (
 observation_id uuid primary key,intent_id uuid not null,
 tenant_id text not null,world text not null,
 attempt_revision bigint not null check(attempt_revision>0),effect_fence bigint not null check(effect_fence>0),
 -- Independent query principal is recorded honestly; never masquerades as the
 -- expired source Run or the owner of a terminal original execution claim.
 observer_principal text not null,observer_credential_digest text not null,
 provider_state text not null check(provider_state in ('accepted','fulfilled','not_found')),
 provider_payload_digest text,provider_reference text,observed_at timestamptz not null,
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_attempts(intent_id,tenant_id,world),
 check((provider_state='not_found' and provider_payload_digest is null and provider_reference is null)
  or (provider_state in ('accepted','fulfilled') and provider_payload_digest is not null and provider_reference is not null and provider_payload_digest ~ '^[a-f0-9]{64}$'
      and provider_reference ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'))
);
alter table runtime.nexloop_effect_attempts owner to nexloop_owner;
alter table runtime.nexloop_effect_observations owner to nexloop_owner;
alter table runtime.nexloop_effect_attempts enable row level security;
alter table runtime.nexloop_effect_attempts force row level security;
alter table runtime.nexloop_effect_observations enable row level security;
alter table runtime.nexloop_effect_observations force row level security;
create policy effect_attempt_tenant on runtime.nexloop_effect_attempts to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy effect_observation_tenant on runtime.nexloop_effect_observations to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_effect_attempts,runtime.nexloop_effect_observations
 from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker;

alter table runtime.nexloop_effect_intents add column origin_run_id uuid references authz.nexloop_run_credentials(run_id);
create table runtime.nexloop_effect_events (
 event_id uuid primary key,intent_id uuid not null,tenant_id text not null,world text not null,
 event_kind text not null check(event_kind in ('dispatching','unknown','observed','fulfilled')),
 effect_fence bigint not null check(effect_fence>0),created_at timestamptz not null,evidence jsonb not null,
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world)
);
alter table runtime.nexloop_effect_events owner to nexloop_owner;
alter table runtime.nexloop_effect_events enable row level security;
alter table runtime.nexloop_effect_events force row level security;
create policy effect_event_tenant on runtime.nexloop_effect_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_effect_events from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker;

-- Private owner helper: a signed nested claim cannot reserve/finalize another
-- intent, change the frozen Action contract, or impersonate its submitter.
create function authz.nexloop_effect_assert_claim(p_intent runtime.nexloop_effect_intents,p_envelope jsonb,p_verb text,p_request_text text)
returns jsonb language plpgsql set search_path=pg_catalog as $$
declare v_text text:=p_envelope->>'payload';v_command jsonb;v_claims jsonb;v_binding jsonb;v_reference jsonb;
begin
 if jsonb_typeof(p_envelope) is distinct from 'object' or p_envelope->>'text' is null
  or p_envelope->>'signature' is null or v_text is null then raise exception 'effect claim unavailable' using errcode='42501';end if;
 v_command:=v_text::jsonb;v_claims:=(p_envelope->>'text')::jsonb;v_binding:=v_command->'binding';v_reference:=v_binding->'action_reference';
 if v_claims->>'expires_at' is null or (v_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claims->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or v_claims->>'permit_id' is null or v_claims->>'permit_id' !~ '^[a-f0-9]{32}$'
  or v_claims->>'verb' is distinct from p_verb or v_command->'key'->>'tenant_id' is distinct from p_intent.tenant_id
  or v_command->'key'->>'action_stable_name' is distinct from p_intent.action_name
  or v_command->'key'->>'idempotency_key' is distinct from p_intent.intent_id::text
  or v_binding->>'invocation_id' is distinct from p_intent.intent_id::text
  or p_request_text is null or p_request_text::jsonb is distinct from p_intent.frozen_request
  or v_binding->>'request_digest' is distinct from encode(sha256(convert_to(p_request_text,'UTF8')),'hex')
  or v_binding->>'adapter_id' is distinct from 'nexloop.effect'
  or v_binding->>'target_system' is distinct from 'service'
  or v_reference->>'tenant_id' is distinct from p_intent.tenant_id
  or v_reference->>'stable_name' is distinct from p_intent.action_name
  or v_reference->>'version' is distinct from p_intent.action_version::text
  or v_reference->>'definition_type' is distinct from 'action'
  or v_reference->>'contract_digest' is distinct from p_intent.action_definition->>'contract_digest'
  or v_binding->'capability_binding' is distinct from jsonb_build_object('capability_name',p_intent.capability->>'capability_name',
     'capability_version',p_intent.capability->>'capability_version','schema_hash',p_intent.capability->>'schema_hash')
 then raise exception 'effect claim binding denied' using errcode='42501';end if;
 return v_command;
end $$;
alter function authz.nexloop_effect_assert_claim(runtime.nexloop_effect_intents,jsonb,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_assert_claim(runtime.nexloop_effect_intents,jsonb,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker;

create function authz.nexloop_effect_execution_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare v_claims jsonb:=p_text::jsonb;v_payload jsonb:=p_payload::jsonb;v_key bytea;v_identity jsonb;
 v_verb text:=v_payload->>'verb';v_tenant text;v_target text;v_query boolean;v_principal text;
 v_intent runtime.nexloop_effect_intents;v_outbox runtime.nexloop_effect_outbox;v_attempt runtime.nexloop_effect_attempts;
 v_observation runtime.nexloop_effect_observations;v_run authz.nexloop_run_credentials;
 v_published control.nexloop_action_definitions;v_action runtime.nexloop_action_claims;
 v_envelope jsonb;v_command jsonb;v_reserved jsonb;v_result jsonb;v_plan jsonb;
 v_fence bigint;v_lease timestamptz;v_action_lease timestamptz;v_seconds integer;v_reference text;v_state text;v_observation_id uuid;
begin
 if session_user<>'nexloop_action_worker' or octet_length(p_text)>1048576 or octet_length(p_payload)>524288
  or jsonb_typeof(v_payload) is distinct from 'object' or v_verb is null
  or v_verb not in ('hint','claim','resolve','admit','query','observe','unknown','finalize') then
  raise exception 'effect execution unavailable' using errcode='42501';end if;
 select s.key_material into v_key from authz.nexloop_authority_signing_keys s where s.key_id=v_claims->>'key_id' and s.active;
 v_target:=v_claims->>'resource_id';
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-execution-v1:'||p_text,'UTF8'),v_key,'sha256'),'hex')
  or v_claims->>'protocol' is distinct from 'nexloop-effect-execution-v1'
  or v_claims->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or v_claims->>'expires_at' is null or (v_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claims->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or v_target is null then raise exception 'effect proof unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,v_claims);
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->'run_context' is distinct from 'null'::jsonb then raise exception 'independent executor required' using errcode='42501';end if;
 v_tenant:=v_identity->'binding'->>'tenant_id';v_principal:=v_identity->'binding'->>'subject_principal_id';
 if v_verb='hint' and v_payload->>'intent_id' is null then
  if v_target is distinct from 'eios:action:nexloop.service.query:1' then raise exception 'effect query required' using errcode='42501';end if;
  select d.* into v_published from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.resource_id=v_target and d.active for share;
  if not found or v_published.definition is distinct from v_claims->'definition' or v_published.capability is distinct from v_claims->'capability'
   or v_published.definition->'governance'->>'approval_mode' is distinct from 'none'
   or v_published.definition->'governance'->>'risk_level' is distinct from 'low'
   or jsonb_array_length(v_published.definition->'governance'->'policy_refs') is distinct from 0 then raise exception 'effect published query unavailable' using errcode='42501';end if;
  select i.* into v_intent from runtime.nexloop_effect_intents i join runtime.nexloop_effect_outbox o on o.intent_id=i.intent_id and o.tenant_id=i.tenant_id and o.world=i.world
   where i.tenant_id=v_tenant and i.world=p_world and i.executor_principal=v_principal and o.state not in ('fulfilled','failed')
   and o.available_at<=clock_timestamp() and (o.lease_until is null or o.lease_until<=clock_timestamp())
   order by o.available_at,o.created_at,i.intent_id limit 1;
  if not found then
   perform authz.nexloop_assert_action_authority(p_digest,p_world,v_claims);
   if (v_claims->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect query expired' using errcode='42501';end if;
   return null;
  end if;
 else
  select i.* into v_intent from runtime.nexloop_effect_intents i where i.intent_id=(v_payload->>'intent_id')::uuid
   and i.tenant_id=v_tenant and i.world=p_world;
 end if;
 if not found or v_intent.executor_principal is distinct from v_principal then raise exception 'owned effect unavailable' using errcode='42501';end if;
 v_query:=v_target='eios:action:nexloop.service.query:1';
 if not v_query and v_target is distinct from 'eios:action:'||v_intent.action_name||':'||v_intent.action_version then
  raise exception 'effect Action unavailable' using errcode='42501';end if;
 if v_verb in ('hint','query','observe','unknown') and not v_query or v_verb in ('admit','finalize') and v_query then
  raise exception 'effect operation authority denied' using errcode='42501';end if;
 if not v_query and p_world is distinct from 'real' then raise exception 'real effect world required' using errcode='42501';end if;
 select d.* into v_published from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.resource_id=v_target and d.active for share;
 if not found or v_published.definition is distinct from v_claims->'definition' or v_published.capability is distinct from v_claims->'capability'
  or v_published.definition->'governance'->>'approval_mode' is distinct from 'none'
  or v_published.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(v_published.definition->'governance'->'policy_refs') is distinct from 0
  or jsonb_array_length(coalesce(v_published.definition->'preconditions','[]'::jsonb))<>0
  or (not v_query and (v_published.definition is distinct from v_intent.action_definition or v_published.capability is distinct from v_intent.capability)) then
  raise exception 'effect published Action stale' using errcode='42501';end if;
 if v_verb='hint' then
  -- Only the fixed executor with a current query Action may prepare a nested
  -- claim. This candidate conveys no source digest, lease or send permission.
  perform authz.nexloop_assert_action_authority(p_digest,p_world,v_claims);
  if (v_claims->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect proof expired' using errcode='42501';end if;
  return jsonb_build_object('intent_id',v_intent.intent_id,'receipt_id',v_intent.receipt_id,'frozen_request',v_intent.frozen_request,
   'action_definition',v_intent.action_definition,'capability',v_intent.capability,'business_digest',v_intent.business_digest,
   'provider_payload_digest',v_intent.provider_payload_digest,
   'has_attempt',exists(select 1 from runtime.nexloop_effect_attempts a where a.intent_id=v_intent.intent_id and a.tenant_id=v_tenant and a.world=p_world));
 end if;
 if v_payload ? 'terminal_only' then
  if v_verb is distinct from 'query' or v_payload->'terminal_only' is distinct from 'true'::jsonb
   or not v_query or not v_intent.governed_claim_finalized or v_intent.state not in ('fulfilled','confirmed') then
   raise exception 'terminal governed receipt unavailable' using errcode='42501';end if;
 end if;
 if v_verb='query' and v_payload->'terminal_only'='true'::jsonb then
  -- Re-read the committed receipt after a finalization ACK is lost. Current
  -- independent query authority is enough to read this historical fact; it
  -- never finalizes an Action, renews its lease, or invokes provider dispatch.
  select a.* into v_action from runtime.nexloop_action_claims a where a.tenant_id=v_tenant and a.world=p_world
   and a.action_name=v_intent.action_name and a.intent_id=v_intent.intent_id::text;
  if not found or v_action.principal_id is distinct from v_principal or v_action.claim->>'state' is distinct from 'terminal'
   or v_action.claim->'terminal_outcome'->>'status' is distinct from 'succeeded'
   or v_action.claim->'terminal_outcome'->>'outcome_id' is distinct from v_intent.receipt_id::text
   or not exists(select 1 from runtime.nexloop_effect_attempts a where a.intent_id=v_intent.intent_id and a.tenant_id=v_tenant and a.world=p_world and a.state='fulfilled')
   or not exists(select 1 from runtime.nexloop_effect_outbox o where o.intent_id=v_intent.intent_id and o.tenant_id=v_tenant and o.world=p_world and o.state='fulfilled')
  then raise exception 'governed receipt inconsistent' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,v_claims);
  if (v_claims->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect receipt permit expired' using errcode='42501';end if;
  return jsonb_build_object('intent_id',v_intent.intent_id,'receipt_id',v_intent.receipt_id,'state',v_intent.state,
   'provider_state','fulfilled','governed_claim_finalized',true,'business_action_success',true,'receipt_replay',true);
 end if;
 -- Execution/control locks precede intent/outbox/attempt locks. Independent
 -- query is allowed after source/control expiry; it cannot call plan or send.
 if not v_query then
  v_plan:=authz.nexloop_assert_effect_plan(v_intent.context_id,v_tenant,p_world);
  if not exists(select 1 from runtime.nexloop_effect_control_reservations r where r.intent_id=v_intent.intent_id
   and r.tenant_id=v_tenant and r.control_id=v_plan->>'control_id') then raise exception 'effect shared budget reservation unavailable' using errcode='42501';end if;
 end if;
 select i.* into v_intent from runtime.nexloop_effect_intents i where i.intent_id=v_intent.intent_id and i.tenant_id=v_tenant and i.world=p_world for update;
 select o.* into v_outbox from runtime.nexloop_effect_outbox o where o.intent_id=v_intent.intent_id and o.tenant_id=v_tenant and o.world=p_world for update;
 if not found then raise exception 'effect outbox unavailable' using errcode='42501';end if;
 select a.* into v_attempt from runtime.nexloop_effect_attempts a where a.intent_id=v_intent.intent_id and a.tenant_id=v_tenant and a.world=p_world for update;
 if v_verb='claim' then
  if v_outbox.state in ('fulfilled','failed') then raise exception 'effect already terminal' using errcode='42501';end if;
  if v_outbox.lease_until is not null and v_outbox.lease_until>clock_timestamp() then raise exception 'effect lease busy' using errcode='40001';end if;
  if v_payload->>'lease_seconds' is null or jsonb_typeof(v_payload->'lease_seconds') is distinct from 'number'
   or (v_payload->>'lease_seconds') !~ '^[0-9]+$' then raise exception 'effect lease invalid' using errcode='22023';end if;
  v_seconds:=(v_payload->>'lease_seconds')::integer;
  if v_seconds not between 3 and 300 then raise exception 'effect lease invalid' using errcode='22023';end if;
  if v_attempt.intent_id is null then
   if v_query then raise exception 'effect initial claim needs execute' using errcode='42501';end if;
   v_envelope:=v_payload->'action_claim';v_command:=authz.nexloop_effect_assert_claim(v_intent,v_envelope,'reserve',v_payload->>'action_request_text');
   v_reserved:=authz.nexloop_action_claim_command(p_digest,p_world,v_envelope->>'text',v_envelope->>'signature',v_envelope->>'payload');
   if v_reserved->>'disposition'='in_progress' then
    select a.* into v_action from runtime.nexloop_action_claims a where a.tenant_id=v_tenant and a.world=p_world
     and a.action_name=v_intent.action_name and a.intent_id=v_intent.intent_id::text for update;
    if not found or v_action.principal_id is distinct from v_principal or v_action.claim->'binding' is distinct from v_command->'binding'
     or v_action.claim->>'state' is distinct from 'active' or v_action.claim->>'lease_expires_at' is null
     or (v_action.claim->>'lease_expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect Action claim unavailable' using errcode='42501';end if;
   elsif v_reserved->>'disposition' is distinct from 'claimed' then raise exception 'effect Action claim unavailable' using errcode='42501';end if;
   v_action_lease:=(coalesce(v_reserved->'claim',v_action.claim)->>'lease_expires_at')::timestamptz;
   if v_intent.origin_run_id is null then
    select r.* into v_run from runtime.nexloop_effect_submissions s join authz.nexloop_run_credentials r on r.run_id=s.run_id
     where s.intent_id=v_intent.intent_id order by s.created_at,s.run_id limit 1;
    if not found then raise exception 'effect source unavailable' using errcode='42501';end if;
    update runtime.nexloop_effect_intents i set origin_run_id=v_run.run_id where i.intent_id=v_intent.intent_id;
    v_intent.origin_run_id:=v_run.run_id;
   end if;
  else
   if not v_query then raise exception 'effect requires query first' using errcode='42501';end if;
   if v_payload ? 'action_claim' then raise exception 'query cannot reserve execution' using errcode='42501';end if;
  end if;
  v_fence:=v_outbox.fence+1;v_lease:=clock_timestamp()+make_interval(secs=>v_seconds);
  update runtime.nexloop_effect_outbox o set state='leased',fence=v_fence,lease_until=v_lease,lease_credential=p_digest,lease_principal=v_principal
   where o.intent_id=v_intent.intent_id;
  v_result:=jsonb_build_object('intent_id',v_intent.intent_id,'effect_fence',v_fence,'lease_until',v_lease,
   'origin_run_id',v_intent.origin_run_id,'has_attempt',v_attempt.intent_id is not null,
   'action_claim',coalesce(v_reserved->'claim',v_action.claim),'action_claim_result',v_reserved);
 else
  if v_payload->>'effect_fence' is null or (v_payload->>'effect_fence') !~ '^[1-9][0-9]*$' then raise exception 'effect fence invalid' using errcode='42501';end if;
  v_fence:=(v_payload->>'effect_fence')::bigint;v_lease:=v_outbox.lease_until;
  if v_outbox.state<>'leased' or v_outbox.fence is distinct from v_fence or v_outbox.lease_credential is distinct from p_digest
   or v_outbox.lease_principal is distinct from v_principal or v_lease is null or v_lease<=clock_timestamp() then
   raise exception 'effect lease lost' using errcode='40001';end if;
  select a.* into v_action from runtime.nexloop_action_claims a where a.tenant_id=v_tenant and a.world=p_world
   and a.action_name=v_intent.action_name and a.intent_id=v_intent.intent_id::text for update;
  if v_verb='resolve' then
   select r.* into v_run from authz.nexloop_run_credentials r where r.run_id=v_intent.origin_run_id;
   if not found then raise exception 'effect source unavailable' using errcode='42501';end if;
   v_result:=jsonb_build_object('intent_id',v_intent.intent_id,'receipt_id',v_intent.receipt_id,'_run_digest',v_run.token_digest,'origin_run_id',v_run.run_id,
    'frozen_request',v_intent.frozen_request,'action_definition',v_intent.action_definition,'capability',v_intent.capability,
    'business_digest',v_intent.business_digest,'action_claim',v_action.claim,'effect_fence',v_fence,
    'lease_until',v_lease,
    'attempt_revision',v_attempt.attempt_revision,'has_attempt',v_attempt.intent_id is not null,
    'provider_key',v_intent.intent_id::text,'provider_payload_digest',v_intent.provider_payload_digest);
  elsif v_verb in ('query','observe','unknown') then
   if v_attempt.intent_id is null then raise exception 'no external attempt to reconcile' using errcode='42501';end if;
   if v_payload->>'attempt_revision' is null or (v_payload->>'attempt_revision') !~ '^[1-9][0-9]*$'
    or v_attempt.attempt_revision is distinct from (v_payload->>'attempt_revision')::bigint then raise exception 'effect attempt stale' using errcode='40001';end if;
   if v_verb='query' then
    v_result:=jsonb_build_object('intent_id',v_intent.intent_id,'provider_key',v_attempt.provider_key,'provider_payload_digest',v_attempt.provider_payload_digest,
     'attempt_revision',v_attempt.attempt_revision,'effect_fence',v_fence);
   elsif v_verb='unknown' then
    update runtime.nexloop_effect_attempts a set state='unknown',effect_fence=v_fence,attempt_revision=a.attempt_revision+1,updated_at=clock_timestamp() where a.intent_id=v_intent.intent_id;
    update runtime.nexloop_effect_intents i set state='unknown',governed_claim_finalized=false where i.intent_id=v_intent.intent_id;
    update runtime.nexloop_effect_outbox o set state='unknown',lease_until=null,lease_credential=null,lease_principal=null,available_at=clock_timestamp() where o.intent_id=v_intent.intent_id;
    insert into runtime.nexloop_effect_events values(gen_random_uuid(),v_intent.intent_id,v_tenant,p_world,'unknown',v_fence,clock_timestamp(),'{}');
    v_result:=jsonb_build_object('intent_id',v_intent.intent_id,'state','unknown','business_action_success',false);
   else
    v_state:=v_payload->>'provider_state';v_reference:=v_payload->>'provider_reference';
    if v_state is null or v_state not in ('accepted','fulfilled','not_found')
     or (v_state='not_found' and (v_payload->'provider_payload_digest' is distinct from 'null'::jsonb or v_payload->'provider_reference' is distinct from 'null'::jsonb))
     or (v_state<>'not_found' and (v_payload->>'provider_payload_digest' is distinct from v_attempt.provider_payload_digest
      or v_reference is null or v_reference !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$')) then raise exception 'effect observation invalid' using errcode='22023';end if;
    if v_attempt.provider_reference is not null and (v_state='not_found' or v_reference is distinct from v_attempt.provider_reference)
     or v_attempt.state='observed_fulfilled' and v_state<>'fulfilled' then raise exception 'effect provider evidence conflict' using errcode='40001';end if;
    v_observation_id:=gen_random_uuid();
    insert into runtime.nexloop_effect_observations values(v_observation_id,v_intent.intent_id,v_tenant,p_world,v_attempt.attempt_revision,v_fence,
     v_principal,p_digest,v_state,v_payload->>'provider_payload_digest',v_reference,clock_timestamp());
    update runtime.nexloop_effect_attempts a set state=case v_state when 'fulfilled' then 'observed_fulfilled' when 'accepted' then 'provider_accepted' else 'unknown' end,
     provider_reference=v_reference,effect_fence=v_fence,attempt_revision=a.attempt_revision+1,updated_at=clock_timestamp() where a.intent_id=v_intent.intent_id;
    update runtime.nexloop_effect_intents i set state=case v_state when 'fulfilled' then 'observed_fulfilled' when 'accepted' then 'dispatching' else 'unknown' end,
     governed_claim_finalized=false where i.intent_id=v_intent.intent_id;
    if v_state<>'fulfilled' then
     update runtime.nexloop_effect_outbox o set state='query_pending',lease_until=null,lease_credential=null,lease_principal=null,
      available_at=clock_timestamp()+interval '1 second' where o.intent_id=v_intent.intent_id;
    end if;
    insert into runtime.nexloop_effect_events values(gen_random_uuid(),v_intent.intent_id,v_tenant,p_world,'observed',v_fence,clock_timestamp(),jsonb_build_object('observation_id',v_observation_id));
    v_result:=jsonb_build_object('intent_id',v_intent.intent_id,'observation_id',v_observation_id,'attempt_revision',v_attempt.attempt_revision+1,
     'state',case v_state when 'fulfilled' then 'observed_fulfilled' when 'accepted' then 'dispatching' else 'unknown' end,'provider_state',v_state,'governed_claim_finalized',false,'business_action_success',false);
   end if;
  else
   -- Only send/finalize branches check the source EXECUTE chain. Query remains
   -- possible after source expiry but never inherits its original execute right.
   select r.* into v_run from authz.nexloop_run_credentials r where r.run_id=v_intent.origin_run_id for share;
   if not found or v_run.status<>'active' or v_run.expires_at<=clock_timestamp()
    or v_payload->'origin_proof'->>'resource_id' is distinct from v_target
    or v_payload->'origin_proof'->>'expires_at' is null
    or (v_payload->'origin_proof'->>'expires_at')::timestamptz<=clock_timestamp()
    or (v_payload->'origin_proof'->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
    or v_payload->'origin_proof'->>'tenant_id' is distinct from v_tenant or v_payload->'origin_proof'->>'world' is distinct from p_world then
    raise exception 'effect origin authority unavailable' using errcode='42501';end if;
   perform authz.nexloop_lock_credential(v_run.token_digest,p_world,v_target);
   perform authz.nexloop_assert_action_authority(v_run.token_digest,p_world,v_payload->'origin_proof');
   if v_verb='admit' then
    if v_attempt.intent_id is not null then raise exception 'effect requires query first' using errcode='42501';end if;
    if v_action.principal_id is distinct from v_principal or v_action.claim->>'state' is distinct from 'active'
     or v_action.claim->>'lease_expires_at' is null or (v_action.claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
     or v_payload->>'action_claim_revision' is null
     or (v_action.claim->>'claim_revision')::bigint is distinct from (v_payload->>'action_claim_revision')::bigint
     or v_action.claim->>'fencing_token' is distinct from v_payload->>'action_fencing_token' then raise exception 'effect Action fence lost' using errcode='40001';end if;
    v_action_lease:=(v_action.claim->>'lease_expires_at')::timestamptz;
    insert into runtime.nexloop_effect_attempts(intent_id,attempt_id,tenant_id,world,provider_key,provider_payload_digest,executor_principal,origin_run_id,
     action_claim_revision,action_fencing_token,effect_fence,attempt_revision,state,dispatched_at,updated_at)
     values(v_intent.intent_id,gen_random_uuid(),v_tenant,p_world,v_intent.intent_id::text,v_intent.provider_payload_digest,v_principal,v_run.run_id,
      (v_action.claim->>'claim_revision')::bigint,v_action.claim->>'fencing_token',v_fence,1,'dispatching',clock_timestamp(),clock_timestamp());
    update runtime.nexloop_effect_intents i set state='dispatching' where i.intent_id=v_intent.intent_id;
    insert into runtime.nexloop_effect_events values(gen_random_uuid(),v_intent.intent_id,v_tenant,p_world,'dispatching',v_fence,clock_timestamp(),'{}');
    v_result:=jsonb_build_object('intent_id',v_intent.intent_id,'provider_key',v_intent.intent_id::text,'provider_payload_digest',v_intent.provider_payload_digest,
     'parameters',v_intent.frozen_request->'parameters','attempt_revision',1,'effect_fence',v_fence,'dispatch_authorized',true);
   elsif v_verb='finalize' then
    if v_attempt.intent_id is null or v_payload->>'attempt_revision' is null
     or v_attempt.attempt_revision is distinct from (v_payload->>'attempt_revision')::bigint then raise exception 'effect attempt stale' using errcode='40001';end if;
    select o.* into v_observation from runtime.nexloop_effect_observations o where o.observation_id=(v_payload->>'observation_id')::uuid
     and o.intent_id=v_intent.intent_id and o.tenant_id=v_tenant and o.world=p_world;
    if not found or v_observation.provider_state<>'fulfilled' or v_observation.provider_payload_digest is distinct from v_attempt.provider_payload_digest
     or v_observation.provider_reference is distinct from v_attempt.provider_reference
     or v_attempt.state<>'observed_fulfilled' then raise exception 'fulfilled provider evidence required' using errcode='42501';end if;
    v_envelope:=v_payload->'action_claim';
    if v_payload->>'phase'='reserve' then
     v_command:=authz.nexloop_effect_assert_claim(v_intent,v_envelope,'reserve',v_payload->>'action_request_text');
     v_reserved:=authz.nexloop_action_claim_command(p_digest,p_world,v_envelope->>'text',v_envelope->>'signature',v_envelope->>'payload');
     if v_reserved->>'disposition'='in_progress' then
      if v_action.principal_id is distinct from v_principal or v_action.claim->'binding' is distinct from v_command->'binding'
       or v_action.claim->>'state' is distinct from 'active' or v_action.claim->>'lease_expires_at' is null
       or (v_action.claim->>'lease_expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect finalization claim unavailable' using errcode='42501';end if;
      v_command:=v_action.claim;
     elsif v_reserved->>'disposition'='claimed' then v_command:=v_reserved->'claim';
     else raise exception 'effect finalization claim unavailable' using errcode='42501';end if;
     v_action_lease:=(v_command->>'lease_expires_at')::timestamptz;
     update runtime.nexloop_effect_attempts a set action_claim_revision=(v_command->>'claim_revision')::bigint,
      action_fencing_token=v_command->>'fencing_token',finalization_xid=txid_current() where a.intent_id=v_intent.intent_id;
     v_result:=jsonb_build_object('intent_id',v_intent.intent_id,'action_claim',v_command,'action_claim_result',v_reserved,'attempt_revision',v_attempt.attempt_revision,'effect_fence',v_fence);
    elsif v_payload->>'phase'='commit' then
     v_action_lease:=(v_action.claim->>'lease_expires_at')::timestamptz;
     if v_attempt.finalization_xid is distinct from txid_current() then raise exception 'effect finalization must be atomic' using errcode='42501';end if;
     v_command:=authz.nexloop_effect_assert_claim(v_intent,v_envelope,'finalize',v_payload->>'action_request_text');
     if v_command->>'expected_claim_revision' is null or (v_command->>'expected_claim_revision')::bigint is distinct from v_attempt.action_claim_revision
      or v_command->>'fencing_token' is distinct from v_attempt.action_fencing_token
      or v_command->'outcome'->>'status' is distinct from 'succeeded'
      or v_command->'outcome'->>'outcome_id' is distinct from v_intent.receipt_id::text
      or v_payload->>'outcome_text' is null
      or (v_payload->>'outcome_text')::jsonb is distinct from jsonb_build_object('intent_id',v_intent.intent_id::text,
       'receipt_id',v_intent.receipt_id::text,'provider_state','fulfilled','provider_reference',v_observation.provider_reference,
       'provider_payload_digest',v_observation.provider_payload_digest)
      or v_command->'outcome'->>'outcome_digest' is distinct from encode(sha256(convert_to(v_payload->>'outcome_text','UTF8')),'hex')
     then raise exception 'effect completion binding denied' using errcode='42501';end if;
     perform authz.nexloop_action_claim_command(p_digest,p_world,v_envelope->>'text',v_envelope->>'signature',v_envelope->>'payload');
     update runtime.nexloop_effect_attempts a set state='fulfilled',finalization_xid=null,updated_at=clock_timestamp() where a.intent_id=v_intent.intent_id;
     update runtime.nexloop_effect_intents i set state='fulfilled',governed_claim_finalized=true where i.intent_id=v_intent.intent_id;
     update runtime.nexloop_effect_outbox o set state='fulfilled',lease_until=null,lease_credential=null,lease_principal=null where o.intent_id=v_intent.intent_id;
     insert into runtime.nexloop_effect_events values(gen_random_uuid(),v_intent.intent_id,v_tenant,p_world,'fulfilled',v_fence,clock_timestamp(),jsonb_build_object('observation_id',v_observation.observation_id));
     v_result:=jsonb_build_object('intent_id',v_intent.intent_id,'receipt_id',v_intent.receipt_id,'state','fulfilled',
      'provider_state','fulfilled','governed_claim_finalized',true,'business_action_success',true);
    else raise exception 'effect finalization phase invalid' using errcode='22023';end if;
   else raise exception 'effect operation invalid' using errcode='42501';end if;
   perform authz.nexloop_assert_action_authority(v_run.token_digest,p_world,v_payload->'origin_proof');
   perform authz.nexloop_service_identity_snapshot(v_run.token_digest,p_world);
   if v_run.expires_at<=clock_timestamp() then raise exception 'effect origin expired' using errcode='42501';end if;
  end if;
 end if;
 if not v_query then perform authz.nexloop_assert_effect_plan(v_intent.context_id,v_tenant,p_world);end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,v_claims);
 if v_lease is null or v_lease<=clock_timestamp() or (v_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'effect permit expired' using errcode='42501';end if;
 if v_verb in ('admit','finalize') or v_verb='claim' and not v_query then
  if v_action_lease is null or v_action_lease<=clock_timestamp() then raise exception 'effect Action lease expired' using errcode='42501';end if;
 end if;
 if v_envelope is not null then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,(v_envelope->>'text')::jsonb);
  if ((v_envelope->>'text')::jsonb->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect nested proof expired' using errcode='42501';end if;
 end if;
 -- Query observation and controlled completion cannot report transcripts,
 -- credentials, identity rows, DSNs or signing material in any public result.
 return v_result;
end $$;
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_execution_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler;
grant execute on function authz.nexloop_effect_execution_command(text,text,text,text,text) to nexloop_action_worker;

-- Required integration verification before publishing this draft lineage:
-- 1. Current genuine executor proof + pinned principal + published definition,
--    atomic existing signed EIOS claim reserve and effect-outbox lease/fence.
-- 2. Private owned-effect resolve selects only a bound original real Run; full
--    current origin and executor Action proofs + actual plan/control/shared
--    budget are checked again while persisting dispatching BEFORE HTTP.
-- 3. Any existing attempt forces query-first. No mark_retryable before query.
-- 4. Independent current query Action proof may persist provider observations
--    after origin expiry. It cannot dispatch, renew an EXECUTE claim or finalize
--    the original claim; observed_fulfilled always means business_success=false.
-- 5. With both current execution authorities, exact active Action lease/fence
--    and effect fence: actual signed EIOS finalize, receipt/event/outbox update
--    are one PG transaction. Failure/expiry rolls all back. Never POST again
--    merely because this result commit or a transport observation is unknown.
