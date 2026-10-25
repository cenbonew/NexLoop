-- NX-056 B2 (ADR-039 draft revisions R2 §7 and R3 §8; with NX-058 slices D2/D3/E, checked with L2): the capability approval settings
-- (execution-time fields, accepted target statuses), the open-time checks, the termination reason requester_no_longer_eligible and the
-- execution refusal codes. Temporary number; the dispatcher renumbers. It
-- follows 0344 (NX-058 D2): control.nexloop_approval_open is re-created from 0344's version, keeping its operation_mismatch and its
-- suspended targets. Published migrations are not edited; every function below is re-created with only these changes
-- (tests/test_function_copy_guard.py).
-- 1. control.nexloop_approval_capability_settings (R3 §8.1-8.3): per capability, its execution-time fields (*_hmac only, a 64-hex value;
--    never in the digest - control.nexloop_approval_digest, now stable: it reads this table - nor in the frozen body) and the member
--    statuses a target may have (default {active}). Append-only, owner only, registered by the capability's own migration before any
--    request of it exists. The five NX-058 D2 capabilities are registered here (R3 §8.1 table), since this table comes with B2.
-- 2. control.nexloop_approval_policy_guard: an execution-time field is never a summary, target or condition field.
-- 3. control.nexloop_approval_open (§7.2, R3 §8.1-8.2): the target's status within the capability's target_statuses (no fixed list),
--    execution-time field values checked (execution_field_invalid), quorum_unreachable for target and not_applicable policies (others
--    eligible with the vote cooldown over; eligible_from in the detail), frozen body without execution-time fields.
-- 4. control.nexloop_approval_terminate + control.nexloop_approval_end_for_requester (§7.3, R3 §8.5): requester_no_longer_eligible ends
--    the waiting and approved-unconsumed requests of one requester principal that lost its standing (the one in flight excepted; another
--    principal of the same subject is untouched). Owner functions call it (NX-058 handlers, the configurator's member apply).
-- 5. authz.nexloop_goal_governed_action (§7.4): execution_refused:<sqlstate>:<code>, the code from the handler's '<prefix>: <code>'.
-- 6. (L6 review of B2) C-B1: requester_no_longer_eligible wired by triggers on the role history (a role's stop) and on memberships
--    (leaving active), so the D2 handlers and the configurator's member apply end the requests without calling it themselves.
--    I-B1/I-B2: the settings guard checks published policies and keeps revoked alone. I-B3: control.nexloop_approval_gate (0327)
--    checks the execution-time HMACs before the handler.

create table control.nexloop_approval_capability_settings (
 capability text primary key,execution_fields text[] not null default '{}' check(cardinality(execution_fields)<=8),
 target_statuses text[] not null default '{active}' check(cardinality(target_statuses) between 1 and 3 and target_statuses<@array['active','suspended','revoked']),
 recorded_at timestamptz not null default clock_timestamp()
);
alter table control.nexloop_approval_capability_settings owner to nexloop_owner;
revoke all on control.nexloop_approval_capability_settings from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create function control.nexloop_approval_capability_settings_guard() returns trigger language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;prior text:=current_setting('eios.tenant_id',true);
begin
 -- R3 §8.2: only HMAC fields, never repeated.
 if exists(select 1 from unnest(new.execution_fields) f where f!~'^[a-z][a-z0-9_]{0,59}_hmac$')
  or cardinality(array(select distinct unnest(new.execution_fields)))<>cardinality(new.execution_fields)
  or cardinality(array(select distinct unnest(new.target_statuses)))<>cardinality(new.target_statuses)
  -- L6 review of B2 (I-B2): revoked stands alone (only an erasure acts on a revoked member; nothing else may)
  or ('revoked'=any(new.target_statuses) and cardinality(new.target_statuses)>1) then
  raise exception 'approval capability settings: invalid' using errcode='22023';end if;
 -- I-B1: a policy already published for the capability never uses one of these fields as a summary, target or condition field.
 if exists(select 1 from control.nexloop_approval_policies p where p.capability=new.capability
   and new.execution_fields&&(p.summary_fields||array[p.target_subject_field,p.condition->>'field'])) then
  raise exception 'approval capability settings: a published policy uses an execution-time field' using errcode='22023';end if;
 if not exists(select 1 from control.nexloop_governed_capabilities c where c.capability=new.capability and c.subject_rule='human') then
  raise exception 'approval capability settings: a human governed capability required' using errcode='22023';end if;
 -- R3 §8.3: registered before any request of the capability exists (those were frozen and digested without these settings).
 -- The requests are tenant-bound (forced RLS): every tenant is looked at under its own setting.
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  if exists(select 1 from control.nexloop_approval_requests r where r.tenant_id=t and r.capability=new.capability) then
   raise exception 'approval capability settings: the capability already has approval requests' using errcode='22023';end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function control.nexloop_approval_capability_settings_guard() owner to nexloop_owner;
revoke all on function control.nexloop_approval_capability_settings_guard() from public;
create trigger approval_capability_settings_guard before insert on control.nexloop_approval_capability_settings
 for each row execute function control.nexloop_approval_capability_settings_guard();
create trigger approval_capability_settings_append_only before update or delete or truncate on control.nexloop_approval_capability_settings
 for each statement execute function control.nexloop_approval_append_only();

create function control.nexloop_approval_execution_fields_of(p_capability text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select coalesce((select execution_fields from control.nexloop_approval_capability_settings where capability=p_capability),'{}'::text[])
$$;
alter function control.nexloop_approval_execution_fields_of(text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_execution_fields_of(text) from public;
create function control.nexloop_approval_target_statuses_of(p_capability text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select coalesce((select target_statuses from control.nexloop_approval_capability_settings where capability=p_capability),'{active}'::text[])
$$;
alter function control.nexloop_approval_target_statuses_of(text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_target_statuses_of(text) from public;

-- R3 §8.1: the NX-058 D2 capabilities (0344) and the target statuses their handlers act on; registered only where 0344 registered them.
insert into control.nexloop_approval_capability_settings(capability,target_statuses)
 select v.capability,v.statuses from (values ('workbench_member.promote_owner','{active}'::text[]),('workbench_member.demote_owner','{active,suspended}'::text[]),
  ('workbench_member.suspend_owner','{active}'::text[]),('workbench_member.resume_owner','{suspended}'::text[]),
  ('workbench_member.revoke_owner','{active,suspended}'::text[])) v(capability,statuses)
 where exists(select 1 from control.nexloop_governed_capabilities c where c.capability=v.capability);

create or replace function control.nexloop_approval_digest(p_tenant text,p_world text,p_action text,p_capability text,p_policy_version integer,p_body jsonb) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(jsonb_build_object('digest_version','nexloop-approval-digest/1','tenant_id',p_tenant,'world',p_world,'action',p_action,
  'policy',p_capability||'@'||p_policy_version,'body',p_body-'request_id'-control.nexloop_approval_execution_fields_of(p_capability))::text,'UTF8')),'hex')
$$;
alter function control.nexloop_approval_digest(text,text,text,text,integer,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_approval_digest(text,text,text,text,integer,jsonb) from public;

create or replace function control.nexloop_approval_policy_guard() returns trigger language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 -- ADR-039 §6.7 (R1, B1): a policy naming the member a request acts on is approved by owners only.
 -- ADR-039 §7.1: an execution-time field is never shown, a target or a condition.
 if control.nexloop_approval_execution_fields_of(new.capability)&&(new.summary_fields||array[new.target_subject_field,new.condition->>'field']) then
  raise exception 'approval policy: an execution-time field cannot be a summary, target or condition field' using errcode='22023';end if;
 if new.target_subject_field is not null and new.approver_roles is distinct from array['owner'] then
  raise exception 'approval policy: a target policy is approved by owners only' using errcode='22023';end if;
 if cardinality(array(select distinct unnest(new.approver_roles)))<>cardinality(new.approver_roles) then
  raise exception 'approval policy: approver roles repeat' using errcode='22023';end if;
 if not (new.summary_fields<@control.nexloop_approval_summary_allowed(new.capability)) then
  raise exception 'approval policy: summary fields outside the capability''s list' using errcode='22023';end if;
 if new.capability like 'approvals.%' or not exists(select 1 from control.nexloop_governed_capabilities c where c.capability=new.capability and c.subject_rule='human') then
  raise exception 'approval policy: a human governed capability required' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_approval_policies p where p.capability=new.capability and p.version>=new.version) then
  raise exception 'approval policy: versions only grow' using errcode='22023';end if;
 if exists(select 1 from unnest(new.summary_fields) f where f!~'^[a-z][a-z0-9_]{0,63}$') then raise exception 'approval policy: summary fields' using errcode='22023';end if;
 -- The workbench shows summary fields to every approver: never a Consumer identifier or free text (L6 review 7).
 if exists(select 1 from unnest(new.summary_fields) f where f~'(^|_)(consumer|principal|subject|customer|identity)(_|$)'
   or f~'(^|_)(reason|basis|text|body|message|note|notes|description|quote|comment|name|email|phone)(_|$)') then
  raise exception 'approval policy: summary fields may not name a consumer or free text' using errcode='22023';end if;
 return new;
end $$;

create or replace function control.nexloop_approval_open(p_tenant text,p_world text,p_capability text,p_action text,p_principal text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pol control.nexloop_approval_policies;v_subject text;v_digest text;r control.nexloop_approval_requests;v_target text;v_from timestamptz[];
begin
 pol:=control.nexloop_approval_policy(p_capability);
 -- An Action version without its policy version is refused (fail closed, design §3).
 if pol.capability is null or p_action!~'^eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}$' or split_part(p_action,':',4)::integer<>pol.action_version then
  raise exception 'approval: policy_unavailable' using errcode='22023';end if;
 -- NX-058 slice D (L6 review of B1): the body's operation is the capability's registered one, or nothing opens.
 if p_body->>'operation' is distinct from (select c.operation from control.nexloop_governed_capabilities c where c.capability=p_capability) then
  raise exception 'approval: operation_mismatch' using errcode='22023';end if;
 v_subject:=control.nexloop_approval_subject_of(p_tenant,p_principal);
 if v_subject is null then raise exception 'approval: requester_unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 v_digest:=control.nexloop_approval_digest(p_tenant,p_world,p_action,p_capability,pol.version,p_body);
 -- NX-058 Z1, fail closed (L6 review 8): a policy naming the member the request acts on needs that member to exist in the tenant.
 if pol.target_subject_field is not null and not exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=p_tenant
   and (m.principal_id=p_body->>pol.target_subject_field or m.subject_id=p_body->>pol.target_subject_field)
   -- only an invited, active or suspended member (L6 review 9; NX-058 slice D2: resume_owner and demoting a suspended owner act on a
   -- suspended member); never a revoked one
   -- R3 §8.1: the statuses this capability declares (default active only); no fixed list
   and m.payload->>'status'=any(control.nexloop_approval_target_statuses_of(p_capability))) then
  raise exception 'approval: target_unresolved' using errcode='22023';end if;
 -- The same request id replays its own approval; the same digest while this person's one waits answers that one (no flooding, design
 -- §4 step 1). Dedupe and cooldown are per requester (subject): another person's request or rejection never answers or blocks this one.
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and capability=p_capability and request_id=p_body->>'request_id';
 if found then
  if r.request_digest is distinct from v_digest then raise exception 'approval: request_reused' using errcode='22023';end if;
  return jsonb_build_object('approval_id',r.approval_id,'status',r.status,'due_by',r.due_by,'replayed',true);
 end if;
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and request_digest=v_digest and requester_subject=v_subject
  and status='waiting_approval';
 if found then return jsonb_build_object('approval_id',r.approval_id,'status',r.status,'due_by',r.due_by,'replayed',true);end if;
 -- Limits (design §8, L6 S3): at most 3 waiting requests of one requester for one capability; the same digest waits 1 hour after a
 -- rejection or a withdrawal (no shopping for an approver).
 if (select count(*) from control.nexloop_approval_requests q where q.tenant_id=p_tenant and q.world=p_world and q.capability=p_capability
     and q.requester_subject=v_subject and q.status='waiting_approval')>=3 then
  raise exception 'approval: too_many_waiting' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_approval_requests q join control.nexloop_approval_decisions d on d.tenant_id=q.tenant_id and d.world=q.world
     and d.approval_id=q.approval_id and d.outcome in ('rejected','cancelled')
   where q.tenant_id=p_tenant and q.world=p_world and q.request_digest=v_digest and q.requester_subject=v_subject and d.decided_at>clock_timestamp()-interval '1 hour') then
  raise exception 'approval: cooldown' using errcode='22023';end if;
 -- R3 §8.2: an execution-time field, when present, is a 64-hex HMAC (it may be absent: the waiting submission need not carry it).
 if exists(select 1 from unnest(control.nexloop_approval_execution_fields_of(p_capability)) f
   where p_body ? f and (jsonb_typeof(p_body->f) is distinct from 'string' or p_body->>f!~'^[0-9a-f]{64}$')) then
  raise exception 'approval: execution_field_invalid' using errcode='22023';end if;
 -- Single-person mode on (A7, design §15.4): a policy only others can satisfy needs that many others eligible now; never lowered.
 if control.nexloop_approval_solo_enabled(p_tenant,p_world)
   -- only what self-approval can never satisfy (quorum above 1, or a policy not applicable to the mode); a quorum-1 request waits
   -- (for another approver, or until the mode takes effect)
   and (pol.quorum>1 or pol.solo_mode<>'allowed')
   and cardinality(array(select s from unnest(control.nexloop_approval_eligible_subjects(p_tenant,pol.approver_roles)) s where s<>v_subject))<pol.quorum then
  raise exception 'approval: quorum_unreachable_in_solo_mode' using errcode='22023';end if;
 -- ADR-039 §7.2 (D8): a target policy, or one the single-person mode does not apply to, opens only while enough others - not the
 -- requester, not the target - are eligible with their vote cooldown over (NX-058 slice D1); otherwise quorum_unreachable now, with
 -- eligible_from (the quorum-th earliest vote_eligible_from of those others) when only cooldowns fall short.
 if pol.target_subject_field is not null or pol.solo_mode='not_applicable' then
  v_target:=control.nexloop_approval_target_subject(p_tenant,p_body,pol.target_subject_field);
  v_from:=array(select e from (select control.nexloop_workbench_vote_eligible_from(p_tenant,p_world,s,pol.approver_roles) e
    from unnest(control.nexloop_approval_eligible_subjects(p_tenant,pol.approver_roles)) s where s<>v_subject and s is distinct from v_target) x
    where e is not null order by e);
  perform set_config('eios.tenant_id',p_tenant,true);
  if (select count(*) from unnest(v_from) e where e<=clock_timestamp())<pol.quorum then
   if cardinality(v_from)>=pol.quorum then
    raise exception 'approval: quorum_unreachable' using errcode='22023',detail=jsonb_build_object('eligible_from',v_from[pol.quorum])::text;
   end if;
   raise exception 'approval: quorum_unreachable' using errcode='22023';
  end if;
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_approval_requests(tenant_id,world,capability,action_resource,policy_version,request_digest,request_id,body,requester_subject,requester_principal,due_by)
  values(p_tenant,p_world,p_capability,p_action,pol.version,v_digest,p_body->>'request_id',
   p_body-control.nexloop_approval_execution_fields_of(p_capability),  -- ADR-039 §7.1: never an execution-time field in the frozen body
   v_subject,p_principal,clock_timestamp()+make_interval(secs=>pol.expires_after_seconds))
  returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-requested:'||r.approval_id,'approval.requested',p_tenant,p_principal,'approval',r.request_id,
   jsonb_build_object('approval_id',r.approval_id,'capability',p_capability,'policy_version',pol.version,'request_digest',v_digest,'due_by',r.due_by),clock_timestamp());
 return jsonb_build_object('approval_id',r.approval_id,'status',r.status,'due_by',r.due_by,'replayed',false);
end $$;

create or replace function control.nexloop_approval_terminate(p_tenant text,p_world text,p_approval uuid,p_reason text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_approval_requests;
begin
 if p_reason not in ('due_by_passed','policy_changed','superseded','consumer_erased','requester_no_longer_eligible') then raise exception 'approval: termination reason' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and approval_id=p_approval for update;
 if not found or r.status not in ('waiting_approval','approved') or (p_reason='due_by_passed' and r.due_by>clock_timestamp())
  or exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=p_tenant and c.world=p_world and c.approval_id=p_approval) then
  return false;end if;
 update control.nexloop_approval_requests set status='expired',revision=revision+1 where tenant_id=p_tenant and world=p_world and approval_id=p_approval;
 insert into control.nexloop_approval_decisions(tenant_id,world,approval_id,outcome,actor_subject,actor_principal,reason)
  values(p_tenant,p_world,p_approval,'expired','system:expiry','system:expiry',p_reason);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-expired:'||p_approval,'approval.expired',p_tenant,'system:expiry','approval',r.request_id,
   jsonb_build_object('approval_id',p_approval,'capability',r.capability,'from',r.status,'reason',p_reason),clock_timestamp()) on conflict do nothing;
 return true;
end $$;

create function control.nexloop_approval_end_for_requester(p_tenant text,p_world text,p_requester_principal text,p_actor text,p_intent text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a uuid;n integer:=0;v_in_flight text:=coalesce(current_setting('nexloop.approval_in_flight',true),'');
begin
 if p_tenant is null or p_requester_principal is null then return 0;end if;
 perform control.nexloop_approval_tenant_lock(p_tenant);  -- lock order (§16): the tenant lock, then the request rows
 perform set_config('eios.tenant_id',p_tenant,true);
 for a in select r.approval_id from control.nexloop_approval_requests r where r.tenant_id=p_tenant and r.world=p_world
   -- R3 §8.5: by the requester principal - another principal of the same person keeps its requests
   and r.requester_principal=p_requester_principal and r.status in ('waiting_approval','approved') and r.approval_id::text<>v_in_flight
   and not exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=r.tenant_id and c.world=r.world and c.approval_id=r.approval_id)
   order by r.approval_id loop
  if control.nexloop_approval_terminate(p_tenant,p_world,a,'requester_no_longer_eligible') then n:=n+1;end if;
  perform set_config('eios.tenant_id',p_tenant,true);
 end loop;
 if n>0 then  -- who and what ended them (the member change), besides each request's approval.expired
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('approval-requester-ended:'||gen_random_uuid(),'approval.requester_ended',p_tenant,p_actor,'approval',p_intent,
    jsonb_build_object('requester_principal',p_requester_principal,'ended',n,'reason','requester_no_longer_eligible'),clock_timestamp());
 end if;
 return n;
end $$;
alter function control.nexloop_approval_end_for_requester(text,text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_end_for_requester(text,text,text,text,text) from public;

create or replace function authz.nexloop_goal_governed_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;
 ident jsonb;v_subject text;v_cap text;v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';v_id text;v_outcome jsonb;v_result jsonb;
 cap control.nexloop_governed_capabilities;
 v_gate jsonb;v_consumed text;v_reason text;  -- NX-056 A2, B2
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'goal command too large' using errcode='22023';end if;
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null then
  raise exception 'goal command expiry required' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-goal-governed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-goal-governed-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'goal permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active;
 v_cap:=d->'capability_binding'->>'capability_name';
 select * into cap from control.nexloop_governed_capabilities where capability=v_cap;
 if d is null or cap.capability is null or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or body->>'operation' is distinct from cap.operation
 then raise exception 'goal Action contract unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_subject:=ident->'binding'->>'subject_kind';
 if ident->'binding'->>'tenant_id' is distinct from v_tenant or ident->'binding'->>'subject_principal_id' is distinct from v_principal then
  raise exception 'goal identity binding mismatch' using errcode='42501';end if;
 if cap.subject_rule='agent_or_human' then
  if v_subject not in ('agent','human') then raise exception 'goal proposal requires an agent or human author' using errcode='42501';end if;
 elsif v_subject is distinct from 'human' then
  -- AT-005: only a human with a current EXECUTE grant changes owner goals, controls, budgets, restrictions or commitments.
  raise exception 'human goal authority required' using errcode='42501';
 end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=v_tenant and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from v_principal or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() or body->>'request_id' is distinct from stored.intent_id then
  raise exception 'goal claim fenced' using errcode='40001';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
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
  -- Not executed (waiting, expired, consumed, policy changed) or refused is no success (L6 A2 review important 4).
  'status',case when v_gate->>'proceed'='false' or v_consumed like 'execution_refused:%' then 'permanent_failure' else 'succeeded' end,'outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 -- Commit tail: writes stay provisional until authority, contract and fences are still current.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or d is distinct from a->'definition' then raise exception 'goal Action changed at commit' using errcode='42501';end if;
 if (a->>'expires_at')::timestamptz<=clock_timestamp() or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k) then
  raise exception 'goal authority expired at commit' using errcode='42501';end if;
 return v_result||jsonb_build_object('outcome_id',v_id,'operation',body->>'operation');
end $$;

-- C-B1 (L6 review of B2): wired where standing is lost, whatever writes it - a role's stop in the role history (NX-058 slice C: the D2
-- handlers' demotions and revocations, the configurator's member apply) and a membership leaving active (suspension, revocation). The
-- request in flight (the approved member Action itself) is skipped by end_for_requester.
create function control.nexloop_approval_requester_standing() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 if tg_table_name='nexloop_workbench_role_history' then
  if new.event='stop' then
   perform control.nexloop_approval_end_for_requester(new.tenant_id,new.world,new.principal_id,'system:member_change','role-history:'||new.entry_id);
  end if;
 elsif old.payload->>'status'='active' and new.payload->>'status' is distinct from 'active' then
  -- world 'real' (S-C4): memberships are the tenant's, without a world, and the workbench, its members and every approval of a
  -- member Action live in world 'real' (the role history is 'real' only too, 0335); other worlds have no member approvals to end.
  perform control.nexloop_approval_end_for_requester(new.tenant_id,'real',new.principal_id,'system:member_change','membership:'||(new.payload->>'status'));
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function control.nexloop_approval_requester_standing() owner to nexloop_owner;
revoke all on function control.nexloop_approval_requester_standing() from public;
create trigger nx056_approval_requester_standing after insert on control.nexloop_workbench_role_history
 for each row execute function control.nexloop_approval_requester_standing();
create trigger nx056_approval_requester_standing after update of payload on control.nexloop_browser_memberships
 for each row execute function control.nexloop_approval_requester_standing();

create or replace function control.nexloop_approval_gate(p_tenant text,p_world text,p_capability text,p_action text,p_principal text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pol control.nexloop_approval_policies;v_subject text;v_digest text;r control.nexloop_approval_requests;v_valid integer;v_by jsonb;v_open jsonb;
 v_result text;
begin
 pol:=control.nexloop_approval_policy(p_capability);
 -- Only a body that carries the condition's field with another value proceeds without approval; a missing field needs approval
 -- (fail closed, L6 A2 review suggestion 3).
 if pol.capability is null or (pol.condition is not null and p_body ? (pol.condition->>'field')
   and p_body->(pol.condition->>'field') is distinct from pol.condition->'equals') then
  return jsonb_build_object('proceed',true);end if;
 v_subject:=control.nexloop_approval_subject_of(p_tenant,p_principal);
 if v_subject is null then raise exception 'approval: requester_unavailable' using errcode='42501';end if;
 v_digest:=control.nexloop_approval_digest(p_tenant,p_world,p_action,p_capability,pol.version,p_body);
 -- Lock order (design §16, N-1): the tenant lock first (member changes take it too), then the approval row (A7, 0332).
 perform control.nexloop_approval_tenant_lock(p_tenant);
 perform set_config('eios.tenant_id',p_tenant,true);
 -- An approved, unconsumed Approval of this very request by this person: locked, so a second concurrent execution waits and then sees
 -- the consumption (L6 N-1; the consumption's primary key is the second guard).
 select * into r from control.nexloop_approval_requests q where q.tenant_id=p_tenant and q.world=p_world and q.capability=p_capability
  and q.request_digest=v_digest and q.requester_subject=v_subject and q.status='approved'
  and not exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=q.tenant_id and c.world=q.world and c.approval_id=q.approval_id)
  order by q.requested_at limit 1 for update;
 -- Rechecked in a new statement (a new snapshot): a concurrent execution that held the lock may have consumed it meanwhile; then this
 -- call answers that consumption below.
 if found and exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=r.tenant_id and c.world=r.world and c.approval_id=r.approval_id) then
  r:=null;
 end if;
 -- Past its due_by it ends expired here (and the request opens anew below), so it never blocks this body (L6 A2 review important 1).
 if found and r.due_by<=clock_timestamp() then
  perform control.nexloop_approval_terminate(p_tenant,p_world,r.approval_id,'due_by_passed');
  perform set_config('eios.tenant_id',p_tenant,true);
  r:=null;
 end if;
 if r.approval_id is null then
  -- Executed already: the same body by the same person within an hour of its consumption answers that consumption (double click, the
  -- loser of a concurrent execution), never a new request (design §4, L6 A2 review important 3). Later it is a new request.
  select q.approval_id,c.result into r.approval_id,v_result from control.nexloop_approval_requests q join control.nexloop_approval_consumptions c
    on c.tenant_id=q.tenant_id and c.world=q.world and c.approval_id=q.approval_id
   where q.tenant_id=p_tenant and q.world=p_world and q.capability=p_capability and q.request_digest=v_digest and q.requester_subject=v_subject
    and c.consumed_at>clock_timestamp()-interval '1 hour' order by c.consumed_at desc limit 1;
  if r.approval_id is not null then
   return jsonb_build_object('proceed',false,'approval_id',r.approval_id,'status','approval_consumed','result',v_result);end if;
  -- Approved or still waiting under an older policy version: the same body under that version's digest. It ends (policy_changed) and
  -- the person submits again under the current policy (design :97, L6 A2 review important 2; a waiting one too, review 6).
  select * into r from control.nexloop_approval_requests q where q.tenant_id=p_tenant and q.world=p_world and q.capability=p_capability
   and q.requester_subject=v_subject and q.status in ('waiting_approval','approved') and q.policy_version<>pol.version
   and q.request_digest=control.nexloop_approval_digest(p_tenant,p_world,p_action,p_capability,q.policy_version,p_body)
   and not exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=q.tenant_id and c.world=q.world and c.approval_id=q.approval_id)
   order by q.requested_at limit 1 for update;
  if found then
   perform control.nexloop_approval_terminate(p_tenant,p_world,r.approval_id,'policy_changed');
   return jsonb_build_object('proceed',false,'approval_id',r.approval_id,'status','policy_changed','policy_version',pol.version);
  end if;
  v_open:=control.nexloop_approval_open(p_tenant,p_world,p_capability,p_action,p_principal,p_body);
  return v_open||jsonb_build_object('proceed',false,'approval','required');
 end if;
 -- Every approver's standing now (L6 C3): an active human member in an approver role, never the requester. A7 adds the single-person
 -- mode's self-approval here.
 v_by:=control.nexloop_approval_valid_votes(p_tenant,p_world,r.approval_id);v_valid:=jsonb_array_length(v_by);
 perform set_config('eios.tenant_id',p_tenant,true);
 if v_valid<pol.quorum then
  -- Back to waiting; if this person meanwhile has another waiting request of the same body, that one stands and this one ends.
  if exists(select 1 from control.nexloop_approval_requests q where q.tenant_id=p_tenant and q.world=p_world and q.request_digest=v_digest
    and q.requester_subject=v_subject and q.status='waiting_approval') then
   perform control.nexloop_approval_terminate(p_tenant,p_world,r.approval_id,'superseded');
  else
   update control.nexloop_approval_requests set status='waiting_approval',revision=revision+1 where tenant_id=p_tenant and world=p_world and approval_id=r.approval_id;
  end if;
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('approval-reopened:'||r.approval_id||':'||(r.revision+1),'approval.reopened',p_tenant,p_principal,'approval',p_body->>'request_id',
    jsonb_build_object('approval_id',r.approval_id,'reason','approver_no_longer_eligible','approvals',v_valid,'quorum',pol.quorum),clock_timestamp());
  return jsonb_build_object('proceed',false,'approval_id',r.approval_id,'status','waiting_approval','reason','approver_no_longer_eligible');
 end if;
 -- A paused control scope stops the execution itself, not only what it dispatches later (L6 I1).
 perform control.nexloop_nx022_assert_not_paused(p_tenant,p_world,jsonb_build_array(jsonb_build_object('kind','action_type','ref',p_action)));
 -- I-B3 (L6 review of B2): the execution's own HMACs checked once here for every capability, besides the handler's check; a bad one
 -- refuses the call before the handler and consumes nothing.
 if exists(select 1 from unnest(control.nexloop_approval_execution_fields_of(p_capability)) f
   where p_body ? f and (jsonb_typeof(p_body->f) is distinct from 'string' or p_body->>f!~'^[0-9a-f]{64}$')) then
  raise exception 'approval: execution_field_invalid' using errcode='22023';end if;
 return jsonb_build_object('proceed',true,'approval_id',r.approval_id,'approved_by',v_by);
end $$;
