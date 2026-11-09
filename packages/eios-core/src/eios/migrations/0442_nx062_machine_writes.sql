-- NX-062 M5b (docs/implementation/NX-062-M5-design.md §3, §4, §5, §9 R2-R5): machine writes without external effect - the intent
-- table, prepare / commit / intent query, the 0124 entry renamed and wrapped with the machine core, the grant guard opened to the
-- allowlisted write capabilities, and the machine requester of an approval. No write capability is registered here (the object note
-- comes with M5c); tests register a probe in their fixtures only. The migration number is temporary; it lives only in the file name and
-- the catalog.

-- R2: the entry this migration wraps must be the 0348 body (another change first has to be merged into this migration).
do $check$
begin
 if encode(sha256(convert_to(pg_get_functiondef('authz.nexloop_goal_governed_action(text,text,text,text,text)'::regprocedure),'UTF8')),'hex')
   is distinct from '3158bf4dfd1fdd86a31c90986515617ee09fde4d174ace9128e2d2eadc966d47' then
  raise exception 'NX-062 M5b: authz.nexloop_goal_governed_action is not the 0348 body this migration wraps' using errcode='55000';end if;
end $check$;

-- 1. Write capabilities: their object types and parameters (append-only; a migration or a fixture registers them) ----------------------
create table control.nexloop_machine_write_capabilities (
 capability text primary key check(capability~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
 object_types text[] not null check(cardinality(object_types) between 1 and 32),
 param_keys text[] not null check(cardinality(param_keys) between 1 and 16),
 description text not null check(length(description) between 1 and 400),
 source_task text not null check(source_task~'^NX-[0-9]{3}$'),
 registered_at timestamptz not null default clock_timestamp(),
 check(not (param_keys && array['tenant_id','principal_id','subject_id','client_id','world','actor','operation','request_id','intent_id','key_id']))
);
alter table control.nexloop_machine_write_capabilities owner to nexloop_owner;
revoke all on control.nexloop_machine_write_capabilities from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create function control.nexloop_machine_write_capability_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare rule text;
begin
 if control.nexloop_machine_capability_forbidden(new.capability) then raise exception 'machine capability never granted' using errcode='42501';end if;
 select g.subject_rule into rule from control.nexloop_governed_capabilities g where g.capability=new.capability;
 if rule is distinct from 'agent_or_human' then raise exception 'machine write capability not a non-human governed capability' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_machine_write_capability_guard() owner to nexloop_owner;
revoke all on function control.nexloop_machine_write_capability_guard() from public;
create trigger nx062_guard before insert on control.nexloop_machine_write_capabilities for each row execute function control.nexloop_machine_write_capability_guard();
create trigger nx062_append_only before update or delete on control.nexloop_machine_write_capabilities for each row execute function control.nexloop_nx022_append_only();
create trigger nx062_no_truncate before truncate on control.nexloop_machine_write_capabilities for each statement execute function control.nexloop_nx022_append_only();

-- A capability may be granted when it is a registered read verb, or a registered write capability whose allowlist row is a current write.
create function control.nexloop_machine_grantable(p_capability text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from control.nexloop_machine_read_verbs v where v.verb=p_capability)
  or (exists(select 1 from control.nexloop_machine_write_capabilities w where w.capability=p_capability)
   and coalesce((select c.kind='write' from control.nexloop_machine_capabilities c where c.capability=p_capability order by c.revision desc limit 1),false))
$$;
alter function control.nexloop_machine_grantable(text) owner to nexloop_owner;
revoke all on function control.nexloop_machine_grantable(text) from public;

alter function control.nexloop_machine_verb_types(text) rename to nexloop_machine_verb_types_before_writes;
create function control.nexloop_machine_verb_types(p_verb text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce((select w.object_types from control.nexloop_machine_write_capabilities w where w.capability=p_verb),
  control.nexloop_machine_verb_types_before_writes(p_verb))
$$;

-- The grant guard and the grant handler accept a grantable capability (was: a read verb only). Both keep their oid (a trigger and the
-- governed registry reference them), so they are redefined in place; the copy guard registers the one change.
create or replace function authz.nexloop_machine_grant_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare latest integer;t text;
begin
 select max(revision) into latest from authz.nexloop_machine_client_grants where tenant_id=new.tenant_id and client_id=new.client_id and capability=new.capability;
 if new.revision is distinct from coalesce(latest,0)+1 then raise exception 'machine grant revision must follow the latest' using errcode='22023';end if;
 if new.state='active' then
  if control.nexloop_machine_capability_forbidden(new.capability) or not control.nexloop_machine_capability_current(new.capability)
   or not control.nexloop_machine_grantable(new.capability) then
   raise exception 'machine grant capability not allowed' using errcode='42501';end if;
  if cardinality(new.object_types)<>(select count(distinct x) from unnest(new.object_types) x) then raise exception 'machine grant types duplicated' using errcode='22023';end if;
  foreach t in array new.object_types loop
   if t is null or t like '%*%' or not (t=any(control.nexloop_machine_verb_types(new.capability))) then
    raise exception 'machine grant type not allowed' using errcode='42501';end if;
  end loop;
 end if;
 return new;
end $$;

create or replace function control.nexloop_governed_grant_machine_capability(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c authz.nexloop_machine_clients;v_types text[];v_revision integer;v_grant boolean:=body->>'operation'='grant_machine_capability';v_current text[];
begin
 perform control.nexloop_machine_access_check(p_tenant,p_world,p_principal,p_subject,body,
  case when v_grant then array['client_id','capability','object_types'] else array['client_id','capability','reason'] end);
 if body->>'operation' not in ('grant_machine_capability','revoke_machine_grant') or coalesce(body->>'client_id','')!~'^[a-z][a-z0-9-]{2,62}$'
  or coalesce(body->>'capability','')!~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$' then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 if v_grant then
  if jsonb_typeof(body->'object_types') is distinct from 'array' or jsonb_array_length(body->'object_types') not between 1 and 32
   or exists(select 1 from jsonb_array_elements(body->'object_types') e where jsonb_typeof(e)<>'string' or e#>>'{}'!~'^[A-Z][A-Za-z0-9_]{0,63}$') then
   perform control.nexloop_machine_access_refuse('invalid');end if;
  select array_agg(x order by x) into v_types from jsonb_array_elements_text(body->'object_types') x;
  if cardinality(v_types)<>(select count(distinct x) from unnest(v_types) x) then perform control.nexloop_machine_access_refuse('invalid');end if;
 elsif jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 select * into c from authz.nexloop_machine_clients where tenant_id=p_tenant and client_id=body->>'client_id' for update;
 if not found then perform control.nexloop_machine_access_refuse('client_not_found');end if;
 v_current:=authz.nexloop_machine_grant_types(p_tenant,c.client_id,body->>'capability');
 if v_grant then
  if c.status<>'active' then perform control.nexloop_machine_access_refuse('client_inactive');end if;
  if control.nexloop_machine_capability_forbidden(body->>'capability') or not control.nexloop_machine_capability_current(body->>'capability')
   or not control.nexloop_machine_grantable(body->>'capability') then
   perform control.nexloop_machine_access_refuse('capability_not_allowed');end if;
  if not (v_types<@control.nexloop_machine_verb_types(body->>'capability')) then perform control.nexloop_machine_access_refuse('type_not_allowed');end if;
  if v_current is not distinct from v_types then return jsonb_build_object('client_id',c.client_id,'capability',body->>'capability','changed',false);end if;
 elsif v_current is null then
  return jsonb_build_object('client_id',c.client_id,'capability',body->>'capability','changed',false);
 end if;
 select coalesce(max(revision),0)+1 into v_revision from authz.nexloop_machine_client_grants where tenant_id=p_tenant and client_id=c.client_id and capability=body->>'capability';
 insert into authz.nexloop_machine_client_grants(tenant_id,client_id,capability,revision,state,object_types,issued_by_principal,issue_request)
  values(p_tenant,c.client_id,body->>'capability',v_revision,case when v_grant then 'active' else 'revoked' end,case when v_grant then v_types else '{}' end,p_principal,p_intent);
 perform control.nexloop_machine_access_audit(p_tenant,p_principal,p_intent,case when v_grant then 'nexloop.machine_client.grant' else 'nexloop.machine_client.revoke_grant' end,
  'machine-grant:'||p_tenant||':'||c.client_id||':'||(body->>'capability')||':'||v_revision,
  jsonb_build_object('client_id',c.client_id,'capability',body->>'capability','revision',v_revision,'object_types',to_jsonb(coalesce(v_types,'{}'))));
 return jsonb_build_object('client_id',c.client_id,'capability',body->>'capability','revision',v_revision,'state',case when v_grant then 'active' else 'revoked' end,
  'object_types',to_jsonb(coalesce(v_types,'{}')),'changed',true);
end $$;

-- 2. The machine requester of an approval: its client subject (a machine has no membership; 0327's gate needs a requester subject).
alter function control.nexloop_approval_subject_of(text,text) rename to nexloop_approval_subject_of_before_machine;
create function control.nexloop_approval_subject_of(p_tenant text,p_principal text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v text;
begin
 select c.subject_id into v from authz.nexloop_machine_clients c where c.tenant_id=p_tenant and c.principal_id=p_principal;
 if v is not null then return v;end if;
 return control.nexloop_approval_subject_of_before_machine(p_tenant,p_principal);
end $$;

-- 3. Intents (C5, C6, R4): no parameters, no text; the result is codes and ids only -------------------------------------------------
create table runtime.nexloop_machine_intents (
 tenant_id text not null,
 client_id text not null,
 intent_id text not null check(intent_id~'^[A-Za-z0-9._:-]{8,128}$'),
 capability text not null,
 world text not null,
 params_digest text not null check(params_digest~'^[0-9a-f]{64}$'),
 prepare_digest text not null check(prepare_digest~'^[0-9a-f]{64}$'),
 expires_at timestamptz not null,
 phase text not null check(phase in ('prepare','pending_approval','committed','refused')),
 result_code text check(result_code~'^[a-z_:0-9A-Z]{1,80}$'),
 action_outcome_id text check(action_outcome_id~'^[0-9a-f]{64}$'),
 approval_id uuid,
 key_prefix text not null check(key_prefix~'^[a-z2-7]{12}$'),
 created_at timestamptz not null default clock_timestamp(),
 committed_at timestamptz,
 primary key(tenant_id,client_id,intent_id),
 foreign key(tenant_id,client_id) references authz.nexloop_machine_clients(tenant_id,client_id),
 check((phase in ('committed','refused'))=(committed_at is not null))
);
alter table runtime.nexloop_machine_intents owner to nexloop_owner;
alter table runtime.nexloop_machine_intents enable row level security;
alter table runtime.nexloop_machine_intents force row level security;
create policy nx062_owner on runtime.nexloop_machine_intents to nexloop_owner using(true) with check(true);
revoke all on runtime.nexloop_machine_intents from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

create function runtime.nexloop_machine_params_digest(p_params jsonb) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(runtime.nexloop_canonical_json(p_params),'UTF8')),'hex')
$$;
create function runtime.nexloop_machine_prepare_digest(p_tenant text,p_client text,p_world text,p_capability text,p_intent text,p_params jsonb,p_expires timestamptz)
 returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(runtime.nexloop_canonical_json(jsonb_build_array('nexloop-machine-prepare-v1',p_tenant,p_client,p_world,p_capability,p_intent,
  p_params,to_char(p_expires at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))),'UTF8')),'hex')
$$;
-- R3: the business intent id of the governed entry.
create function runtime.nexloop_machine_business_intent(p_tenant text,p_client text,p_intent text) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select 'machine:'||encode(sha256(convert_to(runtime.nexloop_canonical_json(jsonb_build_array('nexloop-machine-intent-v1',p_tenant,p_client,p_intent)),'UTF8')),'hex')
$$;

create function runtime.nexloop_machine_intent_view(i runtime.nexloop_machine_intents) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('intent_id',i.intent_id,'capability',i.capability,'phase',i.phase,'result_code',i.result_code,
  'outcome_id',i.action_outcome_id,'approval_id',i.approval_id,'expires_at',to_jsonb(i.expires_at),'committed_at',to_jsonb(i.committed_at))
$$;

create function runtime.nexloop_machine_intent_audit(i runtime.nexloop_machine_intents,p_principal text,p_event text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 -- Ids, codes and the key prefix only: never parameters or text.
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('machine-intent:'||gen_random_uuid(),'machine.intent.'||p_event,i.tenant_id,p_principal,'machine',i.intent_id,
   jsonb_build_object('client_id',i.client_id,'key_prefix',i.key_prefix,'intent_id',i.intent_id,'capability',i.capability,'world',i.world,
    'phase',i.phase,'result_code',i.result_code,'outcome_id',i.action_outcome_id,'approval_id',i.approval_id),clock_timestamp());
end $$;

-- 4. The machine core and the wrapped entry (C7, R2, R3, R5) ---------------------------------------------------------------------
-- The core runs the 0348 entry's steps with the machine identity: the signed command (R5: client, intent, prepare digest, world) is
-- verified here; the intent row decides whether this command may run at all (prepare unexpired or waiting for approval, same prepare
-- digest, the body's parameters re-digested); the machine authority assertion (M5a) replaces the service identity and authority, at the
-- start and again at the commit tail; the capability must be allowlisted (policies fact), in the key's scope (scope fact), granted
-- (grants fact) and a non-human governed capability; the claim is created here in the same transaction. The approval gate, the handler
-- and the consumption are the 0348 text unchanged (the parity test compares it).
create function authz.nexloop_machine_governed_core(p_prefix text,p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb;body jsonb;k bytea;ident jsonb;v_tenant text;v_client text;v_principal text;v_subject text:='machine';v_cap text;
 cap control.nexloop_governed_capabilities;d jsonb;i runtime.nexloop_machine_intents;v_params jsonb;v_business text;v_claim jsonb;
 stored runtime.nexloop_action_claims;v_gate jsonb;v_consumed text;v_reason text;v_result jsonb;v_id text;v_outcome jsonb;v_types text[];
 v_phase text;v_code text;
begin
 if octet_length(p_text)>262144 or octet_length(p_payload)>65536 then raise exception 'machine write: too_large' using errcode='22023';end if;
 a:=p_text::jsonb;body:=p_payload::jsonb;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-machine-governed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-machine-governed-v1' or a->>'expires_at' is null
  or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'machine write: command_rejected' using errcode='42501';end if;
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 v_tenant:=ident->'binding'->>'tenant_id';v_client:=ident->>'client_id';v_principal:=ident->'binding'->>'subject_principal_id';v_cap:=a->>'capability';
 if a->>'tenant_id' is distinct from v_tenant or a->>'client_id' is distinct from v_client or a->>'world' is distinct from p_world
  or ident->>'world' is distinct from p_world then raise exception 'machine write: command_rejected' using errcode='42501';end if;
 -- The intent: this very command, still allowed to run.
 select * into i from runtime.nexloop_machine_intents where tenant_id=v_tenant and client_id=v_client and intent_id=a->>'intent_id' for update;
 if not found then raise exception 'machine write: intent_not_found' using errcode='P0002';end if;
 if i.capability is distinct from v_cap or i.world is distinct from p_world or i.prepare_digest is distinct from a->>'prepare_digest' then
  raise exception 'machine write: digest_mismatch' using errcode='NX409';end if;
 if i.phase not in ('prepare','pending_approval') then raise exception 'machine write: intent_committed' using errcode='NX409';end if;
 if i.phase='prepare' and i.expires_at<=clock_timestamp() then raise exception 'machine write: prepare_expired' using errcode='NX410';end if;
 v_params:=body-'operation'-'request_id';
 if runtime.nexloop_machine_prepare_digest(v_tenant,v_client,p_world,v_cap,i.intent_id,v_params,i.expires_at) is distinct from i.prepare_digest then
  raise exception 'machine write: digest_mismatch' using errcode='NX409';end if;
 v_business:=runtime.nexloop_machine_business_intent(v_tenant,v_client,i.intent_id);
 select * into cap from control.nexloop_governed_capabilities where capability=v_cap;
 if body->>'request_id' is distinct from v_business or cap.capability is null or body->>'operation' is distinct from cap.operation then
  raise exception 'machine write: command_rejected' using errcode='42501';end if;
 -- Authority: identity, key scope, allowlist, the latest grant (M5a, locks included); never a human capability.
 perform authz.nexloop_machine_assert_action_authority(p_prefix,p_digest,p_world,a);
 if cap.subject_rule is distinct from 'agent_or_human' then raise exception 'human goal authority required' using errcode='42501';end if;
 v_types:=authz.nexloop_machine_grant_types(v_tenant,v_client,v_cap);
 if v_params ? 'type_name' and not (v_params->>'type_name'=any(coalesce(v_types,'{}'))) then raise exception 'machine write: not_found' using errcode='P0002';end if;
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active;
 if d is null or d->'capability_binding'->>'capability_name' is distinct from v_cap or d->'governance'->>'approval_mode'<>'none'
  or jsonb_array_length(d->'governance'->'policy_refs')<>0 or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or a->>'resource_id' is distinct from 'eios:action:'||(d->>'stable_name')||':'||(d->>'version') then
  raise exception 'goal Action contract unavailable' using errcode='42501';end if;
 -- The claim (R3: keyed by the business intent id), created or renewed here: the core is the only writer of a machine intent's claim.
 v_claim:=jsonb_build_object('state','active','key',jsonb_build_object('tenant_id',v_tenant,'action_stable_name',d->>'stable_name','idempotency_key',v_business),
  'binding',jsonb_build_object('invocation_id',v_business,'request_digest',encode(sha256(convert_to(p_payload,'UTF8')),'hex'),
   'action_reference',jsonb_build_object('stable_name',d->>'stable_name','version',d->>'version'),'adapter_id','nexloop.machine','target_system','postgres'),
  'lease_expires_at',to_jsonb(clock_timestamp()+interval '20 seconds'));
 perform set_config('eios.tenant_id',v_tenant,true);
 insert into runtime.nexloop_action_claims(tenant_id,world,action_name,intent_id,principal_id,claim) values(v_tenant,p_world,d->>'stable_name',v_business,v_principal,v_claim)
  on conflict(tenant_id,world,action_name,intent_id) do update set claim=excluded.claim where runtime.nexloop_action_claims.principal_id=excluded.principal_id;
 select * into stored from runtime.nexloop_action_claims where tenant_id=v_tenant and world=p_world and action_name=d->>'stable_name' and intent_id=v_business for update;
 if stored.principal_id is distinct from v_principal or stored.claim is distinct from v_claim then raise exception 'goal claim fenced' using errcode='40001';end if;
 -- NX-056 A2 (design v5 §2, §4): an Action under an approval policy runs only by consuming an approved Approval of this very request
 -- (same digest), requested by this person, once; otherwise the request waits for approval and the handler is not called.
 v_gate:=control.nexloop_approval_gate(v_tenant,p_world,v_cap,a->>'resource_id',v_principal,body);
 if v_gate->>'proceed'='false' then
  v_result:=v_gate-'proceed';
 elsif v_gate->'approval_id' is null then
  execute format('select %s($1,$2,$3,$4,$5,$6)',cap.handler::oid::regproc) into v_result using v_tenant,p_world,v_principal,v_subject,stored.intent_id,body;
 else
  -- The approval being executed: a Consumer erasure that starts inside this handler does not end it (0331).
  perform set_config('nexloop.approval_in_flight',v_gate->>'approval_id',true);
  begin  -- a savepoint: a refusal rolls back the handler's own writes only, and the approval is still consumed (L6 N-2)
   execute format('select %s($1,$2,$3,$4,$5,$6)',cap.handler::oid::regproc) into v_result using v_tenant,p_world,v_principal,v_subject,stored.intent_id,body;
   v_consumed:='executed';
  exception when others then
   -- A transient database error is no refusal: re-raised, nothing consumed, the requester may execute again (L6 N-2a).
   -- Also out-of-resources (53), connection (08) and administrator shutdown (57P0x) (L6 A2 review suggestion 3).
   if sqlstate in ('40001','40P01','55P03','57014') or left(sqlstate,2) in ('53','08') or sqlstate like '57P0_' then raise;end if;
   -- ADR-039 §7.4: the handler's fixed code from a '<prefix>: <code>' message, never the rest of the message or a value.
   v_reason:=substring(sqlerrm from '^[a-z][a-z _]{0,63}: ([a-z_]{1,64})$');
   v_consumed:='execution_refused:'||sqlstate||coalesce(':'||v_reason,'');
   v_result:=jsonb_build_object('status','execution_refused','code',sqlstate,'reason',v_reason);
  end;
  perform set_config('nexloop.approval_in_flight','',true);
  perform set_config('eios.tenant_id',v_tenant,true);
  perform control.nexloop_approval_consume(v_tenant,p_world,(v_gate->>'approval_id')::uuid,v_principal,v_consumed,v_gate->'approved_by');
  v_result:=v_result||jsonb_build_object('approval_id',v_gate->>'approval_id','approval','consumed');
 end if;
 v_id:=encode(sha256(convert_to(jsonb_build_array(v_tenant,p_world,body->>'operation',stored.intent_id)::text,'UTF8')),'hex');
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,
  'status',case when v_gate->>'proceed'='false' or v_consumed like 'execution_refused:%' then 'permanent_failure' else 'succeeded' end,
  'outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 -- The intent's phase from the outcome (R4): waiting for approval, refused with a fixed code, or committed.
 if v_gate->>'proceed'='false' and v_gate->>'approval'='required' then
  v_phase:='pending_approval';v_code:='approval_required';
 elsif v_gate->>'proceed'='false' then
  v_phase:='refused';v_code:='approval_'||coalesce(v_gate->>'status','unavailable');
 elsif v_consumed like 'execution_refused:%' then
  v_phase:='refused';v_code:=coalesce(v_result->>'reason',v_result->>'code','execution_refused');
 else
  v_phase:='committed';v_code:='succeeded';
 end if;
 update runtime.nexloop_machine_intents set phase=v_phase,result_code=v_code,action_outcome_id=case when v_phase='committed' then v_id end,
  approval_id=coalesce((v_gate->>'approval_id')::uuid,approval_id),committed_at=case when v_phase in ('committed','refused') then clock_timestamp() end
  where tenant_id=i.tenant_id and client_id=i.client_id and intent_id=i.intent_id returning * into i;
 perform runtime.nexloop_machine_intent_audit(i,v_principal,'commit');
 -- Commit tail: authority, contract and signing key still current.
 perform authz.nexloop_machine_assert_action_authority(p_prefix,p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found then raise exception 'goal Action changed at commit' using errcode='42501';end if;
 if (a->>'expires_at')::timestamptz<=clock_timestamp()
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k) then
  raise exception 'goal authority expired at commit' using errcode='42501';end if;
 return runtime.nexloop_machine_intent_view(i)||jsonb_build_object('replayed',false);
end $$;
alter function authz.nexloop_machine_governed_core(text,text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_machine_governed_core(text,text,text,text,text,text) from public;

alter function authz.nexloop_goal_governed_action(text,text,text,text,text) rename to nexloop_goal_governed_action_before_machine;
create function authz.nexloop_goal_governed_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 -- C7: a machine command ('nxm:<prefix>:<digest>') goes to the machine core; anything else is the unchanged 0348 entry, which still
 -- refuses a machine digest through the service identity (C1, R2's second line).
 if p_digest like 'nxm:%' then
  if p_digest!~'^nxm:[a-z2-7]{12}:[0-9a-f]{64}$' then raise exception 'machine authentication denied' using errcode='42501';end if;
  return authz.nexloop_machine_governed_core(split_part(p_digest,':',2),split_part(p_digest,':',3),p_world,p_text,p_signature,p_payload);
 end if;
 return authz.nexloop_goal_governed_action_before_machine(p_digest,p_world,p_text,p_signature,p_payload);
end $$;

-- 5. prepare, commit and the intent query (granted to nexloop_api) ------------------------------------------------------------------
create function authz.nexloop_machine_prepare(p_prefix text,p_digest text,p_capability text,p_intent text,p_params jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_client text;v_world text;w control.nexloop_machine_write_capabilities;v_types text[];v_params jsonb;
 v_pd text;v_exp timestamptz;i runtime.nexloop_machine_intents;cap control.nexloop_machine_capabilities;
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 v_tenant:=ident->'binding'->>'tenant_id';v_client:=ident->>'client_id';v_world:=ident->>'world';
 if coalesce(p_intent,'')!~'^[A-Za-z0-9._:-]{8,128}$' or coalesce(p_capability,'')!~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'
  or jsonb_typeof(p_params) is distinct from 'object' then raise exception 'machine write: invalid' using errcode='22023';end if;
 -- 2.2-2.4: the key's scope, the allowlist (a current write), the client's grant.
 if not (p_capability=any(array(select jsonb_array_elements_text(ident->'scope')))) then raise exception 'machine write: not_permitted' using errcode='42501';end if;
 select * into cap from control.nexloop_machine_capabilities where capability=p_capability order by revision desc limit 1;
 select * into w from control.nexloop_machine_write_capabilities where capability=p_capability;
 v_types:=authz.nexloop_machine_grant_types(v_tenant,v_client,p_capability);
 if cap.capability is null or cap.state<>'active' or cap.kind<>'write' or w.capability is null or v_types is null
  or control.nexloop_machine_capability_forbidden(p_capability) then raise exception 'machine write: not_permitted' using errcode='42501';end if;
 -- 2.5: an effect capability needs an approval policy.
 if cap.effect and (control.nexloop_approval_policy(p_capability)).capability is null then
  raise exception 'machine write: approval_policy_required' using errcode='42501';end if;
 -- The parameters: only the registered keys; a type the client holds; a visible object.
 if exists(select 1 from jsonb_object_keys(p_params) x where not (x=any(w.param_keys))) then raise exception 'machine write: invalid' using errcode='22023';end if;
 if p_params ? 'type_name' and (jsonb_typeof(p_params->'type_name')<>'string' or not (p_params->>'type_name'=any(v_types))
   or not (p_params->>'type_name'=any(w.object_types))) then raise exception 'machine write: not_found' using errcode='P0002';end if;
 if p_params ? 'object_id' and (not (p_params ? 'type_name') or coalesce(p_params->>'object_id','')!~'^[a-f0-9]{64}$'
   or not authz.nexloop_machine_object_visible(v_tenant,v_world,p_params->>'type_name',p_params->>'object_id')) then
  raise exception 'machine write: not_found' using errcode='P0002';end if;
 v_params:=runtime.nexloop_canonical_json(p_params)::jsonb;
 perform 1 from authz.nexloop_machine_clients c where c.tenant_id=v_tenant and c.client_id=v_client for update;  -- one prepare at a time
 select * into i from runtime.nexloop_machine_intents where tenant_id=v_tenant and client_id=v_client and intent_id=p_intent for update;
 if found then
  if i.phase<>'prepare' then raise exception 'machine write: intent_committed' using errcode='NX409';end if;
  if i.expires_at>clock_timestamp() then
   if i.capability=p_capability and i.world=v_world and i.params_digest=runtime.nexloop_machine_params_digest(v_params) then
    return jsonb_build_object('intent_id',i.intent_id,'prepare_digest',i.prepare_digest,'expires_at',to_jsonb(i.expires_at),'normalized',v_params,'replayed',true);
   end if;
   raise exception 'machine write: intent_conflict' using errcode='NX409';
  end if;
 end if;
 v_exp:=date_trunc('milliseconds',clock_timestamp())+interval '10 minutes';
 v_pd:=runtime.nexloop_machine_prepare_digest(v_tenant,v_client,v_world,p_capability,p_intent,v_params,v_exp);
 insert into runtime.nexloop_machine_intents(tenant_id,client_id,intent_id,capability,world,params_digest,prepare_digest,expires_at,phase,key_prefix)
  values(v_tenant,v_client,p_intent,p_capability,v_world,runtime.nexloop_machine_params_digest(v_params),v_pd,v_exp,'prepare',ident->>'key_prefix')
  on conflict(tenant_id,client_id,intent_id) do update set capability=excluded.capability,world=excluded.world,params_digest=excluded.params_digest,
   prepare_digest=excluded.prepare_digest,expires_at=excluded.expires_at,key_prefix=excluded.key_prefix
  returning * into i;
 perform runtime.nexloop_machine_intent_audit(i,ident->'binding'->>'subject_principal_id','prepare');
 return jsonb_build_object('intent_id',i.intent_id,'prepare_digest',i.prepare_digest,'expires_at',to_jsonb(i.expires_at),'normalized',v_params,'replayed',false);
end $$;

-- The command the API signs needs the Action resource of the capability (its latest active definition in the key's world).
create function authz.nexloop_machine_write_action(p_prefix text,p_digest text,p_capability text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v text;
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select d.resource_id into v from control.nexloop_action_definitions d where d.tenant_id=ident->'binding'->>'tenant_id' and d.world=ident->>'world' and d.active
  and d.definition->'capability_binding'->>'capability_name'=p_capability order by (d.definition->>'version')::integer desc limit 1;
 if v is null then raise exception 'machine write: not_permitted' using errcode='42501';end if;
 return v;
end $$;

create function authz.nexloop_machine_commit(p_prefix text,p_digest text,p_capability text,p_intent text,p_params jsonb,p_prepare_digest text,
 p_text text,p_signature text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_client text;i runtime.nexloop_machine_intents;cap control.nexloop_governed_capabilities;v_payload text;v jsonb;
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 v_tenant:=ident->'binding'->>'tenant_id';v_client:=ident->>'client_id';
 if coalesce(p_intent,'')!~'^[A-Za-z0-9._:-]{8,128}$' or coalesce(p_prepare_digest,'')!~'^[0-9a-f]{64}$' or jsonb_typeof(p_params) is distinct from 'object' then
  raise exception 'machine write: invalid' using errcode='22023';end if;
 select * into i from runtime.nexloop_machine_intents where tenant_id=v_tenant and client_id=v_client and intent_id=p_intent for update;
 if not found then raise exception 'machine write: intent_not_found' using errcode='P0002';end if;
 -- R4: a finished intent answers first (its first result for the same digest), before any expiry.
 if i.phase in ('committed','refused') then
  if i.prepare_digest=p_prepare_digest and i.capability=p_capability
   and i.params_digest=runtime.nexloop_machine_params_digest(runtime.nexloop_canonical_json(p_params)::jsonb) then
   return runtime.nexloop_machine_intent_view(i)||jsonb_build_object('replayed',true);end if;
  raise exception 'machine write: digest_mismatch' using errcode='NX409';
 end if;
 if i.capability is distinct from p_capability or i.prepare_digest is distinct from p_prepare_digest then
  raise exception 'machine write: digest_mismatch' using errcode='NX409';end if;
 if i.phase='prepare' and i.expires_at<=clock_timestamp() then raise exception 'machine write: prepare_expired' using errcode='NX410';end if;
 select * into cap from control.nexloop_governed_capabilities where capability=p_capability;
 if cap.capability is null then raise exception 'machine write: not_permitted' using errcode='42501';end if;
 v_payload:=runtime.nexloop_canonical_json(p_params||jsonb_build_object('operation',cap.operation,
  'request_id',runtime.nexloop_machine_business_intent(v_tenant,v_client,p_intent)));
 begin
  v:=authz.nexloop_goal_governed_action('nxm:'||p_prefix||':'||p_digest,ident->>'world',p_text,p_signature,v_payload);
 exception when insufficient_privilege then
  -- L6 Y1: only a refusal by authority now ends the intent - the machine authority assertion denying (revoked, out of scope, retired,
  -- inactive) or a human capability. An expired or rotated signature, a changed Action definition or facts read before a change are
  -- no decision: re-raised, the intent keeps its phase and the same intent may be committed again with a new command.
  if sqlerrm not in ('machine action authority denied','human goal authority required') then raise;end if;
  perform set_config('eios.tenant_id',v_tenant,true);
  update runtime.nexloop_machine_intents set phase='refused',result_code='not_permitted',committed_at=clock_timestamp()
   where tenant_id=i.tenant_id and client_id=i.client_id and intent_id=i.intent_id returning * into i;
  perform runtime.nexloop_machine_intent_audit(i,ident->'binding'->>'subject_principal_id','refused');
  return runtime.nexloop_machine_intent_view(i)||jsonb_build_object('replayed',false);
 end;
 return v;
end $$;

create function authz.nexloop_machine_intent(p_prefix text,p_digest text,p_intent text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;i runtime.nexloop_machine_intents;v_status text;
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 select * into i from runtime.nexloop_machine_intents where tenant_id=ident->'binding'->>'tenant_id' and client_id=ident->>'client_id' and intent_id=p_intent;
 if not found then raise exception 'machine write: intent_not_found' using errcode='P0002';end if;
 if i.approval_id is not null then
  perform set_config('eios.tenant_id',i.tenant_id,true);
  select q.status into v_status from control.nexloop_approval_requests q where q.tenant_id=i.tenant_id and q.world=i.world and q.approval_id=i.approval_id;
 end if;
 return runtime.nexloop_machine_intent_view(i)||jsonb_build_object('approval_status',v_status);
end $$;

do $grants$
declare f text;pair text[];h text;
begin
 foreach f in array array['control.nexloop_machine_verb_types(text)','control.nexloop_approval_subject_of(text,text)',
  'runtime.nexloop_machine_params_digest(jsonb)','runtime.nexloop_machine_prepare_digest(text,text,text,text,text,jsonb,timestamptz)',
  'runtime.nexloop_machine_business_intent(text,text,text)','runtime.nexloop_machine_intent_view(runtime.nexloop_machine_intents)',
  'runtime.nexloop_machine_intent_audit(runtime.nexloop_machine_intents,text,text)','authz.nexloop_goal_governed_action(text,text,text,text,text)',
  'authz.nexloop_machine_prepare(text,text,text,text,jsonb)','authz.nexloop_machine_write_action(text,text,text)',
  'authz.nexloop_machine_commit(text,text,text,text,jsonb,text,text,text)','authz.nexloop_machine_intent(text,text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
 -- The wrappers take over the grantees of what they wrap; the renamed functions are granted to no role.
 foreach pair slice 1 in array array[
  array['control.nexloop_machine_verb_types(text)','control.nexloop_machine_verb_types_before_writes(text)'],
  array['control.nexloop_approval_subject_of(text,text)','control.nexloop_approval_subject_of_before_machine(text,text)'],
  array['authz.nexloop_goal_governed_action(text,text,text,text,text)','authz.nexloop_goal_governed_action_before_machine(text,text,text,text,text)']] loop
  for h in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
    where p.oid=pair[2]::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner loop
   execute format('grant execute on function %s to %I',pair[1],h);
   execute format('revoke all on function %s from %I',pair[2],h);
  end loop;
  execute format('revoke all on function %s from public',pair[2]);
 end loop;
end $grants$;
grant execute on function authz.nexloop_machine_prepare(text,text,text,text,jsonb),authz.nexloop_machine_write_action(text,text,text),
 authz.nexloop_machine_commit(text,text,text,text,jsonb,text,text,text),authz.nexloop_machine_intent(text,text,text) to nexloop_api;
