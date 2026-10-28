-- NX-056 approval expiry precision (CI seq 186): control.nexloop_approval_open took two clock readings for one request - requested_at
-- from the column default clock_timestamp() and due_by from a second clock_timestamp() - so a request lived its policy's
-- expires_after_seconds plus the microseconds between them. It now reads the clock once: requested_at, due_by = requested_at +
-- expires_after_seconds and the approval.requested audit time are the same instant. Nothing else changes. The migration number is
-- temporary; it lives only in the file name and the catalog. Copy guard.

create or replace function control.nexloop_approval_open(p_tenant text,p_world text,p_capability text,p_action text,p_principal text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pol control.nexloop_approval_policies;v_subject text;v_digest text;r control.nexloop_approval_requests;v_target text;v_from timestamptz[];v_now timestamptz;
begin
 pol:=control.nexloop_approval_policy(p_capability);
 -- An Action version without its policy version is refused (fail closed, design §3).
 if pol.capability is null or p_action!~'^eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}$' or split_part(p_action,':',4)::integer<>pol.action_version then
  raise exception 'approval: policy_unavailable' using errcode='22023';end if;
 -- NX-058 slice D (L6 review of B1): the body's operation is the capability's registered one, or nothing opens.
 if p_body->>'operation' is distinct from (select c.operation from control.nexloop_governed_capabilities c where c.capability=p_capability) then
  raise exception 'approval: operation_mismatch' using errcode='22023';end if;
 -- NX-056 budget approval (§2.3): a loosening opens only against the current row it names (no approval for a stale view).
 if p_capability='goals.budget.loosen' then perform control.nexloop_budget_loosen_check(p_tenant,p_world,p_body,false);end if;
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
 v_now:=clock_timestamp();  -- one reading: requested_at, due_by and the audit time are the same instant
 insert into control.nexloop_approval_requests(tenant_id,world,capability,action_resource,policy_version,request_digest,request_id,body,requester_subject,requester_principal,requested_at,due_by)
  values(p_tenant,p_world,p_capability,p_action,pol.version,v_digest,p_body->>'request_id',
   p_body-control.nexloop_approval_execution_fields_of(p_capability),  -- ADR-039 §7.1: never an execution-time field in the frozen body
   v_subject,p_principal,v_now,v_now+make_interval(secs=>pol.expires_after_seconds))
  returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-requested:'||r.approval_id,'approval.requested',p_tenant,p_principal,'approval',r.request_id,
   jsonb_build_object('approval_id',r.approval_id,'capability',p_capability,'policy_version',pol.version,'request_digest',v_digest,'due_by',r.due_by),v_now);
 return jsonb_build_object('approval_id',r.approval_id,'status',r.status,'due_by',r.due_by,'replayed',false);
end $$;

do $$ begin
 alter function control.nexloop_approval_open(text,text,text,text,text,jsonb) owner to nexloop_owner;
 revoke all on function control.nexloop_approval_open(text,text,text,text,text,jsonb) from public;
end $$;
