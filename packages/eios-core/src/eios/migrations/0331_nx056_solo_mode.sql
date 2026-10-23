-- NX-056 A7 (docs/design/unified-approvals.md v5 §15-17; owner 2026-10-22: single-person mode, S-C1 option A, S-I1; L6 T-C1, T-I1).
-- Temporary number; the dispatcher renumbers.
--
-- 1. control.nexloop_approval_tenant_lock: the tenant's one advisory lock. Lock order everywhere (design §16): this lock first, then
--    member or approval rows. Member changes (the triggers below), decisions, consumption and the mode switch take it.
-- 2. control.nexloop_approval_eligible_subjects: the eligible approvers of a set of roles now - an active membership, an active human
--    subject, a role of the latest workbench configuration among the roles - by subject (two principals of one person count once).
--    Validity dates do not reduce the count (design §15: a larger count only makes the mode harder to take effect).
-- 3. control.nexloop_approval_eligibility_events (append-only, owner only, forced RLS; T-I1): per tenant and per policy (capability and
--    version) the eligible count of the union of its approver roles and the digest of the sorted subjects (T-C1), appended by
--    control.nexloop_approval_eligibility_record only when either changes; written by triggers on memberships, subjects, workbench
--    configurations and policies, and once for every tenant here. Members and roles are the tenant's, so the timeline is recorded
--    for world 'real'; the single-person mode never takes effect in another world (fail closed). A lower count is also audited
--    (approval.eligibility_reduced) for the alert approval_solo_mode (design §15: a removed approver role is part of the sequence).
-- 4. control.nexloop_approval_solo_mode_events (append-only): the owner's switch per (tenant, world), with a reason; the latest row
--    decides. Governed human capability approvals.solo_mode.set (operation set_approval_solo_mode; the caller's current workbench role
--    must be owner; not under any approval policy - 0321 refuses approvals.*).
-- 5. control.nexloop_approval_solo_effective(tenant, world, policy, requester): the mode is on, the policy allows it (solo_mode allowed),
--    the eligible subjects now are exactly the requester, and the policy's timeline covers the last 24 hours with at most one eligible
--    subject throughout (the last record at or before the window start and every record inside it; no record that old: not effective).
--    Computed now, server time, under the tenant lock, at each self-approval and at each consumption (S-I1: a self vote counts only
--    while the mode is effective at consumption). 0321 (decide, open) and 0322 (gate) call it.

create function control.nexloop_approval_tenant_lock(p_tenant text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp as $$
 select pg_advisory_xact_lock(hashtextextended('nexloop-approvals:'||p_tenant,0))
$$;
alter function control.nexloop_approval_tenant_lock(text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_tenant_lock(text) from public;

create function control.nexloop_approval_eligible_subjects(p_tenant text,p_roles text[]) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cfg control.nexloop_workbench_configurations;prior text:=current_setting('eios.tenant_id',true);v text[];
begin
 cfg:=control.nexloop_workbench_current(p_tenant);
 if cfg.tenant_id is null then return '{}'::text[];end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select coalesce(array_agg(distinct m.subject_id order by m.subject_id),'{}'::text[]) into v
  from control.nexloop_browser_memberships m join control.nexloop_browser_subjects b on b.subject_id=m.subject_id
  where m.tenant_id=p_tenant and m.payload->>'status'='active' and b.payload->>'kind'='human' and b.payload->>'status'='active'
   and coalesce(cfg.members->>m.principal_id,'')=any(p_roles);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_approval_eligible_subjects(text,text[]) owner to nexloop_owner;
revoke all on function control.nexloop_approval_eligible_subjects(text,text[]) from public;

-- 3. The eligibility timeline ---------------------------------------------------------------------------------------------------
create table control.nexloop_approval_eligibility_events (
 event_id bigint generated always as identity primary key,tenant_id text not null,world text not null,
 capability text not null,policy_version integer not null,eligible_count integer not null check(eligible_count>=0),
 subjects_digest text not null check(subjects_digest~'^[0-9a-f]{64}$'),recorded_at timestamptz not null default clock_timestamp(),
 foreign key(capability,policy_version) references control.nexloop_approval_policies(capability,version)
);
create index nexloop_approval_eligibility_events_key on control.nexloop_approval_eligibility_events(tenant_id,world,capability,policy_version,recorded_at);
alter table control.nexloop_approval_eligibility_events owner to nexloop_owner;
alter table control.nexloop_approval_eligibility_events enable row level security;
alter table control.nexloop_approval_eligibility_events force row level security;
create policy tenant_boundary on control.nexloop_approval_eligibility_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_approval_eligibility_events from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger approval_eligibility_events_append_only before update or delete or truncate on control.nexloop_approval_eligibility_events
 for each statement execute function control.nexloop_approval_append_only();

create function control.nexloop_approval_eligibility_record(p_tenant text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);pol control.nexloop_approval_policies;v text[];v_digest text;
 last control.nexloop_approval_eligibility_events;n integer:=0;
begin
 if p_tenant is null then return 0;end if;
 perform control.nexloop_approval_tenant_lock(p_tenant);
 for pol in select distinct on (p.capability) p.* from control.nexloop_approval_policies p order by p.capability,p.version desc loop
  v:=control.nexloop_approval_eligible_subjects(p_tenant,pol.approver_roles);
  v_digest:=encode(sha256(convert_to(array_to_json(v)::text,'UTF8')),'hex');
  perform set_config('eios.tenant_id',p_tenant,true);
  select * into last from control.nexloop_approval_eligibility_events e where e.tenant_id=p_tenant and e.world='real' and e.capability=pol.capability
   and e.policy_version=pol.version order by e.recorded_at desc,e.event_id desc limit 1;
  if last.event_id is null or last.eligible_count<>cardinality(v) or last.subjects_digest<>v_digest then
   insert into control.nexloop_approval_eligibility_events(tenant_id,world,capability,policy_version,eligible_count,subjects_digest)
    values(p_tenant,'real',pol.capability,pol.version,cardinality(v),v_digest);
   n:=n+1;
   if last.event_id is not null and cardinality(v)<last.eligible_count then
    insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
     values('approval-eligibility-reduced:'||gen_random_uuid(),'approval.eligibility_reduced',p_tenant,'system:approvals','approval',pol.capability,
      jsonb_build_object('capability',pol.capability,'policy_version',pol.version,'from',last.eligible_count,'to',cardinality(v)),clock_timestamp());
   end if;
  end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return n;
end $$;
alter function control.nexloop_approval_eligibility_record(text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_eligibility_record(text) from public;

create function control.nexloop_approval_eligibility_trigger() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;prior text:=current_setting('eios.tenant_id',true);
begin
 if tg_table_name='nexloop_browser_memberships' then
  perform control.nexloop_approval_eligibility_record(case when tg_op='DELETE' then old.tenant_id else new.tenant_id end);
  if tg_op='UPDATE' and old.tenant_id<>new.tenant_id then perform control.nexloop_approval_eligibility_record(old.tenant_id);end if;
 elsif tg_table_name='nexloop_browser_subjects' then
  -- A subject has no tenant: every tenant it is a member of.
  for t in select distinct m.tenant_id from control.nexloop_browser_memberships m where m.subject_id=coalesce(new.subject_id,old.subject_id) loop
   perform control.nexloop_approval_eligibility_record(t);
  end loop;
 elsif tg_table_name='nexloop_workbench_configurations' then
  perform control.nexloop_approval_eligibility_record(new.tenant_id);
 else  -- a new policy version: every tenant (its approver roles may differ)
  for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
   perform control.nexloop_approval_eligibility_record(t);
  end loop;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function control.nexloop_approval_eligibility_trigger() owner to nexloop_owner;
revoke all on function control.nexloop_approval_eligibility_trigger() from public;
create trigger nx056_approval_eligibility after insert or update or delete on control.nexloop_browser_memberships
 for each row execute function control.nexloop_approval_eligibility_trigger();
create trigger nx056_approval_eligibility after update on control.nexloop_browser_subjects
 for each row execute function control.nexloop_approval_eligibility_trigger();
create trigger nx056_approval_eligibility after insert on control.nexloop_workbench_configurations
 for each row execute function control.nexloop_approval_eligibility_trigger();
create trigger nx056_approval_eligibility after insert on control.nexloop_approval_policies
 for each row execute function control.nexloop_approval_eligibility_trigger();

-- The initial records (design §15: the timeline starts at deployment, so the mode takes effect 24 hours later at the earliest).
do $$declare t text;begin
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop perform control.nexloop_approval_eligibility_record(t);end loop;
end $$;

-- 4. The owner's switch ------------------------------------------------------------------------------------------------------------
create table control.nexloop_approval_solo_mode_events (
 event_id bigint generated always as identity primary key,tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 enabled boolean not null,reason text not null check(char_length(reason) between 1 and 500),
 changed_by_principal text not null,changed_by_subject text not null,intent_id text not null,recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,world,intent_id)
);
alter table control.nexloop_approval_solo_mode_events owner to nexloop_owner;
alter table control.nexloop_approval_solo_mode_events enable row level security;
alter table control.nexloop_approval_solo_mode_events force row level security;
create policy tenant_boundary on control.nexloop_approval_solo_mode_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_approval_solo_mode_events from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger approval_solo_mode_events_append_only before update or delete or truncate on control.nexloop_approval_solo_mode_events
 for each statement execute function control.nexloop_approval_append_only();

create function control.nexloop_approval_solo_enabled(p_tenant text,p_world text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.enabled into v from control.nexloop_approval_solo_mode_events e where e.tenant_id=p_tenant and e.world=p_world order by e.event_id desc limit 1;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return coalesce(v,false);
end $$;
alter function control.nexloop_approval_solo_enabled(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_solo_enabled(text,text) from public;

create function control.nexloop_governed_set_approval_solo_mode(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_subject text;prior control.nexloop_approval_solo_mode_events;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','enabled','reason'))
  or jsonb_typeof(body->'enabled') is distinct from 'boolean'
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'approval solo mode: invalid' using errcode='22023';end if;
 -- Only a human whose current workbench role is owner (design §15.1).
 if p_subject is distinct from 'human' or coalesce((control.nexloop_workbench_current(p_tenant)).members->>p_principal,'')<>'owner' then
  raise exception 'approval solo mode: owner_only' using errcode='42501';end if;
 v_subject:=control.nexloop_approval_subject_of(p_tenant,p_principal);
 if v_subject is null then raise exception 'approval solo mode: owner_only' using errcode='42501';end if;
 perform control.nexloop_approval_tenant_lock(p_tenant);
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into prior from control.nexloop_approval_solo_mode_events where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 if found then return jsonb_build_object('enabled',prior.enabled,'replayed',true);end if;
 insert into control.nexloop_approval_solo_mode_events(tenant_id,world,enabled,reason,changed_by_principal,changed_by_subject,intent_id)
  values(p_tenant,p_world,(body->>'enabled')::boolean,btrim(body->>'reason'),p_principal,v_subject,p_intent);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('approval-solo-mode:'||p_tenant||':'||p_world||':'||p_intent,'approval.solo_mode_set',p_tenant,p_principal,'approval',p_intent,
   jsonb_build_object('world',p_world,'enabled',(body->>'enabled')::boolean),clock_timestamp());
 return jsonb_build_object('enabled',(body->>'enabled')::boolean,'replayed',false);
end $$;
alter function control.nexloop_governed_set_approval_solo_mode(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_set_approval_solo_mode(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('approvals.solo_mode.set','set_approval_solo_mode','human','control.nexloop_governed_set_approval_solo_mode(text,text,text,text,text,jsonb)','NX-056');

-- 5. Whether the mode takes effect now -------------------------------------------------------------------------------------------
create function control.nexloop_approval_solo_effective(p_tenant text,p_world text,p_capability text,p_policy_version integer,p_requester text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);pol control.nexloop_approval_policies;v_window timestamptz:=clock_timestamp()-interval '24 hours';
 v_before integer;v_inside integer;v boolean;
begin
 select * into pol from control.nexloop_approval_policies where capability=p_capability and version=p_policy_version;
 if pol.capability is null or p_world is distinct from 'real' or pol.solo_mode<>'allowed' or p_requester is null
  or not control.nexloop_approval_solo_enabled(p_tenant,p_world) then return false;end if;
 -- Exactly the requester eligible now.
 if control.nexloop_approval_eligible_subjects(p_tenant,pol.approver_roles) is distinct from array[p_requester] then return false;end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 -- The last record at or before the window start (none: the timeline does not cover the window) and the largest count inside it.
 select e.eligible_count into v_before from control.nexloop_approval_eligibility_events e where e.tenant_id=p_tenant and e.world='real'
  and e.capability=pol.capability and e.policy_version=pol.version and e.recorded_at<=v_window order by e.recorded_at desc,e.event_id desc limit 1;
 select max(e.eligible_count) into v_inside from control.nexloop_approval_eligibility_events e where e.tenant_id=p_tenant and e.world='real'
  and e.capability=pol.capability and e.policy_version=pol.version and e.recorded_at>v_window;
 v:=v_before is not null and v_before<=1 and coalesce(v_inside,0)<=1;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_approval_solo_effective(text,text,text,integer,text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_solo_effective(text,text,text,integer,text) from public;
