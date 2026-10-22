-- NX-056 A1 (docs/design/unified-approvals.md v5; owner 2026-10-22 Q1 option A): the unified Approval store and its two governed human
-- Actions. The migration number lives only in the file name and the catalog. Nothing here gates an Action yet: the governed entry asks
-- for an Approval in A2; the first policies come in A5.
-- 1. control.nexloop_approval_policies: trusted configuration, versioned and append-only (capability, Action version, optional one-field
--    equality condition, quorum 1..5, approver roles, expiry 15 min..7 days, summary field allowlist). Only a human governed capability
--    (control.nexloop_governed_capabilities, subject_rule human) other than approvals.* may carry a policy: an effect capability never
--    does (design §2 invariant 3).
-- 2. control.nexloop_approval_requests: one frozen request (body, request_digest, requester subject and principal from the server-side
--    identity, policy version, due_by). Only its status and revision change, only through the owner functions here and in A2.
-- 3. control.nexloop_approval_decisions (append-only): approved / rejected / cancelled / expired, the actor's subject and principal, the
--    reason, self_approved (A7). One approval per subject (a unique index), one rejection ends it (the vendored state machine's rule).
-- 4. control.nexloop_approval_consumptions (append-only, unique approval_id): the one consumption of an approved request and its result
--    (A2, design §4 step 3, L6 N-1).
-- 5. Governed human Actions approvals.decide (decide_approval) and approvals.cancel (cancel_approval). An approver is an active member
--    whose current workbench role is one of the policy's approver roles, is human, is never the requester by subject (L6 C5) and
--    decides once; past due_by (database clock) the request ends expired and the decision is not taken. No path records an approval
--    without a human decision (L6 C4).
-- 6. 0240 outreach approval: self-approval is compared by subject too (L6 C5): runtime.nexloop_governed_outreach_approve redefined from
--    its latest body (0263) with only that comparison added (tests/test_function_copy_guard.py).

-- 0. The subject of a principal (a member's browser subject; null for a principal that is no member) -----------------------------------
create function control.nexloop_approval_subject_of(p_tenant text,p_principal text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v text;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select m.subject_id into v from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=p_principal;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_approval_subject_of(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_subject_of(text,text) from public;

create function control.nexloop_approval_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 raise exception '% is append-only',tg_table_name using errcode='42501';
end $$;
alter function control.nexloop_approval_append_only() owner to nexloop_owner;
revoke all on function control.nexloop_approval_append_only() from public;

-- 1. Policies ----------------------------------------------------------------------------------------------------------------------
create table control.nexloop_approval_policies (
 capability text not null,version integer not null check(version between 1 and 1000000),action_version integer not null check(action_version>=1),
 condition jsonb check(condition is null or (jsonb_typeof(condition)='object' and condition ? 'field' and condition ? 'equals' and condition-'field'-'equals'='{}'::jsonb
  and condition->>'field'~'^[a-z][a-z0-9_]{0,63}$' and jsonb_typeof(condition->'equals') in ('string','number','boolean'))),
 quorum integer not null check(quorum between 1 and 5),
 approver_roles text[] not null check(cardinality(approver_roles) between 1 and 3 and approver_roles<@array['owner','operator','reviewer']),
 expires_after_seconds integer not null check(expires_after_seconds between 900 and 604800),
 summary_fields text[] not null default '{}' check(cardinality(summary_fields)<=16),
 published_by text not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(capability,version)
);
alter table control.nexloop_approval_policies owner to nexloop_owner;
revoke all on control.nexloop_approval_policies from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create function control.nexloop_approval_policy_guard() returns trigger language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if new.capability like 'approvals.%' or not exists(select 1 from control.nexloop_governed_capabilities c where c.capability=new.capability and c.subject_rule='human') then
  raise exception 'approval policy: a human governed capability required' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_approval_policies p where p.capability=new.capability and p.version>=new.version) then
  raise exception 'approval policy: versions only grow' using errcode='22023';end if;
 if exists(select 1 from unnest(new.summary_fields) f where f!~'^[a-z][a-z0-9_]{0,63}$') then raise exception 'approval policy: summary fields' using errcode='22023';end if;
 return new;
end $$;
alter function control.nexloop_approval_policy_guard() owner to nexloop_owner;
revoke all on function control.nexloop_approval_policy_guard() from public;
create trigger approval_policy_guard before insert on control.nexloop_approval_policies for each row execute function control.nexloop_approval_policy_guard();
create trigger approval_policy_append_only before update or delete or truncate on control.nexloop_approval_policies for each statement execute function control.nexloop_approval_append_only();

-- The current policy of a capability, or null.
create function control.nexloop_approval_policy(p_capability text) returns control.nexloop_approval_policies
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select * from control.nexloop_approval_policies where capability=p_capability order by version desc limit 1
$$;
alter function control.nexloop_approval_policy(text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_policy(text) from public;

-- 2. Requests ----------------------------------------------------------------------------------------------------------------------
create table control.nexloop_approval_requests (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,approval_id uuid not null default gen_random_uuid(),
 capability text not null,action_resource text not null check(action_resource~'^eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}$'),
 policy_version integer not null,request_digest text not null check(request_digest~'^[0-9a-f]{64}$'),
 request_id text not null check(char_length(request_id) between 1 and 200),body jsonb not null check(jsonb_typeof(body)='object'),
 requester_subject text not null,requester_principal text not null,requested_at timestamptz not null default clock_timestamp(),due_by timestamptz not null,
 status text not null default 'waiting_approval' check(status in ('waiting_approval','approved','rejected','cancelled','expired')),
 revision integer not null default 1 check(revision>=1),
 primary key(tenant_id,world,approval_id),unique(tenant_id,world,capability,request_id),
 foreign key(capability,policy_version) references control.nexloop_approval_policies(capability,version),check(due_by>requested_at)
);
create index nexloop_approval_requests_waiting on control.nexloop_approval_requests(tenant_id,world,status,due_by) where status='waiting_approval';
create unique index nexloop_approval_requests_one_waiting_digest on control.nexloop_approval_requests(tenant_id,world,request_digest,requester_subject) where status='waiting_approval';
alter table control.nexloop_approval_requests owner to nexloop_owner;
alter table control.nexloop_approval_requests enable row level security;
alter table control.nexloop_approval_requests force row level security;
create policy tenant_boundary on control.nexloop_approval_requests to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_approval_requests from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create function control.nexloop_approval_request_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' then raise exception 'approval requests are deleted only by retention' using errcode='42501';end if;
 -- Erasure and retention (A5, 0326): only the body changes (pseudonymised or redacted), inside an erasure scope this transaction
 -- opened for this table and column; nothing else, not even the revision.
 if new.body is distinct from old.body and exists(select 1 from control.nexloop_erasure_scope s where s.txid=txid_current()
   and s.table_name='control.nexloop_approval_requests' and 'body'=any(s.columns)) then
  if (new.tenant_id,new.world,new.approval_id,new.capability,new.action_resource,new.policy_version,new.request_digest,new.request_id,
      new.requester_subject,new.requester_principal,new.requested_at,new.due_by,new.status,new.revision)
    is distinct from (old.tenant_id,old.world,old.approval_id,old.capability,old.action_resource,old.policy_version,old.request_digest,old.request_id,
      old.requester_subject,old.requester_principal,old.requested_at,old.due_by,old.status,old.revision) then
   raise exception 'approval request is immutable' using errcode='42501';end if;
  return new;
 end if;
 -- Only the status moves, once, out of waiting_approval (or back to it when a vote no longer counts, A2; or approved -> expired when it
 -- passed due_by or its policy changed before it was consumed), with the revision.
 if (new.tenant_id,new.world,new.approval_id,new.capability,new.action_resource,new.policy_version,new.request_digest,new.request_id,new.body,
     new.requester_subject,new.requester_principal,new.requested_at,new.due_by)
   is distinct from (old.tenant_id,old.world,old.approval_id,old.capability,old.action_resource,old.policy_version,old.request_digest,old.request_id,old.body,
     old.requester_subject,old.requester_principal,old.requested_at,old.due_by)
  or new.revision<>old.revision+1
  or not (old.status='waiting_approval' or (old.status='approved' and new.status in ('waiting_approval','expired'))) then
  raise exception 'approval request is immutable' using errcode='42501';end if;
 -- A consumed Approval never changes status again (L6 review 6).
 if exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=old.tenant_id and c.world=old.world and c.approval_id=old.approval_id) then
  raise exception 'approval request is consumed' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_approval_request_guard() owner to nexloop_owner;
revoke all on function control.nexloop_approval_request_guard() from public;
create trigger approval_request_guard before update or delete on control.nexloop_approval_requests for each row execute function control.nexloop_approval_request_guard();
create trigger approval_request_no_truncate before truncate on control.nexloop_approval_requests for each statement execute function control.nexloop_approval_append_only();

-- 3. Decisions ---------------------------------------------------------------------------------------------------------------------
create table control.nexloop_approval_decisions (
 tenant_id text not null,world text not null,approval_id uuid not null,decision_id uuid not null default gen_random_uuid(),
 outcome text not null check(outcome in ('approved','rejected','cancelled','expired')),
 actor_subject text not null,actor_principal text not null,reason text not null check(char_length(reason) between 1 and 500),
 decision_intent text,self_approved boolean not null default false,decided_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,decision_id),foreign key(tenant_id,world,approval_id) references control.nexloop_approval_requests(tenant_id,world,approval_id)
);
create unique index nexloop_approval_decisions_one_approval on control.nexloop_approval_decisions(tenant_id,world,approval_id,actor_subject) where outcome='approved';
alter table control.nexloop_approval_decisions owner to nexloop_owner;
alter table control.nexloop_approval_decisions enable row level security;
alter table control.nexloop_approval_decisions force row level security;
create policy tenant_boundary on control.nexloop_approval_decisions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_approval_decisions from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger approval_decisions_append_only before update or delete or truncate on control.nexloop_approval_decisions for each statement execute function control.nexloop_approval_append_only();

-- 4. Consumptions ------------------------------------------------------------------------------------------------------------------
create table control.nexloop_approval_consumptions (
 tenant_id text not null,world text not null,approval_id uuid not null,consumed_by_subject text not null,consumed_by_principal text not null,
 result text not null check(result~'^(executed|execution_refused:[0-9A-Z]{5}(:[a-z_]{1,64})?)$'),consumed_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,approval_id),foreign key(tenant_id,world,approval_id) references control.nexloop_approval_requests(tenant_id,world,approval_id)
);
alter table control.nexloop_approval_consumptions owner to nexloop_owner;
alter table control.nexloop_approval_consumptions enable row level security;
alter table control.nexloop_approval_consumptions force row level security;
create policy tenant_boundary on control.nexloop_approval_consumptions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_approval_consumptions from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger approval_consumptions_append_only before update or delete or truncate on control.nexloop_approval_consumptions for each statement execute function control.nexloop_approval_append_only();

-- 5. Opening a request (called by the governed entry in A2; owner only) --------------------------------------------------------------
-- request_digest (design §6): sha256 of canonical {digest_version, tenant_id, world, action, policy, body without request_id}.
create function control.nexloop_approval_digest(p_tenant text,p_world text,p_action text,p_capability text,p_policy_version integer,p_body jsonb) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(jsonb_build_object('digest_version','nexloop-approval-digest/1','tenant_id',p_tenant,'world',p_world,'action',p_action,
  'policy',p_capability||'@'||p_policy_version,'body',p_body-'request_id')::text,'UTF8')),'hex')
$$;
alter function control.nexloop_approval_digest(text,text,text,text,integer,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_approval_digest(text,text,text,text,integer,jsonb) from public;

create function control.nexloop_approval_open(p_tenant text,p_world text,p_capability text,p_action text,p_principal text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare pol control.nexloop_approval_policies;v_subject text;v_digest text;r control.nexloop_approval_requests;
begin
 pol:=control.nexloop_approval_policy(p_capability);
 -- An Action version without its policy version is refused (fail closed, design §3).
 if pol.capability is null or p_action!~'^eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}$' or split_part(p_action,':',4)::integer<>pol.action_version then
  raise exception 'approval: policy_unavailable' using errcode='22023';end if;
 v_subject:=control.nexloop_approval_subject_of(p_tenant,p_principal);
 if v_subject is null then raise exception 'approval: requester_unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 v_digest:=control.nexloop_approval_digest(p_tenant,p_world,p_action,p_capability,pol.version,p_body);
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
 insert into control.nexloop_approval_requests(tenant_id,world,capability,action_resource,policy_version,request_digest,request_id,body,requester_subject,requester_principal,due_by)
  values(p_tenant,p_world,p_capability,p_action,pol.version,v_digest,p_body->>'request_id',p_body,v_subject,p_principal,clock_timestamp()+make_interval(secs=>pol.expires_after_seconds))
  returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-requested:'||r.approval_id,'approval.requested',p_tenant,p_principal,'approval',r.request_id,
   jsonb_build_object('approval_id',r.approval_id,'capability',p_capability,'policy_version',pol.version,'request_digest',v_digest,'due_by',r.due_by),clock_timestamp());
 return jsonb_build_object('approval_id',r.approval_id,'status',r.status,'due_by',r.due_by,'replayed',false);
end $$;
alter function control.nexloop_approval_open(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_approval_open(text,text,text,text,text,jsonb) from public;

-- Ending a request by the system: a waiting request, or an approved one never consumed, ends expired with a decision row by the system
-- and the audit approval.expired carrying the reason (due_by_passed, policy_changed, or superseded by the same person's waiting
-- request of the same body). Never executes anything.
create function control.nexloop_approval_terminate(p_tenant text,p_world text,p_approval uuid,p_reason text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_approval_requests;
begin
 if p_reason not in ('due_by_passed','policy_changed','superseded') then raise exception 'approval: termination reason' using errcode='22023';end if;
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
alter function control.nexloop_approval_terminate(text,text,uuid,text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_terminate(text,text,uuid,text) from public;

create function control.nexloop_approval_expire(p_tenant text,p_world text,p_approval uuid) returns boolean
 language sql security definer set search_path=pg_catalog,pg_temp as $$
 select control.nexloop_approval_terminate(p_tenant,p_world,p_approval,'due_by_passed')
$$;
alter function control.nexloop_approval_expire(text,text,uuid) owner to nexloop_owner;
revoke all on function control.nexloop_approval_expire(text,text,uuid) from public;

-- An approver's standing now: an active membership of an active human subject whose current workbench role is one of the policy's
-- approver roles. Used at a decision and again at consumption (A2, L6 C3).
create function control.nexloop_approval_approver_eligible(p_tenant text,p_principal text,p_roles text[]) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cfg control.nexloop_workbench_configurations;prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 cfg:=control.nexloop_workbench_current(p_tenant);
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v:=cfg.tenant_id is not null and exists(select 1 from control.nexloop_browser_memberships m join control.nexloop_browser_subjects b on b.subject_id=m.subject_id
   where m.tenant_id=p_tenant and m.principal_id=p_principal and m.payload->>'status'='active'
    and b.payload->>'kind'='human' and b.payload->>'status'='active') and coalesce(cfg.members->>p_principal,'')=any(p_roles);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_approval_approver_eligible(text,text,text[]) owner to nexloop_owner;
revoke all on function control.nexloop_approval_approver_eligible(text,text,text[]) from public;

-- 6. Governed human Actions ---------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_approval_decide(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_approval_requests;pol control.nexloop_approval_policies;v_subject text;v_outcome text;v_approvals integer;v_status text;
 prior control.nexloop_approval_decisions;v_approval uuid;
begin
 if jsonb_typeof(body) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','approval_id','decision','reason'))
  or coalesce(body->>'approval_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or body->>'decision' not in ('approve','reject') or body->>'decision' is null
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(body->>'reason') not between 1 and 500 then
  raise exception 'approval: invalid' using errcode='22023';end if;
 if p_subject is distinct from 'human' then raise exception 'approval: approver_unauthorized' using errcode='42501';end if;
 v_approval:=(body->>'approval_id')::uuid;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and approval_id=v_approval for update;
 if not found then raise exception 'approval: not_found' using errcode='22023';end if;
 v_subject:=control.nexloop_approval_subject_of(p_tenant,p_principal);
 v_outcome:=case body->>'decision' when 'approve' then 'approved' else 'rejected' end;
 -- The same decision by the same approver replays.
 select * into prior from control.nexloop_approval_decisions d where d.tenant_id=p_tenant and d.world=p_world and d.approval_id=r.approval_id
  and d.actor_subject=v_subject and d.outcome=v_outcome;
 if found then return jsonb_build_object('approval_id',r.approval_id,'status',r.status,'replayed',true);end if;
 if r.status<>'waiting_approval' then raise exception 'approval: not_waiting' using errcode='22023';end if;
 if r.due_by<=clock_timestamp() then
  perform control.nexloop_approval_expire(p_tenant,p_world,r.approval_id);
  return jsonb_build_object('approval_id',r.approval_id,'status','expired','replayed',false);
 end if;
 select * into pol from control.nexloop_approval_policies where capability=r.capability and version=r.policy_version;
 -- No self-approval, by subject (one person under two principals is still the requester; L6 C5). A7 opens the single-person mode here.
 if v_subject is null or v_subject=r.requester_subject then raise exception 'approval: self_approval' using errcode='22023';end if;
 if not control.nexloop_approval_approver_eligible(p_tenant,p_principal,pol.approver_roles) then
  raise exception 'approval: approver_unauthorized' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_approval_decisions(tenant_id,world,approval_id,outcome,actor_subject,actor_principal,reason,decision_intent)
  values(p_tenant,p_world,r.approval_id,v_outcome,v_subject,p_principal,body->>'reason',p_intent);
 -- The vendored state machine's rule: one rejection ends it; a quorum of distinct approvers approves it.
 select count(distinct d.actor_subject) into v_approvals from control.nexloop_approval_decisions d
  where d.tenant_id=p_tenant and d.world=p_world and d.approval_id=r.approval_id and d.outcome='approved';
 v_status:=case when v_outcome='rejected' then 'rejected' when v_approvals>=pol.quorum then 'approved' else 'waiting_approval' end;
 if v_status<>'waiting_approval' then
  update control.nexloop_approval_requests set status=v_status,revision=revision+1 where tenant_id=p_tenant and world=p_world and approval_id=r.approval_id;
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-decided:'||r.approval_id||':'||v_subject||':'||v_outcome,'approval.decided',p_tenant,p_principal,'approval',p_intent,
   jsonb_build_object('approval_id',r.approval_id,'capability',r.capability,'outcome',v_outcome,'status',v_status,'self_approved',false,
    'approvals',v_approvals,'quorum',pol.quorum),clock_timestamp());
 return jsonb_build_object('approval_id',r.approval_id,'status',v_status,'approvals',v_approvals,'quorum',pol.quorum,'replayed',false);
end $$;
alter function control.nexloop_governed_approval_decide(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_approval_decide(text,text,text,text,text,jsonb) from public;

create function control.nexloop_governed_approval_cancel(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_approval_requests;v_subject text;v_approval uuid;
begin
 if jsonb_typeof(body) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','approval_id','reason'))
  or coalesce(body->>'approval_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(body->>'reason') not between 1 and 500 then
  raise exception 'approval: invalid' using errcode='22023';end if;
 v_approval:=(body->>'approval_id')::uuid;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and approval_id=v_approval for update;
 if not found then raise exception 'approval: not_found' using errcode='22023';end if;
 v_subject:=control.nexloop_approval_subject_of(p_tenant,p_principal);
 if v_subject is null or v_subject<>r.requester_subject then raise exception 'approval: not_requester' using errcode='42501';end if;
 if r.status='cancelled' then return jsonb_build_object('approval_id',r.approval_id,'status','cancelled','replayed',true);end if;
 if r.status<>'waiting_approval' then raise exception 'approval: not_waiting' using errcode='22023';end if;
 update control.nexloop_approval_requests set status='cancelled',revision=revision+1 where tenant_id=p_tenant and world=p_world and approval_id=r.approval_id;
 insert into control.nexloop_approval_decisions(tenant_id,world,approval_id,outcome,actor_subject,actor_principal,reason,decision_intent)
  values(p_tenant,p_world,r.approval_id,'cancelled',v_subject,p_principal,body->>'reason',p_intent);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-cancelled:'||r.approval_id,'approval.cancelled',p_tenant,p_principal,'approval',p_intent,
   jsonb_build_object('approval_id',r.approval_id,'capability',r.capability),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('approval_id',r.approval_id,'status','cancelled','replayed',false);
end $$;
alter function control.nexloop_governed_approval_cancel(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_approval_cancel(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('approvals.decide','decide_approval','human','control.nexloop_governed_approval_decide(text,text,text,text,text,jsonb)','NX-056'),
 ('approvals.cancel','cancel_approval','human','control.nexloop_governed_approval_cancel(text,text,text,text,text,jsonb)','NX-056');

-- 7. 0240: self-approval by subject too (L6 C5); re-created from its latest body (0263) with only that comparison added.
CREATE OR REPLACE FUNCTION control.nexloop_governed_outreach_approve(p_tenant text, p_world text, p_principal text, p_subject text, p_intent text, body jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare s runtime.nexloop_outreach_sends;a runtime.nexloop_outreach_approvals;cfg control.nexloop_workbench_configurations;
 v_resource text:='eios:action:nexloop.outreach.approve:1';v_decision text;v_intent text:=body->>'intent_id';
begin
 if jsonb_typeof(body) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','intent_id','decision','reason'))
  or coalesce(body->>'intent_id','')!~'^outreach:[0-9a-f]{64}$' or body->>'decision' not in ('approve','reject') or body->>'decision' is null
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(body->>'reason') not between 1 and 500 then
  raise exception 'outreach approval: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into s from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=v_intent for update;
 select * into a from runtime.nexloop_outreach_approvals where tenant_id=p_tenant and world=p_world and intent_id=v_intent for update;
 if s.intent_id is null or a.intent_id is null then raise exception 'outreach approval: not_pending' using errcode='22023';end if;
 v_decision:=case body->>'decision' when 'approve' then 'approved' else 'rejected' end;
 -- The same decision by the same approver replays; any other decision on a decided approval is refused.
 if a.decision is not null then
  if a.decision=v_decision and a.decided_by=p_principal then return jsonb_build_object('intent_id',s.intent_id,'state',s.state,'decision',a.decision,'replayed',true);end if;
  raise exception 'outreach approval: not_pending' using errcode='22023';end if;
 -- Four eyes: the requester never decides their own request.
 if p_principal=s.principal_id or p_principal=a.requested_by
  or control.nexloop_approval_subject_of(p_tenant,p_principal) in (control.nexloop_approval_subject_of(p_tenant,s.principal_id),control.nexloop_approval_subject_of(p_tenant,a.requested_by))
 then raise exception 'outreach approval: self_approval' using errcode='22023';end if;
 -- The approver's authority now: an active member whose current workbench role holds the Action.
 cfg:=control.nexloop_workbench_current(p_tenant);
 if not exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=p_principal and m.payload->>'status'='active')
  or cfg.tenant_id is null or not coalesce(cfg.roles->(cfg.members->>p_principal) ? v_resource,false) then
  raise exception 'outreach approval: approver_unauthorized' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if a.expires_at<=runtime.nexloop_world_now(p_tenant,p_world) then
  -- Past its window: the send ends expired (terminal) and the decision is not taken.
  perform runtime.nexloop_outreach_expire_approvals(p_tenant,p_world);
  return jsonb_build_object('intent_id',s.intent_id,'state','refused','refusal_code','approval_expired','replayed',false);
 end if;
 update runtime.nexloop_outreach_approvals set decision=v_decision,decided_by=p_principal,decided_at=runtime.nexloop_world_now(p_tenant,p_world),reason=body->>'reason',decision_intent=p_intent
  where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 if v_decision='approved' then
  -- Approved is only accepted: the executor still admits it through every dispatch gate.
  update runtime.nexloop_outreach_sends set state='accepted',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 else
  update runtime.nexloop_outreach_sends set state='refused',refusal_code='approval_rejected',finished_at=runtime.nexloop_world_now(p_tenant,p_world),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-approval:'||s.intent_id,'nexloop.outreach.approval.'||v_decision,p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',s.intent_id,'decision',v_decision),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',s.intent_id,'state',case when v_decision='approved' then 'accepted' else 'refused' end,'decision',v_decision,'replayed',false);
end $function$;
