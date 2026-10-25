-- NX-058 slice D2 (docs/design/nx058-owner-actions.md v2.8 §0, §1, §3, §6): the owner-only member Actions under NX-056 approval -
-- promote_owner, demote_owner, suspend_owner, resume_owner, revoke_owner - and approval_open's operation check. The migration number is
-- temporary; it lives only in the file name and the catalog. issue_owner_reset and invite_owner follow in the next slice.
--
-- * control.nexloop_approval_open (0326): the body's operation must be the registered operation of the capability (L6 review of B1); a
--   request built under one capability with another's operation (an invitee forged under demote_owner) never opens. Copy guard.
--   Its target check also accepts a suspended member (resume_owner and demoting a suspended owner act on one); a revoked member stays
--   refused (target_unresolved).
-- * control.nexloop_approval_summary_allowed (0326): demote_owner may show new_role. Copy guard.
-- * Fail closed, two layers (§1): every owner handler first requires the current policy to be exactly the owner-only shape (no condition,
--   quorum >= 1, solo_mode not_applicable, approver roles exactly {owner}, target_subject_field target_principal) - approval_unavailable
--   otherwise, so an Action registered before its policy never runs; then it looks up the approval the governed entry is executing
--   (nexloop.approval_in_flight, 0327) and requires that very request: this capability, approved and unconsumed, this requester, the
--   policy version just read, and the digest of this body. A forged in-flight setting therefore needs a real, approved request of the same
--   body - the same as passing the gate. The policy version check also covers a policy published between the gate and the handler (the
--   publication takes the policies lock, not the tenant lock; I9).
-- * The handler (§3): tenant setting first (0339's row trigger), the member lock (re-entrant: the gate holds it), the two checks above,
--   requester an active owner, the target only the body's target_principal (AA2; never another principal of its subject), its role and
--   status rechecked under the lock, the last active owner never demoted, suspended or revoked (I8: active owners only, cooldown or not),
--   then the authority mark (0341) before the first authority write, and the writes of slice C (Z0: revoke the old role, configuration,
--   grant the new role; role history; sessions and reset codes end). A demoted, suspended or revoked owner's unused invitations end.
--   A refusal inside the gate's savepoint rolls back the mark with the writes; the approval is consumed as execution_refused (I4).
-- * A demoted owner's own waiting requests stay until they expire; executing one needs the owner-only Action's grant, which the
--   demotion revoked (the entry refuses it). Ending them at once needs a new termination reason (design D8, NX-056), next slice.
set local timezone = 'UTC';

create or replace function control.nexloop_approval_summary_allowed(p_capability text) returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case p_capability
  when 'real_dispatch.set' then array['scope_kind','enabled','data_mode','action_type']
  when 'retention.release_hold' then array[]::text[]  -- a hold id is the requester's own text and may carry a Consumer id (L6 review 10)
  when 'consumer.erase' then array[]::text[]
  when 'incentive.issue' then array['amount_minor','currency','offering_revision']
  when 'workbench_member.demote_owner' then array['new_role']  -- NX-058 slice D (K1): the target is the view's target_member, never a summary
  else array[]::text[] end
$$;

create or replace function control.nexloop_approval_open(p_tenant text,p_world text,p_capability text,p_action text,p_principal text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pol control.nexloop_approval_policies;v_subject text;v_digest text;r control.nexloop_approval_requests;
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
   -- suspended member; promote_owner never does); never a revoked one
   and (m.payload->>'status' in ('invited','active') or (m.payload->>'status'='suspended' and p_capability<>'workbench_member.promote_owner'))) then
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
 -- Single-person mode on (A7, design §15.4): a policy only others can satisfy needs that many others eligible now; never lowered.
 if control.nexloop_approval_solo_enabled(p_tenant,p_world)
   -- only what self-approval can never satisfy (quorum above 1, or a policy not applicable to the mode); a quorum-1 request waits
   -- (for another approver, or until the mode takes effect)
   and (pol.quorum>1 or pol.solo_mode<>'allowed')
   and cardinality(array(select s from unnest(control.nexloop_approval_eligible_subjects(p_tenant,pol.approver_roles)) s where s<>v_subject))<pol.quorum then
  raise exception 'approval: quorum_unreachable_in_solo_mode' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_approval_requests(tenant_id,world,capability,action_resource,policy_version,request_digest,request_id,body,requester_subject,requester_principal,due_by)
  values(p_tenant,p_world,p_capability,p_action,pol.version,v_digest,p_body->>'request_id',p_body,v_subject,p_principal,clock_timestamp()+make_interval(secs=>pol.expires_after_seconds))
  returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-requested:'||r.approval_id,'approval.requested',p_tenant,p_principal,'approval',r.request_id,
   jsonb_build_object('approval_id',r.approval_id,'capability',p_capability,'policy_version',pol.version,'request_digest',v_digest,'due_by',r.due_by),clock_timestamp());
 return jsonb_build_object('approval_id',r.approval_id,'status',r.status,'due_by',r.due_by,'replayed',false);
end $$;

-- §1, two layers: the owner-only policy shape now, and the approval the entry is executing is this very request.
create function control.nexloop_workbench_owner_approval(p_tenant text,p_world text,p_capability text,p_principal text,body jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pol control.nexloop_approval_policies;v text:=nullif(current_setting('nexloop.approval_in_flight',true),'');r control.nexloop_approval_requests;
begin
 pol:=control.nexloop_approval_policy(p_capability);
 if pol.capability is null or pol.condition is not null or pol.quorum<1 or pol.solo_mode is distinct from 'not_applicable'
   or pol.approver_roles is distinct from array['owner'] or pol.target_subject_field is distinct from 'target_principal' then
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
end $$;

-- The member an owner-only Action acts on, rechecked under the lock (§3 steps 4-5; AA2: the body's principal only).
create function control.nexloop_workbench_owner_target(p_tenant text,p_principal text,p_requester text,p_owner boolean) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m control.nexloop_browser_memberships;a control.nexloop_browser_accounts;v_role text;
begin
 if not control.nexloop_workbench_member_active(p_tenant,p_requester,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if p_principal is null or p_principal=p_requester then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 select * into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=p_principal for update;
 v_role:=control.nexloop_workbench_role(p_tenant,p_principal);
 if not found or v_role is null or m.payload->>'status'='revoked' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 -- promote_owner acts on an operator or reviewer, every other owner-only Action on an owner (suspended owners included, Y5).
 if (v_role='owner') is distinct from p_owner then raise exception 'workbench member: target_role_changed' using errcode='22023';end if;
 select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=m.subject_id for update;
 return jsonb_build_object('principal',p_principal,'subject',m.subject_id,'role',v_role,'status',m.payload->>'status','account',to_jsonb(a));
end $$;

-- Active owners of the tenant's latest configuration, but one (I8: active memberships only; the vote cooldown does not matter here).
create function control.nexloop_workbench_other_active_owners(p_tenant text,p_principal text) returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select count(*)::integer from jsonb_each_text(coalesce((control.nexloop_workbench_current(p_tenant)).members,'{}')) c
  join control.nexloop_browser_memberships m on m.tenant_id=p_tenant and m.principal_id=c.key and m.payload->>'status'='active'
  where c.value='owner' and c.key<>p_principal $$;

create function control.nexloop_governed_change_workbench_owner(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t jsonb;op text:=body->>'operation';cap text;cur control.nexloop_workbench_configurations;v_source text:='action:'||p_intent;
 new_role text;v_version integer;keys text[];
begin
 cap:=case op when 'promote_workbench_member' then 'workbench_member.promote_owner' when 'demote_workbench_owner' then 'workbench_member.demote_owner'
  when 'suspend_workbench_owner' then 'workbench_member.suspend_owner' when 'resume_workbench_owner' then 'workbench_member.resume_owner'
  when 'revoke_workbench_owner' then 'workbench_member.revoke_owner' end;
 keys:=case op when 'demote_workbench_owner' then array['new_role','operation','reason','request_id','target_principal'] else array['operation','reason','request_id','target_principal'] end;
 if cap is null or jsonb_typeof(body) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from keys
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or not control.nexloop_workbench_member_id_valid(body->>'target_principal')
  or (op='demote_workbench_owner' and (jsonb_typeof(body->'new_role') is distinct from 'string' or body->>'new_role' not in ('operator','reviewer')))
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 if exists(select 1 from runtime.audit_events where event_id='workbench-owner-change:'||p_intent) then
  return jsonb_build_object('target_principal',body->>'target_principal','operation',op,'replayed',true);
 end if;
 perform control.nexloop_workbench_owner_approval(p_tenant,p_world,cap,p_principal,body);
 t:=control.nexloop_workbench_owner_target(p_tenant,body->>'target_principal',p_principal,op<>'promote_workbench_member');
 if op in ('promote_workbench_member','suspend_workbench_owner') and t->>'status'<>'active' then
  raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 if op='resume_workbench_owner' and t->>'status'<>'suspended' then raise exception 'workbench member: target_not_suspended' using errcode='22023';end if;
 if op in ('demote_workbench_owner','suspend_workbench_owner','revoke_workbench_owner') and t->>'status'='active'
   and control.nexloop_workbench_other_active_owners(p_tenant,t->>'principal')<1 then
  raise exception 'workbench member: last_owner' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Before the first authority write, after every check (I7): the requester's tail recheck keeps its revision (0341).
 perform control.nexloop_authority_handler_mark(p_tenant);
 select * into cur from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
 if op='promote_workbench_member' then
  new_role:='owner';
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject',t->>'role','revoke',v_source,p_principal,p_intent);
  v_version:=control.nexloop_workbench_configuration_append(p_tenant,cur.members||jsonb_build_object(t->>'principal','owner'),v_source);
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject','owner','grant',v_source,p_principal,p_intent);
  insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values
   (p_tenant,t->>'subject',t->>'principal',t->>'role','stop',v_source),(p_tenant,t->>'subject',t->>'principal','owner','start',v_source);
 elsif op='demote_workbench_owner' then
  new_role:=body->>'new_role';
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject','owner','revoke',v_source,p_principal,p_intent);
  v_version:=control.nexloop_workbench_configuration_append(p_tenant,cur.members||jsonb_build_object(t->>'principal',new_role),v_source);
  if t->>'status'='active' then
   perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject',new_role,'grant',v_source,p_principal,p_intent);
   insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values
    (p_tenant,t->>'subject',t->>'principal','owner','stop',v_source),(p_tenant,t->>'subject',t->>'principal',new_role,'start',v_source);
  end if;  -- a suspended owner (S3/D4): configuration only; it holds nothing until resumed under the non-owner Actions
 elsif op='suspend_workbench_owner' then
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject','owner','revoke',v_source,p_principal,p_intent);
  perform control.nexloop_workbench_membership_status(p_tenant,t->>'principal','suspended');
  insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,t->>'subject',t->>'principal','owner','stop',v_source);
 elsif op='resume_workbench_owner' then
  perform control.nexloop_workbench_membership_status(p_tenant,t->>'principal','active');
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject','owner','grant',v_source,p_principal,p_intent);
  insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,t->>'subject',t->>'principal','owner','start',v_source);
 else  -- revoke_workbench_owner: the revocation instance always (slice C S-b)
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject','owner','revoke',v_source,p_principal,p_intent);
  if t->>'status'='active' then
   insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,t->>'subject',t->>'principal','owner','stop',v_source);
  end if;
  v_version:=control.nexloop_workbench_configuration_append(p_tenant,cur.members-(t->>'principal'),v_source);
  perform control.nexloop_workbench_membership_status(p_tenant,t->>'principal','revoked');
 end if;
 if op<>'resume_workbench_owner' then
  perform control.nexloop_workbench_member_settle(p_tenant,t,'member_changed');
  -- An owner no longer active as an owner: its unused invitations end (§3 step 9).
  if op<>'promote_workbench_member' then
   with ended as (update control.nexloop_workbench_invitations set state='revoked',revoked_reason='member_changed'
     where tenant_id=p_tenant and issued_by_principal=t->>'principal' and state='issued' returning invitation_id)
   -- One audit event per ended invitation (its id only; L6 review of D2).
   insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
    select 'workbench-invite-ended:'||e.invitation_id,'nexloop.workbench_member.invitation_ended',p_tenant,p_principal,'workbench_member',p_intent,
     jsonb_build_object('invitation_id',e.invitation_id,'reason','member_changed','issuer_principal',t->>'principal'),clock_timestamp() from ended e;
  end if;
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-owner-change:'||p_intent,'nexloop.'||cap,p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('target_principal',t->>'principal','from_role',t->>'role','to_role',coalesce(new_role,t->>'role'),'operation',op,
    'configuration_version',v_version,'approval_id',current_setting('nexloop.approval_in_flight',true),'reason',body->>'reason'),clock_timestamp());
 return jsonb_build_object('target_principal',t->>'principal','operation',op,'role',coalesce(new_role,t->>'role'),'configuration_version',v_version,'replayed',false);
end $$;

do $$ declare f text; begin
 foreach f in array array['control.nexloop_workbench_owner_approval(text,text,text,text,jsonb)','control.nexloop_workbench_owner_target(text,text,text,boolean)',
   'control.nexloop_workbench_other_active_owners(text,text)','control.nexloop_governed_change_workbench_owner(text,text,text,text,text,jsonb)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('workbench_member.promote_owner','promote_workbench_member','human','control.nexloop_governed_change_workbench_owner(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.demote_owner','demote_workbench_owner','human','control.nexloop_governed_change_workbench_owner(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.suspend_owner','suspend_workbench_owner','human','control.nexloop_governed_change_workbench_owner(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.resume_owner','resume_workbench_owner','human','control.nexloop_governed_change_workbench_owner(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.revoke_owner','revoke_workbench_owner','human','control.nexloop_governed_change_workbench_owner(text,text,text,text,text,jsonb)','NX-058');
