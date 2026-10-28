-- NX-056 budget approval (docs/design/NX-056-budget-approval.md v2, owner 2026-10-28: Q1-Q8 as recommended): a budget is loosened only
-- with another owner's approval; tightening stays immediate. The migration number is temporary; it lives only in the file name and the
-- catalog.
--
-- 1. control.nexloop_budget_direction(tenant, world, body): the server's reading of a set_budget body against the current row (locked):
--    tighten  - the row exists, same unit, limit not higher, period_start unchanged, period_end not later (a value equal to the current one
--               included);
--    renewal  - Q4: the old period has ended (now >= its end), the new one starts at its end, has its length (whole calendar months when
--               the old one was whole months, otherwise the exact duration) and the same limit and unit;
--    loosen   - anything else: a new row, a higher limit, a changed unit, a later end, and any change of period_start (I1 (a): 0068 counts
--               only consumption inside [period_start, period_end), so moving the start drops consumption already recorded).
-- 2. control.nexloop_governed_owner_change (0124, Budget.set's handler): a set_budget body passes control.nexloop_budget_set_tighten_only
--    first - tighten or renewal, or `budget: budget_loosen_requires_approval` and nothing is written. Other operations are untouched.
--    Copy guard.
-- 3. Budget.loosen (nexloop.budget.loosen, capability goals.budget.loosen, operation loosen_budget): the set_budget fields plus what the
--    requester saw - from_unit, from_limit_amount, from_period_start, from_period_end (all null for a new row) and expected_revision (the
--    row's revision, 0 for a new row). control.nexloop_budget_loosen_check refuses a body that does not name the current row
--    (`budget: budget_changed`):
--    * when the approval opens (control.nexloop_approval_open, copy guard): no approval for a stale view;
--    * when the approved request executes (the handler, under the row lock): the approver saw the change that runs; a refusal there is
--      consumed as execution_refused (unified-approvals §4 step 3) and the requester asks again.
--    The handler fails closed like NX-058's owner-only Actions: the policy must be there with the owner shape (no condition, quorum >= 1,
--    approver roles exactly {owner}, no target field; single-person mode is the policy's choice, allowed per Q5) and the approval being
--    executed must be this very request (capability, approved and unconsumed, requester, policy version, digest of this body);
--    `budget: approval_unavailable` otherwise. The write is 0068's set_budget, unchanged.
-- 4. control.nexloop_approval_summary_allowed (0346): goals.budget.loosen may show the kind, the unit and the old and new limit and period.
--    Copy guard.
set local timezone = 'UTC';

create function control.nexloop_budget_direction(p_tenant text,p_world text,body jsonb) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare cur control.nexloop_budget_limits;v_amount numeric;v_start timestamptz;v_end timestamptz;v_months integer;
begin
 v_amount:=control.nexloop_nx022_decimal(body->>'limit_amount');
 v_start:=control.nexloop_nx022_timestamp(body->>'period_start');v_end:=control.nexloop_nx022_timestamp(body->>'period_end');
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_budget_limits where tenant_id=p_tenant and world=p_world and budget_kind=body->>'budget_kind' for update;
 if not found or body->>'unit' is distinct from cur.unit or v_amount>cur.limit_amount then return 'loosen';end if;
 if v_start=cur.period_start and v_end<=cur.period_end then return 'tighten';end if;
 -- Q4 (I1 (b)): a same-amount renewal only once the old period has ended; before that it would drop the old period's consumption.
 if clock_timestamp()>=cur.period_end and v_start=cur.period_end and v_amount=cur.limit_amount then
  v_months:=(extract(year from cur.period_end)::integer-extract(year from cur.period_start)::integer)*12
   +extract(month from cur.period_end)::integer-extract(month from cur.period_start)::integer;
  if v_months between 1 and 120 and cur.period_start+make_interval(months=>v_months)=cur.period_end then
   if v_end=v_start+make_interval(months=>v_months) then return 'renewal';end if;
  elsif v_end-v_start=cur.period_end-cur.period_start then return 'renewal';
  end if;
 end if;
 return 'loosen';
end $$;

-- Budget.set: tightening and the renewal only; anything else is Budget.loosen's (with approval). Returns the body unchanged.
create function control.nexloop_budget_set_tighten_only(p_tenant text,p_world text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if body->>'operation' is distinct from 'set_budget' then return body;end if;
 if control.nexloop_budget_direction(p_tenant,p_world,body)='loosen' then
  raise exception 'budget: budget_loosen_requires_approval' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return body;
end $$;

create or replace function control.nexloop_governed_owner_change(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language sql set search_path=pg_catalog,pg_temp as $$ select control.nexloop_nx022_owner_change(p_tenant,p_world,p_principal,p_intent,control.nexloop_budget_set_tighten_only(p_tenant,p_world,body)) $$;

-- The loosening body names the row the requester saw (all from_* null and expected_revision 0 for a new row); p_lock at execution.
create function control.nexloop_budget_loosen_check(p_tenant text,p_world text,body jsonb,p_lock boolean) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur control.nexloop_budget_limits;v_from text[]:=array['from_limit_amount','from_period_end','from_period_start','from_unit'];
begin
 if jsonb_typeof(body) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from
   array['budget_kind','expected_revision','from_limit_amount','from_period_end','from_period_start','from_unit','limit_amount','operation',
         'period_end','period_start','request_id','unit']
  or body->>'operation' is distinct from 'loosen_budget'
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or jsonb_typeof(body->'budget_kind') is distinct from 'string' or body->>'budget_kind' not in ('model','incentive','evolution')
  or exists(select 1 from unnest(array['unit','limit_amount','period_start','period_end']) f where jsonb_typeof(body->f) is distinct from 'string')
  or jsonb_typeof(body->'expected_revision') is distinct from 'number' or body->>'expected_revision'!~'^(0|[1-9][0-9]{0,17})$'
  or exists(select 1 from unnest(v_from) f where jsonb_typeof(body->f) not in ('string','null'))
  or (select count(distinct jsonb_typeof(body->f)) from unnest(v_from) f)<>1 then
  raise exception 'budget: invalid' using errcode='22023';end if;
 perform control.nexloop_nx022_decimal(body->>'limit_amount'),control.nexloop_nx022_timestamp(body->>'period_start'),
  control.nexloop_nx022_timestamp(body->>'period_end');
 perform set_config('eios.tenant_id',p_tenant,true);
 if p_lock then
  select * into cur from control.nexloop_budget_limits where tenant_id=p_tenant and world=p_world and budget_kind=body->>'budget_kind' for update;
 else
  select * into cur from control.nexloop_budget_limits where tenant_id=p_tenant and world=p_world and budget_kind=body->>'budget_kind';
 end if;
 if not found then
  if (body->>'expected_revision')::bigint<>0 or jsonb_typeof(body->'from_unit')<>'null' then
   raise exception 'budget: budget_changed' using errcode='22023';end if;
 elsif jsonb_typeof(body->'from_unit')<>'string' or (body->>'expected_revision')::bigint<>cur.revision or body->>'from_unit'<>cur.unit
   or control.nexloop_nx022_decimal(body->>'from_limit_amount')<>cur.limit_amount
   or control.nexloop_nx022_timestamp(body->>'from_period_start')<>cur.period_start
   or control.nexloop_nx022_timestamp(body->>'from_period_end')<>cur.period_end then
  raise exception 'budget: budget_changed' using errcode='22023';
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
end $$;

-- Fail closed, two layers (as control.nexloop_workbench_owner_approval, 0346): the owner policy shape now, and the approval the entry is
-- executing is this very request.
create function control.nexloop_budget_loosen_approval(p_tenant text,p_world text,p_principal text,body jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pol control.nexloop_approval_policies;v text:=nullif(current_setting('nexloop.approval_in_flight',true),'');r control.nexloop_approval_requests;
begin
 pol:=control.nexloop_approval_policy('goals.budget.loosen');
 if pol.capability is null or pol.condition is not null or pol.quorum<1 or pol.approver_roles is distinct from array['owner']
   or pol.target_subject_field is not null then
  raise exception 'budget: approval_unavailable' using errcode='22023';end if;
 if v is null or v!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  raise exception 'budget: approval_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and approval_id=v::uuid;
 if not found or r.capability<>'goals.budget.loosen' or r.status<>'approved' or r.policy_version<>pol.version
   or r.requester_subject is distinct from control.nexloop_approval_subject_of(p_tenant,p_principal)
   or r.request_digest is distinct from control.nexloop_approval_digest(p_tenant,p_world,r.action_resource,'goals.budget.loosen',r.policy_version,body)
   or exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=p_tenant and c.world=p_world and c.approval_id=r.approval_id) then
  raise exception 'budget: approval_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
end $$;

create function control.nexloop_governed_loosen_budget(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;v_direction text;
begin
 perform control.nexloop_budget_loosen_check(p_tenant,p_world,body,false);
 perform set_config('eios.tenant_id',p_tenant,true);
 if exists(select 1 from runtime.audit_events where event_id='budget-loosen:'||p_intent) then
  return jsonb_build_object('budget_kind',body->>'budget_kind','operation','loosen_budget','replayed',true);
 end if;
 perform control.nexloop_budget_loosen_approval(p_tenant,p_world,p_principal,body);
 perform control.nexloop_budget_loosen_check(p_tenant,p_world,body,true);
 -- What the approved change does to the current row (for the audit only: a tightening approved as a loosening runs as approved).
 v_direction:=control.nexloop_budget_direction(p_tenant,p_world,jsonb_build_object('budget_kind',body->'budget_kind','unit',body->'unit',
  'limit_amount',body->'limit_amount','period_start',body->'period_start','period_end',body->'period_end'));
 v:=control.nexloop_nx022_owner_change(p_tenant,p_world,p_principal,p_intent,jsonb_build_object('operation','set_budget','request_id',body->'request_id',
  'budget_kind',body->'budget_kind','unit',body->'unit','limit_amount',body->'limit_amount','period_start',body->'period_start','period_end',body->'period_end'));
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('budget-loosen:'||p_intent,'nexloop.goals.budget.loosen',p_tenant,p_principal,'budget',p_intent,
   jsonb_build_object('world',p_world,'budget_kind',body->>'budget_kind','direction',v_direction,'expected_revision',(body->>'expected_revision')::bigint,
    'from',jsonb_build_object('unit',body->'from_unit','limit_amount',body->'from_limit_amount','period_start',body->'from_period_start','period_end',body->'from_period_end'),
    'to',jsonb_build_object('unit',body->'unit','limit_amount',body->'limit_amount','period_start',body->'period_start','period_end',body->'period_end'),
    'control_revision',v->'control_revision','approval_id',current_setting('nexloop.approval_in_flight',true)),clock_timestamp());
 return v||jsonb_build_object('operation','loosen_budget','replayed',false);
end $$;

create or replace function control.nexloop_approval_summary_allowed(p_capability text) returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case p_capability
  when 'real_dispatch.set' then array['scope_kind','enabled','data_mode','action_type']
  when 'retention.release_hold' then array[]::text[]  -- a hold id is the requester's own text and may carry a Consumer id (L6 review 10)
  when 'consumer.erase' then array[]::text[]
  when 'incentive.issue' then array['amount_minor','currency','offering_revision']
  when 'workbench_member.demote_owner' then array['new_role']  -- NX-058 slice D (K1): the target is the view's target_member, never a summary
  when 'goals.budget.loosen' then array['budget_kind','unit','from_limit_amount','limit_amount','from_period_start','from_period_end','period_start','period_end']
  else array[]::text[] end
$$;

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

do $$ declare f text; begin
 foreach f in array array['control.nexloop_budget_direction(text,text,jsonb)','control.nexloop_budget_set_tighten_only(text,text,jsonb)',
   'control.nexloop_governed_owner_change(text,text,text,text,text,jsonb)','control.nexloop_budget_loosen_check(text,text,jsonb,boolean)',
   'control.nexloop_budget_loosen_approval(text,text,text,jsonb)','control.nexloop_governed_loosen_budget(text,text,text,text,text,jsonb)',
   'control.nexloop_approval_summary_allowed(text)','control.nexloop_approval_open(text,text,text,text,text,jsonb)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('goals.budget.loosen','loosen_budget','human','control.nexloop_governed_loosen_budget(text,text,text,text,text,jsonb)','NX-056');
