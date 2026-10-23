-- NX-056 A2 (docs/design/unified-approvals.md v5 §2, §4, §6; L6 C1-C4, N-1, N-2, N-2a): the governed entry asks the unified Approval
-- store before it calls a handler. The migration number lives only in the file name and the catalog.
-- * control.nexloop_approval_gate: no policy, or its condition does not hold -> proceed as before. Otherwise the request (digest over
--   tenant, world, Action version, policy version, body without request_id) either consumes an approved, unconsumed Approval that this
--   very person (subject) requested - under a row lock, rechecking every approver's standing now (a vote that no longer stands does not
--   count: below the quorum the Approval goes back to waiting), its due_by and the control pause - or it opens (or answers) a waiting
--   request and the handler is not called. The executing call is the requester's own resubmission with a new request id (the first
--   submission's action claim is terminal): the entry has already re-verified their current authority; nothing executes on another's
--   behalf (L6 C2).
-- * authz.nexloop_goal_governed_action (0124, re-created with only the gate added; tests/test_function_copy_guard.py): an approved call
--   runs the handler in a savepoint; a refusal still consumes the Approval (execution_refused:<sqlstate>), a transient error
--   (40001, 40P01, 55P03, 57014, classes 53 and 08, 57P0x) is re-raised and consumes nothing. The action claim ends permanent_failure
--   when nothing was executed or the handler refused.
-- * Past due_by an approved Approval ends expired and the body opens anew; within an hour of a consumption the same body answers
--   approval_consumed; an Approval of an older policy version ends with policy_changed.
-- * control.nexloop_approval_consume: the one consumption (primary key approval_id) with the audit approval.consumed naming the actor
--   (requester) and approved_by (the approvers' subjects).

-- The votes that count now (one rule for the gate and for a decision's replay): approvals of distinct subjects, each approver eligible
-- now (L6 C3), never the member the request acts on (NX-058 Z1); the requester's own vote only while the single-person mode takes
-- effect now (A7, S-I1; 0327). Returns the counted subjects.
create function control.nexloop_approval_valid_votes(p_tenant text,p_world text,p_approval uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_approval_requests;pol control.nexloop_approval_policies;v_by jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and approval_id=p_approval;
 select * into pol from control.nexloop_approval_policies where capability=r.capability and version=r.policy_version;
 select coalesce(jsonb_agg(distinct d.actor_subject),'[]'::jsonb) into v_by from control.nexloop_approval_decisions d
  where d.tenant_id=p_tenant and d.world=p_world and d.approval_id=r.approval_id and d.outcome='approved'
   and d.actor_subject<>r.requester_subject and control.nexloop_approval_approver_eligible(p_tenant,d.actor_principal,pol.approver_roles)
   and d.actor_subject is distinct from control.nexloop_approval_target_subject(p_tenant,r.body,pol.target_subject_field);
 perform set_config('eios.tenant_id',p_tenant,true);
 if exists(select 1 from control.nexloop_approval_decisions d where d.tenant_id=p_tenant and d.world=p_world and d.approval_id=r.approval_id
   and d.outcome='approved' and d.actor_subject=r.requester_subject and d.self_approved)
  and control.nexloop_approval_solo_effective(p_tenant,p_world,r.capability,r.policy_version,r.requester_subject) then
  v_by:=v_by||to_jsonb(r.requester_subject);
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return v_by;
end $$;
alter function control.nexloop_approval_valid_votes(text,text,uuid) owner to nexloop_owner;
revoke all on function control.nexloop_approval_valid_votes(text,text,uuid) from public;

create function control.nexloop_approval_gate(p_tenant text,p_world text,p_capability text,p_action text,p_principal text,p_body jsonb) returns jsonb
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
 -- Lock order (design §16, N-1): the tenant lock first (member changes take it too), then the approval row (A7, 0327).
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
 return jsonb_build_object('proceed',true,'approval_id',r.approval_id,'approved_by',v_by);
end $$;
alter function control.nexloop_approval_gate(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_approval_gate(text,text,text,text,text,jsonb) from public;

-- The one consumption; approved_by is the votes the gate counted toward the quorum just now (L6 A2 review suggestion 2).
create function control.nexloop_approval_consume(p_tenant text,p_world text,p_approval uuid,p_principal text,p_result text,p_approved_by jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_approval_requests;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and approval_id=p_approval;
 insert into control.nexloop_approval_consumptions(tenant_id,world,approval_id,consumed_by_subject,consumed_by_principal,result)
  values(p_tenant,p_world,p_approval,r.requester_subject,p_principal,p_result);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-consumed:'||p_approval,'approval.consumed',p_tenant,p_principal,'approval',r.request_id,
   jsonb_build_object('approval_id',p_approval,'capability',r.capability,'result',p_result,'actor',r.requester_subject,'approved_by',p_approved_by),clock_timestamp());
end $$;
alter function control.nexloop_approval_consume(text,text,uuid,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_approval_consume(text,text,uuid,text,text,jsonb) from public;

create or replace function authz.nexloop_goal_governed_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;
 ident jsonb;v_subject text;v_cap text;v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';v_id text;v_outcome jsonb;v_result jsonb;
 cap control.nexloop_governed_capabilities;
 v_gate jsonb;v_consumed text;  -- NX-056 A2
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
  -- The approval being executed: a Consumer erasure that starts inside this handler does not end it (0326).
  perform set_config('nexloop.approval_in_flight',v_gate->>'approval_id',true);
  begin  -- a savepoint: a refusal rolls back the handler's own writes only, and the approval is still consumed (L6 N-2)
   execute format('select %s($1,$2,$3,$4,$5,$6)',cap.handler::oid::regproc) into v_result using v_tenant,p_world,v_principal,v_subject,stored.intent_id,body;
   v_consumed:='executed';
  exception when others then
   -- A transient database error is no refusal: re-raised, nothing consumed, the requester may execute again (L6 N-2a).
   -- Also out-of-resources (53), connection (08) and administrator shutdown (57P0x) (L6 A2 review suggestion 3).
   if sqlstate in ('40001','40P01','55P03','57014') or left(sqlstate,2) in ('53','08') or sqlstate like '57P0_' then raise;end if;
   v_consumed:='execution_refused:'||sqlstate;
   v_result:=jsonb_build_object('status','execution_refused','code',sqlstate);
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

alter function authz.nexloop_goal_governed_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_goal_governed_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_goal_governed_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
