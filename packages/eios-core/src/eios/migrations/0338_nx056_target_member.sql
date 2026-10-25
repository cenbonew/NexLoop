-- NX-056 B1 (ADR-039 §6, draft revision R1; L6 reviews of 0d09d764, d3956c50, 010d312e): the approval view's target member, its policy
-- rule and the alert metrics of rules v15. Temporary number; the dispatcher renumbers. Published migrations are not edited; the
-- functions below are re-created with only these changes (tests/test_function_copy_guard.py).
-- 1. control.nexloop_approval_policy_guard (0326): a policy with target_subject_field is approved by owners only (§6.7); the published
--    policies are checked once here, and the migration fails if one does not comply.
-- 2. runtime.nexloop_approval_target_member: the target_member block of one row for one caller, in the fixed order of §6.1:
--    null (no target field, not invite_owner) -> hidden (the caller is not an owner now: nothing is resolved) -> invite_owner (D7
--    pending: hidden) -> erased -> unresolved -> member. Resolution (§6.2) from existing columns only: the body's principal itself, or
--    for a subject id the first membership by status then principal; the label is p- and the first 8 hex digits of sha256(principal);
--    the username of the first account by status then local_account_id. Any error is unresolved (fail closed); the body value is never
--    returned. Read-time only: nothing is stored, audited or added to the digest.
-- 3. runtime.nexloop_approval_view (0329): every row of the three columns carries target_member.
-- 4. runtime.nexloop_approval_metrics (0328): first_time_approvers_24h and same_person_flags_24h (§6.6).

create or replace function control.nexloop_approval_policy_guard() returns trigger language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 -- ADR-039 §6.7 (R1, B1): a policy naming the member a request acts on is approved by owners only.
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

do $$begin
 if exists(select 1 from control.nexloop_approval_policies where target_subject_field is not null and approver_roles is distinct from array['owner']) then
  raise exception 'approval policy: a published target policy is approved by other roles than owner' using errcode='22023';end if;
end $$;

create function runtime.nexloop_approval_target_member(p_tenant text,r control.nexloop_approval_requests,pol control.nexloop_approval_policies,p_caller text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);cfg control.nexloop_workbench_configurations;v_value text;
 m control.nexloop_browser_memberships;a control.nexloop_browser_accounts;v jsonb;
begin
 -- 1. No target.
 if pol.target_subject_field is null and r.body->>'operation' is distinct from 'invite_workbench_owner' then return null;end if;
 -- 2. Not an owner now: hidden, nothing resolved (a non-owner cannot tell a target that exists from one that does not).
 cfg:=control.nexloop_workbench_current(p_tenant);
 if cfg.tenant_id is null or coalesce(cfg.members->>p_caller,'')<>'owner' then return jsonb_build_object('kind','hidden');end if;
 -- 3. invite_owner: D7 pending, the invitee is not shown (§6.4).
 if r.body->>'operation'='invite_workbench_owner' then return jsonb_build_object('kind','hidden');end if;
 begin
  v_value:=r.body->>pol.target_subject_field;
  perform set_config('eios.tenant_id',p_tenant,true);
  if v_value is not null then
   -- The body's own principal; a subject id: its first membership by status, then principal.
   select * into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=v_value;
   if m.principal_id is null then
    select * into m from control.nexloop_browser_memberships where tenant_id=p_tenant and subject_id=v_value
     order by array_position(array['active','invited','suspended','revoked'],payload->>'status'),principal_id limit 1;
   end if;
  end if;
  if m.principal_id is not null then
   select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=m.subject_id
    order by array_position(array['active','invited','disabled'],payload->>'status'),local_account_id limit 1;
   -- 4. Erased (a pseudonymised member identity; no staff erasure path exists yet, §6.5).
   if m.principal_id like 'pseudonym:%' or m.subject_id like 'pseudonym:%' or coalesce(a.username,'') like 'pseudonym:%' then
    v:=jsonb_build_object('kind','erased');
   else
    -- 6. A member.
    v:=jsonb_build_object('kind','member','label','p-'||left(encode(sha256(convert_to(m.principal_id,'UTF8')),'hex'),8),
     'username',a.username,'account_status',a.payload->>'status','member_status',m.payload->>'status','role',cfg.members->>m.principal_id);
   end if;
  end if;
 exception when others then
  v:=null;
 end;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 -- 5. Not resolved (and any error): never the body's value.
 return coalesce(v,jsonb_build_object('kind','unresolved'));
end $$;
alter function runtime.nexloop_approval_target_member(text,control.nexloop_approval_requests,control.nexloop_approval_policies,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_approval_target_member(text,control.nexloop_approval_requests,control.nexloop_approval_policies,text) from public;

create or replace function runtime.nexloop_approval_view(p_tenant text,p_world text,p_principal text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_subject text:=control.nexloop_approval_subject_of(p_tenant,p_principal);v_decide jsonb;v_mine jsonb;v_execute jsonb;
begin
 if v_subject is null then raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(x.v order by x.due_by,x.approval_id),'[]'::jsonb) into v_decide from (
  select runtime.nexloop_approval_view_row(r,pol)||jsonb_build_object('target_member',runtime.nexloop_approval_target_member(p_tenant,r,pol,p_principal)) v,
   r.due_by,r.approval_id from control.nexloop_approval_requests r
   join control.nexloop_approval_policies pol on pol.capability=r.capability and pol.version=r.policy_version
  where r.tenant_id=p_tenant and r.world=p_world and r.status='waiting_approval' and r.due_by>clock_timestamp()
   -- one's own only in the single-person mode while it takes effect (A7)
   and (r.requester_subject<>v_subject or control.nexloop_approval_solo_effective(p_tenant,p_world,r.capability,r.policy_version,v_subject))
   and not exists(select 1 from control.nexloop_approval_decisions d where d.tenant_id=r.tenant_id and d.world=r.world and d.approval_id=r.approval_id
    and d.actor_subject=v_subject)
   and control.nexloop_approval_approver_eligible(p_tenant,p_principal,pol.approver_roles)
   and v_subject is distinct from control.nexloop_approval_target_subject(p_tenant,r.body,pol.target_subject_field)  -- NX-058 Z1
  order by r.due_by,r.approval_id limit 50) x;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(x.v order by x.requested_at desc,x.approval_id),'[]'::jsonb) into v_mine from (
  select runtime.nexloop_approval_view_row(r,pol)||jsonb_build_object('result',k.result,
    'target_member',runtime.nexloop_approval_target_member(p_tenant,r,pol,p_principal)) v,r.requested_at,r.approval_id from control.nexloop_approval_requests r
   join control.nexloop_approval_policies pol on pol.capability=r.capability and pol.version=r.policy_version
   left join control.nexloop_approval_consumptions k on k.tenant_id=r.tenant_id and k.world=r.world and k.approval_id=r.approval_id
  where r.tenant_id=p_tenant and r.world=p_world and r.requester_subject=v_subject and r.requested_at>clock_timestamp()-interval '30 days'
  order by r.requested_at desc,r.approval_id limit 50) x;
 select coalesce(jsonb_agg(x.v order by x.due_by,x.approval_id),'[]'::jsonb) into v_execute from (
  select runtime.nexloop_approval_view_row(r,pol)||jsonb_build_object('inputs',r.body-'request_id'-'operation',
    'target_member',runtime.nexloop_approval_target_member(p_tenant,r,pol,p_principal)) v,r.due_by,r.approval_id
  from control.nexloop_approval_requests r join control.nexloop_approval_policies pol on pol.capability=r.capability and pol.version=r.policy_version
  where r.tenant_id=p_tenant and r.world=p_world and r.requester_subject=v_subject and r.status='approved' and r.due_by>clock_timestamp()
   and not exists(select 1 from control.nexloop_approval_consumptions k where k.tenant_id=r.tenant_id and k.world=r.world and k.approval_id=r.approval_id)
  order by r.due_by,r.approval_id limit 50) x;
 -- The banner (design §15.5): the switch, and whether anyone else is eligible under a current policy (then the mode does not apply).
 return jsonb_build_object('to_decide',v_decide,'mine',v_mine,'to_execute',v_execute,
  'solo_mode',jsonb_build_object('enabled',control.nexloop_approval_solo_enabled(p_tenant,p_world),
   'others_eligible',exists(select 1 from (select distinct on (p.capability) p.approver_roles from control.nexloop_approval_policies p order by p.capability,p.version desc) q
     where exists(select 1 from unnest(control.nexloop_approval_eligible_subjects(p_tenant,q.approver_roles)) s where s<>v_subject))));
end $$;

create or replace function runtime.nexloop_approval_metrics(p_tenant text,p_world text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select jsonb_build_object('waiting',count(*) filter(where r.status='waiting_approval' and r.due_by>clock_timestamp()),
   'oldest_waiting_age_seconds',coalesce(floor(extract(epoch from clock_timestamp()-min(r.requested_at) filter(where r.status='waiting_approval'
     and r.due_by>clock_timestamp())))::bigint,0),
   'approved_unconsumed',count(*) filter(where r.status='approved' and r.due_by>clock_timestamp() and not exists(select 1 from control.nexloop_approval_consumptions k
     where k.approval_id=r.approval_id)),
   'overdue',count(*) filter(where r.status in ('waiting_approval','approved') and r.due_by<=clock_timestamp() and not exists(
     select 1 from control.nexloop_approval_consumptions k where k.approval_id=r.approval_id)))
  into v from control.nexloop_approval_requests r where r.tenant_id=p_tenant and r.world=p_world;
 -- A7 (design §15, alert approval_solo_mode): the switch, self-approvals and reduced eligible counts of the last 24 hours.
 v:=v||jsonb_build_object('solo_mode_enabled',case when control.nexloop_approval_solo_enabled(p_tenant,p_world) then 1 else 0 end,
  'self_approvals_24h',(select count(*) from control.nexloop_approval_decisions d where d.tenant_id=p_tenant and d.world=p_world and d.self_approved
    and d.decided_at>clock_timestamp()-interval '24 hours'),
  'eligibility_reductions_24h',(select count(*) from runtime.audit_events a where a.tenant_id=p_tenant and a.action='approval.eligibility_reduced'
    and a.created_at>clock_timestamp()-interval '24 hours'));
 -- ADR-039 §6.6 (R1, B1): first approve votes of the tenant in the last 24 hours (decision events, self votes included; §6.7) and the
 -- same-person flags NX-058 slice D writes (approval.same_person_flagged; 0 before it).
 v:=v||jsonb_build_object('first_time_approvers_24h',(select count(*) from (select d.actor_subject,min(d.decided_at) first_at
    from control.nexloop_approval_decisions d where d.tenant_id=p_tenant and d.outcome='approved' group by d.actor_subject) f
    where f.first_at>clock_timestamp()-interval '24 hours'),
  'same_person_flags_24h',(select count(*) from runtime.audit_events a where a.tenant_id=p_tenant and a.action='approval.same_person_flagged'
    and a.created_at>clock_timestamp()-interval '24 hours'));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
