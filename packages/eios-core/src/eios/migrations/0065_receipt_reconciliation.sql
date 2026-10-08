-- DRAFT: independent current receipt authority; no dispatch or original Role grant.
create table runtime.nexloop_effect_query_admissions(
 query_id uuid primary key,intent_id uuid not null,tenant_id text not null,world text not null,
 effect_fence bigint not null,attempt_revision bigint not null,profile_digest text not null,
 payload_digest text not null,principal_id text not null,credential_digest text not null,
 issued_at timestamptz not null,expires_at timestamptz not null,observation_id uuid unique,
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_attempts(intent_id,tenant_id,world));
create table runtime.nexloop_effect_recovery_audits(
 recovery_id uuid primary key,intent_id uuid not null unique,tenant_id text not null,world text not null,
 query_id uuid not null references runtime.nexloop_effect_query_admissions(query_id),observation_id uuid not null,
 effect_fence bigint not null,original_claim_revision bigint not null,original_claim_fence text not null,
 recovery_actor text not null,recovery_credential_digest text not null,recovery_decision text not null,recovery_claim jsonb not null,
 original_claim jsonb not null,outcome jsonb not null,recorded_at timestamptz not null);
alter table runtime.nexloop_effect_query_admissions owner to nexloop_owner;
alter table runtime.nexloop_effect_recovery_audits owner to nexloop_owner;
alter table runtime.nexloop_effect_query_admissions enable row level security;
alter table runtime.nexloop_effect_query_admissions force row level security;
alter table runtime.nexloop_effect_recovery_audits enable row level security;
alter table runtime.nexloop_effect_recovery_audits force row level security;
create policy query_admission_tenant on runtime.nexloop_effect_query_admissions to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy recovery_audit_tenant on runtime.nexloop_effect_recovery_audits to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_effect_query_admissions,runtime.nexloop_effect_recovery_audits from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

create function authz.nexloop_receipt_reconcile_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;tenant text;actor text;target text;
 verb text:=p->>'verb';e jsonb;r jsonb;d control.nexloop_action_definitions;qd control.nexloop_action_definitions;
 i runtime.nexloop_effect_intents;o runtime.nexloop_effect_outbox;a runtime.nexloop_effect_attempts;
 q runtime.nexloop_effect_query_admissions;obs runtime.nexloop_effect_observations;ac runtime.nexloop_action_claims;
 recovery runtime.nexloop_action_claims;cmd jsonb;result jsonb;outcome jsonb;terminal jsonb;now_at timestamptz;rid uuid;final_now timestamptz;affected bigint;nested_claims jsonb;
begin
 if session_user<>'nexloop_action_worker' or p_world is distinct from 'real' or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576 or jsonb_typeof(c) is distinct from 'object'
  or jsonb_typeof(p) is distinct from 'object' or verb is null or verb not in ('admit_query','observe_query','recover')
 then raise exception 'receipt recovery unavailable' using errcode='42501';end if;
 if (select count(*) from jsonb_object_keys(p))<>(case when verb='recover' then 9 else 5 end) then raise exception 'recovery parameters rejected' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active for share;
 target:=case when verb='recover' then 'eios:action:nexloop.service.receipt_reconcile:1' else 'eios:action:nexloop.service.query:1' end;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-execution-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-execution-v1' or c->>'resource_id' is distinct from target
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
 then raise exception 'receipt recovery proof unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb then raise exception 'independent recovery actor required' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';actor:=ident->'binding'->>'subject_principal_id';
 select * into d from control.nexloop_action_definitions where tenant_id=tenant and world=p_world and resource_id=target and active for share;
 if not found or d.definition is distinct from c->'definition' or d.capability is distinct from c->'capability'
  or d.definition->'governance'->>'risk_level' is distinct from 'low' or d.definition->'governance'->>'approval_mode' is distinct from 'none'
  or d.definition->'governance'->'policy_refs' is distinct from '[]'::jsonb
  or coalesce(d.definition->'preconditions','[]'::jsonb)<>'[]'::jsonb
 then raise exception 'recovery contract unavailable' using errcode='42501';end if;
 if verb='recover' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,p->'query_proof');
  if p->'query_proof'->>'resource_id' is distinct from 'eios:action:nexloop.service.query:1'
   then raise exception 'current query authority required' using errcode='42501';end if;
  select * into qd from control.nexloop_action_definitions where tenant_id=tenant and world=p_world
   and resource_id='eios:action:nexloop.service.query:1' for share;
  if not found or not qd.active or qd.definition is distinct from p->'query_proof'->'definition'
   or qd.capability is distinct from p->'query_proof'->'capability'
   or qd.definition->'governance'->>'risk_level' is distinct from 'low'
   or qd.definition->'governance'->>'approval_mode' is distinct from 'none'
   or qd.definition->'governance'->'policy_refs' is distinct from '[]'::jsonb
   or coalesce(qd.definition->'preconditions','[]'::jsonb)<>'[]'::jsonb
   or p->'query_proof'->>'expires_at' is null or (p->'query_proof'->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  then raise exception 'current query contract required' using errcode='42501';end if;
  perform pg_advisory_xact_lock(hashtextextended('nexloop-action:'||tenant||':'||p_world||':nexloop.service.receipt_reconcile:receipt-reconcile:'||(p->>'intent_id'),0));
 end if;
 select * into i from runtime.nexloop_effect_intents where intent_id=(p->>'intent_id')::uuid and tenant_id=tenant and world=p_world for update;
 if not found or i.executor_principal is distinct from actor then raise exception 'owned receipt required' using errcode='42501';end if;
 select * into o from runtime.nexloop_effect_outbox where intent_id=i.intent_id and tenant_id=tenant and world=p_world for update;
 if not found or o.state<>'leased' or o.fence is distinct from (p->>'effect_fence')::bigint
  or o.lease_principal is distinct from actor or o.lease_credential is distinct from p_digest
  or o.lease_until is null or o.lease_until<=clock_timestamp() then raise exception 'current effect fence required' using errcode='40001';end if;
 select * into a from runtime.nexloop_effect_attempts where intent_id=i.intent_id and tenant_id=tenant and world=p_world for update;
 if not found or a.provider_key is distinct from i.intent_id::text then raise exception 'stable external intent required' using errcode='42501';end if;
 if verb='admit_query' then
  e:=p->'query_envelope';
  if (e->>'payload')::jsonb is distinct from jsonb_build_object('verb','query','intent_id',i.intent_id::text,'effect_fence',o.fence,'attempt_revision',a.attempt_revision,'provider_profile_digest',p->>'provider_profile_digest')
  then raise exception 'query binding unavailable' using errcode='42501';end if;
  r:=authz.nexloop_effect_execution_command(p_digest,p_world,e->>'text',e->>'signature',e->>'payload');
  q.query_id:=gen_random_uuid();
  insert into runtime.nexloop_effect_query_admissions values(q.query_id,i.intent_id,tenant,p_world,o.fence,a.attempt_revision,p->>'provider_profile_digest',r->>'provider_payload_digest',actor,p_digest,clock_timestamp(),least(o.lease_until,(c->>'expires_at')::timestamptz),null);
  result:=r||jsonb_build_object('query_id',q.query_id);
 else
  select * into q from runtime.nexloop_effect_query_admissions where query_id=(p->>'query_id')::uuid and tenant_id=tenant and world=p_world for update;
  if not found or q.intent_id is distinct from i.intent_id or q.effect_fence is distinct from o.fence
   or q.principal_id is distinct from actor or q.credential_digest is distinct from p_digest
   or q.payload_digest is distinct from a.provider_payload_digest or not exists(select 1 from runtime.nexloop_effect_provider_bindings pb where pb.intent_id=i.intent_id and pb.tenant_id=tenant and pb.world=p_world and pb.provider_profile_digest=q.profile_digest)
   then raise exception 'query lineage unavailable' using errcode='42501';end if;
  if verb='observe_query' then
   if q.observation_id is not null or q.expires_at<=clock_timestamp() or q.attempt_revision is distinct from a.attempt_revision then raise exception 'query admission stale' using errcode='40001';end if;
   e:=p->'observation_envelope';cmd:=(e->>'payload')::jsonb;
   if jsonb_typeof(cmd) is distinct from 'object' or (select count(*) from jsonb_object_keys(cmd))<>8
    or cmd->>'verb' is distinct from 'observe' or cmd->>'intent_id' is distinct from i.intent_id::text
    or cmd->>'effect_fence' is distinct from o.fence::text or cmd->>'attempt_revision' is distinct from a.attempt_revision::text
    or cmd->>'provider_profile_digest' is distinct from q.profile_digest then raise exception 'query observation mismatch' using errcode='42501';end if;
   result:=authz.nexloop_effect_execution_command(p_digest,p_world,e->>'text',e->>'signature',e->>'payload');
   update runtime.nexloop_effect_query_admissions set observation_id=(result->>'observation_id')::uuid where query_id=q.query_id;
   result:=result||jsonb_build_object('query_id',q.query_id);
  else
   select * into obs from runtime.nexloop_effect_observations where observation_id=q.observation_id and tenant_id=tenant and world=p_world;
   if not found or obs.intent_id is distinct from i.intent_id or obs.effect_fence is distinct from o.fence
    or obs.attempt_revision+1 is distinct from a.attempt_revision or obs.attempt_revision is distinct from q.attempt_revision
    or obs.observer_principal is distinct from actor or obs.observer_credential_digest is distinct from p_digest
    or obs.provider_state is distinct from 'fulfilled' or obs.provider_payload_digest is distinct from a.provider_payload_digest
    or obs.provider_reference is distinct from a.provider_reference or a.state is distinct from 'observed_fulfilled'
    or a.effect_fence is distinct from o.fence or a.executor_principal is distinct from actor or a.origin_run_id is distinct from i.origin_run_id
    or i.state is distinct from 'observed_fulfilled' or i.governed_claim_finalized
   then raise exception 'durable fulfilled query required' using errcode='42501';end if;
   select * into ac from runtime.nexloop_action_claims where tenant_id=tenant and world=p_world and action_name=i.action_name and intent_id=i.intent_id::text for update;
   if not found or ac.principal_id is distinct from actor or ac.claim->>'state' is null or ac.claim->>'state' not in ('active','retryable')
    or ac.claim->>'claim_revision' is distinct from a.action_claim_revision::text
    or ac.claim->>'fencing_token' is distinct from a.action_fencing_token
    then raise exception 'original claim fence required' using errcode='42501';end if;
   -- Frozen original binding is checked as data; no invented SEND authorization envelope.
   if p->>'original_request_text' is null or (p->>'original_request_text')::jsonb is distinct from i.frozen_request
    or ac.claim->'key' is distinct from jsonb_build_object('tenant_id',tenant,'action_stable_name',i.action_name,'idempotency_key',i.intent_id::text)
    or ac.claim->'binding'->>'invocation_id' is distinct from i.intent_id::text
    or ac.claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p->>'original_request_text','UTF8')),'hex')
    or ac.claim->'binding'->>'adapter_id' is distinct from 'nexloop.effect' or ac.claim->'binding'->>'target_system' is distinct from 'service'
    or ac.claim->'binding'->'action_reference' is distinct from jsonb_build_object('tenant_id',tenant,'definition_type','action','stable_name',i.action_name,'version',i.action_version,'contract_digest',i.action_definition->>'contract_digest')
    or ac.claim->'binding'->'capability_binding' is distinct from jsonb_build_object('capability_name',i.capability->>'capability_name','capability_version',i.capability->'capability_version','schema_hash',i.capability->>'schema_hash')
    then raise exception 'original frozen binding required' using errcode='42501';end if;
   e:=p->'recovery_reserve';cmd:=(e->>'payload')::jsonb;
   if cmd->'key' is distinct from jsonb_build_object('tenant_id',tenant,'action_stable_name','nexloop.service.receipt_reconcile','idempotency_key','receipt-reconcile:'||i.intent_id::text)
    or cmd->'binding'->'action_reference' is distinct from jsonb_build_object('tenant_id',tenant,'definition_type','action','stable_name','nexloop.service.receipt_reconcile','version',1,'contract_digest',d.definition->>'contract_digest')
    or cmd->'key'->>'idempotency_key' is distinct from 'receipt-reconcile:'||i.intent_id::text
    or cmd->'binding'->>'invocation_id' is distinct from 'receipt-reconcile:'||i.intent_id::text
    or cmd->'binding'->'action_reference'->>'contract_digest' is distinct from d.definition->>'contract_digest'
    or cmd->'binding'->>'adapter_id' is distinct from 'nexloop.receipt.reconcile' or cmd->'binding'->>'target_system' is distinct from 'service'
    or cmd->'binding'->'capability_binding' is distinct from d.definition->'capability_binding'
    or cmd->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p->>'recovery_request_text','UTF8')),'hex')
    or (p->>'recovery_request_text')::jsonb is distinct from jsonb_build_object('intent_id',i.intent_id::text,'effect_fence',o.fence,'query_id',q.query_id::text)
   then raise exception 'recovery action binding unavailable' using errcode='42501';end if;
   r:=authz.nexloop_action_claim_command(p_digest,p_world,e->>'text',e->>'signature',e->>'payload');
   if r->>'disposition' is distinct from 'claimed' then raise exception 'recovery action claim unavailable' using errcode='42501';end if;
   recovery.claim:=r->'claim';rid:=gen_random_uuid();now_at:=clock_timestamp();
   outcome:=jsonb_build_object('intent_id',i.intent_id::text,'receipt_id',i.receipt_id::text,'provider_state','fulfilled','provider_reference',obs.provider_reference,'provider_payload_digest',obs.provider_payload_digest);
   terminal:=ac.claim||jsonb_build_object('state','terminal','terminal_outcome',jsonb_build_object('outcome_id',i.receipt_id::text,'outcome_revision',1,'status','succeeded','outcome_digest',encode(sha256(convert_to(p->>'outcome_text','UTF8')),'hex'),'finalized_at',now_at));
   if (p->>'outcome_text')::jsonb is distinct from outcome then raise exception 'recovery outcome mismatch' using errcode='42501';end if;
   -- Explicit recovery policy: preserve principal/binding/revision/fence; never renew SEND lease.
   update runtime.nexloop_action_claims set claim=terminal where tenant_id=tenant and world=p_world and action_name=ac.action_name and intent_id=ac.intent_id;
   get diagnostics affected=row_count;
   if affected<>1 then raise exception 'original action terminal row unavailable' using errcode='42501';end if;
   update runtime.nexloop_effect_attempts set state='fulfilled',finalization_xid=null,updated_at=now_at where intent_id=i.intent_id;
   get diagnostics affected=row_count;
   if affected<>1 then raise exception 'effect attempt terminal row unavailable' using errcode='42501';end if;
   update runtime.nexloop_effect_intents set state='fulfilled',governed_claim_finalized=true where intent_id=i.intent_id;
   get diagnostics affected=row_count;
   if affected<>1 then raise exception 'effect intent terminal row unavailable' using errcode='42501';end if;
   update runtime.nexloop_effect_outbox set state='fulfilled',lease_until=null,lease_credential=null,lease_principal=null where intent_id=i.intent_id;
   get diagnostics affected=row_count;
   if affected<>1 then raise exception 'effect outbox terminal row unavailable' using errcode='42501';end if;
   insert into runtime.nexloop_effect_events values(gen_random_uuid(),i.intent_id,tenant,p_world,'fulfilled',o.fence,now_at,jsonb_build_object('query_id',q.query_id,'observation_id',obs.observation_id,'recovery_id',rid,'recovery_actor',actor));
   -- Recovery claim is finalized by the same governed owner transition after an actual reserve.
   recovery.claim:=recovery.claim||jsonb_build_object('state','terminal','terminal_outcome',jsonb_build_object('outcome_id',rid::text,'outcome_revision',1,'status','succeeded','outcome_digest',encode(sha256(convert_to(p->>'outcome_text','UTF8')),'hex'),'finalized_at',now_at));
   update runtime.nexloop_action_claims set claim=recovery.claim where tenant_id=tenant and world=p_world and action_name='nexloop.service.receipt_reconcile' and intent_id='receipt-reconcile:'||i.intent_id::text;
   get diagnostics affected=row_count;
   if affected<>1 then raise exception 'recovery terminal row unavailable' using errcode='42501';end if;
   insert into runtime.nexloop_effect_recovery_audits values(rid,i.intent_id,tenant,p_world,q.query_id,obs.observation_id,o.fence,a.action_claim_revision,a.action_fencing_token,actor,p_digest,(e->>'text')::jsonb->>'permit_id',recovery.claim,ac.claim,outcome,now_at);
   result:=jsonb_build_object('state','fulfilled','provider_state','fulfilled','business_action_success',true,'governed_claim_finalized',true,'recovery_claim_result',r);
  end if;
 end if;
 -- All potentially blocking authority helpers precede the final no-lock clock gate.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if verb='recover' then perform authz.nexloop_assert_action_authority(p_digest,p_world,p->'query_proof');end if;
 nested_claims:=(e->>'text')::jsonb;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,nested_claims);
 final_now:=clock_timestamp();
 if c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=final_now
  or o.lease_until is null or o.lease_until<=final_now
  or nested_claims->>'expires_at' is null or (nested_claims->>'expires_at')::timestamptz<=final_now
  or (verb='observe_query' and (q.expires_at is null or q.expires_at<=final_now))
  or (verb='recover' and (p->'query_proof'->>'expires_at' is null or (p->'query_proof'->>'expires_at')::timestamptz<=final_now
   or recovery.claim->>'lease_expires_at' is null or (recovery.claim->>'lease_expires_at')::timestamptz<=final_now))
 then raise exception 'recovery authority expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_receipt_reconcile_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_receipt_reconcile_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_receipt_reconcile_command(text,text,text,text,text) to nexloop_action_worker;

alter function authz.nexloop_effect_execution_command(text,text,text,text,text) rename to nexloop_effect_execution_before_receipt_v0064;
revoke all on function authz.nexloop_effect_execution_before_receipt_v0064(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function authz.nexloop_effect_execution_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare r jsonb;ref text;
begin
 r:=authz.nexloop_effect_execution_before_receipt_v0064(p_digest,p_world,p_text,p_signature,p_payload);
 if p_payload::jsonb->>'verb'='resolve' then
  -- Tenant is already authenticated and scoped by actual owned resolve.
  select provider_reference into ref from runtime.nexloop_effect_attempts where intent_id=(r->>'intent_id')::uuid and tenant_id=current_setting('eios.tenant_id',true) and world=p_world;
  r:=r||jsonb_build_object('provider_reference',ref);
 end if;
 return r;
end $$;
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_execution_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_effect_execution_command(text,text,text,text,text) to nexloop_action_worker;
