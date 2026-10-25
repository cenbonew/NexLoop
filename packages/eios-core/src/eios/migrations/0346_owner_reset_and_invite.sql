-- NX-058 slice D3 (docs/design/nx058-owner-actions.md v2.8 §4, §5; ADR-039 R3 §8, NX-056 B2 0345): issue_owner_reset and invite_owner
-- under NX-056 approval, their execution-time fields, acceptance v3 with the same-person flag, and the claim digest without the
-- execution-time fields (R3 §8.4 option a). The migration number is temporary; it lives only in the file name and the catalog.
--
-- * Capabilities workbench_member.issue_owner_reset (issue_workbench_owner_reset) and workbench_member.invite_owner
--   (invite_workbench_owner), registered with their approval settings in this migration, before any request (R3 §8.3):
--   issue_owner_reset {code_hmac, issuer_source_hmac}, targets {active}; invite_owner {token_hmac, issuer_source_hmac} (no target).
--   The code or token is drawn by the workbench HTTP layer at every submission and shown only in the response of the execution; its HMAC
--   never enters the digest nor the frozen body (B2), and is not written outside the reset code or invitation row (design I6).
-- * Both handlers fail closed like slice D2 (the policy shape - with no target field for invite_owner - and the approval being executed),
--   then follow slice B's issue and slice A's invite with the owner as the target or the role; an owner invitation records its approval.
-- * control.nexloop_accept_workbench_invitation_v3 (replaces v2 for nexloop_identity): an owner invitation is accepted only when its
--   approval's consumption is 'executed' (design I6); every acceptance from the issuer's source writes approval.same_person_flagged (ADR-039
--   R1 §6.6; ids and the reason code only).
-- * control.nexloop_complete_workbench_reset (0333; L6 C-D3-1): the code's origin decides the target's required standing - an owner for a
--   code issued under an approval (control.nexloop_workbench_reset_codes.approval_id, new), an operator or reviewer otherwise. Copy guard.
-- * authz.nexloop_goal_governed_action (0345): the claim's request digest may be over the body without the capability's execution-time
--   fields (canonical JSON, as goal_controls computes it), so a retry with the same request id and a new HMAC replays the claim's terminal
--   outcome (no second code) instead of conflicting with it (L6 I-C2). Copy guard.
set local timezone = 'UTC';

alter table control.nexloop_workbench_invitations add column approval_id uuid;
-- The approval an owner's reset code was issued under (issue_owner_reset); null for issue_reset's codes. Redemption reads it (C-D3-1).
alter table control.nexloop_workbench_reset_codes add column approval_id uuid;

-- §1, two layers, for an owner-only Action with or without a target field (invite_owner has none).
create function control.nexloop_workbench_owner_approval_v2(p_tenant text,p_world text,p_capability text,p_principal text,body jsonb,p_target_field text) returns uuid
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pol control.nexloop_approval_policies;v text:=nullif(current_setting('nexloop.approval_in_flight',true),'');r control.nexloop_approval_requests;
begin
 pol:=control.nexloop_approval_policy(p_capability);
 if pol.capability is null or pol.condition is not null or pol.quorum<1 or pol.solo_mode is distinct from 'not_applicable'
   or pol.approver_roles is distinct from array['owner'] or pol.target_subject_field is distinct from p_target_field then
  raise exception 'workbench member: approval_unavailable' using errcode='22023';end if;
 if v is null or v!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  raise exception 'workbench member: approval_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and approval_id=v::uuid;
 if not found or r.capability<>p_capability or r.status<>'approved' or r.policy_version<>pol.version
   or r.requester_subject is distinct from control.nexloop_approval_subject_of(p_tenant,p_principal)
   or r.request_digest is distinct from control.nexloop_approval_digest(p_tenant,p_world,r.action_resource,p_capability,r.policy_version,body)
   or exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=p_tenant and c.world=p_world and c.approval_id=r.approval_id) then
  raise exception 'workbench member: approval_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return r.approval_id;
end $$;

create function control.nexloop_governed_issue_workbench_owner_reset(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_reset_codes;a control.nexloop_browser_accounts;target_subject text;v_approval uuid;
begin
 if jsonb_typeof(body) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from array['code_hmac','issuer_source_hmac','operation','request_id','target_principal']
  or body->>'operation' is distinct from 'issue_workbench_owner_reset'
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or not control.nexloop_workbench_member_id_valid(body->>'target_principal')
  or coalesce(body->>'code_hmac','')!~'^[0-9a-f]{64}$' or coalesce(body->>'issuer_source_hmac','')!~'^[0-9a-f]{64}$' then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  -- A replay of this handler's own earlier execution in the same request (the governed entry replays a finished claim before it
  -- gets here; this guards the handler on its own): the same code only, never a second one.
  if r.target_principal is distinct from body->>'target_principal' or r.code_hmac is distinct from body->>'code_hmac' then
   raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,'replayed',true);
 end if;
 v_approval:=control.nexloop_workbench_owner_approval_v2(p_tenant,p_world,'workbench_member.issue_owner_reset',p_principal,body,'target_principal');
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 -- The issuing owner's limit, shared with issue_reset (design §5).
 if (select count(*) from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if body->>'target_principal'=p_principal then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 -- The target, rechecked under the lock: an active owner (an operator or reviewer is issue_reset's).
 if control.nexloop_workbench_role(p_tenant,body->>'target_principal') is distinct from 'owner' then
  raise exception 'workbench member: target_role_changed' using errcode='22023';end if;
 if not control.nexloop_workbench_member_active(p_tenant,body->>'target_principal',true) then
  raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 a:=control.nexloop_workbench_member_account(p_tenant,body->>'target_principal');
 if a.local_account_id is null or a.payload->>'status' is distinct from 'active' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 target_subject:=a.subject_id;
 perform control.nexloop_workbench_reset_revoke(p_tenant,target_subject,'reissued');
 insert into control.nexloop_workbench_reset_codes(tenant_id,request_id,target_principal,target_subject,code_hmac,issued_by_principal,issued_by_subject,
   issued_at,expires_at,issuer_source,state,approval_id)
  values(p_tenant,body->>'request_id',body->>'target_principal',target_subject,body->>'code_hmac',p_principal,p_subject,clock_timestamp(),
   clock_timestamp()+interval '30 minutes',body->>'issuer_source_hmac','issued',v_approval) returning * into r;
 -- R5: only now (the approval consumed by this execution) do the target's sessions end.
 perform control.nexloop_workbench_kick(p_tenant,a);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-reset-issue:'||r.reset_id,'nexloop.workbench_member.issue_owner_reset',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,
    'approval_id',current_setting('nexloop.approval_in_flight',true)),clock_timestamp());
 return jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,'replayed',false);
end $$;

create function control.nexloop_governed_invite_workbench_owner(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_invitations;v_hours integer;v_approval uuid;
begin
 if jsonb_typeof(body) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from array['expires_in_hours','issuer_source_hmac','operation','request_id','token_hmac','username']
  or body->>'operation' is distinct from 'invite_workbench_owner'
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'username','')!~'^[a-z0-9][a-z0-9._-]{2,63}$' or coalesce(body->>'token_hmac','')!~'^[0-9a-f]{64}$'
  or coalesce(body->>'issuer_source_hmac','')!~'^[0-9a-f]{64}$' or jsonb_typeof(body->'expires_in_hours') is distinct from 'number'
  or (body->>'expires_in_hours')!~'^[0-9]{1,2}$' or (body->>'expires_in_hours')::integer not between 1 and 72 then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  -- A replay of this handler's own earlier execution (as above): the same invitation, never a second token.
  if (r.username,r.role,r.token_hmac) is distinct from (body->>'username','owner',body->>'token_hmac') then raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'replayed',true);
 end if;
 v_approval:=control.nexloop_workbench_owner_approval_v2(p_tenant,p_world,'workbench_member.invite_owner',p_principal,body,null);
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if (select count(*) from control.nexloop_workbench_invitations where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and lower(a.username)=body->>'username') then
  raise exception 'workbench member: username_taken' using errcode='22023';end if;
 update control.nexloop_workbench_invitations set state='revoked',revoked_reason='reissued' where tenant_id=p_tenant and username=body->>'username' and state='issued';
 v_hours:=(body->>'expires_in_hours')::integer;
 insert into control.nexloop_workbench_invitations(tenant_id,request_id,username,role,token_hmac,issued_by_principal,issued_by_subject,issued_at,expires_at,issuer_source,state,approval_id)
  values(p_tenant,body->>'request_id',body->>'username','owner',body->>'token_hmac',p_principal,p_subject,clock_timestamp(),
   clock_timestamp()+make_interval(hours=>v_hours),body->>'issuer_source_hmac','issued',v_approval) returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-invite:'||r.invitation_id,'nexloop.workbench_member.invite_owner',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('invitation_id',r.invitation_id,'role',r.role,'expires_at',r.expires_at,'approval_id',v_approval),clock_timestamp());
 return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'replayed',false);
end $$;

-- ADR-039 §6.4 (NX-058 slice D3): the workbench HTTP layer refuses an owner invitation of a username the tenant's accounts already hold,
-- before any approval opens. Only an owner holding the invite_owner grant reaches it (the caller checks the grant first); the tenant comes
-- from the session, never from the caller.
create function authz.nexloop_workbench_invitee_free(p_digest text,p_world text,p_username text) returns void
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant text:=ident->'binding'->>'tenant_id';
begin
 if ident->'binding'->>'subject_kind' is distinct from 'human' then raise exception 'workbench requires a human session' using errcode='42501';end if;
 if coalesce(p_username,'')!~'^[a-z0-9][a-z0-9._-]{2,63}$' then raise exception 'workbench member: invalid' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_browser_accounts a where a.tenant_id=v_tenant and lower(a.username)=p_username) then
  raise exception 'workbench member: username_taken' using errcode='22023';end if;
end $$;

-- v3: an owner invitation needs its approval executed (I6); the same-person flag for every acceptance (S1, ADR-039 R1 §6.6).
create function control.nexloop_accept_workbench_invitation_v3(p_tenant text,p_token_hmac text,p_subject jsonb,p_membership jsonb,p_account jsonb,
  p_hash text,p_source text,p_trace text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare inv control.nexloop_workbench_invitations;r jsonb;
begin
 if session_user<>'nexloop_identity' or coalesce(p_token_hmac,'')!~'^[0-9a-f]{64}$' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into inv from control.nexloop_workbench_invitations where tenant_id=p_tenant and token_hmac=p_token_hmac for update;
 if found and inv.role='owner' and (inv.approval_id is null or not exists(select 1 from control.nexloop_approval_consumptions c
   where c.tenant_id=p_tenant and c.world='real' and c.approval_id=inv.approval_id and c.result='executed')) then
  raise exception 'invitation invalid' using errcode='22023';end if;
 r:=control.nexloop_accept_workbench_invitation_v2(p_tenant,p_token_hmac,p_subject,p_membership,p_account,p_hash,p_source,p_trace);
 perform set_config('eios.tenant_id',p_tenant,true);
 if inv.issuer_source is not null and inv.issuer_source=p_source then
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('approval-same-person:'||inv.invitation_id,'approval.same_person_flagged',p_tenant,r->>'principal_id','approval','workbench-invite:'||inv.invitation_id,
    jsonb_build_object('invitation_id',inv.invitation_id,'issued_by',inv.issued_by_principal,'principal_id',r->>'principal_id','role',inv.role,
     'reason','same_source_as_issuer','approval_id',inv.approval_id),clock_timestamp());
 end if;
 return r;
end $$;

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
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
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
 -- NX-058 slice D3 (ADR-039 R3 §8.4 option a, L6 I-C2): the claim's request digest is over the body, or over the body without the
 -- capability's execution-time fields (canonical JSON, as goal_controls computes it), so a retry with the same request id and a newly
 -- drawn HMAC replays the claim's terminal outcome instead of conflicting with it.
 if v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
   and v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(
     runtime.nexloop_canonical_json(body-control.nexloop_approval_execution_fields_of(v_cap)),'UTF8')),'hex') then
  raise exception 'goal permit rejected' using errcode='42501';end if;
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

do $$ declare f text; begin
 foreach f in array array['control.nexloop_workbench_owner_approval_v2(text,text,text,text,jsonb,text)',
   'control.nexloop_governed_issue_workbench_owner_reset(text,text,text,text,text,jsonb)','control.nexloop_governed_invite_workbench_owner(text,text,text,text,text,jsonb)',
   'control.nexloop_accept_workbench_invitation_v3(text,text,jsonb,jsonb,jsonb,text,text,text)','authz.nexloop_workbench_invitee_free(text,text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
revoke execute on function control.nexloop_accept_workbench_invitation_v2(text,text,jsonb,jsonb,jsonb,text,text,text) from nexloop_identity;
grant execute on function control.nexloop_accept_workbench_invitation_v3(text,text,jsonb,jsonb,jsonb,text,text,text) to nexloop_identity;
grant execute on function authz.nexloop_workbench_invitee_free(text,text,text) to nexloop_api;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('workbench_member.issue_owner_reset','issue_workbench_owner_reset','human','control.nexloop_governed_issue_workbench_owner_reset(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.invite_owner','invite_workbench_owner','human','control.nexloop_governed_invite_workbench_owner(text,text,text,text,text,jsonb)','NX-058');
-- invite_owner has no target field: its target statuses are never consulted (the column's default is recorded, nothing more).
insert into control.nexloop_approval_capability_settings(capability,execution_fields,target_statuses) values
 ('workbench_member.issue_owner_reset','{code_hmac,issuer_source_hmac}','{active}');
insert into control.nexloop_approval_capability_settings(capability,execution_fields) values
 ('workbench_member.invite_owner','{token_hmac,issuer_source_hmac}');

create or replace function control.nexloop_complete_workbench_reset(p_tenant text,p_code_hmac text,p_expected bigint,p_hash text,p_source text,p_trace text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_reset_codes;a control.nexloop_browser_accounts;now_at timestamptz;body jsonb;
begin
 if session_user<>'nexloop_identity' or coalesce(p_code_hmac,'')!~'^[0-9a-f]{64}$' or p_hash is null or length(p_hash)>1024
  or p_hash not like '$argon2id$%' or (p_source is not null and p_source!~'^[0-9a-f]{64}$') or p_trace is null or length(p_trace) not between 1 and 320 then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and code_hmac=p_code_hmac for update;
 -- Every refusal is the same: no such code, not issued, expired, issuer no longer an active owner, target no longer an active
 -- non-owner member, or the account changed since the lookup.
 if not found or r.state<>'issued' or r.expires_at<=clock_timestamp()
  or not control.nexloop_workbench_member_active(p_tenant,r.issued_by_principal,true)
  -- NX-058 slice D3 (L6 C-D3-1): the code's origin decides, never the target's role now - issue_owner_reset's code (its approval
  -- recorded) needs an active owner, issue_reset's an active operator or reviewer; a code of a member changed since is refused.
  or not control.nexloop_workbench_member_active(p_tenant,r.target_principal,r.approval_id is not null) then
  raise exception 'reset invalid' using errcode='22023';end if;
 select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=r.target_subject for update;
 if not found or a.payload->>'status' is distinct from 'active' or (a.payload->>'revision')::bigint is distinct from p_expected then
  raise exception 'reset invalid' using errcode='22023';end if;
 now_at:=clock_timestamp();
 body:=control.nexloop_browser_password_write(p_tenant,a,p_hash,now_at);
 update control.nexloop_workbench_reset_codes set state='completed',completed_at=now_at,completer_source=p_source where reset_id=r.reset_id;
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,a.subject_id,'owner_issued_password_reset','reset',r.issued_by_principal,'workbench-reset:'||r.reset_id,p_trace,
   jsonb_build_object('reset_id',r.reset_id,'account_id',a.local_account_id,'account_revision',p_expected+1,'session_epoch',(body->>'session_epoch')::bigint,
    'same_source_as_issuer',r.issuer_source is not null and r.issuer_source=p_source),now_at);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-reset-complete:'||r.reset_id,'nexloop.workbench_member.reset_completed',p_tenant,r.target_principal,'workbench_member','workbench-reset:'||r.reset_id,
   jsonb_build_object('reset_id',r.reset_id,'issued_by',r.issued_by_principal,'target_principal',r.target_principal,
    'same_source_as_issuer',r.issuer_source is not null and r.issuer_source=p_source),now_at);
 return jsonb_build_object('reset_id',r.reset_id,'completed_at',now_at);
end $$;
